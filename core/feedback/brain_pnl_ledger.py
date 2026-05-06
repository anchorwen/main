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
    health_signal: str = "insufficient_data"
    # Breakdowns
    long_win_rate: float = 0.0
    short_win_rate: float = 0.0
    long_count: int = 0
    short_count: int = 0

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
            "health_signal": self.health_signal,
            "long_win_rate": self.long_win_rate,
            "short_win_rate": self.short_win_rate,
            "long_count": self.long_count,
            "short_count": self.short_count,
        }


class BrainPnLStore:
    """Counterfactual P&L ledger for per-brain independent accounting.

    Each brain's directional call is recorded at signal time and settled
    when the next bar's close price arrives.  P&L is computed *per unit*
    (not notional) so volume scaling is applied later.
    """

    # Annualisation factor for M5 bars: 288 bars/day * 252 trading days
    ANNUAL_FACTOR = 288 * 252  # 72576

    def __init__(self, window_size: int = 100) -> None:
        self._window_size = window_size
        self._pending: dict[str, dict[str, Any]] = {}  # signal_id → entry
        self._settled: dict[str, list[dict[str, Any]]] = {}  # brain_id → list[outcome]

    # ── recording ──────────────────────────────────────────────────────

    def record_signal(
        self,
        brain_id: str,
        symbol: str,
        direction: str,
        entry_price: float,
        *,
        confidence: float = 0.5,
        snapshot_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Record a brain's directional signal, pending next-bar settlement.

        Returns the signal_id, or None for neutral signals (no P&L to track).
        """
        if direction == "neutral":
            return None

        signal_id = f"{brain_id}_{datetime.now(UTC).timestamp():.6f}"
        self._pending[signal_id] = {
            "signal_id": signal_id,
            "brain_id": brain_id,
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "confidence": confidence,
            "snapshot_id": snapshot_id,
            "entry_time": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "metadata": metadata or {},
        }
        return signal_id

    def settle_one(
        self,
        signal_id: str,
        close_price: float,
        close_time: str | None = None,
    ) -> dict[str, Any] | None:
        """Settle a single pending signal.  Returns the outcome or None."""
        entry = self._pending.pop(signal_id, None)
        if entry is None:
            return None

        return self._settle(entry, close_price, close_time)

    def settle_all(
        self,
        close_price: float,
        close_time: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Settle every pending signal at once.  Returns {brain_id: outcome}."""
        results: dict[str, dict[str, Any]] = {}
        for signal_id in list(self._pending.keys()):
            outcome = self.settle_one(signal_id, close_price, close_time)
            if outcome is not None:
                results[outcome["brain_id"]] = outcome
        return results

    def _settle(
        self,
        entry: dict[str, Any],
        close_price: float,
        close_time: str | None,
    ) -> dict[str, Any]:
        direction = entry["direction"]
        entry_price = entry["entry_price"]

        if direction == "long":
            pnl_per_unit = close_price - entry_price
        else:
            pnl_per_unit = entry_price - close_price

        pnl_bps = (pnl_per_unit / entry_price) * 10_000 if entry_price > 0 else 0.0

        outcome = {
            **entry,
            "close_price": close_price,
            "close_time": close_time or datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "pnl_per_unit": round(pnl_per_unit, 6),
            "pnl_bps": round(pnl_bps, 2),
            "is_win": pnl_per_unit > 0,
        }

        brain_id = entry["brain_id"]
        if brain_id not in self._settled:
            self._settled[brain_id] = []
        self._settled[brain_id].append(outcome)

        # Keep only the most recent window_size outcomes
        if len(self._settled[brain_id]) > self._window_size:
            self._settled[brain_id] = self._settled[brain_id][-self._window_size :]

        return outcome

    # ── metrics ────────────────────────────────────────────────────────

    def get_metrics(self, brain_id: str, window: int | None = None) -> BrainPnLMetrics:
        """Compute rolling performance metrics for a brain."""
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

        # Unbiased std (ddof=1) for small samples
        variance = sum((p - avg_return) ** 2 for p in pnls) / max(1, n - 1)
        std_return = math.sqrt(variance)

        # Annualised Sharpe
        if std_return > 1e-12:
            sharpe = (avg_return / std_return) * math.sqrt(self.ANNUAL_FACTOR)
        else:
            sharpe = 0.0

        # Max drawdown from cumulative P&L
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

        # Profit factor
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 1e-12 else float("inf")

        # Recent P&L (last 20)
        recent_20 = sum(pnls[-20:]) if n >= 20 else cumulative

        # Directional breakdown
        long_outs = [o for o in outcomes if o["direction"] == "long"]
        short_outs = [o for o in outcomes if o["direction"] == "short"]
        long_wr = sum(1 for o in long_outs if o["is_win"]) / max(1, len(long_outs))
        short_wr = sum(1 for o in short_outs if o["is_win"]) / max(1, len(short_outs))

        health = self._assess_health(n, sharpe, win_rate, max_dd)

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
            health_signal=health,
            long_win_rate=round(long_wr, 4),
            short_win_rate=round(short_wr, 4),
            long_count=len(long_outs),
            short_count=len(short_outs),
        )

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

    @staticmethod
    def _assess_health(
        n: int,
        sharpe: float,
        win_rate: float,
        max_dd: float,
    ) -> str:
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
        p.write_text(data, encoding="utf-8")
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
        return store

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
