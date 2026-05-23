"""Unit tests for ExperienceReplayBuffer — R-weighting, shuffle, persistence."""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

from core.feedback.experience_replay import (
    _EMA_ALPHA,
    _MAX_WEIGHT,
    _MIN_WEIGHT,
    ExperienceReplayBuffer,
)

# ── Helpers ──


def _make_feat(seed: int) -> np.ndarray:
    rng = np.random.RandomState(seed)
    return rng.randn(40).astype(np.float64)


def _new_buf(size: int = 10, tmp_path: Path | None = None) -> ExperienceReplayBuffer:
    """Create a buffer with an isolated state file — no cross-test pollution."""
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp(prefix="replay_test_"))
    return ExperienceReplayBuffer(buffer_size=size, state_path=str(tmp_path / "replay_state.json"))


# ── Weight computation ──


class TestWeightComputation:
    def test_weight_proportional_to_r(self):
        buf = _new_buf()
        w_large = buf._compute_weight(pnl=10.0, volume=0.01)
        w_small = buf._compute_weight(pnl=1.0, volume=0.01)
        assert w_large > w_small, f"large PnL={w_large:.3f} <= small PnL={w_small:.3f}"

    def test_weight_clipped(self):
        buf = _new_buf()
        buf._running_r_mean = 0.01
        w = buf._compute_weight(pnl=10.0, volume=0.01)
        assert w == _MAX_WEIGHT, f"expected {_MAX_WEIGHT}, got {w}"

        buf._running_r_mean = 1000.0
        w = buf._compute_weight(pnl=1.0, volume=0.01)
        assert w == _MIN_WEIGHT, f"expected {_MIN_WEIGHT}, got {w}"

    def test_ema_converges(self):
        buf = _new_buf()
        for _ in range(50):
            buf._compute_weight(pnl=2.0, volume=0.01)
        expected = 1.0
        for _ in range(50):
            expected = _EMA_ALPHA * 2.0 + (1.0 - _EMA_ALPHA) * expected
        assert abs(buf._running_r_mean - expected) < 0.001

    def test_zero_pnl_handled(self):
        buf = _new_buf()
        w = buf._compute_weight(pnl=0.0, volume=0.01)
        assert _MIN_WEIGHT <= w <= _MAX_WEIGHT


# ── Buffer mechanics ──


class TestBufferMechanics:
    def test_add_and_is_ready(self):
        buf = _new_buf(size=5)
        for i in range(4):
            buf.add(_make_feat(i), label=1, pnl=5.0, volume=0.01)
            assert not buf.is_ready()
        buf.add(_make_feat(4), label=-1, pnl=-3.0, volume=0.01)
        assert buf.is_ready()
        assert buf.size == 5

    def test_flush_clears_buffer(self):
        buf = _new_buf(size=3)
        for i in range(3):
            buf.add(_make_feat(i), label=1, pnl=5.0, volume=0.01)
        assert buf.is_ready()
        batch = buf.flush()
        assert len(batch) >= 3
        assert buf.size == 0
        assert not buf.is_ready()

    def test_flush_shuffles(self):
        buf = _new_buf(size=5)
        for i in range(5):
            buf.add(_make_feat(i), label=1 if i % 2 == 0 else -1, pnl=5.0, volume=0.01)

        labels_insert_order = [lbl for _, lbl, _, _ in buf._buffer]

        differed = False
        for _ in range(10):
            for i in range(5):
                buf.add(_make_feat(i), label=1 if i % 2 == 0 else -1, pnl=5.0, volume=0.01)
            batch = buf.flush()
            batch_labels = [lbl for _, lbl in batch]
            if batch_labels != labels_insert_order:
                differed = True
                break

        assert differed, "shuffle never produced a different order in 10 attempts"

    def test_empty_flush(self):
        buf = _new_buf(size=20)
        assert buf.flush() == []


# ── Class imbalance ──


class TestClassImbalance:
    def test_extreme_imbalance_detected(self, caplog):
        buf = _new_buf(size=20)
        for i in range(19):
            buf.add(_make_feat(i), label=-1, pnl=-5.0, volume=0.01)
        buf.add(_make_feat(19), label=1, pnl=5.0, volume=0.01)

        with caplog.at_level("WARNING"):
            buf.flush()

        imbalance_logged = any("class imbalance" in rec.message.lower() for rec in caplog.records)
        assert (
            imbalance_logged
        ), f"Expected class imbalance warning, got: {[r.message for r in caplog.records]}"

    def test_balanced_no_warning(self, caplog):
        buf = _new_buf(size=6)
        for i in range(3):
            buf.add(_make_feat(i), label=1, pnl=5.0, volume=0.01)
        for i in range(3, 6):
            buf.add(_make_feat(i), label=-1, pnl=-5.0, volume=0.01)

        with caplog.at_level("WARNING"):
            buf.flush()

        imbalance_logged = any("class imbalance" in rec.message.lower() for rec in caplog.records)
        assert not imbalance_logged, f"Unexpected warning: {[r.message for r in caplog.records]}"


# ── Persistence ──


class TestPersistence:
    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "replay_state.json"
            buf = _new_buf(size=10, tmp_path=Path(tmp))
            for i in range(7):
                buf.add(_make_feat(i), label=1 if i % 2 == 0 else -1, pnl=5.0, volume=0.01)

            r_mean_before = buf._running_r_mean
            total_before = buf._total_added

            buf2 = ExperienceReplayBuffer(buffer_size=10, state_path=str(state_path))
            assert buf2.size == 7
            assert buf2._total_added == total_before
            assert abs(buf2._running_r_mean - r_mean_before) < 0.001
            assert buf2._total_flushed == 0

    def test_load_corrupt_state_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "replay_state.json"
            state_path.write_text("{not valid json", encoding="utf-8")
            buf = ExperienceReplayBuffer(buffer_size=10, state_path=str(state_path))
            assert buf.size == 0
            assert buf._running_r_mean == 1.0

    def test_load_missing_state(self):
        buf = ExperienceReplayBuffer(buffer_size=10, state_path="nonexistent/path.json")
        assert buf.size == 0
        assert buf._running_r_mean == 1.0


# ── Integration: hook wiring ──


class TestHookIntegration:
    def test_hook_uses_buffer(self):
        from core.feedback.online_feedback_hook import OnlineFeedbackHook

        class DummyAdapter:
            def __init__(self):
                self.fit_calls = []
                self.weights_saved = False

            def partial_fit(self, feat, label):
                self.fit_calls.append((label, feat.shape))
                return True

            def save_weights(self):
                self.weights_saved = True

        adapter = DummyAdapter()
        buf = _new_buf(size=3)

        hook = OnlineFeedbackHook(
            adapter=adapter,
            journal_path="/nonexistent/journal.jsonl",
            replay_buffer=buf,
        )
        result = hook.process_new_trades()
        assert result["status"] == "no_journal"

    def test__extract_pnl_volume(self):
        from core.feedback.online_feedback_hook import OnlineFeedbackHook

        pnl, vol = OnlineFeedbackHook._extract_pnl_volume({"pnl": 5.0, "volume": 0.02})
        assert pnl == 5.0
        assert vol == 0.02

        pnl, vol = OnlineFeedbackHook._extract_pnl_volume({"detail": {"pnl": -3.0}})
        assert pnl == -3.0
        assert vol == 0.01

        pnl, vol = OnlineFeedbackHook._extract_pnl_volume({"pnl": 0.0, "volume": 0.0})
        assert vol == 0.01
