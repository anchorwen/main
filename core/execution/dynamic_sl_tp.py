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
from enum import Enum

# ── Strategy family discriminator ─────────────────────────────────────────


class StrategyFamily(str, Enum):
    """Strategy archetype for asymmetric volatility response.

    Mean reversion (OU/StatArb): high vol → SL widens to survive noise,
    TP tightens or stays flat (reversion profits shrink in turbulence).

    Trend following (Barrier/Swing): high vol → SL and TP widen
    synchronously (trend profits scale with volatility).
    """

    MEAN_REVERSION = "mean_reversion"
    TREND_FOLLOWING = "trend_following"


# ── Institutional hard clipping bounds ────────────────────────────────────

MIN_SL_ATR = 0.8  # absolute floor — below this, noise triggers the stop
MAX_SL_ATR = 4.0  # absolute ceiling — above this, one loss is too costly
MIN_TP_ATR = 1.0  # reward:risk floor — TP must be at least 1.0 ATR
MAX_TP_ATR = 6.0  # TP ceiling — beyond this the target is rarely hit


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


def _compute_regime_factors(
    vol_ratio: float,
    strategy_family: str,
) -> tuple[float, float]:
    """Asymmetric SL/TP regime factors from volatility ratio.

    Returns (sl_factor, tp_factor) — multipliers applied to base_sl_mult
    and base_tp_mult.  At vol_ratio=1.0 both factors equal 1.0.

    Trend following: synchronous sqrt scaling — SL and TP widen together.
    Mean reversion: SL widens (sqrt), TP tightens (inverse 4th root) —
    reversion profits shrink in turbulence, don't chase them.

    Empty strategy_family → (1.0, 1.0) — backward compatible no-op.
    """
    if not strategy_family:
        return 1.0, 1.0

    if strategy_family == StrategyFamily.TREND_FOLLOWING.value:
        sl_factor = vol_ratio**0.5
        tp_factor = vol_ratio**0.5
    else:  # mean_reversion
        sl_factor = vol_ratio**0.5
        tp_factor = vol_ratio**-0.25

    sl_factor = max(0.55, min(1.80, sl_factor))
    tp_factor = max(0.55, min(2.00, tp_factor))
    return sl_factor, tp_factor


