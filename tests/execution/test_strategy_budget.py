"""Tests for core/execution/strategy_budget.py — per-strategy risk budget."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from core.execution.strategy_budget import StrategyBudget

# Deliberate invalid-input probes — Any-typed so mypy permits the static violation
# while runtime validation still receives the raw bad value (both-mode clean).
_BAD_PNL: Any = "bad"
_NONE_PNL: Any = None
_BAD_TS: Any = "bad"
_BAD_SAVED: Any = "not_a_dict"
_NONE_SAVED: Any = None


class TestStrategyBudgetCheckPause:
    def test_not_paused_returns_false(self):
        budget = StrategyBudget(strategy_name="barrier_12bar")
        assert budget.check_pause() is False

    def test_paused_within_cooldown_returns_true(self, monkeypatch):
        budget = StrategyBudget(strategy_name="barrier_12bar", cooldown_minutes=30)
        budget.paused = True
        budget.paused_at = 1000.0

        # time is within cooldown (1000 + 30*60 = 2800)
        monkeypatch.setattr("time.time", lambda: 1000.0 + 60.0)
        assert budget.check_pause() is True

    def test_paused_after_cooldown_returns_false(self, monkeypatch):
        budget = StrategyBudget(strategy_name="barrier_12bar", cooldown_minutes=30)
        budget.paused = True
        budget.paused_at = 1000.0

        monkeypatch.setattr("time.time", lambda: 1000.0 + 30.0 * 60.0 + 1.0)
        assert budget.check_pause() is False
        assert budget.paused is False  # auto-unpaused

    def test_zero_cooldown_never_unpauses_automatically(self, monkeypatch):
        """With cooldown 0 the pause check uses > 0 comparison, no auto-unpause."""
        budget = StrategyBudget(strategy_name="barrier_12bar", cooldown_minutes=0)
        budget.paused = True
        budget.paused_at = 500.0
        monkeypatch.setattr("time.time", lambda: 500.0 + 9999.0)
        # cooldown detection: cooldown_minutes > 0 is False, so stays paused
        assert budget.check_pause() is True


class TestStrategyBudgetRecordTrade:
    def test_win_resets_consecutive_losses(self):
        budget = StrategyBudget(strategy_name="micro_3bar")
        budget.consecutive_losses = 3
        budget.record_trade(pnl_pct=0.01, is_win=True)
        assert budget.consecutive_losses == 0

    def test_consecutive_losses_increment(self):
        budget = StrategyBudget(strategy_name="micro_3bar")
        budget.record_trade(pnl_pct=-0.005, is_win=False)
        assert budget.consecutive_losses == 1
        budget.record_trade(pnl_pct=-0.003, is_win=False)
        assert budget.consecutive_losses == 2

    def test_max_consecutive_losses_pauses(self):
        budget = StrategyBudget(strategy_name="statarb_dynamic", max_consecutive_losses=3)
        for _ in range(3):
            budget.record_trade(pnl_pct=-0.01, is_win=False)
        assert budget.paused is True
        assert budget.check_pause() is True

    def test_daily_loss_limit_pauses(self):
        budget = StrategyBudget(
            strategy_name="barrier_12bar",
            daily_loss_limit_pct=-0.03,
            account_balance=10000.0,
        )
        # Single trade with -0.04 PnL = -4% → exceeds -3% limit
        budget.record_trade(pnl_pct=-0.04, is_win=False)
        assert budget.paused is True

    def test_daily_loss_limit_not_breached(self):
        budget = StrategyBudget(
            strategy_name="barrier_12bar",
            daily_loss_limit_pct=-0.03,
            account_balance=10000.0,
        )
        budget.record_trade(pnl_pct=-0.01, is_win=False)
        budget.record_trade(pnl_pct=-0.01, is_win=False)
        # -2% total, within -3% limit
        assert budget.paused is False

    def test_daily_reset_on_new_day(self, monkeypatch):
        budget = StrategyBudget(strategy_name="barrier_12bar")
        budget.daily_pnl_pct = -0.02
        budget.consecutive_losses = 3
        budget.total_trades_today = 5
        budget.total_wins_today = 2

        # Simulate new day — datetime.now(UTC) calls classmethod now(cls, tz)
        def _fake_now(tz=None):
            return datetime(2026, 1, 2, 9, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "core.execution.strategy_budget.datetime",
            type("FakeDT", (), {"now": staticmethod(_fake_now)})(),
        )

        # Trigger _today() which should detect new day
        budget.last_trade_day = "2026-01-01"
        budget.record_trade(pnl_pct=0.01, is_win=True)

        # After reset: daily counters fresh, single win added
        assert budget.total_trades_today == 1
        assert budget.total_wins_today == 1
        assert budget.daily_pnl_pct == pytest.approx(0.01)

    def test_record_trade_returns_dict(self):
        budget = StrategyBudget(strategy_name="barrier_12bar")
        result = budget.record_trade(pnl_pct=0.02, is_win=True)
        assert isinstance(result, dict)
        assert "event" in result

    def test_win_rate_today_zero_trades(self):
        budget = StrategyBudget(strategy_name="barrier_12bar")
        assert budget.win_rate_today == 0.0

    def test_win_rate_today_calculation(self):
        budget = StrategyBudget(strategy_name="barrier_12bar")
        budget.total_trades_today = 5
        budget.total_wins_today = 3
        assert budget.win_rate_today == 0.6

    def test_to_dict_export(self):
        budget = StrategyBudget(strategy_name="barrier_12bar", account_balance=50000.0)
        d = budget.to_dict()
        assert d["strategy_name"] == "barrier_12bar"
        assert d["paused"] is False
        assert "daily_pnl_pct" in d
        assert "consecutive_losses" in d
        assert "total_trades_today" in d
        assert "win_rate_today" in d

    def test_record_win_after_pause_does_not_reset_pause(self):
        """Wins don't auto-unpause — cooldown timer does."""
        budget = StrategyBudget(
            strategy_name="barrier_12bar",
            max_consecutive_losses=2,
            cooldown_minutes=30,
        )
        budget.record_trade(pnl_pct=-0.01, is_win=False)
        budget.record_trade(pnl_pct=-0.01, is_win=False)
        assert budget.paused is True
        # A win while paused should not unpause
        budget.record_trade(pnl_pct=0.01, is_win=True)
        assert budget.paused is True


