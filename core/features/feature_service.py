import json
import logging
import time
from datetime import UTC, datetime

import numpy as np

_logger = logging.getLogger(__name__)

from core.contracts.ids import new_snapshot_id

# Schemas that the current runtime environment can compute.
# Updated when new feature computer implementations are added.
_IMPLEMENTED_SCHEMAS: set[str] = {
    "v9_institutional_40",  # V9LiveFeatureComputer → V9FeatureAdapter
    "v9_40dim_ou3",  # V9 40 + 3 OU physics (assembled by _build_meta_feature_vector)
    "v9_micro_49",  # V9MicroComputer → 40 V9 + 9 micro
    "daily_swing_24",  # DailySwingFeatureComputer
    "swing_24",  # → daily_swing_24 alias
    "v4.3_microstructure_9",  # MicrostructureFeatureComputer
    "v4.5_microstructure_9",  # → v4.3_microstructure_9 alias
    "v2_microstructure_9",  # → v4.3_microstructure_9 alias
    "v2_microstructure_288",  # MicrostructureFeatureComputer × 32
    "swing_enhanced_35",  # 24 swing macro + 9 micro + 2 TF-specific (OU_Theta, Hurst)
    "swing_enhanced_29",  # 21 swing macro + 6 micro + 2 TF (XAU cross-asset removed for BTC)
    "swing_enhanced_21",  # 21 swing macro only — pure daily, no micro/TF
    "btc_macro_enhanced_41",  # FIX-081: BTC 37-dim (AUDJPY, XAU, BTC/XAU ratio + ROC)
    "v6_price_series_1",  # OU Params Z-Score
}

