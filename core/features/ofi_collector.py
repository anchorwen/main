"""OFI Lite — Order Flow Imbalance from MT5 symbol_info_tick.

FIX-20260616-097: Tick Rule-based buy/sell volume inference without
Level 2 market book.  Runs as a lightweight accumulator — thread-safe,
zero external dependencies, graceful degradation on failure.

Algorithm (IC Approved):
    ΔP = current_price - prev_price
    ΔP > 0  → +volume (active buying)
    ΔP < 0  → -volume (active selling)
    ΔP = 0:
        current == ask  → +volume
        current == bid  → -volume
        else            → same direction as previous tick

Per M5 bar:
    OFI_M5 = buy_volume - sell_volume
    OFI_ZScore_20 = rolling z-score over last 20 bars
    OFI_Cumulative_1H = sum over last 12 bars (1 hour)

Usage (in bridge worker or feature computer):
    collector = OFICollector()
    collector.on_tick(price=65800, bid=65795, ask=65805, volume=0.5)
    # At M5 bar close:
    ofi = collector.settle_m5_bar()
    # → {"OFI_M5": 3.2, "OFI_ZScore_20": 1.15, "OFI_Cumulative_1H": 12.8}
"""

from __future__ import annotations

import threading
from collections import deque

import numpy as np


class OFICollector:
    """Tick-rule order flow imbalance accumulator.

    Thread-safe.  Call on_tick() for each MT5 tick, settle_m5_bar()
    at each 5-minute boundary to get the aggregated OFI features.
    """

    def __init__(self, window: int = 20, cumulative_bars: int = 12) -> None:
        self._lock = threading.Lock()
        self._window = window
        self._cumulative_bars = cumulative_bars

        # Per-bar accumulators
        self._buy_volume: float = 0.0
        self._sell_volume: float = 0.0
        self._tick_count: int = 0

        # Rolling history
        self._ofi_history: deque[float] = deque(maxlen=window)
        self._total_volume_history: deque[float] = deque(maxlen=window)

        # Previous tick state
        self._prev_price: float | None = None
        self._prev_dir: int = 0  # +1 buy, -1 sell, 0 unknown

    def on_tick(
        self,
        price: float,
        bid: float,
        ask: float,
        volume: float = 0.0,
    ) -> None:
        """Process one tick from MT5 symbol_info_tick.

        Args:
            price: Current last price.
            bid: Current best bid.
            ask: Current best ask.
            volume: Tick volume (if available, else use 1.0 as unit).
        """
        if volume <= 0:
            volume = 1.0  # Unit counting mode — each tick is one event

        with self._lock:
            if self._prev_price is None:
                self._prev_price = price
                return  # First tick — just set baseline

            delta = price - self._prev_price

            if delta > 0:
                direction = 1  # Buy
            elif delta < 0:
                direction = -1  # Sell
            else:
                # Zero delta — use bid/ask proximity
                if abs(price - ask) < abs(price - bid):
                    direction = 1  # Closer to ask → buy
                elif abs(price - bid) < abs(price - ask):
                    direction = -1  # Closer to bid → sell
                else:
                    direction = self._prev_dir  # Same as last tick

            if direction > 0:
                self._buy_volume += volume
            else:
                self._sell_volume += volume

            self._tick_count += 1
            self._prev_price = price
            self._prev_dir = direction

    def settle_m5_bar(self) -> dict[str, float]:
        """Close current M5 bar and return OFI features.

        Returns dict with keys: OFI_M5, OFI_ZScore_20, OFI_Cumulative_1H,
        OFI_Tick_Count, OFI_Total_Volume.
        Call this at each 5-minute boundary, then a new bar begins.
        """
        with self._lock:
            buy = self._buy_volume
            sell = self._sell_volume
            ticks = self._tick_count

            # Reset for next bar
            self._buy_volume = 0.0
            self._sell_volume = 0.0
            self._tick_count = 0

        ofi = buy - sell
        total_vol = buy + sell

        self._ofi_history.append(ofi)
        self._total_volume_history.append(total_vol)

        # Z-Score over rolling window
        if len(self._ofi_history) >= 4:
            arr = np.array(list(self._ofi_history), dtype=np.float64)
            mean = float(np.mean(arr))
            std = float(np.std(arr))
            zscore = float((ofi - mean) / std) if std > 1e-10 else 0.0
        else:
            zscore = 0.0

        # Cumulative 1H (last 12 bars)
        recent = list(self._ofi_history)[-self._cumulative_bars:]
        cumulative = float(sum(recent))

        return {
            "OFI_M5": round(ofi, 2),
            "OFI_ZScore_20": round(zscore, 4),
            "OFI_Cumulative_1H": round(cumulative, 2),
            "OFI_Tick_Count": ticks,
            "OFI_Total_Volume": round(total_vol, 2),
        }

    @property
    def is_warm(self) -> bool:
        """True if enough bars collected for reliable Z-Score."""
        return len(self._ofi_history) >= self._window


# ── Module-level singleton ──
_collector: OFICollector | None = None
_lock = threading.Lock()


def get_ofi_collector() -> OFICollector:
    global _collector
    if _collector is None:
        with _lock:
            if _collector is None:
                _collector = OFICollector()
    return _collector
