import logging
from datetime import UTC, datetime

import numpy as np

from core.contracts.ids import new_snapshot_id
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES
from core.features.schemas.v9_micro_schema import V9_MICRO_49_FEATURES

# Schemas that the current runtime environment can compute.
# Updated when new feature computer implementations are added.
_IMPLEMENTED_SCHEMAS: set[str] = {
    "v9_institutional_40",  # V9LiveFeatureComputer → V9FeatureAdapter
    "v9_micro_49",  # V9MicroComputer → 40 V9 + 9 micro
    "daily_swing_24",  # DailySwingFeatureComputer
    "swing_24",  # → daily_swing_24 alias
    "v4.3_microstructure_9",  # MicrostructureFeatureComputer
    "v4.5_microstructure_9",  # → v4.3_microstructure_9 alias
    "v2_microstructure_9",  # → v4.3_microstructure_9 alias
    "v2_microstructure_288",  # MicrostructureFeatureComputer × 32
    "v6_price_series_1",  # OU Params Z-Score
}

# Schema name → feature count
_SCHEMA_DIMS: dict[str, int] = {
    "v9_institutional_40": 40,
    "v9_micro_49": 49,
}


def _schema_dimension(schema_name: str) -> int:
    return _SCHEMA_DIMS.get(schema_name, 40)


def _schema_feature_names(schema_name: str) -> list[str]:
    """Return the canonical feature name list for a schema."""
    if schema_name == "v9_institutional_40":
        return list(V9_INSTITUTIONAL_40_FEATURES)
    if schema_name == "v9_micro_49":
        return list(V9_MICRO_49_FEATURES)
    return list(V9_INSTITUTIONAL_40_FEATURES)


