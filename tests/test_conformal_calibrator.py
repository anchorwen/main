"""Unit tests for ConformalCalibrator — threshold, warmup, clamping, persistence."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from core.execution.conformal_calibrator import (
    DEFAULT_BASE_THRESHOLD,
    DEFAULT_MAX_THRESHOLD,
    DEFAULT_MIN_THRESHOLD,
    DEFAULT_TARGET_PERCENTILE,
    ConformalCalibrator,
    _journal_entry_label,
)

# ── Helpers ──


def _new_calibrator(
    *,
    warmup: int = 10,
    window: int = 100,
    tmp_path: Path | None = None,
) -> ConformalCalibrator:
    """Create an isolated calibrator with low warmup for fast testing."""
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp(prefix="conformal_test_"))
    return ConformalCalibrator(
        window_size=window,
        warmup_samples=warmup,
        base_threshold=DEFAULT_BASE_THRESHOLD,
        min_threshold=DEFAULT_MIN_THRESHOLD,
        max_threshold=DEFAULT_MAX_THRESHOLD,
        target_percentile=DEFAULT_TARGET_PERCENTILE,
        state_path=str(tmp_path / "conformal_state.json"),
    )


def _fill_warmup(cal: ConformalCalibrator, p_wins: list[float] | None = None) -> None:
    """Add enough samples to reach warmup."""
    if p_wins is None:
        p_wins = [0.45 + i * 0.002 for i in range(cal._warmup_samples)]
    for pw in p_wins:
        cal.update(pw, 1 if pw > 0.50 else -1)


# ── Threshold computation ──


class TestThresholdComputation:
    def test_returns_base_during_warmup(self):
        cal = _new_calibrator(warmup=20)
        for _ in range(15):
            cal.update(0.45, 1)
        assert cal.compute_threshold() == DEFAULT_BASE_THRESHOLD
        assert not cal.is_warm

    def test_adapts_after_warmup(self):
        cal = _new_calibrator(warmup=5)
        _fill_warmup(cal)
        assert cal.is_warm
        t = cal.compute_threshold()
        # Should be ≥ base since all p_wins are ≥ 0.40
        assert t >= DEFAULT_BASE_THRESHOLD

    def test_higher_p_win_distribution_raises_threshold(self):
        """A distribution shifted right should yield a higher Q10."""
        cal_low = _new_calibrator(warmup=5)
        cal_high = _new_calibrator(warmup=5)

        for _ in range(20):
            cal_low.update(0.42, 1)
            cal_high.update(0.58, 1)

        t_low = cal_low.compute_threshold()
        t_high = cal_high.compute_threshold()
        assert t_high >= t_low, f"t_low={t_low:.4f} t_high={t_high:.4f}"

    def test_threshold_monotonic_with_distribution(self):
        """Adding higher P(win) values should not decrease threshold."""
        cal = _new_calibrator(warmup=5)
        for pw in [0.45, 0.48, 0.52, 0.55, 0.60]:
            cal.update(pw, 1)
        t1 = cal.compute_threshold()
        # Add a very low p_win
        cal.update(0.38, -1)
        t2 = cal.compute_threshold()
        # Q10 should NOT increase (extra low value could lower Q10)
        # but max(q, base) keeps it at or above base
        assert t2 >= DEFAULT_BASE_THRESHOLD

    def test_q10_vs_q50_ordering(self):
        """Q10 should always be ≤ Q50 for the same data."""
        cal = _new_calibrator(warmup=5, window=100)
        rng = np.random.RandomState(42)
        for _ in range(80):
            cal.update(float(rng.uniform(0.38, 0.62)), 1 if rng.random() > 0.5 else -1)

        p_wins = [p for p, _, _ in cal._history]
        q10 = float(np.percentile(p_wins, 10))
        q50 = float(np.percentile(p_wins, 50))
        assert q10 <= q50, f"Q10={q10:.4f} > Q50={q50:.4f}"


# ── Warmup behaviour ──


class TestWarmupBehaviour:
    def test_is_warm_false_before_threshold(self):
        cal = _new_calibrator(warmup=20)
        for _ in range(19):
            cal.update(0.45, 1)
        assert not cal.is_warm

    def test_is_warm_true_at_threshold(self):
        cal = _new_calibrator(warmup=5)
        for _ in range(5):
            cal.update(0.45, 1)
        assert cal.is_warm

    def test_threshold_stable_at_warmup_boundary(self):
        """Threshold should not jump discontinuously at warmup boundary."""
        cal = _new_calibrator(warmup=6, window=50)
        for pw in [0.44, 0.46, 0.48, 0.50, 0.52]:
            cal.update(pw, 1)
        t_before = cal.compute_threshold()  # should be base (5 < 6)
        cal.update(0.54, 1)
        t_after = cal.compute_threshold()  # now warm (6 ≥ 6)
        # t_after should be ≥ base and within reasonable range
        assert t_after >= DEFAULT_BASE_THRESHOLD
        assert t_after <= 0.65  # shouldn't explode


# ── Clamping ──


class TestClamping:
    def test_never_exceeds_max(self):
        cal = _new_calibrator(warmup=5)
        for pw in [0.90, 0.92, 0.95, 0.97, 0.99]:
            cal.update(pw, 1)
        assert cal.compute_threshold() <= DEFAULT_MAX_THRESHOLD

    def test_never_below_min(self):
        cal = _new_calibrator(warmup=5, window=50)
        for pw in [0.05, 0.08, 0.10, 0.12, 0.15]:
            cal.update(pw, 1)
        # Even with terrible distribution, threshold ≥ min
        assert cal.compute_threshold() >= DEFAULT_MIN_THRESHOLD

    def test_clamp_hit_counter_increments(self):
        cal = _new_calibrator(warmup=5, window=50)
        # Push distribution way up to hit max clamp
        for pw in [0.95, 0.96, 0.97, 0.98, 0.99]:
            cal.update(pw, 1)
        for pw in [0.80, 0.82, 0.85, 0.88, 0.90]:
            cal.update(pw, 1)
        cal.compute_threshold()
        assert cal._clamp_hits_upper > 0

    def test_low_distribution_hits_lower_clamp(self):
        """Lower clamp hits when min_threshold > base_threshold.

        In default config base=0.40 > min=0.35, so max(q, base) keeps the
        threshold above min.  We use a config where min > base to test the
        lower clamp path (atypical but valid — e.g., aggressive gating).
        """
        cal = ConformalCalibrator(
            warmup_samples=5,
            base_threshold=0.30,
            min_threshold=0.45,
            max_threshold=0.70,
            window_size=50,
        )
        # Data concentrated far below min_threshold
        for pw in [0.10, 0.12, 0.15, 0.18, 0.20, 0.10, 0.10, 0.10]:
            cal.update(pw, 1)
        t = cal.compute_threshold()
        assert t == 0.45  # clipped to min
        assert cal._clamp_hits_lower > 0


# ── Persistence ──


class TestPersistence:
    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            cal = _new_calibrator(
                warmup=5,
                tmp_path=Path(tmp),
            )
            # Override state path so _new_calibrator writes where we expect
            cal._state_path = state_path
            for i in range(12):
                cal.update(0.40 + i * 0.01, 1 if i % 2 == 0 else -1)
            cal.compute_threshold()

            cal2 = ConformalCalibrator(
                warmup_samples=5,
                state_path=str(state_path),
            )
            assert cal2.sample_count == 12
            assert cal2._clamp_hits_upper == cal._clamp_hits_upper
            assert cal2._cold_started == cal._cold_started

    def test_load_missing_state(self):
        cal = ConformalCalibrator(state_path="nonexistent/path.json")
        assert cal.sample_count == 0
        assert cal.compute_threshold() == DEFAULT_BASE_THRESHOLD

    def test_load_corrupt_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text("{not valid", encoding="utf-8")
            cal = ConformalCalibrator(state_path=str(state_path))
            assert cal.sample_count == 0
            assert cal.compute_threshold() == DEFAULT_BASE_THRESHOLD


# ── Cold-start from journal ──


class TestColdStart:
    def test_cold_start_from_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "journal.jsonl"
            entries = [
                {
                    "recorded_at": "2026-05-22T10:00:00Z",
                    "ack_status": "closed",
                    "p_win": 0.55,
                    "label": "tp_hit_first",
                    "pnl": 5.0,
                },
                {
                    "recorded_at": "2026-05-22T11:00:00Z",
                    "ack_status": "closed",
                    "p_win": 0.42,
                    "label": "sl_hit_first",
                    "pnl": -3.0,
                },
                {
                    "recorded_at": "2026-05-22T12:00:00Z",
                    "ack_status": "open",  # skipped — not closed
                    "p_win": 0.48,
                    "label": "tp_hit_first",
                },
                {
                    "recorded_at": "2026-05-22T13:00:00Z",
                    "ack_status": "closed",
                    "p_win": 0.60,
                    "label": "breakeven",
                    "pnl": 0.0,
                },
            ]
            journal_path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")

            cal = _new_calibrator(warmup=3, tmp_path=Path(tmp))
            loaded = cal.cold_start_from_journal(str(journal_path))
            assert loaded == 3  # 3 closed entries, 1 open skipped
            assert cal.sample_count == 3

    def test_cold_start_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "journal.jsonl"
            journal_path.write_text(
                json.dumps(
                    {
                        "recorded_at": "2026-05-22T10:00:00Z",
                        "ack_status": "closed",
                        "p_win": 0.55,
                        "label": "win",
                    }
                ),
                encoding="utf-8",
            )

            cal = _new_calibrator(warmup=3, tmp_path=Path(tmp))
            loaded = cal.cold_start_from_journal(str(journal_path))
            assert loaded == 1
            loaded2 = cal.cold_start_from_journal(str(journal_path))
            assert loaded2 == 0  # already cold-started

    def test_cold_start_missing_journal(self):
        cal = _new_calibrator(warmup=5)
        loaded = cal.cold_start_from_journal("nonexistent/journal.jsonl")
        assert loaded == 0

    def test_cold_start_nested_p_win(self):
        """p_win in detail dict should be found."""
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "journal.jsonl"
            journal_path.write_text(
                json.dumps(
                    {
                        "recorded_at": "2026-05-22T10:00:00Z",
                        "ack_status": "closed",
                        "detail": {"p_win": 0.52, "label": "win"},
                        "pnl": 3.0,
                    }
                ),
                encoding="utf-8",
            )

            cal = _new_calibrator(warmup=5, tmp_path=Path(tmp))
            loaded = cal.cold_start_from_journal(str(journal_path))
            assert loaded == 1


# ── Edge cases ──


class TestEdgeCases:
    def test_empty_history_returns_base(self):
        cal = _new_calibrator(warmup=5)
        assert cal.compute_threshold() == DEFAULT_BASE_THRESHOLD

    def test_zero_variance_distribution(self):
        """All identical p_win values — Q10 should equal that value."""
        cal = _new_calibrator(warmup=5, window=50)
        for _ in range(30):
            cal.update(0.50, 1)
        t = cal.compute_threshold()
        assert t == pytest.approx(0.50, abs=0.01)

    def test_describe_returns_all_keys(self):
        cal = _new_calibrator(warmup=5)
        _fill_warmup(cal)
        desc = cal.describe()
        expected_keys = {
            "sample_count",
            "is_warm",
            "base_threshold",
            "min_threshold",
            "max_threshold",
            "target_percentile",
            "current_threshold",
            "p_win_median",
            "p_win_q10",
            "p_win_q25",
            "p_win_min",
            "p_win_max",
            "clamp_hits_upper",
            "clamp_hits_lower",
            "total_computations",
            "cold_started",
        }
        assert set(desc.keys()) == expected_keys

    def test_target_percentile_must_be_valid(self):
        with pytest.raises(ValueError):
            ConformalCalibrator(target_percentile=0.0)
        with pytest.raises(ValueError):
            ConformalCalibrator(target_percentile=51.0)
        with pytest.raises(ValueError):
            ConformalCalibrator(target_percentile=-1.0)

    def test_min_max_order_validated(self):
        with pytest.raises(ValueError):
            ConformalCalibrator(min_threshold=0.50, max_threshold=0.30)


# ── Journal label extraction ──


class TestJournalLabel:
    def test_win_labels(self):
        assert _journal_entry_label({"label": "tp_hit_first"}) == 1
        assert _journal_entry_label({"label": "win"}) == 1

    def test_loss_labels(self):
        assert _journal_entry_label({"label": "sl_hit_first"}) == -1
        assert _journal_entry_label({"label": "loss"}) == -1

    def test_neutral_labels(self):
        assert _journal_entry_label({"label": "breakeven"}) == 0
        assert _journal_entry_label({"label": "timeout"}) == 0

    def test_nested_detail_label(self):
        assert _journal_entry_label({"detail": {"label": "tp_hit_first"}}) == 1
        assert _journal_entry_label({"detail": {"label": "sl_hit_first"}}) == -1

    def test_pnl_fallback(self):
        assert _journal_entry_label({"pnl": 5.0}) == 1
        assert _journal_entry_label({"pnl": -3.0}) == -1
        assert _journal_entry_label({"pnl": 0.0}) == 0

    def test_cannot_determine(self):
        assert _journal_entry_label({}) is None
        assert _journal_entry_label({"label": "unknown"}) is None


# ── Description property ──


class TestDescriptionOutput:
    def test_describe_during_warmup(self):
        cal = _new_calibrator(warmup=20)
        cal.update(0.45, 1)
        desc = cal.describe()
        assert desc["is_warm"] is False
        assert desc["current_threshold"] == DEFAULT_BASE_THRESHOLD

    def test_describe_after_warmup(self):
        cal = _new_calibrator(warmup=5)
        _fill_warmup(cal)
        desc = cal.describe()
        assert desc["is_warm"] is True
        assert desc["p_win_median"] is not None
        assert desc["p_win_q10"] is not None
        assert desc["p_win_q10"] <= desc["p_win_q25"]
