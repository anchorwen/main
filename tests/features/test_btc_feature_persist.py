"""Phase 4 / M2 hard assertions — 特征仓写侧修正与飞轮启动 (feature store write-side fix).

FIX-20260803-005 / IC 最高批准:
  1. BTC inference vectors (btc_macro_enhanced_41_v2, future 46-dim) must
     persist to the local feature store — unplugging the v9 hardcode that made
     the shadow-accumulate → retrain flywheel impossible.
  2. Write-side precision guard: dimension mismatch / all-zero / unregistered
     schema → fail-open skip (never a wrong-width or polluted record).
  3. reconcile_store_schemas.py repairs the registered field lists from SSOT
     (the R3 evidence: btc_macro_enhanced_41 registered 37 fields, real 41).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from core.features.local_feature_store import LocalFeatureStore
from core.features.schemas.registry import get_schema_feature_names
from core.features.store_contracts import FeatureSchema
from core.runtime.btc_feature_persist import persist_btc_features
from scripts.features.reconcile_store_schemas import reconcile_store_schemas

V2 = "btc_macro_enhanced_41_v2"


class _Cfg:
    def __init__(self, store_dir: str, symbol: str = "BTCUSDc") -> None:
        self.feature_store_dir = store_dir
        self.symbol = symbol


def _register_schema(store_dir: str, name: str = V2, symbol: str = "BTCUSDc") -> None:
    store = LocalFeatureStore(store_dir)
    store.register_schema(
        FeatureSchema(
            name=name,
            version="1.0.0",
            fields=tuple(get_schema_feature_names(name)),
            symbol=symbol,
            timeframe="M5",
            description="test",
        )
    )


# ── 1. persist → read-back bit-identical ──────────────────────────────────


class TestPersistRoundtrip:
    def test_persist_roundtrip_values(self, tmp_path: Path) -> None:
        store_dir = str(tmp_path / "store")
        _register_schema(store_dir)
        vec = np.arange(41, dtype=np.float64) + 1.0  # non-zero, all 41 slots
        persist_btc_features(_Cfg(store_dir), vec)

        store = LocalFeatureStore(store_dir)
        rec = store.latest("BTCUSDc", "M5", schema_name=V2)
        assert rec is not None, "record must be written for a registered schema"
        names = get_schema_feature_names(V2)
        for i, name in enumerate(names):
            assert abs(float(rec.values[name]) - float(vec[i])) < 1e-9, f"slot {name}"

    def test_persist_schema_name_matches_source(self, tmp_path: Path) -> None:
        store_dir = str(tmp_path / "store")
        _register_schema(store_dir)
        vec = np.arange(41, dtype=np.float64) + 1.0
        persist_btc_features(_Cfg(store_dir), vec)
        store = LocalFeatureStore(store_dir)
        rec = store.latest("BTCUSDc", "M5", schema_name=V2)
        assert rec is not None
        assert rec.schema_name == V2  # write-side schema SSOT = BTC_PERSIST_SCHEMA
        assert len(rec.values) == 41

    def test_none_vector_noop(self, tmp_path: Path) -> None:
        store_dir = str(tmp_path / "store")
        _register_schema(store_dir)
        persist_btc_features(_Cfg(store_dir), None)
        store = LocalFeatureStore(store_dir)
        assert store.latest("BTCUSDc", "M5", schema_name=V2) is None


# ── 2. fail-open guards (all-zero / dimension / unregistered) ──────────────


class TestFailOpenGuards:
    def test_all_zero_vector_skipped(self, tmp_path: Path) -> None:
        store_dir = str(tmp_path / "store")
        _register_schema(store_dir)
        persist_btc_features(_Cfg(store_dir), np.zeros(41))
        store = LocalFeatureStore(store_dir)
        assert (
            store.latest("BTCUSDc", "M5", schema_name=V2) is None
        ), "all-zero vector must NOT be persisted (MT5 not ready)"

    def test_dimension_mismatch_skipped(self, tmp_path: Path) -> None:
        store_dir = str(tmp_path / "store")
        _register_schema(store_dir)
        # 40-dim vector against a 41-dim schema → must be skipped (precision guard).
        persist_btc_features(_Cfg(store_dir), np.ones(40))
        store = LocalFeatureStore(store_dir)
        assert store.latest("BTCUSDc", "M5", schema_name=V2) is None

    def test_unregistered_schema_skipped_without_crash(self, tmp_path: Path) -> None:
        store_dir = str(tmp_path / "store")
        # No schema registered at all — persist must fail-open (no exception).
        persist_btc_features(_Cfg(store_dir), np.arange(41, dtype=np.float64) + 1.0)
        store = LocalFeatureStore(store_dir)
        assert store.latest("BTCUSDc", "M5", schema_name=V2) is None


# ── 3. reconcile_store_schemas (R3 evidence: 37→41 drift) ──────────────────


class TestReconcileStoreSchemas:
    def test_registers_missing_v2(self, tmp_path: Path) -> None:
        store_dir = str(tmp_path / "store")
        # Pre-register only the legacy v1 with a WRONG 37-field list (R3 evidence).
        store = LocalFeatureStore(store_dir)
        wrong41 = tuple(f"f{i}" for i in range(37))
        store.register_schema(
            FeatureSchema(
                name="btc_macro_enhanced_41",
                version="1.0.0",
                fields=wrong41,  # wrong: only 37
                symbol="BTCUSDc",
                timeframe="M5",
                description="test",
            )
        )
        report = reconcile_store_schemas(store_dir, schema_names=("btc_macro_enhanced_41_v2",))
        assert report["registered"], "v2 must be newly registered"
        assert len(report["errors"]) == 0
        # Now the persist path resolves the version → write succeeds.
        persist_btc_features(_Cfg(store_dir), np.arange(41, dtype=np.float64) + 1.0)
        rec = store.latest("BTCUSDc", "M5", schema_name=V2)
        assert rec is not None

    def test_repairs_37_to_41(self, tmp_path: Path) -> None:
        store_dir = str(tmp_path / "store")
        store = LocalFeatureStore(store_dir)
        store.register_schema(
            FeatureSchema(
                name="btc_macro_enhanced_41",
                version="1.0.0",
                fields=tuple(f"f{i}" for i in range(37)),  # drifted down
                symbol="BTCUSDc",
                timeframe="M5",
                description="test",
            )
        )
        report = reconcile_store_schemas(store_dir, schema_names=("btc_macro_enhanced_41",))
        assert len(report["repaired"]) == 1
        assert report["repaired"][0]["fields"] == "37 → 41"
        # Re-read: now SSOT-aligned 41 fields.
        store2 = LocalFeatureStore(store_dir)
        schema = next(s for s in store2.list_schemas() if s.name == "btc_macro_enhanced_41")
        assert len(schema.fields) == 41
        assert tuple(schema.fields) == tuple(get_schema_feature_names("btc_macro_enhanced_41"))

    def test_no_drift_is_ok(self, tmp_path: Path) -> None:
        store_dir = str(tmp_path / "store")
        _register_schema(store_dir, name=V2)
        report = reconcile_store_schemas(store_dir, schema_names=(V2,))
        assert report["ok"], "already aligned → ok, no rewrite"
        assert not report["repaired"]
        assert not report["registered"]


# ── 4. produce_from_live_computer values_provider (non-v9 schema) ──────────


class TestProducerValuesProvider:
    def test_values_provider_drives_record(self) -> None:
        from core.deployment.feature_update_producer import produce_from_live_computer
        from core.features.store_contracts import FeatureSchema

        schema = FeatureSchema(
            name="btc_macro_enhanced_41_v2",
            version="1.0.0",
            fields=tuple(get_schema_feature_names(V2)),
            symbol="BTCUSDc",
            timeframe="M5",
            description="test",
        )

        def _provider() -> dict[str, float]:
            names = get_schema_feature_names(V2)
            return {name: float(i + 1) for i, name in enumerate(names)}

        records = list(
            produce_from_live_computer(
                None,  # provider replaces compute_all (computer param unannotated)
                schema,
                "BTCUSDc",
                feature_names=schema.fields,
                values_provider=_provider,
            )
        )
        assert len(records) == 1
        rec = records[0]
        assert rec.schema_name == V2
        assert len(rec.values) == 41
        assert abs(rec.values[get_schema_feature_names(V2)[0]] - 1.0) < 1e-9

    def test_provider_all_zero_returns_nothing(self) -> None:
        from core.deployment.feature_update_producer import produce_from_live_computer
        from core.features.store_contracts import FeatureSchema

        schema = FeatureSchema(
            name=V2,
            version="1.0.0",
            fields=tuple(get_schema_feature_names(V2)),
            symbol="BTCUSDc",
            timeframe="M5",
            description="test",
        )
        records = list(
            produce_from_live_computer(
                None,
                schema,
                "BTCUSDc",
                feature_names=schema.fields,
                values_provider=lambda: {n: 0.0 for n in get_schema_feature_names(V2)},
            )
        )
        assert records == []
