"""Feature freshness gate — Strangler Fig #29 from live_cycle.py.

Extracted from live_cycle.py:execute_live_cycle() (~50 lines).
Checks M5 feature store freshness and trips circuit breaker on
consecutive stale features (>=3 cycles).
"""

from __future__ import annotations

import json
import time as _time
from datetime import UTC
from typing import Any

from core.runtime.fault_handler import log_and_continue
from core.runtime.time_utils import _utc_iso


def _emit(event: str, /, **fields: Any) -> None:
    """Emit a structured JSON event to stdout."""
    payload: dict[str, Any] = {"event": event, "time": _utc_iso()}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def check_feature_freshness(
    config: Any,
    state: Any,
    feature_store: Any,
) -> None:
    """Check M5 feature freshness and trip circuit breaker if stale.

    Reads the latest M5 record from the feature store, checks its age
    against a 300-second threshold, and tracks consecutive stale cycles.
    After 3 consecutive stale cycles, trips the circuit breaker into
    management-only mode.

    Args:
        config: LiveCycleConfig (reads ``no_mt5``).
        state: LiveCycleState, mutated to track
            ``_consecutive_stale_features`` and circuit breaker fields.
        feature_store: FeatureStore with ``.latest()`` method.
    """
    if config.no_mt5 or feature_store is None:
        return

    with log_and_continue(component="FeatureCheck:freshness"):
        from core.execution.pre_trade_guards import check_feature_freshness as _cff

        latest_record = feature_store.latest(config.symbol, "M5")
        if latest_record is not None:
            ts = getattr(latest_record, "event_time", None)
            if ts is not None:
                if hasattr(ts, "timestamp"):
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                    ts_unix = ts.timestamp()
                else:
                    ts_unix = float(ts)
                freshness = _cff(ts_unix, max_age_seconds=300.0)
                if not freshness["fresh"]:
                    state._consecutive_stale_features += 1
                    _emit(
                        "feature_stale_warning",
                        age_seconds=freshness.get("age_seconds"),
                        max_age_seconds=freshness["max_age_seconds"],
                        consecutive_stale_features=state._consecutive_stale_features,
                    )
                    if state._consecutive_stale_features >= 3:
                        state._circuit_breaker_tripped = True
                        state._circuit_breaker_tripped_at = _time.time()
                        state._circuit_breaker_trip_reason = "feature_staleness"
                        _emit(
                            "circuit_breaker_feature_staleness_trip",
                            consecutive_stale_features=state._consecutive_stale_features,
                            trip_reason=state._circuit_breaker_trip_reason,
                            action="management_only_mode",
                        )
                else:
                    state._consecutive_stale_features = 0
