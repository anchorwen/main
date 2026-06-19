"""Trend-aware volume guards — pure functions extracted from strategy_line.py.

Strangler Fig #16: extracted from ``StrategyLine.evaluate()`` L1158-1178 and L1226-1248.
Pure function contract: zero I/O, zero global state, same input → same output.

Related FIXes: FIX-20260609-008 (MetaFilter extraction), FIX-20260609-009 (trend gates)
"""

from __future__ import annotations

from typing import Any

# ── Counter-trend volume penalty ─────────────────────────────────────────


# Per-strategy counter-trend penalise thresholds.
CT_PENALISE: dict[str, dict[str, float]] = {
    "statarb_dynamic": {"penalise": 0.30, "h4_penalise": 0.20},
    "statarb_m15": {"penalise": 0.30, "h4_penalise": 0.20},
}


def compute_counter_trend_volume_mult(
    strategy_name: str,
    direction: str,
    regime_info: dict[str, Any] | None,
    *,
    default_mult: float = 1.0,
    penalised_mult: float = 0.70,
) -> float:
    """Apply counter-trend volume penalty for OU/statarb strategies.

    When the market is trending (H1 ADX confirms direction) and the trade
    direction goes against the primary trend, reduce volume to limit
    counter-trend exposure.

    Args:
        strategy_name: Strategy identifier (e.g. "statarb_dynamic").
        direction: Trade direction ("long", "short", "neutral").
        regime_info: Regime gate output dict.
        default_mult: Volume multiplier when no penalty applies.
        penalised_mult: Volume multiplier when penalty IS applied.

    Returns:
        Volume multiplier — 0.70 when penalised, otherwise default_mult.
    """
    if regime_info is None:
        return default_mult

    if direction == "neutral":
        return default_mult

    ct_cfg = CT_PENALISE.get(strategy_name)
    if ct_cfg is None:
        return default_mult

    rg = regime_info.get("regime_gate", {}) if isinstance(regime_info, dict) else {}
    h1_adx = float(rg.get("h1_adx") or 0.0)
    primary_dir = str(rg.get("primary_trend") or "neutral")

    if h1_adx <= 0:
        return default_mult
    if primary_dir == "neutral":
        return default_mult

    if direction == primary_dir:
        return default_mult  # with-trend — no penalty

    penalise_threshold = ct_cfg.get("penalise", 0.30)
    if h1_adx >= penalise_threshold:
        return penalised_mult

    return default_mult


# ── Minimum RR Guard ──────────────────────────────────────────────────────


def check_minimum_rr(
    entry_price: float,
    sl: float,
    tp: float,
    *,
    min_rr_ratio: float = 1.2,
) -> bool:
    """Check whether a trade meets the minimum reward-to-risk ratio.

    Args:
        entry_price: Entry price level.
        sl: Stop-loss price level.
        tp: Take-profit price level.
        min_rr_ratio: Minimum acceptable RR ratio. Default 1.2.

    Returns:
        True if the trade meets or exceeds the minimum RR, False otherwise.
    """
    tp_dist = abs(tp - entry_price)
    sl_dist = abs(sl - entry_price)
    if sl_dist <= 0:
        return False
    return (tp_dist / sl_dist) >= min_rr_ratio
