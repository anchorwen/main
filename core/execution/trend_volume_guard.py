"""Trend-aware volume guards — pure functions extracted from strategy_line.py.

Strangler Fig #16: extracted from ``StrategyLine.evaluate()`` L1158-1178 and L1226-1248.
Pure function contract: zero I/O, zero global state, same input → same output.

Related FIXes: FIX-20260609-008 (MetaFilter extraction), FIX-20260609-009 (trend gates)
"""

from __future__ import annotations

from typing import Any

# ── Counter-trend volume penalty ─────────────────────────────────────────
# FIX-20260701-206: hardcoded CT_PENALISE dict removed.  Thresholds are now
# passed by the caller via ``penalise_threshold`` / ``h4_penalise_threshold``
# (sourced from StrategyLineConfig.adx_mild_trend_threshold, default 999 =
# thermal fuse per FIX-20260622-064 P0-3).
#
# Only statarb strategies are eligible for counter-trend volume penalties.
_CT_VOLUME_ELIGIBLE: frozenset[str] = frozenset({"statarb_dynamic", "statarb_m15"})


def compute_counter_trend_volume_mult(
    strategy_name: str,
    direction: str,
    regime_info: dict[str, Any] | None,
    *,
    default_mult: float = 1.0,
    penalised_mult: float = 0.70,
    penalise_threshold: float = 999.0,
    h4_penalise_threshold: float = 999.0,
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
        penalise_threshold: H1 ADX threshold for volume penalty (default 999 =
            thermal fuse disabled per FIX-20260622-064).  Read from
            StrategyLineConfig.adx_mild_trend_threshold.
        h4_penalise_threshold: H4 ADX threshold for volume penalty (default 999).

    Returns:
        Volume multiplier — 0.70 when penalised, otherwise default_mult.
    """
    if regime_info is None:
        return default_mult

    if direction == "neutral":
        return default_mult

    if strategy_name not in _CT_VOLUME_ELIGIBLE:
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
    # FIX-20260704-007: 1e-9 tolerance for IEEE 754 floating-point boundary.
    # When RR guard pre-compensation produces exactly min_rr_ratio mathematically,
    # floating-point rounding can make tp_dist/sl_dist ≈ min_rr_ratio - 2.8e-14,
    # failing the >= check.  1e-9 << any meaningful price ratio (< 0.0001%),
    # so no real risk slips through.
    return (tp_dist / sl_dist) >= (min_rr_ratio - 1e-9)


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
        # ── FIX-20260715-010: BTC counter-trend thresholds tightened ──
        # Audit (2026-07-15): BTC SHORT trades lost -$51.54 in a confirmed
        # H4 bull trend (trend_strength≈0.60).  The old H4 block threshold
        # of 0.80 never fired — counter-trend SHORTs passed freely.
        # New thresholds: H4 ≥ 0.60 → BLOCK, H4 ≥ 0.40 → PENALISE
        # (half confidence × half volume).  Still allows counter-trend
        # exploration in weak trends (H4 < 0.40) but blocks when the
        # higher-TF trend is unambiguous.
        "btc_swing": {
            "block": 0.85,
            "penalise": 0.55,
            "conf_mult": 0.65,
            "vol_mult": 0.75,
            "h4_block": 0.60,
            "h4_penalise": 0.40,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        },
    }
    t = thresholds.get(strategy_name)
    if t is None:
        # ── FIX-20260715-011: Prefix-match multi-TF variants ──
        # Strategy names like "btc_swing_m30" / "btc_swing_h4" must resolve
        # to the "btc_swing" threshold entry.  Without this they fell through
        # to the lenient default (h4_block=0.70), leaving multi-TF BTC
        # strategies unprotected.
        # Sorted by key length descending so more-specific prefixes
        # (e.g. "btc_swing_h1" if it existed) match before "btc_swing".
        for prefix in sorted(thresholds.keys(), key=len, reverse=True):
            if strategy_name.startswith(prefix + "_"):
                t = thresholds[prefix]
                break
    if t is None:
        t = {
            "block": 0.60,
            "penalise": 0.35,
            "conf_mult": 0.65,
            "vol_mult": 0.70,
            "h4_block": 0.70,
            "h4_penalise": 0.40,
            "h4_conf_mult": 0.65,
            "h4_vol_mult": 0.70,
        }

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