class FeatureService:
    """Unified feature snapshot service with tiered resolution.

    Resolution order:
      1. LocalFeatureStore (warm cache, populated by update job)
      2. Live MT5 computer + adapter (V9LiveFeatureComputer → V9FeatureAdapter)
      3. Zero-vector stub

    Wraps feature adapters and provides a consistent interface for
    the RuntimeLoop.
    """

    @staticmethod
    def available_schemas() -> set[str]:
        """Return the set of feature schema IDs this runtime can compute.

        Used by BrainIntegrityCheck for capability handshake:
        a brain whose feature_schema_id is NOT in this set is blocked
        from loading, preventing code-vs-model version skew in production.
        """
        return _IMPLEMENTED_SCHEMAS

    def __init__(
        self,
        feature_adapter=None,
        feature_computer=None,
        default_venue: str = "MT5",
        feature_store=None,
        default_symbol: str = "XAUUSD",
        store_schema_name: str = "v9_institutional_40",
        store_timeframe: str = "M5",
    ):
        self._adapter = feature_adapter
        self._computer = feature_computer
        self._default_venue = default_venue
        self._store = feature_store
        self._default_symbol = default_symbol
        self._store_schema_name = store_schema_name
        self._store_timeframe = store_timeframe

    def build_snapshot(self, trigger: dict):
        from apps.engine.runtime_loop import SimpleFeatureSnapshot

        feature_vector = self.build_feature_vector(trigger)

        return SimpleFeatureSnapshot(
            snapshot_id=new_snapshot_id(),
            event_time=datetime.now(UTC).replace(tzinfo=None),
            symbol=trigger.get("symbol", "UNKNOWN"),
            venue=trigger.get("venue", self._default_venue),
            feature_vector=feature_vector,
        )

    def build_feature_vector(
        self, trigger: dict | None = None, schema_name: str | None = None
    ) -> np.ndarray:
        """Compute the normalized feature vector via tiered resolution.

        Tier 1 — LocalFeatureStore (warm cache):
          Queries the latest record for `symbol` + `timeframe`.  If found,
          normalizes through self._adapter (when available) or builds a raw
          vector in the requested schema's feature order.

        Tier 2 — Live MT5 computer:
          Routes to V9LiveFeatureComputer (v9_institutional_40) or
          V9MicroComputer (v9_micro_49) and normalizes through the
          corresponding adapter.

        Tier 3 — Zero-vector stub:
          Returns np.zeros(n_features) as a safe no-op.
        """
        schema = schema_name or self._store_schema_name
        n_features = _schema_dimension(schema)
        symbol = (trigger or {}).get("symbol", self._default_symbol)

        # ── Tier 1: LocalFeatureStore (warm cache) ──
        if self._store is not None:
            try:
                record = self._store.latest(
                    symbol,
                    self._store_timeframe,
                    schema_name=self._store_schema_name,
                )
                if record is not None and record.values:
                    # ── Freshness SLA check ──
                    _stale = False
                    feature_ts = getattr(record, "event_time", None)
                    if feature_ts is not None:
                        try:
                            from core.execution.pre_trade_guards import check_feature_freshness

                            ts = (
                                feature_ts.timestamp()
                                if hasattr(feature_ts, "timestamp")
                                else float(feature_ts)
                            )
                            freshness = check_feature_freshness(ts, max_age_seconds=300.0)
                            if not freshness["fresh"]:
                                logging.warning(
                                    "FeatureService stale cache for %s: age=%.1fs (limit=%.0fs), falling through to live compute",
                                    symbol,
                                    freshness.get("age_seconds", -1),
                                    freshness["max_age_seconds"],
                                )
                                _stale = True
                        except Exception:
                            pass  # freshness check is best-effort
                    if not _stale:
                        if self._adapter is not None:
                            return self._adapter.build_model_input(record.values)[0]
                        # Raw vector in schema feature order (no normalization)
                        feat_names = _schema_feature_names(schema)
                        raw = np.asarray(
                            [float(record.values.get(name, 0.0)) for name in feat_names],
                            dtype=np.float32,
                        )
                        return raw
            except Exception:
                logging.exception(
                    "FeatureService failed reading from local store for symbol=%s",
                    symbol,
                )

        # ── Tier 2: Live MT5 computer + adapter ──
        if self._computer is not None and self._adapter is not None:
            try:
                features = self._computer.compute_all()

                # ── Write-back to LocalFeatureStore (best-effort, non-blocking) ──
                if self._store is not None:
                    try:
                        from core.features.store_contracts import FeatureRecord

                        feat_names = _schema_feature_names(schema)
                        persisted = {name: float(features.get(name, 0.0)) for name in feat_names}
                        self._store.write_records(
                            [
                                FeatureRecord(
                                    schema_name=self._store_schema_name,
                                    schema_version="1.0",
                                    symbol=symbol,
                                    timeframe=self._store_timeframe,
                                    event_time=datetime.now(UTC).replace(tzinfo=None),
                                    values=persisted,
                                    source="mt5_live",
                                    ingested_at=datetime.now(UTC).replace(tzinfo=None),
                                )
                            ]
                        )
                    except Exception:
                        logging.exception(
                            (
                                "FeatureService failed writing computed features"
                                " to store for symbol=%s"
                            ),
                            symbol,
                        )

                model_input = self._adapter.build_model_input(features)
                return model_input[0]  # (n_features,) 1-D
            except Exception:
                logging.exception(
                    "FeatureService live MT5 feature computation failed for symbol=%s",
                    symbol,
                )

        # ── Tier 3: Zero-vector stub ──
        return np.zeros(n_features, dtype=np.float32)


class FeatureBrainRegistry:
    """Manages the set of active brain entries.

    Wraps a list of brain configuration dicts and provides
    ``list_active_entries()`` for BrainRunService.
    """

    def __init__(self, entries: list[dict] | None = None):
        self._entries = list(entries) if entries else []

    def register(self, entry: dict) -> None:
        self._entries.append(entry)

    def remove(self, brain_id: str) -> None:
        self._entries = [e for e in self._entries if e.get("brain_id") != brain_id]

    def list_active_entries(self) -> list[dict]:
        return [e for e in self._entries if e.get("status", "live") not in {"retired", "frozen"}]

    def list_all_entries(self) -> list[dict]:
        return list(self._entries)

    def get_entry(self, brain_id: str) -> dict | None:
        for e in self._entries:
            if e.get("brain_id") == brain_id:
                return e
        return None


class IntentExplainer:
    """Builds reason tags for decision intents."""

    def __init__(self, prefix: str = "v9"):
        self._prefix = prefix

    def build_reason_tags(self, candidate, action: str, side: str) -> list[str]:
        tags = [self._prefix]
        tags.append(action)
        tags.append(side)
        return tags