from core.features.schemas.registry import get_schema_dimension as _schema_dimension
from core.features.schemas.registry import get_schema_feature_names as _schema_feature_names


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
        default_symbol: str = "",  # FIX-20260601-044: must be explicit per-symbol
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
        self._last_known_vector: np.ndarray | None = None

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
        self,
        trigger: dict | None = None,
        schema_name: str | None = None,
        timeout_seconds: float = 3.0,
    ) -> np.ndarray:
        """Compute the normalized feature vector via tiered resolution.

        Tier 1 — LocalFeatureStore (warm cache):
          Queries the latest record for `symbol` + `timeframe`.  If found,
          normalizes through self._adapter (when available) or builds a raw
          vector in the requested schema's feature order.

        Tier 2 — Live MT5 computer (timeout-guarded):
          Runs ``compute_all()`` in a worker thread with a *timeout_seconds*
          cap.  If the thread doesn't finish in time the main loop is never
          blocked — the last known good vector (or zeros) is returned.
          A ``feature_compute_timeout`` warning is logged so ops can track
          degrading MT5 latency.

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

                            if hasattr(feature_ts, "timestamp"):
                                if feature_ts.tzinfo is None:
                                    feature_ts = feature_ts.replace(tzinfo=UTC)
                                ts = feature_ts.timestamp()
                            else:
                                ts = float(feature_ts)
                            freshness = check_feature_freshness(ts, max_age_seconds=300.0)
                            if not freshness["fresh"]:
                                logging.warning(
                                    "FeatureService stale cache for %s: age=%.1fs (limit=%.0fs), falling through to live compute",
                                    symbol,
                                    freshness.get("age_seconds", -1),
                                    freshness["max_age_seconds"],
                                )
                                _stale = True
                        except Exception:  # noqa: BLE001
                            logging.warning(
                                "FeatureService freshness check failed for %s — "
                                "forcing live recompute to avoid stale cache",
                                symbol,
                                exc_info=True,
                            )
                            _stale = True
                    if not _stale:
                        if self._adapter is not None:
                            vec = self._adapter.build_model_input(record.values)[0]
                            self._last_known_vector = np.asarray(vec, dtype=np.float32).copy()
                            return self._last_known_vector
                        # Raw vector in schema feature order (no normalization)
                        feat_names = _schema_feature_names(schema)
                        raw = np.asarray(
                            [float(record.values.get(name, 0.0)) for name in feat_names],
                            dtype=np.float32,
                        )
                        self._last_known_vector = np.asarray(raw, dtype=np.float32).copy()
                        return raw
            except Exception:
                logging.exception(
                    "FeatureService failed reading from local store for symbol=%s",
                    symbol,
                )

        # ── Tier 2: Live MT5 computer + adapter (timeout-guarded) ──
        if self._computer is not None and self._adapter is not None:
            import threading

            _compute_result: list[dict[str, float] | None] = [None]
            _compute_error: list[Exception | None] = [None]

            def _run_compute() -> None:
                try:
                    _compute_result[0] = self._computer.compute_all()
                except Exception as exc:  # noqa: BLE001
                    _compute_error[0] = exc

            _t = threading.Thread(target=_run_compute, daemon=True)
            _t0 = time.monotonic()
            _t.start()
            _t.join(timeout=timeout_seconds)
            _elapsed = time.monotonic() - _t0

            if _t.is_alive():
                # ── TIMEOUT: computation blocked too long — don't hold the main loop ──
                logging.error(
                    "FeatureService live compute TIMEOUT after %.1fs (limit=%.1fs) — "
                    "returning %s",
                    _elapsed,
                    timeout_seconds,
                    "last-known vector" if hasattr(self, "_last_known_vector") else "zeros",
                )
                _dur_ms = round(_elapsed * 1000)
                print(
                    json.dumps(
                        {
                            "event": "feature_compute_timeout",
                            "time": datetime.now(UTC).isoformat(),
                            "elapsed_ms": _dur_ms,
                            "timeout_ms": round(timeout_seconds * 1000),
                            "fallback": "last_known"
                            if self._last_known_vector is not None
                            else "zeros",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if self._last_known_vector is not None:
                    return self._last_known_vector.copy()
                return np.zeros(n_features, dtype=np.float64)

            if _compute_error[0] is not None:
                logging.exception(
                    "FeatureService live compute failed: %s",
                    _compute_error[0],
                )
                if self._last_known_vector is not None:
                    return self._last_known_vector.copy()
                return np.zeros(n_features, dtype=np.float64)

            features = _compute_result[0]
            if features is None:
                if self._last_known_vector is not None:
                    return self._last_known_vector.copy()
                return np.zeros(n_features, dtype=np.float64)

            # ── Log compute duration for ops visibility ──
            _dur_ms = round(_elapsed * 1000)
            if _dur_ms > 200:
                logging.info(
                    "FeatureService live compute: %.0f ms (%d features) for %s",
                    _elapsed * 1000,
                    len(features),
                    symbol,
                )

            try:
                # ── Write-back to LocalFeatureStore (best-effort, non-blocking) ──
                if self._store is not None:
                    try:
                        from core.features.store_contracts import FeatureRecord

                        resolved_version = self._store.resolve_version(
                            schema_name=self._store_schema_name,
                            symbol=symbol,
                            timeframe=self._store_timeframe,
                        )
                        if resolved_version is None:
                            logging.warning(
                                "FeatureService write-back skipped: no registered schema "
                                "for name=%s symbol=%s timeframe=%s",
                                self._store_schema_name,
                                symbol,
                                self._store_timeframe,
                            )
                        else:
                            feat_names = _schema_feature_names(schema)
                            persisted = {
                                name: float(features.get(name, 0.0)) for name in feat_names
                            }
                            _event_time = datetime.now(UTC).replace(tzinfo=None)
                            _ingested_at = datetime.now(UTC).replace(tzinfo=None)
                            self._store.write_records(
                                [
                                    FeatureRecord(
                                        schema_name=self._store_schema_name,
                                        schema_version=resolved_version,
                                        symbol=symbol,
                                        timeframe=self._store_timeframe,
                                        event_time=_event_time,
                                        values=persisted,
                                        source="mt5_live",
                                        ingested_at=_ingested_at,
                                    )
                                ]
                            )

                            # ── DQAF-20260614-011: Micro features now written from
                            # live_cycle with real values AND matching event_time.
                            # FeatureService's V9LiveFeatureComputer doesn't produce
                            # micro fields — writing here created zero-valued records
                            # that raced with the real ones from live_cycle.
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
        logging.warning(
            "FeatureService returned ZERO feature vector for symbol=%s — "
            "both LocalFeatureStore and live MT5 computation failed. "
            "Downstream ML brains will receive constant input → FROZEN confidence.",
            symbol,
        )
        try:
            from core.deployment.brain_alert import emit_brain_alert

            emit_brain_alert(
                "feature_service",
                "zero_feature_vector_fallback",
                {"symbol": symbol, "n_features": n_features, "tier": 3},
            )
        except Exception:  # noqa: BLE001
            pass
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
        return [
            e
            for e in self._entries
            if e.get("enabled", True) and e.get("status", "live") not in {"retired", "frozen"}
        ]

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
