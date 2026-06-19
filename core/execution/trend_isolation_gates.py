"""Trend isolation gates — Strangler Fig extraction from strategy_line.py.

FIX-20260609-009: Extracted from ``StrategyLine.evaluate()`` sections
4aa (direction-aware trend isolation), 4b (multi-TF hard filter),
4c (counter-trend gate), 4d (z-score inflection gate).

All four gates apply to OU/statarb strategies and follow the same
pattern: check a condition → return rejected StrategyDecision or None.
"""

from __future__ import annotations

from typing import Any

from core.execution.strategy_decision import StrategyDecision


def apply_trend_isolation_gates(
    *,
    name: str,
    direction: str,
    confidence: float,
    entry_z_score: float,
    regime_info: dict[str, Any] | None,
    config: Any,
    brain_ids: list[str],
    support_count: int,
    total_count: int,
    regime_gate_mode: str,
    # ── v3.2 state ──
    last_entry_z: float | None = None,
) -> StrategyDecision | None:
    """Apply trend/timeframe isolation gates for OU/statarb strategies.

    Returns a rejected StrategyDecision if any gate blocks the signal,
    or None if all gates pass (continue to downstream evaluation).

    Gate order (priority):
      4aa: Direction-aware counter-trend (statarb only)
      4b:  Hard multi-TF trend filter (all swing/trend strategies)
      4c:  Counter-trend action (strategy-specific thresholds)
      4d:  Z-score inflection (catches falling knives)
    """

    # ── 4aa. Direction-aware trend isolation gate (OU/statarb only) ──
    if "statarb" in name and regime_info:
        _rg_4aa = regime_info.get("regime_gate", {}) if isinstance(regime_info, dict) else {}
        _trend_strength = float(_rg_4aa.get("h1_adx") or 0.0)
        _h4_ts = float(_rg_4aa.get("h4_trend_strength") or 0.0)
        _m5_ts = float(_rg_4aa.get("m5_trend_strength") or 0.0)
        _h1_dir = str(_rg_4aa.get("h1_trend_direction") or "neutral")
        _primary_dir = str(_rg_4aa.get("primary_trend") or "neutral")
        _primary_source = str(_rg_4aa.get("primary_trend_source") or "h1")

        _is_strong_trend = _trend_strength > 25.0
        _mtf_consensus = _trend_strength > 20.0 and _h4_ts > 0.5 and _m5_ts > 0.5

        if _is_strong_trend or _mtf_consensus:
            _ref_dir = _primary_dir if _primary_dir != "neutral" else _h1_dir
            _is_counter_trend = (
                direction != "neutral" and _ref_dir != "neutral" and direction != _ref_dir
            )
            if _is_counter_trend:
                return StrategyDecision(
                    strategy_name=name,
                    magic=config.magic,
                    should_trade=False,
                    direction=direction,
                    confidence=confidence,
                    volume=0.0,
                    sl=0.0,
                    tp=0.0,
                    hard_sl=0.0,
                    brain_ids=brain_ids,
                    supporting_count=support_count,
                    total_count=total_count,
                    regime_mode=regime_gate_mode,
                    reason=f"counter_trend_blocked:{direction}_vs_{_ref_dir}({_primary_source})_ts={_trend_strength:.1f}",
                )

    # ── 4b. Hard multi-TF trend filter (Phase C Fix 1) ──
    if regime_info is not None:
        _rg_4b = regime_info.get("regime_gate", {}) if isinstance(regime_info, dict) else {}
        _h4_trend = str(_rg_4b.get("h4_trend_direction") or "neutral")
        _h1_trend = str(_rg_4b.get("h1_trend_direction") or "neutral")
        _is_swing = name in ("m15_swing", "m30_swing", "m5_swing", "daily_swing",
                            "h1_swing", "h4_swing", "btc_swing")
        if _is_swing and _h4_trend != "neutral" and _h1_trend != "neutral" and _h4_trend != _h1_trend:
            return StrategyDecision(
                strategy_name=name,
                magic=config.magic,
                should_trade=False,
                direction=direction,
                confidence=confidence,
                volume=0.0,
                sl=0.0,
                tp=0.0,
                hard_sl=0.0,
                brain_ids=brain_ids,
                supporting_count=support_count,
                total_count=total_count,
                regime_mode=regime_gate_mode,
                reason=f"hard_trend_filter_{direction}_vs_h1h4_{_h1_trend}_{_h4_trend}",
            )

    # ── 4c. Counter-trend gate ──
    if regime_info is not None:
        _rg_4c = regime_info.get("regime_gate", {}) if isinstance(regime_info, dict) else {}
        _h1_adx = float(_rg_4c.get("h1_adx") or 0.0)
        _h1_dir_4c = str(_rg_4c.get("h1_trend_direction") or "neutral")
        _primary_dir_4c = str(_rg_4c.get("primary_trend") or "neutral")

        # Strategy-specific counter-trend thresholds
        _COUNTER_TREND_THRESHOLDS: dict[str, dict[str, float]] = {
            "statarb_dynamic": {"block": 0.55, "penalise": 0.30, "h4_block": 0.35, "h4_penalise": 0.20},
            "statarb_m15": {"block": 0.55, "penalise": 0.30, "h4_block": 0.35, "h4_penalise": 0.20},
            "m15_swing": {"block": 0.55, "penalise": 0.30},
            "m30_swing": {"block": 0.55, "penalise": 0.30},
            "h1_swing": {"block": 0.55, "penalise": 0.30},
            "h4_swing": {"block": 0.55, "penalise": 0.30},
        }
        _thresholds = _COUNTER_TREND_THRESHOLDS.get(name)

        if _thresholds and _h1_adx > 0 and direction != "neutral" and _primary_dir_4c != "neutral":
            if direction != _primary_dir_4c:
                _block = _thresholds.get("block", 0.55)
                _h4_block = _thresholds.get("h4_block", 0.55)
                _h4_trend_dir = str(_rg_4c.get("h4_trend_direction") or "neutral")
                _h4_adx = float(_rg_4c.get("h4_adx") or 0.0)

                if _h1_adx >= _block:
                    return StrategyDecision(
                        strategy_name=name,
                        magic=config.magic,
                        should_trade=False,
                        direction=direction,
                        confidence=confidence,
                        volume=0.0,
                        sl=0.0,
                        tp=0.0,
                        hard_sl=0.0,
                        brain_ids=brain_ids,
                        supporting_count=support_count,
                        total_count=total_count,
                        regime_mode=regime_gate_mode,
                        reason=f"counter_trend_blocked_h1_adx_{_h1_adx:.1f}_gte_{_block}",
                    )
                if _h4_block > 0 and _h4_trend_dir != "neutral" and direction != _h4_trend_dir and _h4_adx >= _h4_block:
                    return StrategyDecision(
                        strategy_name=name,
                        magic=config.magic,
                        should_trade=False,
                        direction=direction,
                        confidence=confidence,
                        volume=0.0,
                        sl=0.0,
                        tp=0.0,
                        hard_sl=0.0,
                        brain_ids=brain_ids,
                        supporting_count=support_count,
                        total_count=total_count,
                        regime_mode=regime_gate_mode,
                        reason=f"counter_trend_blocked_h4_adx_{_h4_adx:.1f}_gte_{_h4_block}",
                    )

    # ── 4d. Z-score inflection gate (v3.2) ──
    if "statarb" in name or "ou" in name.lower():
        if entry_z_score != 0.0:
            from core.execution.strategy_line import check_z_inflection

            _z_entry = 1.3 if "statarb" in name else 1.5
            _inf_allow, _inf_reason = check_z_inflection(
                entry_z_score,
                last_entry_z,
                direction,
                z_entry=_z_entry,
            )
            if not _inf_allow:
                return StrategyDecision(
                    strategy_name=name,
                    magic=config.magic,
                    should_trade=False,
                    direction=direction,
                    confidence=confidence,
                    volume=0.0,
                    sl=0.0,
                    tp=0.0,
                    hard_sl=0.0,
                    brain_ids=brain_ids,
                    supporting_count=support_count,
                    total_count=total_count,
                    regime_mode=regime_gate_mode,
                    reason=_inf_reason,
                )

    return None  # all gates passed
