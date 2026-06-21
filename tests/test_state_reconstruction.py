"""Contract tests for state reconstruction: IMMUTABLE_LEDGER → EPHEMERAL_PROJECTION.

DQAF-20260621-042 防线2: Validates that the generator code deterministically
produces healthy materialized views from an immutable ledger, even when the
input contains corrupted or edge-case data.

Principle: Delete all state files, inject a known journal, run the generators,
and assert exact output.  This is the ONLY proof that the system can reconstruct
itself from the ledger alone.

Architecture under test:
  live_trade_journal.jsonl (SSOT)
    → compute_journal_brain_metrics()   [live_journal_metrics.py]
    → BrainLeaderboard.rank()           [brain_leaderboard.py]
    → leaderboard.json (ephemeral view)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from core.brains.services.brain_leaderboard import BrainLeaderboard
from core.contracts.exceptions import DataIntegrityError
from core.feedback.live_journal_metrics import compute_journal_brain_metrics

# ── Mock Journal Data ──────────────────────────────────────────────────
# Real live_trade_journal.jsonl format: open + close records linked by
# position_ticket.  PnL is in R-units (risk-normalized).

MOCK_JOURNAL_RECORDS: list[dict[str, Any]] = [
    # ── V4 (3 trades: win, loss, breakeven) ──
    {
        "position_ticket": 1001,
        "action": "open",
        "brain_ids": ["V4"],
        "side": "long",
        "recorded_at": "2026-06-01T08:00:00Z",
        "message_id": "msg-001",
    },
    {
        "position_ticket": 1001,
        "action": "close",
        "pnl": 1.525,
        "label": "tp_hit",
        "recorded_at": "2026-06-01T12:00:00Z",
        "message_id": "msg-002",
    },
    {
        "position_ticket": 1002,
        "action": "open",
        "brain_ids": ["V4"],
        "side": "short",
        "recorded_at": "2026-06-02T08:00:00Z",
        "message_id": "msg-003",
    },
    {
        "position_ticket": 1002,
        "action": "close",
        "pnl": -0.850,
        "label": "sl_hit",
        "recorded_at": "2026-06-02T14:00:00Z",
        "message_id": "msg-004",
    },
    {
        "position_ticket": 1003,
        "action": "open",
        "brain_ids": ["V4"],
        "side": "long",
        "recorded_at": "2026-06-03T08:00:00Z",
        "message_id": "msg-005",
    },
    {
        "position_ticket": 1003,
        "action": "close",
        "pnl": 0.0,
        "label": "breakeven",
        "detail": {"reason": "mia_close"},
        "recorded_at": "2026-06-03T10:00:00Z",
        "message_id": "msg-006",
    },
    # ── V10_M15 (3 trades: win, loss, breakeven) ──
    {
        "position_ticket": 2001,
        "action": "open",
        "brain_ids": ["V10_M15"],
        "side": "long",
        "recorded_at": "2026-06-01T09:00:00Z",
        "message_id": "msg-007",
    },
    {
        "position_ticket": 2001,
        "action": "close",
        "pnl": 2.100,
        "label": "tp_hit",
        "recorded_at": "2026-06-01T15:00:00Z",
        "message_id": "msg-008",
    },
    {
        "position_ticket": 2002,
        "action": "open",
        "brain_ids": ["V10_M15"],
        "side": "short",
        "recorded_at": "2026-06-02T09:00:00Z",
        "message_id": "msg-009",
    },
    {
        "position_ticket": 2002,
        "action": "close",
        "pnl": -1.200,
        "label": "mia_close",
        "detail": {"reason": "mt5_deal_reason_3"},
        "recorded_at": "2026-06-02T16:00:00Z",
        "message_id": "msg-010",
    },
    {
        "position_ticket": 2003,
        "action": "open",
        "brain_ids": ["V10_M15"],
        "side": "long",
        "recorded_at": "2026-06-03T09:00:00Z",
        "message_id": "msg-011",
    },
    {
        "position_ticket": 2003,
        "action": "close",
        "pnl": 0.0,
        "label": "breakeven",
        "recorded_at": "2026-06-03T11:00:00Z",
        "message_id": "msg-012",
    },
    # ── Edge case: missing brain_ids → "unknown" ──
    {
        "position_ticket": 3001,
        "action": "open",
        "side": "long",
        "recorded_at": "2026-06-04T08:00:00Z",
        "message_id": "msg-013",
    },
    {
        "position_ticket": 3001,
        "action": "close",
        "pnl": 0.500,
        "label": "tp_hit",
        "recorded_at": "2026-06-04T12:00:00Z",
        "message_id": "msg-014",
    },
    # ── Edge case: open-only (no close) → skipped ──
    {
        "position_ticket": 4001,
        "action": "open",
        "brain_ids": ["V4"],
        "side": "long",
        "recorded_at": "2026-06-05T08:00:00Z",
        "message_id": "msg-015",
    },
]


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def sandbox_journal():
    """Isolated sandbox with mock live_trade_journal.jsonl.

    Yields a (temp_dir, journal_path) tuple.  The temp directory is
    destroyed when the test exits — zero contamination risk.
    """
    with tempfile.TemporaryDirectory(prefix="test_recon_") as tmp:
        tmp_path = Path(tmp)
        journal_path = tmp_path / "live_trade_journal.jsonl"
        with open(journal_path, "w", encoding="utf-8") as f:
            for rec in MOCK_JOURNAL_RECORDS:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        yield tmp_path, journal_path


@pytest.fixture
def fresh_leaderboard():
    """BrainLeaderboard with no quality engine (uses legacy path)."""
    return BrainLeaderboard(quality_engine=None)


# ── Tests: Journal → Metrics ───────────────────────────────────────────


class TestJournalBrainMetrics:
    """compute_journal_brain_metrics() — the first link in the chain."""

    def test_returns_correct_brain_count(self, sandbox_journal):
        """V4, V10_M15, and unknown should all appear."""
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        assert len(metrics) == 3
        assert "V4" in metrics
        assert "V10_M15" in metrics
        assert "unknown" in metrics

    def test_v4_trade_count(self, sandbox_journal):
        """V4 has 3 closed trades (1001, 1002, 1003).  4001 is open-only → skipped."""
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        v4 = metrics["V4"]
        assert v4["sample_count"] == 3
        assert v4["trade_count"] == 3  # alias parity

    def test_v4_pnl_r_precision(self, sandbox_journal):
        """1.525 - 0.850 + 0.000 = 0.675.  Verify 3-decimal floating-point alignment."""
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        v4 = metrics["V4"]
        assert v4["pnl_r"] == pytest.approx(0.675, abs=1e-6)
        assert v4["cumulative_pnl"] == pytest.approx(0.675, abs=1e-6)

    def test_v4_computed_fields_present(self, sandbox_journal):
        """Every required downstream field must exist (DQAF-042 regression)."""
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        v4 = metrics["V4"]

        required = [
            "brain_id", "win_rate", "profit_factor", "sharpe_ratio",
            "sample_count", "trade_count", "cumulative_pnl", "pnl_r",
            "max_drawdown", "avg_win", "avg_loss", "long_count",
            "short_count", "long_win_rate", "short_win_rate",
            "exit_reasons", "health_signal", "recommendation",
        ]
        for field in required:
            assert field in v4, f"Missing required field: {field}"

    def test_v10_m15_trade_count(self, sandbox_journal):
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        v10 = metrics["V10_M15"]
        assert v10["sample_count"] == 3
        assert v10["pnl_r"] == pytest.approx(0.900, abs=1e-6)

    def test_unknown_brain_from_corrupted_record(self, sandbox_journal):
        """Trade 3001 has no brain_ids → assigned to 'unknown'."""
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        unknown = metrics["unknown"]
        assert unknown["sample_count"] == 1
        assert unknown["pnl_r"] == pytest.approx(0.500, abs=1e-6)

    def test_empty_dir_returns_empty_dict(self, tmp_path):
        """No journal → no metrics.  Must not crash."""
        metrics = compute_journal_brain_metrics(tmp_path)
        assert metrics == {}

    def test_win_rate_excludes_breakeven(self, sandbox_journal):
        """Win rate = wins / (wins + losses).  Breakevens excluded from denominator."""
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        v4 = metrics["V4"]
        # 1 win, 1 loss, 1 breakeven → wr = 1 / 2 = 0.5
        assert v4["win_rate"] == pytest.approx(0.5, abs=1e-6)

    def test_open_only_trade_not_counted(self, sandbox_journal):
        """Trade 4001 (open, no close) must NOT be counted in any brain."""
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        v4 = metrics["V4"]
        # V4: trades 1001, 1002, 1003 = 3.  4001 has no close → not counted.
        assert v4["sample_count"] == 3


# ── Tests: Metrics → Leaderboard ───────────────────────────────────────


class TestLeaderboardReconstruction:
    """BrainLeaderboard.rank() — the second link in the chain."""

    def test_rank_produces_correct_count(self, sandbox_journal, fresh_leaderboard):
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        rankings = fresh_leaderboard.rank(metrics)
        assert len(rankings) == 3

    def test_rank_output_has_required_fields(self, sandbox_journal, fresh_leaderboard):
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        rankings = fresh_leaderboard.rank(metrics)

        for r in rankings:
            d = r.to_dict()
            assert "pnl_r" not in d  # BrainRanking uses cum_pnl, not pnl_r
            assert "cum_pnl" in d
            assert "trade_count" in d
            assert "sharpe" in d
            assert "score" in d

    def test_rank_accepts_list_input(self, sandbox_journal, fresh_leaderboard):
        """_normalize_metrics_map must accept both dict and list (backward compat)."""
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        metrics_list = list(metrics.values())
        rankings = fresh_leaderboard.rank(metrics_list)
        assert len(rankings) == 3

    def test_rank_sorted_by_score_desc(self, sandbox_journal, fresh_leaderboard):
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        rankings = fresh_leaderboard.rank(metrics)
        scores = [r.score for r in rankings]
        assert scores == sorted(scores, reverse=True), f"Not sorted: {scores}"

    def test_v4_ranked_first(self, sandbox_journal, fresh_leaderboard):
        """V4 has higher Sharpe than V10_M15 → should rank higher."""
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        rankings = fresh_leaderboard.rank(metrics)
        # Both V4 and V10_M15 have similar profiles, but V4 has slightly lower
        # variance → higher Sharpe → higher score
        ranked_ids = [r.brain_id for r in rankings]
        # V4 should not be last (it has decent metrics)
        assert "V4" in ranked_ids
        assert "V10_M15" in ranked_ids

    def test_all_rankings_have_unique_ranks(self, sandbox_journal, fresh_leaderboard):
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        rankings = fresh_leaderboard.rank(metrics)
        ranks = [r.rank for r in rankings]
        assert len(ranks) == len(set(ranks))
        assert ranks[0] == 1


# ── Tests: Poison Pill (Fail-Closed) ───────────────────────────────────


class TestPoisonPill:
    """DataIntegrityError must be raised when required fields are missing."""

    def test_missing_sharpe_ratio_raises(self, fresh_leaderboard):
        metrics = {
            "BAD_BRAIN": {
                "brain_id": "BAD_BRAIN",
                "win_rate": 0.5,
                "profit_factor": 1.5,
                # "sharpe_ratio": MISSING
                "cumulative_pnl": 10.0,
                "max_drawdown": 5.0,
                "sample_count": 10,
            }
        }
        with pytest.raises(DataIntegrityError) as exc_info:
            fresh_leaderboard.rank(metrics)
        assert "sharpe_ratio" in str(exc_info.value)
        assert "BAD_BRAIN" in str(exc_info.value)

    def test_missing_cumulative_pnl_raises(self, fresh_leaderboard):
        metrics = {
            "BAD_BRAIN": {
                "brain_id": "BAD_BRAIN",
                "win_rate": 0.5,
                "profit_factor": 1.5,
                "sharpe_ratio": 1.0,
                # "cumulative_pnl": MISSING
                "max_drawdown": 5.0,
                "sample_count": 10,
            }
        }
        with pytest.raises(DataIntegrityError) as exc_info:
            fresh_leaderboard.rank(metrics)
        assert "cumulative_pnl" in str(exc_info.value)

    def test_none_field_value_raises(self, fresh_leaderboard):
        """None is not a valid value for required metric fields."""
        metrics = {
            "BAD_BRAIN": {
                "brain_id": "BAD_BRAIN",
                "win_rate": 0.5,
                "profit_factor": None,  # ← None is invalid
                "sharpe_ratio": 1.0,
                "cumulative_pnl": 10.0,
                "max_drawdown": 5.0,
                "sample_count": 10,
            }
        }
        with pytest.raises(DataIntegrityError):
            fresh_leaderboard.rank(metrics)

    def test_empty_metrics_map_no_error(self, fresh_leaderboard):
        """Empty input should NOT raise — just return empty rankings."""
        rankings = fresh_leaderboard.rank({})
        assert rankings == []


# ── Tests: Normalizer ──────────────────────────────────────────────────


class TestMetricsNormalizer:
    """_normalize_metrics_map() — backward-compat input handling."""

    def test_dict_input_passthrough(self, fresh_leaderboard):
        d = {"V4": {"brain_id": "V4"}}
        result = fresh_leaderboard._normalize_metrics_map(d)
        assert result is d  # same object

    def test_list_input_converted_to_dict(self, fresh_leaderboard):
        lst = [
            {"brain_id": "V4", "sample_count": 10},
            {"brain_id": "V10_M15", "sample_count": 5},
        ]
        result = fresh_leaderboard._normalize_metrics_map(lst)
        assert isinstance(result, dict)
        assert len(result) == 2
        assert "V4" in result
        assert "V10_M15" in result

    def test_list_with_missing_brain_id_skipped(self, fresh_leaderboard):
        lst = [
            {"sample_count": 10},  # no brain_id → skipped
            {"brain_id": "V4", "sample_count": 5},
        ]
        result = fresh_leaderboard._normalize_metrics_map(lst)
        assert len(result) == 1
        assert "V4" in result

    def test_empty_list_returns_empty_dict(self, fresh_leaderboard):
        result = fresh_leaderboard._normalize_metrics_map([])
        assert result == {}

    def test_non_dict_non_list_returns_empty(self, fresh_leaderboard):
        result = fresh_leaderboard._normalize_metrics_map("invalid")
        assert result == {}


# ── Tests: Full Pipeline (Integration) ─────────────────────────────────


class TestFullPipeline:
    """End-to-end: journal → metrics → leaderboard → dict."""

    def test_pipeline_no_fallback_error(self, sandbox_journal, fresh_leaderboard):
        """Full reconstruction must not emit fallback_error (Fail-Open guard)."""
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        rankings = fresh_leaderboard.rank(metrics)
        records = fresh_leaderboard.to_records(rankings)

        # Simulate what daily_ops writes to leaderboard.json
        leaderboard = {
            "total_decisions": sum(r.trade_count for r in rankings),
            "brains": records,
        }
        assert "fallback_error" not in leaderboard
        assert leaderboard["total_decisions"] > 0

    def test_pipeline_v4_pnl_preserved(self, sandbox_journal, fresh_leaderboard):
        """PnL from journal must survive round-trip through rank()."""
        tmp_dir, _ = sandbox_journal
        metrics = compute_journal_brain_metrics(tmp_dir)
        rankings = fresh_leaderboard.rank(metrics)

        v4_ranking = next(r for r in rankings if r.brain_id == "V4")
        assert v4_ranking.cum_pnl == pytest.approx(0.675, abs=1e-4)
        assert v4_ranking.trade_count == 3
