"""Volatility-normalized SL/TP computation.

Replaces the fixed ATR-multiplier approach with dynamic multipliers that
normalize risk contribution across volatility regimes.  When ATR is high the
multipliers shrink, and when ATR is low they expand — so every trade carries
approximately the same dollar risk.

Institutional reference:
  "Position sizing should be inversely proportional to recent volatility so
   that each trade contributes equal risk to the portfolio." — Grinold & Kahn
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DynamicSLTP:
    """Computed SL/TP levels for one trade."""

    sl_distance: float  # Absolute price distance to stop-loss
    tp_distance: float  # Absolute price distance to take-profit
    hard_sl_distance: float  # Server-side hard SL (wider, disaster protection)
    sl_atr_mult: float  # Effective multiplier used
    tp_atr_mult: float
    vol_ratio: float  # current_atr / ref_atr


def compute_dynamic_sl_tp(
    base_sl_mult: float,
    base_tp_mult: float,
    *,
    current_atr: float,
    ref_atr: float = 5.0,
    hard_sl_ratio: float = 1.5,
    min_sl_mult: float = 0.5,
    max_sl_mult: float = 4.0,
) -> DynamicSLTP:
    """Compute volatility-normalized SL/TP distances.

    Args:
        base_sl_mult: Base SL multiplier at reference ATR (e.g. 2.0).
        base_tp_mult: Base TP multiplier at reference ATR (e.g. 3.5).
        current_atr: Current ATR(14) value.
        ref_atr: Reference ATR (median / long-run average), default 5.0 for XAUUSD M5.
        hard_sl_ratio: Hard SL = normal SL × this ratio (server-side disaster protection).
        min_sl_mult: Floor for effective SL multiplier.
        max_sl_mult: Ceiling for effective SL multiplier.

    Returns:
        DynamicSLTP with absolute distances and effective multipliers.
    """
    if current_atr <= 0:
        current_atr = ref_atr

    vol_ratio = current_atr / ref_atr

    # Scale multipliers inversely to volatility → constant risk budget
    sl_mult = base_sl_mult / vol_ratio
    tp_mult = base_tp_mult / vol_ratio

    # Clamp to reasonable bounds
    sl_mult = max(min_sl_mult, min(max_sl_mult, sl_mult))
    tp_mult = max(min_sl_mult, min(max_sl_mult, tp_mult))

    sl_distance = sl_mult * current_atr
    tp_distance = tp_mult * current_atr
    hard_sl_distance = sl_distance * hard_sl_ratio

    return DynamicSLTP(
        sl_distance=round(sl_distance, 5),
        tp_distance=round(tp_distance, 5),
        hard_sl_distance=round(hard_sl_distance, 5),
        sl_atr_mult=round(sl_mult, 4),
        tp_atr_mult=round(tp_mult, 4),
        vol_ratio=round(vol_ratio, 4),
    )


def compute_sl_tp_levels(
    side: str,
    entry_price: float,
    dsl: DynamicSLTP,
) -> dict[str, float]:
    """Convert distances to absolute price levels for MT5 order placement.

    Returns dict with keys: stop_loss, take_profit, hard_sl.
    """
    if side == "long":
        return {
            "stop_loss": round(entry_price - dsl.sl_distance, 5),
            "take_profit": round(entry_price + dsl.tp_distance, 5),
            "hard_sl": round(entry_price - dsl.hard_sl_distance, 5),
        }
    else:
        return {
            "stop_loss": round(entry_price + dsl.sl_distance, 5),
            "take_profit": round(entry_price - dsl.tp_distance, 5),
            "hard_sl": round(entry_price + dsl.hard_sl_distance, 5),
        }