# ═══════════════════════════════════════════════════════════════════════════
# UGR-A08: CapResult-wrapped validated methods
# ═══════════════════════════════════════════════════════════════════════════


class TestStrategyBudgetRecordTradeChecked:
    """Tests for record_trade_checked() — CapResult-wrapped record_trade."""

    def test_ok_returns_capresult_ok(self):
        budget = StrategyBudget(strategy_name="barrier_12bar")
        result = budget.record_trade_checked(pnl_pct=0.02, is_win=True)
        assert result.is_ok()
        assert result.is_err() is False
        inner = result.match(ok=lambda v: v, err=lambda e: None)
        assert inner is not None
        assert inner["event"] == "trade_recorded"

    def test_err_on_non_numeric_pnl(self):
        budget = StrategyBudget(strategy_name="barrier_12bar")
        result = budget.record_trade_checked(pnl_pct=_BAD_PNL, is_win=True)
        assert result.is_err()
        error = result.match(ok=lambda v: None, err=lambda e: e)
        assert "pnl_pct must be numeric" in (error or "")

    def test_err_on_none_pnl(self):
        budget = StrategyBudget(strategy_name="barrier_12bar")
        result = budget.record_trade_checked(pnl_pct=_NONE_PNL, is_win=True)
        assert result.is_err()

    def test_loss_count_increments_on_err(self):
        """Error path does NOT mutate state — validation fails before mutation."""
        budget = StrategyBudget(strategy_name="barrier_12bar")
        initial = budget.consecutive_losses
        budget.record_trade_checked(pnl_pct=_BAD_PNL, is_win=False)
        assert budget.consecutive_losses == initial  # no mutation on error

    def test_ok_pauses_on_daily_loss_limit(self):
        budget = StrategyBudget(
            strategy_name="barrier_12bar",
            daily_loss_limit_pct=-0.03,
        )
        result = budget.record_trade_checked(pnl_pct=-0.04, is_win=False)
        assert result.is_ok()
        inner = result.match(ok=lambda v: v, err=lambda e: None)
        assert inner is not None
        assert inner["event"] == "strategy_paused"
        assert inner["reason"] == "daily_loss_limit"

    def test_match_pattern_works(self):
        """Verify .match() pattern works as expected for live_cycle integration."""
        budget = StrategyBudget(strategy_name="barrier_12bar")
        seen_ok: list[str] = []
        seen_err: list[str] = []

        budget.record_trade_checked(0.01, True).match(
            ok=lambda v: seen_ok.append(v["event"]),
            err=lambda e: seen_err.append(e),
        )
        assert len(seen_ok) == 1
        assert seen_ok[0] == "trade_recorded"
        assert len(seen_err) == 0

        budget.record_trade_checked(_BAD_PNL, True).match(
            ok=lambda v: seen_ok.append(v["event"]),
            err=lambda e: seen_err.append(e),
        )
        assert len(seen_ok) == 1  # unchanged
        assert len(seen_err) == 1


