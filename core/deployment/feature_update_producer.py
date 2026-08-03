"""Feature update producer — wraps V9LiveFeatureComputer for IncrementalFeatureUpdateJob.

An IncrementalFeatureUpdateJob needs a producer callable:
    Callable[[datetime | None], Iterable[FeatureRecord]]

This module provides ``produce_from_live_computer`` which satisfies that
contract by calling V9LiveFeatureComputer.compute_all() for each call.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES
from core.features.store_contracts import FeatureRecord, FeatureSchema


def produce_from_live_computer(
    computer,  # V9LiveFeatureComputer (or compatible)
    schema: FeatureSchema,
    symbol: str,
    *,
    feature_names: tuple[str, ...] | None = None,
    values_provider=None,
) -> Iterable[FeatureRecord]:
    """Yield a single FeatureRecord per call — no historical backfill.

    This is designed for periodic (e.g. every 60s) incremental updates.
    Each call computes all features from the current MT5 snapshot and
    yields exactly one record.

    Phase 4 / M2 (FIX-20260803-005): generalized to non-v9 schemas.  The
    default v9 40-dim vector (M5_*/M15_*/M30_*/H1_* + OU/Hurst) is a complete
    multi-timeframe holographic snapshot — every M5 record already carries the
    most recent M15/M30/H1 slice.  No timeframe splitting is needed.

    Args:
        computer: Live feature computer with ``compute_all()``.
        schema: Target FeatureSchema (dictates field names unless overridden).
        symbol: MT5 symbol.
        feature_names: Optional field-name override (default: v9 40-dim names).
        values_provider: Optional callable returning a dict of feature values.
            When set, it replaces ``computer.compute_all()`` — used by BTC
            paths whose 41-dim vector comes from the shared pure assembly
            (``BTCFeatureAugmenter.augment``) rather than the v9 computer.
    """
    field_names = feature_names if feature_names is not None else V9_INSTITUTIONAL_40_FEATURES
    features = values_provider() if values_provider is not None else computer.compute_all()
    # ── FIX-20260717-018: All-zero guard ──
    # When MT5 data is unavailable (bridge restart, symbol not in Market Watch),
    # the live computer returns all zeros via _fill_zeros().  Writing all-zero
    # records to the feature store pollutes offline training and OOD
    # calibration datasets.  micro_persist.py L34-38 has the same guard.
    _all_zero = all(abs(float(features.get(name, 0.0))) < 1e-15 for name in field_names)
    if _all_zero:
        return
    event_time = datetime.now(UTC).replace(tzinfo=None)
    yield FeatureRecord(
        schema_name=schema.name,
        schema_version=schema.version,
        symbol=symbol,
        timeframe=schema.timeframe,
        event_time=event_time,
        values={name: features.get(name, 0.0) for name in field_names},
        source="mt5_live",
        ingested_at=event_time,
    )


def build_v9_schema(symbol: str, timeframe: str = "M5") -> FeatureSchema:
    """Build the standard V9 institutional feature schema."""
    return FeatureSchema(
        name="v9_institutional_40",
        version="1.0.0",
        fields=tuple(V9_INSTITUTIONAL_40_FEATURES),
        symbol=symbol,
        timeframe=timeframe,
        description="V9 Institutional Survival model — 40 features across M5/M15/M30/H1",
    )
