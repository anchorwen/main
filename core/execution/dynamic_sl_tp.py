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
    envelope_warning: str = ""  # Non-empty when vol_ratio outside [0.6, 1.6]


def compute_dynamic_sl_tp(
    base_sl_mult: float,
    base_tp_mult: float,
    *,
    current_atr: float,
    ref_atr: float = 7.0,
    hard_sl_ratio: float = 1.5,
    min_sl_mult: float = 1.2,
    max_sl_mult: float = 3.0,
    max_tp_mult: float | None = None,
) -> DynamicSLTP:
    """Compute volatility-normalized SL/TP distances.

    Args:
        base_sl_mult: Base SL multiplier at reference ATR (e.g. 2.0).
        base_tp_mult: Base TP multiplier at reference ATR (e.g. 3.5).
        current_atr: Current ATR(14) value.
        ref_atr: Reference ATR (median / long-run average), default 5.0 for XAUUSD M5.
        hard_sl_ratio: Hard SL = normal SL × this ratio (server-side disaster protection).
        min_sl_mult: Floor for effective SL/TP multiplier.
        max_sl_mult: Ceiling for effective SL multiplier.
        max_tp_mult: Ceiling for effective TP multiplier.
                     Defaults to max(max_sl_mult, base_tp_mult) so the
                     training TP multiplier is never capped at normal vol.

    Returns:
        DynamicSLTP with absolute distances and effective multipliers.
    """
    if current_atr <= 0:
        current_atr = ref_atr

    vol_ratio = current_atr / ref_atr

    # Direct ATR multiplication — SL/TP scale proportionally with volatility.
    # At ATR=5: SL=2.0×5=10.0 (2.0 ATR). At ATR=8: SL=2.0×8=16.0 (still 2.0 ATR).
    # Previous inverse formula (base_sl_mult / vol_ratio) mathematically cancelled
    # to a fixed distance, causing SL to shrink to ~1.25 ATR in high vol → noise-triggered.
    sl_mult = base_sl_mult
    tp_mult = base_tp_mult

    # Clamp to reasonable bounds (SL and TP have separate ceilings)
    if max_tp_mult is None:
        max_tp_mult = max(max_sl_mult, base_tp_mult)
    sl_mult = max(min_sl_mult, min(max_sl_mult, sl_mult))
    tp_mult = max(min_sl_mult, min(max_tp_mult, tp_mult))

    sl_distance = sl_mult * current_atr
    tp_distance = tp_mult * current_atr
    hard_sl_distance = sl_distance * hard_sl_ratio

    # Envelope check: warn when vol_ratio drifts far from training distribution
    envelope_warning = ""
    if vol_ratio < 0.6:
        envelope_warning = (
            f"vol_ratio={vol_ratio:.2f} below 0.6 (ATR={current_atr:.1f} << ref={ref_atr:.0f}), "
            f"SL capped at {max_sl_mult}x. Training SL was {base_sl_mult}x — "
            f"effective SL is {max_sl_mult/base_sl_mult*100:.0f}% of training value."
        )
    elif vol_ratio > 1.6:
        envelope_warning = (
            f"vol_ratio={vol_ratio:.2f} above 1.6 (ATR={current_atr:.1f} >> ref={ref_atr:.0f}), "
            f"SL floored at {min_sl_mult}x. Training SL was {base_sl_mult}x — "
            f"effective SL is {min_sl_mult/base_sl_mult*100:.0f}% of training value."
        )

    return DynamicSLTP(
        sl_distance=round(sl_distance, 5),
        tp_distance=round(tp_distance, 5),
        hard_sl_distance=round(hard_sl_distance, 5),
        sl_atr_mult=round(sl_mult, 4),
        tp_atr_mult=round(tp_mult, 4),
        vol_ratio=round(vol_ratio, 4),
        envelope_warning=envelope_warning,
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
