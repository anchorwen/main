"""Tests for core/execution/strategy_budget.py — per-strategy risk budget."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.execution.strategy_budget import StrategyBudget


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
