"""Per-strategy risk budget tracking.

Each strategy line gets an independent risk budget.  If a strategy hits its
daily loss limit or maximum consecutive losses it is paused for the rest of
the day — but OTHER strategies continue unaffected.  This is institutional
standard: you don't shut down the whole fund because one PM is having a bad day.

Per-SL graduated cooldown (added 2026-05-13):
  Tier 1 (1st SL in window)    →  60 s cooldown
  Tier 2 (2nd SL in window)    → 300 s cooldown  (5 min)
  Tier 3 (3rd SL in window)    → 1800 s cooldown (30 min)
  Tier 4 (4th SL in window)    → pause rest of day
  Window: 1800 s (30 min) — SLs outside this window don't count.

This is inspired by Citadel's graduated PM risk limits: small losses cool
the strategy briefly; clustered losses escalate the cooling period.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ── Graduated per-SL cooldown ────────────────────────────────────────────

SL_COOLDOWN_TIERS: list[tuple[int, float]] = [
    (1, 60.0),  # 1st SL → 60 s
    (2, 300.0),  # 2nd SL → 5 min
    (3, 1800.0),  # 3rd SL → 30 min
    (4, float("inf")),  # 4th SL → rest of day
]
SL_COOLDOWN_WINDOW: float = 1800.0  # 30 min lookback


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

    # Per-SL graduated cooldown (separate from consecutive-loss pause)
    _sl_timestamps: list[float] = field(default_factory=list)
    _sl_cooldown_until: float = 0.0
    _sl_paused_rest_of_day: bool = False

    # Account reference
    account_balance: float = 1000.0

    def _today(self) -> str:
        return datetime.now(UTC).date().isoformat()

    def _reset_daily(self) -> None:
        self.daily_pnl_pct = 0.0
        self.consecutive_losses = 0
        self.total_trades_today = 0
        self.total_wins_today = 0
        self._sl_timestamps.clear()
        self._sl_cooldown_until = 0.0
        self._sl_paused_rest_of_day = False
        # FIX-20260610-007: Clear loss-limit pause on cross-day reset.
        # Previously only _sl_paused_rest_of_day was cleared; self.paused
        # stayed True across midnight, causing permanent budget_paused lock
        # when cooldown_minutes=0 (never auto-unpauses).
        self.paused = False
        self.paused_at = 0.0
        self.last_trade_day = self._today()

    def check_pause(self) -> bool:
        """Return True if this strategy is currently paused (loss limit or SL cooldown)."""
        # ── Day-level SL pause (4+ SLs in window) ──
        if self._sl_paused_rest_of_day:
            if self.last_trade_day != self._today():
                self._sl_paused_rest_of_day = False  # new day
            else:
                return True

        # ── Graduated SL cooldown ──
        if self._sl_cooldown_until > 0:
            if _time.time() < self._sl_cooldown_until:
                return True
            self._sl_cooldown_until = 0.0

        # ── Consecutive-loss pause ──
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

    def record_sl(self, timestamp: float | None = None) -> dict[str, Any]:
        """Record a stop-loss event and apply graduated cooldown.

        Call this when a position is closed by SL (native MT5 or managed).
        Returns a status dict suitable for logging.

        Cooldown tiers (SL_COOLDOWN_TIERS):
          1st SL in window →  60 s
          2nd SL in window → 300 s (5 min)
          3rd SL in window → 1800 s (30 min)
          4th SL in window → rest of day paused
        """
        now = timestamp if timestamp is not None else _time.time()

        # Reset daily state if needed
        _today = self._today()
        if self.last_trade_day != _today:
            self._reset_daily()

        # Clean SL timestamps outside the lookback window
        cutoff = now - SL_COOLDOWN_WINDOW
        self._sl_timestamps = [t for t in self._sl_timestamps if t > cutoff]
        self._sl_timestamps.append(now)

        sl_count = len(self._sl_timestamps)

        # Find the applicable tier
        cooldown_seconds: float = 0.0
        for tier_count, tier_seconds in SL_COOLDOWN_TIERS:
            if sl_count >= tier_count:
                cooldown_seconds = tier_seconds

        if cooldown_seconds >= float("inf") or sl_count >= 4:
            self._sl_paused_rest_of_day = True
            self._sl_cooldown_until = 0.0
            return {
                "event": "sl_cooldown_paused_day",
                "strategy": self.strategy_name,
                "sl_count": sl_count,
                "reason": "4_sl_in_window",
            }

        if cooldown_seconds > 0:
            self._sl_cooldown_until = now + cooldown_seconds
            return {
                "event": "sl_cooldown_applied",
                "strategy": self.strategy_name,
                "sl_count": sl_count,
                "cooldown_seconds": cooldown_seconds,
                "cooldown_until_utc": datetime.fromtimestamp(
                    self._sl_cooldown_until, tz=UTC
                ).isoformat(),
            }

        return {"event": "sl_recorded", "strategy": self.strategy_name, "sl_count": sl_count}

    def get_sl_cooldown_info(self) -> dict[str, Any]:
        """Return current SL cooldown state for logging/debugging."""
        return {
            "strategy": self.strategy_name,
            "sl_count_in_window": len(self._sl_timestamps),
            "sl_cooldown_active": _time.time() < self._sl_cooldown_until,
            "sl_cooldown_remaining_s": max(0.0, self._sl_cooldown_until - _time.time()),
            "sl_paused_rest_of_day": self._sl_paused_rest_of_day,
        }

    @property
    def win_rate_today(self) -> float:
        if self.total_trades_today == 0:
            return 0.0
        return self.total_wins_today / self.total_trades_today

    def get_streak_multiplier(self) -> float:
        """Graduated position-size reduction based on consecutive losses.

        Instead of a binary on/off at max_consecutive_losses, this scales
        position size down smoothly: 0.9^n_losses.  The hard pause still
        fires at max_consecutive_losses as a safety floor.

        Returns:
            Multiplier in [0.0, 1.0] — 1.0 = no reduction, 0.0 = paused.
        """
        if self.paused:
            return 0.0
        if self.consecutive_losses <= 0:
            return 1.0
        mult = round(0.90**self.consecutive_losses, 4)
        return max(0.30, mult)  # floor at 30% — hard pause takes over below that

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "paused": self.paused
            or self._sl_paused_rest_of_day
            or _time.time() < self._sl_cooldown_until,
            "daily_pnl_pct": round(self.daily_pnl_pct, 4),
            "consecutive_losses": self.consecutive_losses,
            "total_trades_today": self.total_trades_today,
            "win_rate_today": round(self.win_rate_today, 4),
            "streak_multiplier": self.get_streak_multiplier(),
            "sl_cooldown": self.get_sl_cooldown_info(),
        }

    # ── Execution state persistence (FIX-20260603-072: Global Execution State Hydration) ──

    def get_state(self) -> dict[str, Any]:
        """Serializable snapshot of all mutable runtime state for restart recovery."""
        _now = _time.time()
        return {
            "daily_pnl_pct": round(self.daily_pnl_pct, 4),
            "consecutive_losses": self.consecutive_losses,
            "total_trades_today": self.total_trades_today,
            "total_wins_today": self.total_wins_today,
            "paused": self.paused,
            "paused_at": self.paused_at,
            "last_trade_day": self.last_trade_day,
            "_sl_timestamps": [t for t in self._sl_timestamps if t > _now - SL_COOLDOWN_WINDOW],
            "_sl_cooldown_until": self._sl_cooldown_until
            if self._sl_cooldown_until > _now
            else 0.0,
            "_sl_paused_rest_of_day": self._sl_paused_rest_of_day,
        }

    def load_state(self, saved: dict[str, Any]) -> None:
        """Restore mutable runtime state from a previously persisted snapshot.

        Only restores fields that exist in *saved* — backward-compatible with
        older snapshot versions that may lack newer fields.
        """
        _now = _time.time()
        if "daily_pnl_pct" in saved:
            self.daily_pnl_pct = float(saved["daily_pnl_pct"])
        if "consecutive_losses" in saved:
            self.consecutive_losses = int(saved["consecutive_losses"])
        if "total_trades_today" in saved:
            self.total_trades_today = int(saved["total_trades_today"])
        if "total_wins_today" in saved:
            self.total_wins_today = int(saved["total_wins_today"])
        if "paused" in saved:
            self.paused = bool(saved["paused"])
        if "paused_at" in saved:
            self.paused_at = float(saved["paused_at"])
        # FIX-20260610-007: Force-unpause on cross-day restore.
        # If the saved state is from a previous day, the daily loss limit
        # has reset — the pause should not survive into the new day.
        if self.paused and self.last_trade_day == "":
            self.paused = False
            self.paused_at = 0.0
        if "last_trade_day" in saved:
            _saved_day = str(saved["last_trade_day"])
            _today = self._today()
            if _saved_day == _today:
                self.last_trade_day = _saved_day
            else:
                # Stale day — counters will reset on next record_trade()
                self.last_trade_day = ""
        if "_sl_timestamps" in saved:
            _cutoff = _now - SL_COOLDOWN_WINDOW
            self._sl_timestamps = [t for t in saved["_sl_timestamps"] if t > _cutoff]
        if "_sl_cooldown_until" in saved:
            _until = float(saved["_sl_cooldown_until"])
            self._sl_cooldown_until = _until if _until > _now else 0.0
        if "_sl_paused_rest_of_day" in saved:
            _paused_day = bool(saved["_sl_paused_rest_of_day"])
            if _paused_day and self.last_trade_day == self._today():
                self._sl_paused_rest_of_day = True
