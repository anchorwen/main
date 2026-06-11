"""BrainPnLStore — per-brain counterfactual P&L ledger.

Tracks what each brain's signal WOULD have earned if traded alone,
independent of the parliament consensus. Enables per-brain Sharpe ratio,
win rate, and drawdown tracking for dynamic weighting and governance.

Fully decoupled from any runtime — works with live, shadow, and backtest.

Usage::

    store = BrainPnLStore(window_size=100)

    # Cycle N: record each brain's directional signal
    for proposal in proposals:
        if proposal.prediction["direction_bias"] != "neutral":
            store.record_signal(
                brain_id=proposal.brain_id,
                symbol="XAUUSDc",
                direction=proposal.prediction["direction_bias"],
                entry_price=mid_price,
                confidence=proposal.prediction.get("confidence", 0.5),
            )

    # Cycle N+1: settle all pending signals with the new close price
    store.settle_all(new_mid_price)

    # Query metrics
    metrics = store.get_metrics("V9_Institutional_01")
    print(metrics.sharpe_ratio, metrics.win_rate)

    # Persist
    store.save("data/brain_pnl_ledger.json")
    store = BrainPnLStore.load("data/brain_pnl_ledger.json")
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class BrainPnLMetrics:
    """Rolling performance metrics for a single brain."""

    brain_id: str = ""
    sample_count: int = 0
    cumulative_pnl: float = 0.0  # total P&L per unit over the window
    win_rate: float = 0.0
    avg_return: float = 0.0  # mean return per signal
    std_return: float = 0.0
    sharpe_ratio: float = 0.0  # annualised (M5: 288 bars/day * 252 days)
    max_drawdown: float = 0.0  # max peak-to-trough in price units
    profit_factor: float = 0.0  # gross profit / gross loss
    recent_pnl_20: float = 0.0  # sum of last 20 settled outcomes
    recent_win_rate: float = 0.0  # win rate over last 20 settled outcomes
    consecutive_losses: int = 0  # trailing consecutive losses
    health_signal: str = "insufficient_data"
    # Breakdowns
    long_win_rate: float = 0.0
    short_win_rate: float = 0.0
    long_count: int = 0
    short_count: int = 0
    # Friction costs
    total_spread_cost: float = 0.0
    total_slippage_cost: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "brain_id": self.brain_id,
            "sample_count": self.sample_count,
            "cumulative_pnl": self.cumulative_pnl,
            "win_rate": self.win_rate,
            "avg_return": self.avg_return,
            "std_return": self.std_return,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown": self.max_drawdown,
            "profit_factor": self.profit_factor,
            "recent_pnl_20": self.recent_pnl_20,
            "recent_win_rate": self.recent_win_rate,
            "consecutive_losses": self.consecutive_losses,
            "health_signal": self.health_signal,
            "long_win_rate": self.long_win_rate,
            "short_win_rate": self.short_win_rate,
            "long_count": self.long_count,
            "short_count": self.short_count,
            "total_spread_cost": self.total_spread_cost,
            "total_slippage_cost": self.total_slippage_cost,
        }


class BrainPnLStore:
    """Counterfactual P&L ledger for per-brain independent accounting.

    Each brain's directional call is recorded at signal time and settled
    when its horizon-matched TTL expires (horizon bars later).  P&L is
    computed *per unit* (not notional) so volume scaling is applied later.

    Per-cycle flow::

        # 1. Update MFE/MAE for all pending signals + decrement TTL
        ready = store.update_pending(mid_price)

        # 2. Settle only expired signals (TTL=0)
        store.settle_all(mid_price)

        # 3. Record new signals with per-brain horizon from registry
        for p in proposals:
            horizon = brain_registry.get_training_horizon(p.brain_id)
            store.record_signal(..., expected_horizon=horizon)
    """

    # Annualisation factor for M5 bars: 288 bars/day * 252 trading days
    ANNUAL_FACTOR = 288 * 252  # 72576

    # ── recording ──────────────────────────────────────────────────────

    def record_signal(
        self,
        brain_id: str,
        symbol: str,
        direction: str,
        entry_price: float,
        *,
        confidence: float = 0.5,
        expected_horizon: int = 1,
        snapshot_id: str = "",
        metadata: dict[str, Any] | None = None,
        entry_spread: float = 0.0,
        entry_slippage: float = 0.0,
    ) -> str | None:
        """Record a brain's directional signal, pending horizon-matched settlement.

        Signals now carry a TTL (expected_horizon) and are only settled when
        TTL reaches 0, matching each brain's training label horizon.

        Returns the signal_id, or None for neutral signals (no P&L to track).
        """
        if direction == "neutral":
            return None

        # ── FIX-20260611-003: Data flywheel — write-time assertions ──
        _entry_price_f = float(entry_price)
        _reject_reason = None
        if _entry_price_f <= 0:
            _reject_reason = f"entry_price={_entry_price_f} <= 0"
        elif confidence < 0 or confidence > 1:
            _reject_reason = f"confidence={confidence} out of [0,1]"
        elif direction not in ("long", "short"):
            _reject_reason = f"direction={direction} not long/short"
        elif not brain_id or not symbol:
            _reject_reason = "missing brain_id or symbol"
        if _reject_reason is not None:
            import logging as _assert_log

            _assert_log.getLogger(__name__).warning(
                "[DATA_ASSERT] PnL record_signal REJECTED: brain=%s reason=%s",
                brain_id,
                _reject_reason,
            )
            return None

        # ── FIX-20260611-005: Cross-brain identity leak defense ──
        # signal_id embeds brain_id — prevents two brains from sharing the
        # same record.  The timestamp component ensures uniqueness per signal.
        _ts = datetime.now(UTC).timestamp()
        signal_id = f"{brain_id}_{_ts:.6f}"

        # Check: does another brain already have a signal at the same timestamp?
        # This detects shadow evaluator brain_id assignment bugs (BTC identity leak).
        for _existing_sid, _existing_sig in self._pending.items():
            if _existing_sig.get("brain_id") != brain_id:
                _existing_ts = _existing_sid.rsplit("_", 1)[-1]
                if (
                    abs(float(_existing_ts) - _ts) < 0.01
                    and _existing_sig.get("entry_price") == _entry_price_f
                ):
                    import logging as _dup_log

                    _dup_log.getLogger(__name__).warning(
                        "[IDENTITY_LEAK] PnL record_signal: brain=%s has same entry_price=%.2f "
                        "as brain=%s at t=%.3f — possible identity leak in shadow evaluator",
                        brain_id,
                        _entry_price_f,
                        _existing_sig["brain_id"],
                        _ts,
                    )

        self._pending[signal_id] = {
            "signal_id": signal_id,
            "brain_id": brain_id,
            "symbol": symbol,
            "direction": direction,
            "entry_price": _entry_price_f,
            "confidence": confidence,
            "expected_horizon": expected_horizon,
            "ttl": expected_horizon,
            "snapshot_id": snapshot_id,
            "entry_time": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "metadata": metadata or {},
            "entry_spread": entry_spread,
            "entry_slippage": entry_slippage,
            # MFE/MAE tracking fields (updated per-cycle via update_pending)
            "mfe_price": _entry_price_f,  # best price in trade direction
            "mae_price": _entry_price_f,  # worst price in trade direction
            # FIX-20260611-005: position linkage for per-position settlement
            "position_ticket": metadata.get("position_ticket", 0) if metadata else 0,
        }

        # ── FIX-20260611-021: Event Sourcing — dual-write signal recorded ──
        if self._event_writer is not None:
            try:
                from core.contracts.events import DataSource, PnLEvent

                _event = PnLEvent(
                    timestamp=datetime.now(UTC),
                    source=DataSource.LIVE,
                    event_type="SignalRecorded",
                    brain_id=brain_id,
                    symbol=symbol,
                    direction=direction,
                    entry_price=_entry_price_f,
                    exit_price=None,  # Not yet settled
                    pnl_r=0.0,  # PnL determined at settlement
                    confidence=confidence,
                    position_ticket=metadata.get("position_ticket") if metadata else None,
                    generated_by="brain_pnl_ledger.record_signal",
                )
                _ = self._event_writer.write(_event)
            except (OSError, ValueError, TypeError):
                pass  # EventWriter failure must never break the hot path

        return signal_id

    def update_pending(self, mid_price: float) -> int:
        """Update MFE/MAE and decrement TTL for all pending signals.

        Called once per cycle BEFORE settle_all.  For each pending signal:
        - Tracks the most favorable price excursion (MFE) in the trade direction.
        - Tracks the most adverse price excursion (MAE) against the trade direction.
        - Decrements TTL by 1.

        Returns the number of signals whose TTL just reached 0 (ready to settle).
        """
        ready_count = 0
        for sig in self._pending.values():
            sig["ttl"] = max(0, int(sig.get("ttl", 0)) - 1)

            direction = sig["direction"]
            mid = float(mid_price)

            # MFE: best price in trade direction (highest for long, lowest for short)
            if direction == "long":
                if mid > float(sig.get("mfe_price", sig["entry_price"])):
                    sig["mfe_price"] = mid
                if mid < float(sig.get("mae_price", sig["entry_price"])):
                    sig["mae_price"] = mid
            else:  # short
                if mid < float(sig.get("mfe_price", sig["entry_price"])):
                    sig["mfe_price"] = mid
                if mid > float(sig.get("mae_price", sig["entry_price"])):
                    sig["mae_price"] = mid

            if sig["ttl"] == 0:
                ready_count += 1

        return ready_count

    def settle_one(
        self,
        signal_id: str,
        close_price: float,
        close_time: str | None = None,
        *,
        spread: float = 0.0,
        slippage: float = 0.0,
        mfe_r: float = 0.0,
        mae_r: float = 0.0,
        mfe_price: float | None = None,
        mae_price: float | None = None,
    ) -> dict[str, Any] | None:
        """Settle a single pending signal.  Returns the outcome or None.

        When mfe_price/mae_price are provided, they override the internally
        tracked values (useful for backfill or manual settlement).
        """
        entry = self._pending.pop(signal_id, None)
        if entry is None:
            return None

        return self._settle(
            entry,
            close_price,
            close_time,
            spread=spread,
            slippage=slippage,
            mfe_r=mfe_r,
            mae_r=mae_r,
            mfe_price=mfe_price,
            mae_price=mae_price,
        )

    def settle_all(
        self,
        close_price: float,
        close_time: str | None = None,
        *,
        spread: float = 0.0,
        slippage: float = 0.0,
        mfe_r: float = 0.0,
        mae_r: float = 0.0,
        force_all: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Settle pending signals whose TTL has expired.

        By default (force_all=False), only signals with ttl <= 0 are settled.
        This matches each brain's training_horizon to the holding period:
        a barrier_12bar brain (horizon=12) settles after 12 bars, not 1.

        Set force_all=True for backward compat (settle everything immediately,
        e.g. shutdown, backtest).

        Returns {brain_id: outcome}.
        """
        results: dict[str, dict[str, Any]] = {}
        for signal_id in list(self._pending.keys()):
            sig = self._pending.get(signal_id)
            if sig is None:
                continue
            ttl = int(sig.get("ttl", 0))
            if not force_all and ttl > 0:
                continue

            outcome = self.settle_one(
                signal_id,
                close_price,
                close_time,
                spread=spread,
                slippage=slippage,
                mfe_r=mfe_r,
                mae_r=mae_r,
                mfe_price=float(sig.get("mfe_price", sig["entry_price"])),
                mae_price=float(sig.get("mae_price", sig["entry_price"])),
            )
            if outcome is not None:
                results[outcome["brain_id"]] = outcome
        return results

    def _settle(
        self,
        entry: dict[str, Any],
        close_price: float,
        close_time: str | None,
        *,
        spread: float = 0.0,
        slippage: float = 0.0,
        mfe_r: float = 0.0,
        mae_r: float = 0.0,
        mfe_price: float | None = None,
        mae_price: float | None = None,
    ) -> dict[str, Any]:
        direction = entry["direction"]
        entry_price = float(entry["entry_price"])
        entry_spread = float(entry.get("entry_spread", 0.0) or 0.0)
        entry_slippage = float(entry.get("entry_slippage", 0.0) or 0.0)

        # Exit at effective price:
        #   LONG:  sell at bid  = mid - spread/2 - slippage
        #   SHORT: buy at ask   = mid + spread/2 + slippage
        half_entry_spread = entry_spread / 2.0 + entry_slippage
        half_exit_friction = spread / 2.0 + slippage

        if direction == "long":
            pnl_per_unit = close_price - entry_price - half_entry_spread - half_exit_friction
        else:
            pnl_per_unit = entry_price - close_price - half_entry_spread - half_exit_friction

        pnl_bps = (pnl_per_unit / entry_price) * 10_000 if entry_price > 0 else 0.0

        # Compute MFE/MAE R-multiples from tracked prices (Track 2).
        # If explicit mfe_r/mae_r are passed (non-zero), they take precedence
        # over the price-derived computation (backward compat / manual override).
        if mfe_r == 0.0 and mae_r == 0.0 and mfe_price is not None and mae_price is not None:
            if entry_price > 0:
                if direction == "long":
                    mfe_r = (float(mfe_price) - entry_price) / entry_price
                    mae_r = (entry_price - float(mae_price)) / entry_price
                else:
                    mfe_r = (entry_price - float(mfe_price)) / entry_price
                    mae_r = (float(mae_price) - entry_price) / entry_price

        outcome = {
            **entry,
            "close_price": close_price,
            "close_time": close_time or datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "pnl_per_unit": round(pnl_per_unit, 6),
            "pnl_bps": round(pnl_bps, 2),
            "is_win": pnl_per_unit > 0,
            "exit_spread": spread,
            "exit_slippage": slippage,
            "total_friction": round(half_entry_spread + half_exit_friction, 6),
            "mfe_r": round(mfe_r, 6),
            "mae_r": round(mae_r, 6),
        }

        brain_id = entry["brain_id"]
        if brain_id not in self._settled:
            self._settled[brain_id] = []
        self._settled[brain_id].append(outcome)

        # ── O(1) accumulator update (Phase B: PnL alert rules) ──
        self._update_accumulators(outcome, brain_id)

        # ── FIX-20260611-021: Event Sourcing — dual-write to event stream ──
        if self._event_writer is not None:
            try:
                from core.contracts.events import DataSource, PnLEvent

                _settled_at = (
                    outcome.get("close_time") or datetime.now(UTC).replace(tzinfo=None).isoformat()
                )
                _event = PnLEvent(
                    timestamp=datetime.now(UTC),
                    source=DataSource.LIVE,
                    event_type="SignalSettled",
                    brain_id=brain_id,
                    symbol=entry.get("symbol", ""),
                    direction=direction,
                    entry_price=entry_price,
                    exit_price=close_price,
                    pnl_r=round(pnl_per_unit / (entry_price * 0.01) if entry_price > 0 else 0.0, 4),
                    confidence=float(entry.get("confidence", 0.5)),
                    position_ticket=entry.get("position_ticket"),
                    generated_by="brain_pnl_ledger._settle",
                )
                _ = self._event_writer.write(_event)
            except (OSError, ValueError, TypeError):
                pass  # EventWriter failure must never break the hot path

        # Keep only the most recent window_size outcomes
        if len(self._settled[brain_id]) > self._window_size:
            popped = self._settled[brain_id].pop(0)
            if popped.get("is_win"):
                self._running_win_count = max(0, self._running_win_count - 1)
            self._running_trade_count = max(0, self._running_trade_count - 1)

        return outcome

    # ── O(1) accumulators (Phase B) ─────────────────────────────────────

    def _update_accumulators(self, outcome: dict[str, Any], brain_id: str) -> None:
        """Update O(1) running stats after each settled trade.

        Called from _settle() on every new closed trade.  Midnight reset
        re-zeros daily accumulators when the close date changes.
        """
        close_time = outcome.get("close_time", "")
        trade_date = close_time[:10]  # "YYYY-MM-DD"
        is_win = bool(outcome.get("is_win", False))
        pnl = float(outcome.get("pnl_per_unit", 0.0))

        # Midnight reset: new trading day
        if trade_date and trade_date != self._current_date:
            self._running_daily_pnl = 0.0
            self._current_date = trade_date

        self._running_daily_pnl += pnl
        if is_win:
            self._running_consecutive_losses = 0
            self._running_win_count += 1
        else:
            self._running_consecutive_losses += 1
        self._running_trade_count += 1

    def get_quick_stats(self) -> dict[str, Any]:
        """O(1) access to pre-computed accumulators for alert rules.

        Returns a dict suitable for direct injection into the alert context.
        No disk I/O, no iteration over _settled (护栏3 compliance).
        """
        trades = max(1, self._running_trade_count)
        return {
            "daily_pnl_usd": round(self._running_daily_pnl, 2),
            "consecutive_losses": self._running_consecutive_losses,
            "rolling_win_rate": round(self._running_win_count / trades, 4),
            "total_trades_window": self._running_trade_count,
        }

    # ── metrics ────────────────────────────────────────────────────────

    def get_metrics(self, brain_id: str, window: int | None = None) -> BrainPnLMetrics:
        """Compute rolling performance metrics for a brain.

        Delegates to get_metrics_calibrated() for unified health assessment.
        """
        return self.get_metrics_calibrated(brain_id, window)

    def get_all_metrics(self) -> dict[str, BrainPnLMetrics]:
        """Return metrics for every tracked brain."""
        return {bid: self.get_metrics(bid) for bid in self._settled}

    def get_summary_table(self) -> list[dict[str, Any]]:
        """Return a list-of-dicts summary suitable for JSON logging."""
        return [
            m.to_dict()
            for m in sorted(
                self.get_all_metrics().values(),
                key=lambda m: m.sharpe_ratio,
                reverse=True,
            )
        ]

    # ── health assessment ──────────────────────────────────────────────

    # Fixed fallback thresholds (used when sample < 30 cross-brain)
    FIXED_THRESHOLDS: dict[str, dict[str, float]] = {
        "critical": {"sharpe": -1.0, "win_rate": 0.30},
        "degraded": {"sharpe": -0.5, "win_rate": 0.40},
        "warning": {"sharpe": 0.0, "win_rate": 0.48, "max_dd": 3.0},
        "healthy": {"sharpe": 1.0, "win_rate": 0.55},
    }

    # Phase B O(1) accumulator type declarations (forward ref for methods before __init__)
    _running_daily_pnl: float
    _running_consecutive_losses: int
    _running_win_count: int
    _running_trade_count: int
    _current_date: str

    def __init__(self, window_size: int = 100, event_writer: Any = None) -> None:
        self._window_size = window_size
        self._pending: dict[str, dict[str, Any]] = {}
        self._settled: dict[str, list[dict[str, Any]]] = {}
        # Rolling quantile thresholds (recalibrated from cross-brain distribution)
        self._calibrated_thresholds: dict[str, dict[str, float]] | None = None
        self._last_calibration_n: int = 0
        # ── Phase B O(1) event-driven accumulators (护栏3 + 提升1) ──
        self._running_daily_pnl = 0.0
        self._running_consecutive_losses = 0
        self._running_win_count = 0
        self._running_trade_count = 0
        self._current_date = ""
        # ── FIX-20260611-021: Event Sourcing — optional dual-write hook ──
        self._event_writer: Any = event_writer  # EventWriter | None

    @staticmethod
    def _assess_health(
        n: int,
        sharpe: float,
        win_rate: float,
        max_dd: float,
    ) -> str:
        # DEPRECATED (2026-05-12): New code should use BrainQualityEngine.assess()
        # from core.feedback.brain_quality_engine.  Kept for internal get_metrics()
        # backward compat — do NOT add new callers.
        if n < 10:
            return "insufficient_data"
        if sharpe < -1.0 or win_rate < 0.30:
            return "critical"
        if sharpe < -0.5 or win_rate < 0.40:
            return "degraded"
        if sharpe < 0.0 or win_rate < 0.48 or max_dd > 3.0:
            return "warning"
        if sharpe >= 1.0 and win_rate >= 0.55:
            return "healthy"
        return "stable"

    def calibrate_thresholds(self, min_samples: int = 30) -> dict[str, dict[str, float]]:
        """Compute percentile-based health thresholds from cross-brain distribution.

        Uses the bottom 20% of Sharpe/win_rate as "critical", bottom 40% as
        "degraded", top 30% as "healthy".  Falls back to fixed thresholds
        when total settled samples < min_samples.
        """
        all_metrics = self.get_all_metrics()
        sharpes: list[float] = []
        win_rates: list[float] = []
        for m in all_metrics.values():
            if m.sample_count >= 10:
                sharpes.append(m.sharpe_ratio)
                win_rates.append(m.win_rate)

        total_samples = sum(m.sample_count for m in all_metrics.values())
        if len(sharpes) < 3 or total_samples < min_samples:
            self._calibrated_thresholds = None
            self._last_calibration_n = total_samples
            return dict(self.FIXED_THRESHOLDS)

        import numpy as np

        s = np.array(sharpes)
        w = np.array(win_rates)

        calibrated = {
            "critical": {
                "sharpe": round(float(np.percentile(s, 20)), 2),
                "win_rate": round(float(np.percentile(w, 20)), 4),
            },
            "degraded": {
                "sharpe": round(float(np.percentile(s, 40)), 2),
                "win_rate": round(float(np.percentile(w, 40)), 4),
            },
            "warning": {
                "sharpe": round(float(np.percentile(s, 60)), 2),
                "win_rate": round(float(np.percentile(w, 60)), 4),
                "max_dd": self.FIXED_THRESHOLDS["warning"]["max_dd"],
            },
            "healthy": {
                "sharpe": round(float(np.percentile(s, 70)), 2),
                "win_rate": round(float(np.percentile(w, 70)), 4),
            },
            "_meta": {
                "n_brains": len(sharpes),
                "total_samples": total_samples,
                "sharpe_median": round(float(np.median(s)), 2),
                "win_rate_median": round(float(np.median(w)), 4),
            },
        }

        self._calibrated_thresholds = calibrated
        self._last_calibration_n = total_samples
        return calibrated

    def assess_health_calibrated(
        self,
        brain_id: str,
        n: int,
        sharpe: float,
        win_rate: float,
        max_dd: float,
    ) -> str:
        """Assess health using calibrated thresholds when available, else fixed."""
        thresholds = (
            self._calibrated_thresholds
            if self._calibrated_thresholds is not None
            else dict(self.FIXED_THRESHOLDS)
        )

        if n < 10:
            return "insufficient_data"

        crit = thresholds.get("critical", self.FIXED_THRESHOLDS["critical"])
        degr = thresholds.get("degraded", self.FIXED_THRESHOLDS["degraded"])
        warn = thresholds.get("warning", self.FIXED_THRESHOLDS["warning"])
        heal = thresholds.get("healthy", self.FIXED_THRESHOLDS["healthy"])

        if sharpe < crit["sharpe"] or win_rate < crit["win_rate"]:
            return "critical"
        if sharpe < degr["sharpe"] or win_rate < degr["win_rate"]:
            return "degraded"
        if (
            sharpe < warn["sharpe"]
            or win_rate < warn["win_rate"]
            or max_dd > warn.get("max_dd", 3.0)
        ):
            return "warning"
        if sharpe >= heal["sharpe"] and win_rate >= heal["win_rate"]:
            return "healthy"
        return "stable"

    def get_metrics_calibrated(self, brain_id: str, window: int | None = None) -> BrainPnLMetrics:
        """Same as get_metrics() but uses calibrated health thresholds."""
        outcomes = self._settled.get(brain_id, [])
        if window is not None:
            outcomes = outcomes[-window:]

        n = len(outcomes)
        if n < 5:
            pnls = [o["pnl_per_unit"] for o in outcomes]
            cumulative = sum(pnls)
            win_count = sum(1 for o in outcomes if o["is_win"])
            return BrainPnLMetrics(
                brain_id=brain_id,
                sample_count=n,
                cumulative_pnl=round(cumulative, 6),
                win_rate=round(win_count / n, 4) if n > 0 else 0.0,
                long_count=sum(1 for o in outcomes if o["direction"] == "long"),
                short_count=sum(1 for o in outcomes if o["direction"] == "short"),
            )

        pnls = [o["pnl_per_unit"] for o in outcomes]
        cumulative = sum(pnls)
        win_count = sum(1 for o in outcomes if o["is_win"])
        win_rate = win_count / n
        avg_return = cumulative / n

        variance = sum((p - avg_return) ** 2 for p in pnls) / max(1, n - 1)
        std_return = math.sqrt(variance)

        if std_return > 1e-12:
            sharpe = (avg_return / std_return) * math.sqrt(self.ANNUAL_FACTOR)
        else:
            sharpe = 0.0

        cumsum = 0.0
        peak = -1e18
        max_dd = 0.0
        for p in pnls:
            cumsum += p
            if cumsum > peak:
                peak = cumsum
            dd = peak - cumsum
            if dd > max_dd:
                max_dd = dd

        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 1e-12 else float("inf")

        recent_20 = sum(pnls[-20:]) if n >= 20 else cumulative

        # Recent win rate: last 20 settled outcomes
        recent_n = min(20, n)
        recent_wins = sum(1 for o in outcomes[-recent_n:] if o["is_win"])
        recent_wr = recent_wins / recent_n if recent_n > 0 else win_rate

        # Consecutive losses (trailing)
        consecutive_losses = 0
        for o in reversed(outcomes):
            if not o["is_win"]:
                consecutive_losses += 1
            else:
                break

        long_outs = [o for o in outcomes if o["direction"] == "long"]
        short_outs = [o for o in outcomes if o["direction"] == "short"]
        long_wr = sum(1 for o in long_outs if o["is_win"]) / max(1, len(long_outs))
        short_wr = sum(1 for o in short_outs if o["is_win"]) / max(1, len(short_outs))

        health = self.assess_health_calibrated(brain_id, n, sharpe, win_rate, max_dd)

        # Friction cost aggregation
        total_spread = sum(
            float(o.get("entry_spread", 0) or 0) + float(o.get("exit_spread", 0) or 0)
            for o in outcomes
        )
        total_slippage = sum(
            float(o.get("entry_slippage", 0) or 0) + float(o.get("exit_slippage", 0) or 0)
            for o in outcomes
        )

        return BrainPnLMetrics(
            brain_id=brain_id,
            sample_count=n,
            cumulative_pnl=round(cumulative, 6),
            win_rate=round(win_rate, 4),
            avg_return=round(avg_return, 6),
            std_return=round(std_return, 6),
            sharpe_ratio=round(sharpe, 2),
            max_drawdown=round(max_dd, 6),
            profit_factor=round(profit_factor, 2) if profit_factor != float("inf") else 999.0,
            recent_pnl_20=round(recent_20, 6),
            recent_win_rate=round(recent_wr, 4),
            consecutive_losses=consecutive_losses,
            health_signal=health,
            long_win_rate=round(long_wr, 4),
            short_win_rate=round(short_wr, 4),
            long_count=len(long_outs),
            short_count=len(short_outs),
            total_spread_cost=round(total_spread, 4),
            total_slippage_cost=round(total_slippage, 4),
        )

    # ── persistence ────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "brain_pnl_ledger.v1",
            "window_size": self._window_size,
            "pending": self._pending,
            "settled": self._settled,
        }

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, p)
        return p

    @classmethod
    def load(cls, path: str | Path) -> BrainPnLStore:
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        store = cls(window_size=data.get("window_size", 100))
        store._pending = data.get("pending", {})
        store._settled = data.get("settled", {})
        # ── FIX-20260603-065 P1: hydrate in-memory accumulators from disk ──
        store._hydrate_accumulators()
        return store

    def _hydrate_accumulators(self) -> None:
        """Rebuild in-memory PnL accumulators from settled disk data.

        FIX-20260603-065: _running_daily_pnl and related counters were only
        updated by settle_one().  After a restart (or manual ledger rebuild),
        the in-memory state was empty while the disk had data → alerts showed
        wrong daily PnL.  Now load() calls this to sync memory ← disk.
        """
        from datetime import UTC, datetime

        _today = datetime.now(UTC).date()
        self._running_daily_pnl = 0.0
        self._running_consecutive_losses = 0
        self._running_win_count = 0
        self._running_trade_count = 0

        for _bid, _trades in self._settled.items():
            for _t in _trades:
                _pnl = _t.get("pnl", 0) or 0.0
                _ts_str = _t.get("settled_at", "") or _t.get("entry_time", "")
                try:
                    _ts = datetime.fromisoformat(_ts_str)
                    if _ts.date() == _today:
                        self._running_daily_pnl += _pnl
                except (ValueError, TypeError, OSError):
                    pass
                self._running_trade_count += 1
                if _pnl > 0:
                    self._running_win_count += 1
                    self._running_consecutive_losses = 0
                elif _pnl < 0:
                    self._running_consecutive_losses += 1

    def retention_prune(self, retention_days: int = 90) -> dict[str, int]:
        """Remove settled entries older than *retention_days*.

        Returns {brain_id: pruned_count} for audit logging.
        Entries without a parsable entry_time are retained (conservative).
        """
        from datetime import UTC, datetime, timedelta

        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        pruned: dict[str, int] = {}
        for bid in list(self._settled.keys()):
            entries = self._settled[bid]
            keep = []
            removed = 0
            for e in entries:
                ts_str = e.get("entry_time", "")
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts < cutoff:
                        removed += 1
                        continue
                except (ValueError, TypeError):
                    pass  # unparsable timestamp → keep
                keep.append(e)
            if removed > 0:
                self._settled[bid] = keep
                pruned[bid] = removed
        return pruned

    # ── properties ─────────────────────────────────────────────────────

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def brain_ids(self) -> list[str]:
        return sorted(self._settled.keys())

    @property
    def total_settled(self) -> int:
        return sum(len(v) for v in self._settled.values())
