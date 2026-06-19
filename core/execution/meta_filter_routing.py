"""MetaFilter gate routing — Strangler Fig extraction from strategy_line.py.

FIX-20260609-008: Extracted from ``StrategyLine.evaluate()`` sections 4ab + 4e.
Unifies the two MetaFilter invocation paths into a single dispatch function.

Section 4ab (statarb/swing): uses entry_z_score * 12.5 as s1_prediction proxy.
Section 4e (barrier_12bar): uses extract_probe_score from brain proposals.

Both paths call meta_filter.filter_arrays() and either return a rejected
StrategyDecision or set _meta_p_win for downstream Kelly sizing.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import numpy as np

from core.execution.strategy_decision import StrategyDecision

logger = logging.getLogger(__name__)


def apply_meta_filter_gate(
    *,
    name: str,
    direction: str,
    confidence: float,
    entry_z_score: float,
    feature_vector: np.ndarray,
    micro_feature_vector: Any,
    meta_filter: Any,
    proposals: list[Any],
    config: Any,
    brain_ids: list[str],
    support_count: int,
    total_count: int,
    regime_gate_mode: str,
    # ── mutable output via return ──
    _meta_p_win: float | None = None,
    # ── OU cold-explore gate state ──
    _last_ou_result: dict[str, Any] | None = None,
    # ── FIX-20260610-007: Direction-specific routing ──
    meta_filter_long: Any = None,
    meta_filter_short: Any = None,
) -> tuple[float | None, StrategyDecision | None]:
    """Apply MetaFilter gate and return (meta_p_win, rejected_decision_or_None).

    Returns (None, StrategyDecision) when MetaFilter rejects the signal.
    Returns (float, None) when MetaFilter approves — the float is p_win for
    downstream Kelly sizing.
    Returns (None, None) when MetaFilter is not applicable (passthrough).

    Covers all strategy routing:
      - statarb_dynamic/m15: z_score * 12.5 proxy (section 4ab)
      - swing (m15/m30/h1/h4/btc): z_score * 12.5 proxy (section 4ab extended)
      - barrier_12bar: Stage 1 probe score from Huber brain (section 4e)
    """
    # ── FIX-20260610-007: Direction-specific routing ──
    # When per-direction models are available, route to the matching one.
    # This captures directional asymmetry (XAU shorts cascade, longs grind).
    _active_filter = meta_filter
    if direction == "long" and meta_filter_long is not None:
        _active_filter = meta_filter_long
    elif direction == "short" and meta_filter_short is not None:
        _active_filter = meta_filter_short

    # ── Section 4ab: statarb + swing MetaFilter routing ──
    if (
        _active_filter is not None
        and (
            "statarb" in name
            or name in ("m15_swing", "m30_swing", "h1_swing", "h4_swing", "btc_swing")
        )
        and _meta_p_win is None
    ):
        try:
            _z_proxy = entry_z_score * 12.5
            _result = _active_filter.filter_arrays(
                direction=direction,
                s1_prediction=_z_proxy,
                v9_array=feature_vector,
                micro_array=micro_feature_vector,
            )
            if not _result.passed:
                # COLD phase exploration bypass
                if _last_ou_result and _last_ou_result.get("force_min_volume"):
                    return None, None  # don't use MetaFilter p_win for cold explore
                print(
                    json.dumps(
                        {
                            "event": "kelly_diag",
                            "time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                            "strategy": name,
                            "stage": "meta_filter_rejected_statarb",
                            "z_score": round(entry_z_score, 4),
                            "z_proxy": round(_z_proxy, 4),
                            "result_p_win": round(float(getattr(_result, "p_win", 0)), 4),
                            "passed": False,
                            "reason": getattr(_result, "reason", None) or "threshold",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return None, StrategyDecision(
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
                    reason=f"meta_filter_rejected_statarb:{getattr(_result, 'reason', 'threshold')}",
                )
            _meta_p_win = float(_result.p_win)
            print(
                json.dumps(
                    {
                        "event": "kelly_diag",
                        "time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        "strategy": name,
                        "stage": "meta_filter_p_win_statarb",
                        "z_score": round(entry_z_score, 4),
                        "z_proxy": round(_z_proxy, 4),
                        "result_p_win": round(_meta_p_win, 4),
                        "passed": True,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return _meta_p_win, None
        except Exception:  # BLE001:REVIEWED
            logger.warning(
                "MetaFilter statarb routing failed for %s: fallthrough to p_win resolution",
                name,
                exc_info=True,
            )

    # ── Section 4e: barrier_12bar Meta-Labeling Gate ──
    if _active_filter is not None and name == "barrier_12bar":
        from core.execution.meta_pipeline import extract_probe_score

        _s1_prediction: float | None = None
        for spec in config.meta_probe_specs:
            _s1 = extract_probe_score(proposals, spec.brain_id)
            if _s1 is not None:
                _s1_prediction = _s1
                break
        if _s1_prediction is None:
            for p in proposals:
                raw = getattr(p, "raw_score", None)
                if raw is not None:
                    _s1_prediction = float(raw)
                    break
                ext = getattr(p, "extensions", None)
                if ext and isinstance(ext, dict):
                    ro = ext.get("raw_outputs", {})
                    if isinstance(ro, dict):
                        raw = ro.get("raw_score")
                        if raw is not None:
                            _s1_prediction = float(raw)
                            break

        if _s1_prediction is not None:
            result = _active_filter.filter_arrays(
                direction=direction,
                s1_prediction=_s1_prediction,
                v9_array=feature_vector,
                micro_array=micro_feature_vector,
            )
            if not result.passed:
                _diag_p_win = getattr(result, "p_win", None)
                print(
                    json.dumps(
                        {
                            "event": "kelly_diag",
                            "time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                            "strategy": name,
                            "stage": "meta_filter_rejected",
                            "s1_prediction": round(_s1_prediction, 6) if _s1_prediction else None,
                            "result_p_win": round(float(_diag_p_win), 4)
                            if _diag_p_win is not None
                            else None,
                            "passed": False,
                            "reason": result.reason if result.reason else "threshold",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return None, StrategyDecision(
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
                    reason=f"meta_filter_rejected:{result.reason}"
                    if result.reason
                    else "meta_filter_rejected",
                )
            _meta_p_win = float(result.p_win)
            print(
                json.dumps(
                    {
                        "event": "kelly_diag",
                        "time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        "strategy": name,
                        "stage": "meta_filter_p_win",
                        "s1_prediction": round(_s1_prediction, 6),
                        "result_p_win": round(_meta_p_win, 4),
                        "passed": result.passed,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return _meta_p_win, None

    return None, None  # passthrough
