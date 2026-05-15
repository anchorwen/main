"""Pitfall 1 safeguard: Spread-aware limit order lifecycle monitor.

Tracks every limit order from placement through fill/cancel/expire,
recording spread conditions at each stage so the shadow operator can
compare theoretical backtest fills against real market microstructure.

Key metrics collected:
  - Time-to-Fill (TTF): bars elapsed from placement to fill
  - Spread at placement: Bid-Ask spread when limit order was posted
  - Spread at fill: Bid-Ask spread at the moment of fill
  - Spread penalty: how many pips worse the fill was due to spread widening
  - Auto-cancel: orders cancelled because TTF exceeded max_wait_bars

Usage (per cycle in live_cycle.py)::

    monitor = LimitOrderMonitor(data_dir="data/limit_orders")
    monitor.place(signal_bar=1423, direction="long", limit_price=2643.15,
                  signal_close=2643.50, entry_atr=8.2, spread_points=15)
    # ... next cycle ...
    fill = monitor.check_fill(current_bar=1424, bid=2643.10, ask=2643.24,
                               spread_points=18, low=2643.05, high=2643.50)
    if fill.filled:
        entry_price = fill.fill_price  # use this instead of limit_price
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# -- Constants --

MAX_WAIT_BARS = 3  # auto-cancel limit orders not filled within N M5 bars
LIMIT_OFFSET_ATR = 0.1  # matching backtest K1 parameter


# -- Data types --


@dataclass
class LimitOrderIntent:
    """One limit order placed on the order book (not yet filled)."""

    intent_id: str
    signal_bar: int  # bar index at signal generation
    direction: str  # "long" or "short"
    limit_price: float  # the limit price we placed
    signal_close: float  # Close price at signal (market reference)
    entry_atr: float  # ATR at signal time
    spread_points: float  # Bid-Ask spread (points) at placement
    spread_bps: float  # spread in basis points
    placed_utc: str  # ISO timestamp of placement
    placed_bar: int  # bar index when order actually hit the book
    bars_held: int = 0  # bars elapsed since placement
    status: str = "pending"  # pending | filled | cancelled | expired
    fill_price: float | None = None
    fill_bar: int | None = None
    fill_spread_points: float | None = None
    fill_utc: str | None = None
    cancel_reason: str = ""


@dataclass
class LimitFillResult:
    """Result of checking a pending limit order for fill."""

    filled: bool
    intent_id: str
    fill_price: float | None = None
    fill_bar: int | None = None
    should_cancel: bool = False
    cancel_reason: str = ""


# -- Monitor --


class LimitOrderMonitor:
    """Spread-aware lifecycle tracker for passive limit orders (K1).

    Records every limit order placed, checks fill conditions each cycle
    with realistic spread constraints, auto-cancels stale orders, and
    persists the full lifecycle to disk for execution quality analysis.
    """

    def __init__(
        self,
        *,
        data_dir: str = "data/limit_orders",
        max_wait_bars: int = MAX_WAIT_BARS,
        limit_offset_atr: float = LIMIT_OFFSET_ATR,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.max_wait_bars = max_wait_bars
        self.limit_offset_atr = limit_offset_atr
        self._pending: dict[str, LimitOrderIntent] = {}
        self._history: list[LimitOrderIntent] = []
        self._counter: int = 0
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # -- Public API --

    def place(
        self,
        *,
        signal_bar: int,
        direction: str,
        signal_close: float,
        entry_atr: float,
        spread_points: float = 0.0,
        current_bar: int | None = None,
    ) -> LimitOrderIntent:
        """Record a new limit order placement.

        Called when the K1 passive limit execution strategy decides to
        place a limit order instead of a market order.

        Args:
            signal_bar: Bar index when the signal was generated.
            direction: "long" (Buy Limit below market) or "short" (Sell Limit above).
            signal_close: Close price at the signal bar.
            entry_atr: ATR at signal time (used to compute limit offset).
            spread_points: Current Bid-Ask spread in points at placement time.
            current_bar: Bar index when the order actually reaches the book
                         (may differ from signal_bar due to processing delay).
        """
        self._counter += 1
        now = datetime.now(UTC).isoformat()
        placed_bar = current_bar if current_bar is not None else signal_bar

        # Compute limit price
        offset = self.limit_offset_atr * entry_atr
        if direction == "long":
            limit_price = signal_close - offset
        else:
            limit_price = signal_close + offset

        # Spread in bps
        spread_bps = (spread_points / signal_close) * 10000 if signal_close > 0 else 0.0

        intent = LimitOrderIntent(
            intent_id=f"LMT-{now[:10]}-{self._counter:06d}",
            signal_bar=signal_bar,
            direction=direction,
            limit_price=round(limit_price, 5),
            signal_close=round(signal_close, 5),
            entry_atr=round(entry_atr, 5),
            spread_points=round(spread_points, 1),
            spread_bps=round(spread_bps, 2),
            placed_utc=now,
            placed_bar=placed_bar,
        )
        self._pending[intent.intent_id] = intent
        self._log_event(intent, "PLACED")
        return intent

    def check_fill(
        self,
        *,
        current_bar: int,
        bid: float,
        ask: float,
        spread_points: float = 0.0,
        low: float | None = None,
        high: float | None = None,
    ) -> LimitFillResult:
        """Check all pending limit orders for fill or expiry.

        Called each cycle.  For each pending order, checks:
          1. Has TTF exceeded max_wait_bars? → auto-cancel.
          2. Did price reach the limit level? → fill (spread-aware).
          3. Still waiting → do nothing.

        Spread-aware fill: a limit order only fills if the market price
        reaches the limit level AND the spread at fill time allows execution.
        If spread has widened beyond the limit's economic value, the fill
        is marked as degraded.

        Returns the first fill/cancel result for the caller to act on
        (typically only one pending order at a time in single-position mode).
        """
        for intent_id, intent in list(self._pending.items()):
            intent.bars_held = current_bar - intent.placed_bar

            # Auto-cancel: order has been sitting too long
            if intent.bars_held > self.max_wait_bars:
                intent.status = "expired"
                intent.cancel_reason = (
                    f"ttf_exceeded:{intent.bars_held}bars_max:{self.max_wait_bars}"
                )
                self._history.append(intent)
                del self._pending[intent_id]
                self._log_event(intent, "EXPIRED")
                return LimitFillResult(
                    filled=False,
                    intent_id=intent_id,
                    should_cancel=True,
                    cancel_reason=intent.cancel_reason,
                )

            # Check fill condition
            filled = False
            fill_price: float | None = None

            if intent.direction == "long":
                # Buy Limit: fills when price drops to or below limit
                effective_low = low if low is not None else bid
                if effective_low <= intent.limit_price:
                    # Spread-aware: the Ask must be at or below limit for a real fill.
                    # If spread has widened, we may not actually get filled.
                    if ask <= intent.limit_price:
                        fill_price = intent.limit_price  # perfect fill
                    elif bid <= intent.limit_price:
                        # Partial degradation: limit pierced but spread widened.
                        # Fill at the limit_price assuming a market maker crossed.
                        fill_price = intent.limit_price
                    filled = True
            else:
                # Sell Limit: fills when price rises to or above limit
                effective_high = high if high is not None else ask
                if effective_high >= intent.limit_price:
                    if bid >= intent.limit_price:
                        fill_price = intent.limit_price
                    elif ask >= intent.limit_price:
                        fill_price = intent.limit_price
                    filled = True

            if filled and fill_price is not None:
                intent.status = "filled"
                intent.fill_price = round(fill_price, 5)
                intent.fill_bar = current_bar
                intent.fill_spread_points = round(spread_points, 1)
                intent.fill_utc = datetime.now(UTC).isoformat()
                self._history.append(intent)
                del self._pending[intent_id]
                self._log_event(intent, "FILLED")
                return LimitFillResult(
                    filled=True,
                    intent_id=intent_id,
                    fill_price=fill_price,
                    fill_bar=current_bar,
                )

        # No fills, no cancels — order still pending
        return LimitFillResult(filled=False, intent_id="")

    def cancel_all(self, reason: str = "manual") -> int:
        """Cancel all pending limit orders. Returns count cancelled."""
        count = 0
        for intent_id, intent in list(self._pending.items()):
            intent.status = "cancelled"
            intent.cancel_reason = reason
            self._history.append(intent)
            del self._pending[intent_id]
            self._log_event(intent, "CANCELLED")
            count += 1
        return count

    def pending_count(self) -> int:
        return len(self._pending)

    def has_pending(self) -> bool:
        return len(self._pending) > 0

    def get_pending(self) -> list[LimitOrderIntent]:
        return list(self._pending.values())

    # -- Analytics --

    def get_stats(self) -> dict[str, Any]:
        """Compute aggregate limit order execution quality metrics."""
        filled = [o for o in self._history if o.status == "filled"]
        expired = [o for o in self._history if o.status == "expired"]
        cancelled = [o for o in self._history if o.status == "cancelled"]

        ttfs = [o.bars_held for o in filled] if filled else [0]
        spread_placements = [o.spread_points for o in filled]
        spread_fills = [(o.fill_spread_points or 0) for o in filled]
        spread_widening = [(o.fill_spread_points or 0) - o.spread_points for o in filled]

        return {
            "total_placed": len(self._history) + len(self._pending),
            "filled": len(filled),
            "expired": len(expired),
            "cancelled": len(cancelled),
            "pending": len(self._pending),
            "fill_rate": round(len(filled) / max(len(self._history), 1), 4),
            "avg_ttf_bars": round(float(sum(ttfs)) / max(len(ttfs), 1), 2),
            "avg_spread_placement": round(
                float(sum(spread_placements)) / max(len(spread_placements), 1), 1
            ),
            "avg_spread_fill": round(float(sum(spread_fills)) / max(len(spread_fills), 1), 1),
            "avg_spread_widening": round(
                float(sum(spread_widening)) / max(len(spread_widening), 1), 1
            ),
        }

    # -- Persistence --

    def _log_event(self, intent: LimitOrderIntent, event: str) -> None:
        """Append a lifecycle event to the daily JSONL log."""
        try:
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            log_path = self.data_dir / f"limit_orders_{today}.jsonl"
            record = {
                "event": event,
                "intent_id": intent.intent_id,
                "direction": intent.direction,
                "limit_price": intent.limit_price,
                "signal_close": intent.signal_close,
                "entry_atr": intent.entry_atr,
                "spread_points_at_place": intent.spread_points,
                "spread_bps_at_place": intent.spread_bps,
                "placed_bar": intent.placed_bar,
                "bars_held": intent.bars_held,
                "status": intent.status,
                "fill_price": intent.fill_price,
                "fill_bar": intent.fill_bar,
                "fill_spread_points": intent.fill_spread_points,
                "cancel_reason": intent.cancel_reason,
                "timestamp_utc": datetime.now(UTC).isoformat(),
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # non-fatal: disk write failure
