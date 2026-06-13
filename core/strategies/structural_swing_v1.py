"""Structural_Swing_V1 — Zero-ML rule-based strategy.

No machine learning. No feature vectors. No brain configs.
Just three mechanical rules backed by calibration math.

Calibration source: FIX-20260613-030 — XAUUSDc M5, 50K bars
  SL=3.0, TP=1.5 → EV=+0.2044R (after spread + slippage)
  TP rate=47.6%, SL rate=16.8%, timeout=35.6%

Rules:
  1. Trend filter: H1 EMA(20)-EMA(50) > 0.5×ATR  (reduce whipsaw)
  2. Execution: Bid/Ask-aware SL/TP matching profitability_calibrator.py
  3. Time stop: 12-bar horizon, market close on timeout

Usage:
    from core.strategies.structural_swing_v1 import StructuralSwingV1

    strat = StructuralSwingV1()
    signal = strat.evaluate(m5_ohlc, h1_ohlc, bar_index)
    if signal:
        entry, sl, tp = signal
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass
class SwingSignal:
    direction: str  # "long" or "short"
    entry_price: float
    stop_loss: float
    take_profit: float
    atr: float
    ema_diff: float
    bar_index: int


class StructuralSwingV1:
    """Pure rule-based strategy — no ML, no features, just math."""

    def __init__(
        self,
        sl_atr_mult: float = 3.0,
        tp_atr_mult: float = 1.5,
        horizon_bars: int = 12,
        atr_period: int = 14,
        ema_fast: int = 20,
        ema_slow: int = 50,
        ema_threshold_atr_mult: float = 0.5,
        spread_points: float = 30,
        slippage_points: float = 10,
        tick_size: float = 0.001,
    ):
        self.sl_mult = sl_atr_mult
        self.tp_mult = tp_atr_mult
        self.horizon = horizon_bars
        self.atr_period = atr_period
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.ema_threshold = ema_threshold_atr_mult
        self.spread_points = spread_points
        self.slippage_points = slippage_points
        self.tick_size = tick_size

    # ═══════════════════════════════════════════════════════════════════════════
    # Indicators (self-contained, no external dependencies)
    # ═══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _ema(data: np.ndarray, period: int) -> np.ndarray:
        """Exponential moving average."""
        if len(data) < period:
            return np.full_like(data, np.nan, dtype=np.float64)
        alpha = 2.0 / (period + 1)
        result = np.full_like(data, np.nan, dtype=np.float64)
        result[period - 1] = np.mean(data[:period])
        for i in range(period, len(data)):
            result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
        return result

    @staticmethod
    def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
        """Average True Range."""
        if len(closes) < period + 1:
            return np.full_like(closes, np.nan, dtype=np.float64)
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ),
        )
        atr = np.full_like(closes, np.nan, dtype=np.float64)
        atr[period] = np.mean(tr[:period])
        for i in range(period + 1, len(closes)):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i - 1]) / period
        return atr

    # ═══════════════════════════════════════════════════════════════════════════
    # Trend filter (Rule 1)
    # ═══════════════════════════════════════════════════════════════════════════

    def _check_trend(self, h1_closes: np.ndarray, h1_atr: np.ndarray, idx: int) -> tuple[bool, str, float]:
        """Check H1 trend. Returns (allowed, direction, ema_diff)."""
        if idx < self.ema_slow:
            return False, "neutral", 0.0

        ema_f = self._ema(h1_closes[: idx + 1], self.ema_fast)
        ema_s = self._ema(h1_closes[: idx + 1], self.ema_slow)
        diff = ema_f[idx] - ema_s[idx]

        atr_val = h1_atr[idx]
        if np.isnan(atr_val) or atr_val <= 0:
            return False, "neutral", 0.0

        threshold = self.ema_threshold * atr_val

        if abs(diff) < threshold:
            return False, "neutral", diff  # whipsaw — stay out
        if diff > 0:
            return True, "long", diff
        return True, "short", diff

    # ═══════════════════════════════════════════════════════════════════════════
    # Execution (Rule 2): Bid/Ask-aware barrier computation
    # ═══════════════════════════════════════════════════════════════════════════

    def _compute_barriers(
        self, direction: str, ref_price: float, atr_val: float
    ) -> tuple[float, float, float]:
        """Compute entry, SL, TP with correct Bid/Ask friction.

        Matches profitability_calibrator.py exactly:
          - Long:  buy at Ask  = ref + slippage
          - Short: sell at Bid = ref - slippage
          - SL widened by spread (stop fills at worse price)
          - TP tightened by spread (exit at market)
        """
        slippage = self.slippage_points * self.tick_size
        spread = self.spread_points * self.tick_size

        sl_dist = self.sl_mult * atr_val + spread   # wider: stop-fill at worse price
        tp_dist = self.tp_mult * atr_val - spread   # tighter: exit at market
        tp_dist = max(tp_dist, sl_dist * 0.3)       # minimum TP = 0.3 × SL

        if direction == "long":
            entry = ref_price + slippage
            sl = entry - sl_dist
            tp = entry + tp_dist
        else:
            entry = ref_price - slippage
            sl = entry + sl_dist
            tp = entry - tp_dist

        return entry, sl, tp

    # ═══════════════════════════════════════════════════════════════════════════
    # Main entry point
    # ═══════════════════════════════════════════════════════════════════════════

    def evaluate(
        self,
        m5_opens: np.ndarray,
        m5_highs: np.ndarray,
        m5_lows: np.ndarray,
        m5_closes: np.ndarray,
        h1_closes: np.ndarray,
        bar_index: int,
    ) -> SwingSignal | None:
        """Evaluate one M5 bar. Returns SwingSignal if entry, None if skip.

        Args:
            m5_opens, m5_highs, m5_lows, m5_closes: M5 OHLC arrays
            h1_closes: H1 close array (resampled to M5 for index alignment)
            bar_index: Current M5 bar index (0-based)
        """
        # Need enough history for H1 indicators
        if bar_index < max(self.ema_slow, self.atr_period + 1):
            return None

        # ── Rule 1: Trend filter ──
        h1_atr = self._atr(
            h1_closes[: bar_index + 1],
            h1_closes[: bar_index + 1],  # using close as proxy for H1 high/low
            h1_closes[: bar_index + 1],
            self.atr_period,
        )
        allowed, direction, ema_diff = self._check_trend(h1_closes, h1_atr, bar_index)
        if not allowed:
            return None

        # ── Rule 2: Compute barriers ──
        m5_atr = self._atr(
            m5_highs[: bar_index + 1],
            m5_lows[: bar_index + 1],
            m5_closes[: bar_index + 1],
            self.atr_period,
        )
        atr_val = m5_atr[bar_index]
        if np.isnan(atr_val) or atr_val <= 0:
            return None

        ref_price = m5_opens[bar_index + 1] if bar_index + 1 < len(m5_opens) else m5_closes[bar_index]
        entry, sl, tp = self._compute_barriers(direction, float(ref_price), float(atr_val))
        if entry <= 0 or sl <= 0 or tp <= 0:
            return None

        return SwingSignal(
            direction=direction,
            entry_price=round(entry, 3),
            stop_loss=round(sl, 3),
            take_profit=round(tp, 3),
            atr=round(float(atr_val), 4),
            ema_diff=round(float(ema_diff), 4),
            bar_index=bar_index,
        )

    def to_dict(self) -> dict[str, Any]:
        """Export strategy parameters for logging."""
        return {
            "strategy": "Structural_Swing_V1",
            "sl_atr_mult": self.sl_mult,
            "tp_atr_mult": self.tp_mult,
            "horizon_bars": self.horizon,
            "atr_period": self.atr_period,
            "ema_fast": self.ema_fast,
            "ema_slow": self.ema_slow,
            "ema_threshold_atr_mult": self.ema_threshold,
            "spread_points": self.spread_points,
            "slippage_points": self.slippage_points,
            "tick_size": self.tick_size,
        }
