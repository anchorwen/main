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

DQAF-20260707-004 (Phase 1 Flow Features):
    OFI_Cumulative_Delta = running sum of all OFI_M5 since collector start
    OFI_Delta_Divergence = 1.0 when price direction disagrees with delta direction
    OFI_Volume_Real_Ratio = avg(volume_real / tick_volume) from recent ticks

Usage (in bridge worker or feature computer):
    collector = OFICollector()
    collector.on_tick(price=65800, bid=65795, ask=65805, volume=0.5, volume_real=0.3)
    # At M5 bar close:
    ofi = collector.settle_m5_bar()
    # → {"OFI_M5": 3.2, "OFI_ZScore_20": 1.15, "OFI_Cumulative_1H": 12.8,
    #     "OFI_Cumulative_Delta": 45.6, "OFI_Delta_Divergence": 0.0,
    #     "OFI_Volume_Real_Ratio": 0.62}
"""

from __future__ import annotations

import threading
from collections import deque

import numpy as np


class OFICollector:
    """Tick-rule order flow imbalance accumulator.

    Thread-safe.  Call on_tick() for each MT5 tick, settle_m5_bar()
    at each 5-minute boundary to get the aggregated OFI features.

    DQAF-20260707-004: Extended with cumulative delta tracking (running
    sum of all OFI_M5 since collector start), delta/price divergence
    detection, and real_volume/tick_volume ratio from tick data.
    """

    def __init__(self, window: int = 20, cumulative_bars: int = 12) -> None:
        self._lock = threading.Lock()
        self._window = window
        self._cumulative_bars = cumulative_bars

        # Per-bar accumulators
        self._buy_volume: float = 0.0
        self._sell_volume: float = 0.0
        self._tick_count: int = 0
        self._bar_settled: bool = False  # Guard against double-settlement

        # ── DQAF-20260707-004: Cumulative delta + divergence state ──
        self._cumulative_delta: float = 0.0  # Running sum of all OFI_M5
        self._prev_bar_ofi: float = 0.0  # Previous bar's OFI_M5 for divergence
        self._bar_price_open: float | None = None  # First tick price of current bar
        self._bar_price_close: float | None = None  # Last tick price of current bar

        # ── DQAF-20260707-004: real_volume accumulator ──
        self._real_volume_sum: float = 0.0  # Sum of volume_real for current bar
        self._tick_volume_sum: float = 0.0  # Sum of tick_volume for current bar

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
        volume_real: float = 0.0,
    ) -> None:
        """Process one tick from MT5 symbol_info_tick.

        Args:
            price: Current last price.
            bid: Current best bid.
            ask: Current best ask.
            volume: Tick volume — number of price changes (if available).
            volume_real: Real traded volume at this tick (MT5 tick index 7).
        """
        if volume <= 0:
            volume = 1.0  # Unit counting mode — each tick is one event

        with self._lock:
            self._bar_settled = False  # New tick → bar is active again

            # ── DQAF-20260707-004: Track bar open/close for divergence ──
            if self._bar_price_open is None:
                self._bar_price_open = price
            self._bar_price_close = price

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

            # ── DQAF-20260707-004: Accumulate real_volume ratio components ──
            self._tick_volume_sum += volume
            if volume_real > 0:
                self._real_volume_sum += volume_real

            self._prev_price = price
            self._prev_dir = direction

    def settle_m5_bar(self) -> dict[str, float]:
        """Close current M5 bar and return OFI features.

        Idempotent: returns empty dict if already settled this bar (guard
        against double-settlement from multiple Feature Lake consumers).

        Returns dict with keys:
            OFI_M5, OFI_ZScore_20, OFI_Cumulative_1H,
            OFI_Tick_Count, OFI_Total_Volume,
            OFI_Cumulative_Delta, OFI_Delta_Divergence, OFI_Volume_Real_Ratio.
        """
        with self._lock:
            if self._bar_settled:
                return {}  # Already settled — no new data
            self._bar_settled = True

            buy = self._buy_volume
            sell = self._sell_volume
            ticks = self._tick_count
            bar_open = self._bar_price_open
            bar_close = self._bar_price_close
            real_vol_sum = self._real_volume_sum
            tick_vol_sum = self._tick_volume_sum

            # Reset for next bar
            self._buy_volume = 0.0
            self._sell_volume = 0.0
            self._tick_count = 0
            self._bar_price_open = None
            self._bar_price_close = None
            self._real_volume_sum = 0.0
            self._tick_volume_sum = 0.0

        ofi = buy - sell
        total_vol = buy + sell

        self._ofi_history.append(ofi)
        self._total_volume_history.append(total_vol)

        # ── DQAF-20260707-004: Cumulative delta (running sum since start) ──
        self._cumulative_delta += ofi

        # ── DQAF-20260707-004: Delta/Price divergence ──
        # Divergence = price moved up but delta is negative, or vice versa.
        # 1.0 when diverging (potential reversal), 0.0 when aligned.
        delta_divergence = 0.0
        if bar_open is not None and bar_close is not None and bar_open > 0:
            price_return = (bar_close - bar_open) / bar_open
            price_dir = 1 if price_return > 0.0001 else (-1 if price_return < -0.0001 else 0)
            ofi_dir = 1 if ofi > 0 else (-1 if ofi < 0 else 0)
            if price_dir != 0 and ofi_dir != 0 and price_dir != ofi_dir:
                delta_divergence = 1.0

        # ── DQAF-20260707-004: Volume real/tick ratio ──
        # High ratio = large trades relative to noise (institutional flow proxy).
        # Clamp to [0, 1] — values >1 suggest data quality issues (real > tick).
        volume_real_ratio = 0.0
        if tick_vol_sum > 0:
            volume_real_ratio = min(real_vol_sum / tick_vol_sum, 1.0)

        # Z-Score over rolling window
        if len(self._ofi_history) >= 4:
            arr = np.array(list(self._ofi_history), dtype=np.float64)
            mean = float(np.mean(arr))
            std = float(np.std(arr))
            zscore = float((ofi - mean) / std) if std > 1e-10 else 0.0
        else:
            zscore = 0.0

        # Cumulative 1H (last 12 bars)
        recent = list(self._ofi_history)[-self._cumulative_bars :]
        cumulative = float(sum(recent))

        self._prev_bar_ofi = ofi

        return {
            "OFI_M5": round(ofi, 2),
            "OFI_ZScore_20": round(zscore, 4),
            "OFI_Cumulative_1H": round(cumulative, 2),
            "OFI_Tick_Count": ticks,
            "OFI_Total_Volume": round(total_vol, 2),
            # ── DQAF-20260707-004: Flow feature additions ──
            "OFI_Cumulative_Delta": round(self._cumulative_delta, 2),
            "OFI_Delta_Divergence": delta_divergence,
            "OFI_Volume_Real_Ratio": round(volume_real_ratio, 4),
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
