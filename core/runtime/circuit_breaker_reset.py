"""Circuit breaker unified auto-reset — Strangler Fig #31 from live_cycle.py.

Extracted from live_cycle.py (~66 lines).  Implements DQAF-20260608-003:
resets ALL degradation counters when cooldown elapses and all trigger
conditions are clear.  Previously only cleared _consecutive_degraded_cycles,
causing surviving counters from non-reset paths to immediately re-trip.
"""

from __future__ import annotations

import json
import time as _time
from typing import Any

from core.runtime.time_utils import _utc_iso


def _emit(event: str, /, **fields: Any) -> None:
    """Emit a structured JSON event to stdout."""
    payload: dict[str, Any] = {"event": event, "time": _utc_iso()}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def auto_reset_circuit_breaker(
    config: Any,
    state: Any,
    *,
    degraded_wakeup: bool = False,
    cycle_duration: float = 0.0,
) -> None:
    """Attempt to auto-reset the circuit breaker if conditions are clear.

    Handles two cases:
    1. Budget-breached trips — reset when all strategy budgets have
       recovered (cross-day reset).
    2. All other trips — reset when cooldown has elapsed AND bridge is
       alive AND cycle is not stalled AND no degradation wakeup.

    On reset, ALL degradation counters are cleared to prevent immediate
    re-trip from surviving counters (DQAF-003 root-cause fix).

    Args:
        config: LiveCycleConfig.
        state: LiveCycleState, mutated to clear circuit breaker fields
            and degradation counters.
        degraded_wakeup: Whether current cycle detected degradation.
        cycle_duration: Current cycle duration in seconds.
    """
    # Budget-breached trips: immune to auto-reset, wait for cross-day
    if state._circuit_breaker_tripped and state._circuit_breaker_trip_reason == "budget_breached":
        _any_paused = False
        for _strat in getattr(state, "_strategies", {}).values():
            _budget = getattr(_strat, "budget", None)
            if _budget is not None and _budget.check_pause():
                _any_paused = True
                break
        if not _any_paused:
            state._circuit_breaker_tripped = False
            state._circuit_breaker_tripped_at = 0.0
            state._circuit_breaker_trip_reason = ""
            state.block_new_entries = False

    # ── Compute clearance signals once (shared by tripped + untripped branches) ──
    _bridge_alive = (
        _time.time() - state._last_bridge_ack_time
    ) <= config.max_bridge_silence_seconds
    _not_stalled = cycle_duration <= config.cycle_stall_threshold_seconds
    _not_degraded = not degraded_wakeup

    if state._circuit_breaker_tripped and state._circuit_breaker_trip_reason != "budget_breached":
        _cooldown_elapsed = (
            _time.time() - state._circuit_breaker_tripped_at
        ) > config.circuit_breaker_cooldown_seconds
        if _cooldown_elapsed and _bridge_alive and _not_stalled and _not_degraded:
            _prev_reason = state._circuit_breaker_trip_reason
            _emit(
                "circuit_breaker_reset",
                reason="cooldown_elapsed_all_conditions_clear",
                tripped_duration_seconds=round(
                    _time.time() - state._circuit_breaker_tripped_at, 1
                ),
                previous_consecutive_degraded=state._consecutive_degraded_cycles,
                previous_consecutive_stale_cycles=state._consecutive_stale_cycles,
                previous_consecutive_stale_features=state._consecutive_stale_features,
                previous_trip_reason=_prev_reason,
            )
            state._circuit_breaker_tripped = False
            state._circuit_breaker_tripped_at = 0.0
            state._circuit_breaker_trip_reason = ""
            state._consecutive_degraded_cycles = 0
            state._consecutive_stale_cycles = 0
            state._consecutive_stale_features = 0
    elif (
        _bridge_alive
        and _not_stalled
        and _not_degraded
        and state._consecutive_degraded_cycles > 0
    ):
        # FIX-20260620-020: previously reset on ``not degraded_wakeup`` alone,
        # which was true every cycle even while bridge_silence persisted.
        # Now reset only when all degradation sources have cleared.
        state._consecutive_degraded_cycles = 0
