"""Micro feature persistence — Strangler Fig #34 from live_cycle.py.

Extracted from live_cycle.py (~62 lines).  Writes 9 microstructure features
to the local feature store, matching the co-timestamp of the v9 record just
written by FeatureService.  Skips all-zero records (MT5 not ready yet).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def persist_micro_features(
    config: Any,
    micro_features: dict[str, float] | None,
) -> None:
    """Persist microstructure features to the local feature store.

    Filters to only the 9 registered MICROSTRUCTURE_9_FEATURES fields.
    Skips write when all values are zero (MT5 tick data not ready).
    Uses the same event_time as the v9 record for co-timestamp matching.

    Args:
        config: LiveCycleConfig with ``feature_store_dir`` and ``symbol``.
        micro_features: Dict of micro feature name → value from
            MicrostructureFeatureComputer.compute_all().
    """
    if not micro_features:
        return

    from core.features.schemas.microstructure_schema import MICROSTRUCTURE_9_FEATURES

    _all_zero = all(
        abs(float(micro_features.get(fn, 0.0))) < 1e-15
        for fn in MICROSTRUCTURE_9_FEATURES
    )
    if _all_zero:
        return

    try:
        from core.features.local_feature_store import LocalFeatureStore
        from core.features.store_contracts import FeatureRecord

        _micro_store = LocalFeatureStore(config.feature_store_dir)
        _micro_version = _micro_store.resolve_version(
            schema_name="v4.3_microstructure_9",
            symbol=config.symbol,
            timeframe="M5",
        )
        if _micro_version is not None:
            _now = datetime.now(UTC).replace(tzinfo=None)
            _latest_v9 = _micro_store.latest(
                config.symbol, "M5", schema_name="v9_institutional_40"
            )
            if _latest_v9 is not None and _latest_v9.event_time is not None:
                _now = _latest_v9.event_time
            _micro_values = {
                fn: float(micro_features.get(fn, 0.0))
                for fn in MICROSTRUCTURE_9_FEATURES
            }
            _micro_store.write_records(
                [
                    FeatureRecord(
                        schema_name="v4.3_microstructure_9",
                        schema_version=_micro_version,
                        symbol=config.symbol,
                        timeframe="M5",
                        event_time=_now,
                        values=_micro_values,
                        source="mt5_live",
                        ingested_at=_now,
                    )
                ]
            )
    except Exception:  # BLE001:REVIEWED
        pass  # best-effort — micro store write must not block cycle