class TestStrategyBudgetRecordSLChecked:
    """Tests for record_sl_checked() — CapResult-wrapped record_sl."""

    def test_ok_returns_capresult_ok(self, monkeypatch):
        budget = StrategyBudget(strategy_name="barrier_12bar")
        # Freeze time so SL cooldown is deterministic
        monkeypatch.setattr("time.time", lambda: 1000000.0)
        result = budget.record_sl_checked(timestamp=1000000.0)
        assert result.is_ok()
        inner = result.match(ok=lambda v: v, err=lambda e: None)
        assert inner is not None
        assert "event" in (inner or {})

    def test_ok_with_none_timestamp_uses_current_time(self):
        budget = StrategyBudget(strategy_name="barrier_12bar")
        result = budget.record_sl_checked(timestamp=None)
        assert result.is_ok()

    def test_err_on_non_numeric_timestamp(self):
        budget = StrategyBudget(strategy_name="barrier_12bar")
        result = budget.record_sl_checked(timestamp=_BAD_TS)
        assert result.is_err()
        error = result.match(ok=lambda v: None, err=lambda e: e)
        assert "timestamp must be numeric" in (error or "")

    def test_graduated_cooldown_tiers(self, monkeypatch):
        """4 SLs in window → rest-of-day pause."""
        budget = StrategyBudget(strategy_name="barrier_12bar")
        base = 1000000.0
        monkeypatch.setattr("time.time", lambda: base)

        for i in range(4):
            result = budget.record_sl_checked(timestamp=base + i)
            assert result.is_ok(), f"SL {i + 1} should be ok"

        # 4th SL triggers rest-of-day pause
        last = budget.record_sl_checked(timestamp=base + 4)
        inner = last.match(ok=lambda v: v, err=lambda e: None)
        assert inner is not None
        assert inner["event"] == "sl_cooldown_paused_day"

    def test_sl_count_not_mutated_on_error(self):
        """Error path does NOT register an SL."""
        budget = StrategyBudget(strategy_name="barrier_12bar")
        initial_count = len(budget._sl_timestamps)
        budget.record_sl_checked(timestamp=_BAD_TS)
        assert len(budget._sl_timestamps) == initial_count


class TestStrategyBudgetLoadStateChecked:
    """Tests for load_state_checked() — CapResult-wrapped load_state."""

    def test_ok_restores_state(self):
        budget = StrategyBudget(strategy_name="barrier_12bar")
        _today = budget._today()
        result = budget.load_state_checked(
            {
                "daily_pnl_pct": -0.02,
                "consecutive_losses": 3,
                "total_trades_today": 10,
                "total_wins_today": 4,
                "paused": False,
                "last_trade_day": _today,  # same-day restore to avoid cross-day reset
            }
        )
        assert result.is_ok()
        result.match(ok=lambda _: None, err=lambda e: None)
        assert budget.daily_pnl_pct == pytest.approx(-0.02)
        assert budget.consecutive_losses == 3
        assert budget.total_trades_today == 10

    def test_err_on_non_dict_input(self):
        budget = StrategyBudget(strategy_name="barrier_12bar")
        result = budget.load_state_checked(_BAD_SAVED)
        assert result.is_err()
        error = result.match(ok=lambda v: None, err=lambda e: e)
        assert "expected dict" in (error or "")

    def test_err_on_none_input(self):
        budget = StrategyBudget(strategy_name="barrier_12bar")
        result = budget.load_state_checked(_NONE_SAVED)
        assert result.is_err()

    def test_state_not_mutated_on_error(self):
        """Error path does NOT mutate budget state."""
        budget = StrategyBudget(strategy_name="barrier_12bar")
        budget.daily_pnl_pct = -0.01
        original = budget.daily_pnl_pct
        budget.load_state_checked(_BAD_SAVED)
        assert budget.daily_pnl_pct == original

    def test_partial_state_backward_compatible(self):
        """Empty/partial saved dict is valid — load_state tolerates missing keys."""
        budget = StrategyBudget(strategy_name="barrier_12bar")
        result = budget.load_state_checked({})
        assert result.is_ok()

    def test_cross_day_reset(self, monkeypatch):
        """Cross-day saved state triggers eager daily reset (DQAF-20260614-001)."""
        budget = StrategyBudget(strategy_name="barrier_12bar")
        budget.daily_pnl_pct = -0.05
        budget.consecutive_losses = 5
        budget.last_trade_day = "2026-01-01"

        # Simulate new day
        def _fake_now(tz=None):
            return datetime(2026, 1, 2, 9, 0, tzinfo=UTC)

        monkeypatch.setattr(
            "core.execution.strategy_budget.datetime",
            type("FakeDT", (), {"now": staticmethod(_fake_now)})(),
        )

        result = budget.load_state_checked(
            {
                "daily_pnl_pct": -0.05,
                "consecutive_losses": 5,
                "last_trade_day": "2026-01-01",
            }
        )
        assert result.is_ok()
        # Cross-day reset: counters zeroed
        assert budget.daily_pnl_pct == 0.0
        assert budget.consecutive_losses == 0
