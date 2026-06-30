"""Trend-aware volume guards — pure functions extracted from strategy_line.py.

Strangler Fig #16: extracted from ``StrategyLine.evaluate()`` L1158-1178 and L1226-1248.
Pure function contract: zero I/O, zero global state, same input → same output.

Related FIXes: FIX-20260609-008 (MetaFilter extraction), FIX-20260609-009 (trend gates)
"""

from __future__ import annotations

from typing import Any

# ── Counter-trend volume penalty ─────────────────────────────────────────


# Per-strategy counter-trend penalise thresholds.
# DQAF-20260630-198: thresholds are raw ADX (0-100 scale), NOT normalized (0-1).
# The regime_gate stores raw ADX values — same root cause as trend_isolation_gates.py.
#   penalise >= 20.0: emerging trend → apply volume penalty
#   h4_penalise >= 20.0: H4 emerging trend → apply volume penalty
CT_PENALISE: dict[str, dict[str, float]] = {
    "statarb_dynamic": {"penalise": 20.0, "h4_penalise": 20.0},
    "statarb_m15": {"penalise": 20.0, "h4_penalise": 20.0},
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

    penalise_threshold = ct_cfg.get("penalise", 20.0)
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


# ── Counter-trend action classifier ────────────────────────────────────────
# FIX-20260620-015: moved from strategy_line.py (Strangler Fig pattern).
# Pure function — zero I/O, zero global state, same input → same output.


def _counter_trend_action(
    strategy_name: str,
    trend_strength: float,
    h4_trend_strength: float = 0.0,
) -> dict[str, Any]:
    """Determine how a strategy reacts to counter-trend signals.

    Per-strategy rules:
      - barrier_12bar: block at H1 >= 0.30 or H4 >= 0.25,
                       penalise at H1 >= 0.15 or H4 >= 0.15
                       (strict: barrier strategy needs trend alignment)
      - micro_*:       block at H1 >= 0.50, penalise at H1 >= 0.25
                       (moderate: short-horizon microstructure can trade
                       counter-trend, but with reduced conviction + volume)
      - statarb_dynamic: block at H1 >= 0.55 or H4 >= 0.35,
                        penalise at H1 >= 0.30 or H4 >= 0.20
                        (permissive: mean-reversion is counter-trend by design,
                        but strong trends crush OU mean-reversion, especially shorts)
      - statarb_m15:    same as statarb_dynamic — M15 mean-reversion uses
                        identical permissive thresholds

    Penalise now applies BOTH a confidence reduction AND a volume multiplier
    (vol_mult), making the penalty meaningful.  Previously only confidence was
    reduced, and a 0.90→0.72 signal still easily cleared the 0.40 threshold.

    H4 takes priority — higher-TF block fires before H1 thresholds.

    Returns dict with keys: action ("block"|"penalise"|"allow"),
                            confidence_mult, vol_mult (for penalise).
    """
    thresholds: dict[str, dict[str, Any]] = {
        "barrier_12bar": {
            "block": 0.30,
            "penalise": 0.15,
            "conf_mult": 0.60,
            "vol_mult": 0.65,
            "h4_block": 0.25,
            "h4_penalise": 0.15,
            "h4_conf_mult": 0.50,
            "h4_vol_mult": 0.50,
        },
        "micro_3bar": {
            "block": 0.50,
            "penalise": 0.25,
            "conf_mult": 0.65,
            "vol_mult": 0.70,
            "h4_block": 0.99,
            "h4_penalise": 0.99,
            "h4_conf_mult": 1.0,
            "h4_vol_mult": 1.0,
        },
        "micro_m15": {
            "block": 0.50,
            "penalise": 0.25,
            "conf_mult": 0.65,
            "vol_mult": 0.70,
            "h4_block": 0.99,
            "h4_penalise": 0.99,
            "h4_conf_mult": 1.0,
            "h4_vol_mult": 1.0,
        },
        "micro_h1": {
            "block": 0.45,
            "penalise": 0.22,
            "conf_mult": 0.60,
            "vol_mult": 0.65,
            "h4_block": 0.99,
            "h4_penalise": 0.99,
            "h4_conf_mult": 1.0,
            "h4_vol_mult": 1.0,
        },
        "statarb_dynamic": {
            "block": 0.55,
            "penalise": 0.30,
            "conf_mult": 0.70,
            "vol_mult": 0.75,
            "h4_block": 0.35,
            "h4_penalise": 0.20,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        },
        "statarb_m15": {
            "block": 0.55,
            "penalise": 0.30,
            "conf_mult": 0.70,
            "vol_mult": 0.75,
            "h4_block": 0.35,
            "h4_penalise": 0.20,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        },
        "m15_swing": {
            "block": 0.70,
            "penalise": 0.25,
            "conf_mult": 0.65,
            "vol_mult": 0.75,
            "h4_block": 0.60,
            "h4_penalise": 0.30,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        },
        "m30_swing": {
            "block": 0.70,
            "penalise": 0.25,
            "conf_mult": 0.65,
            "vol_mult": 0.75,
            "h4_block": 0.60,
            "h4_penalise": 0.30,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        },
        "h1_swing": {
            "block": 0.75,
            "penalise": 0.55,
            "conf_mult": 0.65,
            "vol_mult": 0.75,
            "h4_block": 0.70,
            "h4_penalise": 0.50,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        },
        "h4_swing": {
            "block": 0.80,
            "penalise": 0.60,
            "conf_mult": 0.65,
            "vol_mult": 0.75,
            "h4_block": 0.75,
            "h4_penalise": 0.55,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        },
        "btc_swing": {
            "block": 0.85,
            "penalise": 0.55,
            "conf_mult": 0.65,
            "vol_mult": 0.75,
            "h4_block": 0.80,
            "h4_penalise": 0.55,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        },
    }
    t = thresholds.get(
        strategy_name,
        {
            "block": 0.60,
            "penalise": 0.35,
            "conf_mult": 0.65,
            "vol_mult": 0.70,
            "h4_block": 0.70,
            "h4_penalise": 0.40,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        },
    )

    # H4 gate checked first — higher TF takes priority
    if h4_trend_strength >= t["h4_block"]:
        return {
            "action": "block",
            "confidence_mult": t["h4_conf_mult"],
            "vol_mult": t["h4_vol_mult"],
        }
    if h4_trend_strength >= t["h4_penalise"]:
        return {
            "action": "penalise",
            "confidence_mult": t["h4_conf_mult"],
            "vol_mult": t["h4_vol_mult"],
        }

    # H1 thresholds
    if trend_strength >= t["block"]:
        return {"action": "block", "confidence_mult": t["conf_mult"], "vol_mult": t["vol_mult"]}
    if trend_strength >= t["penalise"]:
        return {"action": "penalise", "confidence_mult": t["conf_mult"], "vol_mult": t["vol_mult"]}
    return {"action": "allow", "confidence_mult": 1.0, "vol_mult": 1.0}
