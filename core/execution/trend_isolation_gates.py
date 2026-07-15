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
        # ── FIX-20260715-019: BTC exempt from hard multi-TF trend-direction lock ──
        # BTC is a 24/7 crypto market — the H4≠H1 direction-divergence filter was
        # designed for session-based gold.  Blocking all BTC swing trades on pure
        # TF disagreement is overly restrictive.  BTC direction is already protected
        # by the counter-trend gate (FIX-017, universal cold_explore application).
        _is_swing = name in (
            "m15_swing",
            "m30_swing",
            "m5_swing",
            "daily_swing",
            "h1_swing",
            "h4_swing",
            # "btc_swing" removed — FIX-019
        )
        # Multi-TF variants all start with "btc_swing" — exempt them too
        _is_btc_swing = name.startswith("btc_swing")
        if (
            _is_swing
            and not _is_btc_swing
            and _h4_trend != "neutral"
            and _h1_trend != "neutral"
            and _h4_trend != _h1_trend
        ):
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
    # FIX-20260701-206: thresholds are NOW read from strategy config
    # (adx_trending_threshold / adx_mild_trend_threshold), default 999 = thermal
    # fuse per FIX-20260622-064 P0-3.  Previously hardcoded at 25.0/20.0 (raw
    # ADX scale), which re-enabled the gate after the 999 fuse had intentionally
    # disabled it (DQAF-20260630-198/FIX-199 regression).
    #
    # Strategy eligibility: only the strategies listed below participate in
    # counter-trend ADX gating.  Each strategy can override thresholds via
    # live.yaml → StrategyLineConfig.adx_trending_threshold / adx_mild_trend_threshold.
    _CT_ELIGIBLE: frozenset[str] = frozenset(
        {
            "statarb_dynamic",
            "statarb_m15",
            "m15_swing",
            "m30_swing",
            "h1_swing",
            "h4_swing",
        }
    )

    if regime_info is not None and name in _CT_ELIGIBLE:
        _rg_4c = regime_info.get("regime_gate", {}) if isinstance(regime_info, dict) else {}
        _h1_adx = float(_rg_4c.get("h1_adx") or 0.0)
        _h1_dir_4c = str(_rg_4c.get("h1_trend_direction") or "neutral")
        _primary_dir_4c = str(_rg_4c.get("primary_trend") or "neutral")

        # Read thresholds from strategy config — honors live.yaml 999 thermal fuse
        _block = float(getattr(config, "adx_trending_threshold", 999.0))
        _penalise = float(getattr(config, "adx_mild_trend_threshold", 999.0))
        _h4_block = _block  # H4 uses same global threshold
        _h4_penalise = _penalise

        if _h1_adx > 0 and direction != "neutral" and _primary_dir_4c != "neutral":
            if direction != _primary_dir_4c:
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
                if (
                    _h4_block > 0
                    and _h4_trend_dir != "neutral"
                    and direction != _h4_trend_dir
                    and _h4_adx >= _h4_block
                ):
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
