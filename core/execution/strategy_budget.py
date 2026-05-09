"""Per-strategy risk budget tracking.

Each strategy line gets an independent risk budget.  If a strategy hits its
daily loss limit or maximum consecutive losses it is paused for the rest of
the day — but OTHER strategies continue unaffected.  This is institutional
standard: you don't shut down the whole fund because one PM is having a bad day.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass
class StrategyBudget:
    strategy_name: str
    daily_loss_limit_pct: float = -0.03  # e.g. -3% of account
    max_consecutive_losses: int = 5
    cooldown_minutes: int = 30

    # Runtime state
    daily_pnl_pct: float = 0.0
    consecutive_losses: int = 0
    total_trades_today: int = 0
    total_wins_today: int = 0
    paused: bool = False
    paused_at: float = 0.0
    last_trade_day: str = ""  # ISO date, to reset daily counters

    # Account reference
    account_balance: float = 1000.0

    def _today(self) -> str:
        return datetime.now(UTC).date().isoformat()

    def _reset_daily(self) -> None:
        self.daily_pnl_pct = 0.0
        self.consecutive_losses = 0
        self.total_trades_today = 0
        self.total_wins_today = 0
        self.last_trade_day = self._today()

    def check_pause(self) -> bool:
        """Return True if this strategy is currently paused."""
        if not self.paused:
            return False
        if self.cooldown_minutes > 0 and _time.time() - self.paused_at > self.cooldown_minutes * 60:
            self.paused = False
            self.paused_at = 0.0
            return False
        return True

    def record_trade(self, pnl_pct: float, is_win: bool) -> dict[str, Any]:
        """Record a trade outcome and check if budget has been breached.

        Returns a status dict suitable for logging.
        """
        today = self._today()
        if self.last_trade_day != today:
            self._reset_daily()

        self.total_trades_today += 1
        self.daily_pnl_pct += pnl_pct

        if is_win:
            self.total_wins_today += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1

        # Check daily loss limit
        if self.daily_pnl_pct <= self.daily_loss_limit_pct:
            self.paused = True
            self.paused_at = _time.time()
            return {
                "event": "strategy_paused",
                "strategy": self.strategy_name,
                "reason": "daily_loss_limit",
                "daily_pnl_pct": round(self.daily_pnl_pct, 4),
                "limit": self.daily_loss_limit_pct,
            }

        # Check consecutive losses
        if self.consecutive_losses >= self.max_consecutive_losses:
            self.paused = True
            self.paused_at = _time.time()
            return {
                "event": "strategy_paused",
                "strategy": self.strategy_name,
                "reason": "max_consecutive_losses",
                "consecutive": self.consecutive_losses,
                "limit": self.max_consecutive_losses,
            }

        return {
            "event": "trade_recorded",
            "strategy": self.strategy_name,
            "daily_pnl_pct": round(self.daily_pnl_pct, 4),
            "consecutive_losses": self.consecutive_losses,
        }

    @property
    def win_rate_today(self) -> float:
        if self.total_trades_today == 0:
            return 0.0
        return self.total_wins_today / self.total_trades_today

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "paused": self.paused,
            "daily_pnl_pct": round(self.daily_pnl_pct, 4),
            "consecutive_losses": self.consecutive_losses,
            "total_trades_today": self.total_trades_today,
            "win_rate_today": round(self.win_rate_today, 4),
        }