def compute_dynamic_sl_tp(
    base_sl_mult: float,
    base_tp_mult: float,
    *,
    current_atr: float,
    ref_atr: float = 5.0,  # keep in sync with StrategyLineConfig.ref_atr default
    hard_sl_ratio: float = 1.5,
    min_sl_mult: float = MIN_SL_ATR,  # institutional floor — noise-proof
    max_sl_mult: float = MAX_SL_ATR,  # institutional ceiling — loss budget
    max_tp_mult: float | None = None,
    timeframe_mult: int = 1,
    min_sl_distance: float = 0.0,
    min_rr_ratio: float = 0.0,
    strategy_family: str = "",  # Phase 4: asymmetric regime response per archetype
) -> DynamicSLTP:
    """Compute volatility-normalized SL/TP distances.

    Args:
        base_sl_mult: Base SL multiplier at reference ATR (e.g. 2.0).
        base_tp_mult: Base TP multiplier at reference ATR (e.g. 3.5).
        current_atr: Current ATR(14) value on the base (M5) timeframe.
        ref_atr: Reference ATR (median / long-run average), default 5.0 for XAUUSD M5.
        hard_sl_ratio: Hard SL = normal SL × this ratio (server-side disaster protection).
        min_sl_mult: Floor for effective SL/TP multiplier.
        max_sl_mult: Ceiling for effective SL multiplier.
        max_tp_mult: Ceiling for effective TP multiplier.
                     Defaults to max(max_sl_mult, base_tp_mult) so the
                     training TP multiplier is never capped at normal vol.
        timeframe_mult: M5-bar equivalent of the strategy timeframe (e.g. H1=12).
                        DEPRECATED for SL/TP ATR scaling (FIX-20260629-197).
                        Retained for backward compatibility; the parameter is
                        accepted but no longer applied to current_atr.
        min_sl_distance: Absolute price-distance floor for SL (e.g. 0.80 for
                         8 pips on XAUUSD).  When ATR collapses the raw SL
                         distance can drop below the spread, leaving no net
                         breathing room.  This floor guarantees a minimum
                         distance regardless of ATR.
        min_rr_ratio: When SL is floored up by min_sl_distance, stretch TP
                      to maintain at least this reward:risk ratio.  Prevents
                      negative asymmetry from the absolute floor.
                      0.0 = disabled.

    Returns:
        DynamicSLTP with absolute distances and effective multipliers.
    """

    if current_atr <= 0:
        current_atr = ref_atr

    # ── ATR preserved at native M5 scale (√t scaling removed FIX-20260629-197) ──
    # √t scaling REMOVED: ATR stays at native M5 resolution.
    # Training labels use M5 ATR; live SL/TP must match the same scale.
    # Prior √t inflation (commit 6c081245, 2026-05-21) caused configured
    # SL=2.0 ATR to become 5.8–13.9 ATR for non-M5 timeframes.
    # See DQAF-20260629-197 for forensic evidence.
    raw_atr = current_atr
    # timeframe_mult is retained for backward compatibility but no longer
    # applied to ATR.  It remains available for future non-SL/TP uses.

    vol_ratio = raw_atr / ref_atr

    # ── Asymmetric regime scaling (Phase 4) ──
    # vol_ratio-driven regime factors: widen/tighten SL/TP per strategy archetype.
    # Mean reversion: SL widens to survive noise, TP tightens (don't chase).
    # Trend following: SL and TP widen synchronously (trend profits scale with vol).
    # Empty strategy_family → (1.0, 1.0) — backward compatible no-op.
    sl_factor, tp_factor = _compute_regime_factors(vol_ratio, strategy_family)

    sl_mult = base_sl_mult * sl_factor
    tp_mult = base_tp_mult * tp_factor

    # Clamp to institutional bounds (SL and TP have separate ceilings)
    if max_tp_mult is None:
        max_tp_mult = MAX_TP_ATR
    sl_mult = max(min_sl_mult, min(max_sl_mult, sl_mult))
    tp_mult = max(MIN_TP_ATR, min(max_tp_mult, tp_mult))

    sl_distance = sl_mult * current_atr
    tp_distance = tp_mult * current_atr

    # ── Absolute distance floor ──
    # When ATR collapses (e.g. 3.17 on XAUUSD M5), raw SL can drop below the
    # spread, leaving ~2 pips of net breathing room.  min_sl_distance guarantees
    # a minimum absolute price distance regardless of ATR.
    if min_sl_distance > 0 and sl_distance < min_sl_distance:
        sl_distance = min_sl_distance

    # ── RR guard: when SL is floored up, stretch TP to maintain min RR ──
    if min_rr_ratio > 0 and tp_distance < sl_distance * min_rr_ratio:
        tp_distance = sl_distance * min_rr_ratio

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
    *,
    spread_points: float = 0.0,
    tick_size: float = 0.01,
) -> dict[str, float]:
    """Convert distances to absolute price levels for MT5 order placement.

    When spread_points > 0, TP is tightened by spread cost (exit fills at bid
    for long / ask for short) and SL is widened by spread cost (stop fills
    suffer adverse slippage in fast moves).  This aligns live order placement
    with training-label barrier adjustments in label_contract.py.

    Default spread_points=0.0 preserves backward-compatible behaviour.
    """
    if spread_points > 0 and tick_size > 0:
        spread_cost = spread_points * tick_size
        if side == "long":
            return {
                "stop_loss": round(entry_price - dsl.sl_distance - spread_cost, 5),
                "take_profit": round(entry_price + dsl.tp_distance - spread_cost, 5),
                "hard_sl": round(entry_price - dsl.hard_sl_distance - spread_cost, 5),
            }
        else:
            return {
                "stop_loss": round(entry_price + dsl.sl_distance + spread_cost, 5),
                "take_profit": round(entry_price - dsl.tp_distance + spread_cost, 5),
                "hard_sl": round(entry_price + dsl.hard_sl_distance + spread_cost, 5),
            }
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
