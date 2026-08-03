"""BTC feature persistence — write-side fix for the shadow-accumulate → retrain flywheel.

Phase 4 / M2 (FIX-20260803-005, 战役四 — 特征仓写侧修正 / IC 最高批准):

  The feature store write side hardcoded ``store_schema_name="v9_institutional_40"``
  and BTC inference vectors (``btc_macro_enhanced_41`` / ``_41_v2``) NEVER
  persisted → the current schema had 0 records → "shadow accumulate → retrain"
  was impossible.  This module is the ONLY BTC feature-persistence entry point.

  Contract (mirrors ``core/runtime/micro_persist.py``):
    - Column names come from the schema registry (``get_schema_feature_names``) —
      no drift between what the augmenter produces and what lands on disk.
    - All-zero vector → skip (MT5 not ready) — fail-open, never blocks the cycle.
    - Schema not registered in schemas.json → warning + skip (reconcile_store_schemas.py
      owns registration; persist never silently writes unregistered fields).
    - Write failure → logged warning, NOT raised (best-effort telemetry).

  Used by ``live_cycle.py`` right after ``BTCFeatureAugmenter.augment()`` and by
  the future 46-dim (``btc_macro_flow_46``) path via the ``schema_name`` arg.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import numpy as np

_log = logging.getLogger(__name__)

# The canonical write-side schema for live BTC inference vectors.
BTC_PERSIST_SCHEMA = "btc_macro_enhanced_41_v2"
# Matching timeframe used for the store partition (M5 = BTC swing bar clock).
BTC_PERSIST_TIMEFRAME = "M5"


def persist_btc_features(
    config: Any,
    btc_aug_vector: Any,
    *,
    schema_name: str = BTC_PERSIST_SCHEMA,
) -> None:
    """Persist a BTC 41-dim (or 46-dim) inference vector to the local feature store.

    Args:
        config: LiveCycleConfig with ``feature_store_dir`` and ``symbol``.
        btc_aug_vector: The (41,) numpy array from BTCFeatureAugmenter.augment().
        schema_name: Schema to write under (default btc_macro_enhanced_41_v2).

    Fail-open by design: any failure here logs a warning and returns — the
    trading cycle must never be blocked by persistence.
    """
    if btc_aug_vector is None:
        return
    try:
        arr = np.asarray(btc_aug_vector, dtype=np.float64).ravel()
        if arr.size == 0:
            return

        from core.features.schemas.registry import get_schema_feature_names

        names = get_schema_feature_names(schema_name)
        if not names:
            _log.warning(
                "[btc_feature_persist] schema %r has no registered feature names — "
                "skip write (reconcile_store_schemas.py must register it).",
                schema_name,
            )
            return
        if len(names) != arr.size:
            _log.warning(
                "[btc_feature_persist] dimension mismatch: schema %r wants %d fields "
                "but augmenter produced %d — skip write (write-side precision guard).",
                schema_name,
                len(names),
                arr.size,
            )
            return

        # All-zero guard: MT5 not ready / augmenter degraded.  Writing zeros
        # pollutes offline training (same guard as micro_persist L34-38).
        if all(abs(float(v)) < 1e-15 for v in arr):
            return

        from core.features.local_feature_store import LocalFeatureStore
        from core.features.store_contracts import FeatureRecord

        store = LocalFeatureStore(config.feature_store_dir)
        version = store.resolve_version(schema_name, config.symbol, BTC_PERSIST_TIMEFRAME)
        if version is None:
            _log.warning(
                "[btc_feature_persist] schema %r not registered in schemas.json — "
                "skip write. Run scripts/features/reconcile_store_schemas.py first.",
                schema_name,
            )
            return

        _now = datetime.now(UTC).replace(tzinfo=None)
        values = {name: float(arr[i]) for i, name in enumerate(names)}
        store.write_records(
            [
                FeatureRecord(
                    schema_name=schema_name,
                    schema_version=version,
                    symbol=config.symbol,
                    timeframe=BTC_PERSIST_TIMEFRAME,
                    event_time=_now,
                    values=values,
                    source="mt5_live",
                    ingested_at=_now,
                )
            ]
        )
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
        # Best-effort telemetry — a persistence failure must never kill a cycle.
        _log.warning("[btc_feature_persist] write failed (fail-open, cycle continues): %s", exc)
