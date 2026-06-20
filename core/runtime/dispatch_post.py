"""Dispatch post-processing — Strangler Fig #33 from live_cycle.py.

Extracted from live_cycle.py (~54 lines).  Records family entries for
dispatched positions and logs brain outcomes for outcome tracking.
"""

from __future__ import annotations

import json
import time as _time
from typing import Any

from core.runtime.order_dispatch import _record_brain_outcomes
from core.runtime.time_utils import _utc_iso


def _emit(event: str, /, **fields: Any) -> None:
    """Emit a structured JSON event to stdout."""
    payload: dict[str, Any] = {"event": event, "time": _utc_iso()}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def process_dispatch_results(
    dispatch_results: list[Any],
    state: Any,
    strategies: dict[str, Any],
    feature_vector: Any,
    micro_feature_vector: Any,
    mid_price: float | None,
    daily_feature_vector: Any = None,
    tracker: Any = None,
    symbol: str = "",
) -> None:
    """Post-process dispatch results: record family entries + log brain outcomes.

    Args:
        dispatch_results: List of DispatchResult from execution queue flush.
        state: LiveCycleState, reads ``_family_entry_tracker``.
        strategies: Dict of strategy_name → strategy instance.
        feature_vector: Feature vector for brain inference replay.
        micro_feature_vector: Micro feature vector.
        mid_price: Current mid price.
        daily_feature_vector: Daily feature vector (24h).
        tracker: Shadow tracker for brain outcome recording.
        symbol: Trading symbol.
    """
    # ── Record family entries ──
    if state._family_entry_tracker is not None:
        from core.execution.pre_trade_guards import strategy_to_family

        for dr in dispatch_results:
            if dr.dispatched and dr.direction in ("long", "short"):
                _fam = strategy_to_family(dr.strategy_name)
                if _fam != dr.strategy_name:
                    state._family_entry_tracker.record_entry(
                        family=_fam,
                        direction=dr.direction,
                        timestamp=_time.time(),
                    )
                    _emit(
                        "family_entry_recorded",
                        strategy=dr.strategy_name,
                        family=_fam,
                        direction=dr.direction,
                    )

    # ── Log brain outcomes ──
    for dr in dispatch_results:
        if dr.dispatched and dr.strategy_name in strategies:
            strategy = strategies[dr.strategy_name]
            try:
                strategy_proposals = strategy._run_inference(
                    feature_vector,
                    micro_feature_vector,
                    mid_price,
                    daily_feature_vector=daily_feature_vector,
                )
                _record_brain_outcomes(
                    strategy_proposals, dr.direction, "pending", tracker,
                    symbol=symbol,
                )
            except Exception as _bi_exc:  # BLE001:FOG_DEFERRED
                _emit(
                    "brain_inference_failed",
                    strategy=dr.strategy_name,
                    error=f"{type(_bi_exc).__name__}: {str(_bi_exc)[:200]}",
                    level="DEGRADE",
                )
