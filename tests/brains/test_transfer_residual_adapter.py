"""TransferResidualBrainAdapter tests — T30② runtime evaluator (FIX-20260804-005).

Core immunity: the runtime adapter's base+residual composition must be
bit-identical to the training-side ``ResidualTransferLearner.predict()``.
This is the anti-train-serve-fork guarantee — the same ``y = y_A + r`` on the
same boosters and the same 46-dim input must emit the same raw score.

Layout:
  1. Bit-identical (mock boosters)   — deterministic, always runs
  2. Real-artifact integration        — BrainFactory.build on the real Flow46
     config revives the "dead object" (dimension guard no longer trips)
  3. Regression: existing expected_r towers still route to LightGBMBrainAdapter
  4. Fail-closed: missing base artifact / dimension mismatch
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from core.brains.adapters.lightgbm_brain_adapter import LightGBMBrainAdapter
from core.brains.adapters.transfer_residual_brain_adapter import (
    TransferResidualBrainAdapter,
)
from core.brains.services.brain_factory import BrainFactory

FLOW46_CONFIG = Path("configs/brains_btc/BTC_Flow46_V1_SHORT_20260803_120909.json")
V5_CONFIG = Path("configs/brains_btc/BTC_Expected_R_V5_SHORT_20260803_120850.json")
RESIDUAL_META = Path("data_btc/models/btc_flow46_v1/residual_short_best.meta.json")

FLOW_FEATURES = [
    "OFI_M5",
    "OFI_ZScore_20",
    "OFI_Cumulative_Delta",
    "OFI_Delta_Divergence",
    "OFI_Volume_Real_Ratio",
]


def _train_and_save_booster(X: np.ndarray, y: np.ndarray, path: Path) -> None:
    """Tiny deterministic LightGBM booster (regression) saved to path."""
    import lightgbm as lgb

    ds = lgb.Dataset(X, label=y)
    bst = lgb.train(
        {
            "objective": "regression",
            "num_leaves": 4,
            "max_depth": 3,
            "learning_rate": 0.1,
            "verbosity": -1,
            "seed": 42,
        },
        ds,
        num_boost_round=8,
    )
    bst.save_model(str(path))


def _flow46_entry(residual_path: Path, base_path: Path) -> dict:
    return {
        "schema_version": "brain_registry_entry.v1",
        "brain_id": "TEST_Flow46",
        "brain_type": "expected_r_short",
        "feature_schema_id": "btc_macro_flow_46",
        "artifact_path": str(residual_path),
        "artifact_hash": "test",
        "status": "shadow",
        "vote_weight": 0.0,
        "magic": 999999,
        "features": [f"f{i:02d}" for i in range(46)],
        "training_params": {"objective": "expected_r_short"},
        "activation_threshold": 0.15,
        "transfer": {
            "kind": "freeze_and_residual",
            "frozen_base_artifact_path": str(base_path),
        },
    }


# ────────────────────────────────────────────────────────────────────────────
# 1. Bit-identical — mock boosters (deterministic, always runs)
# ────────────────────────────────────────────────────────────────────────────


class TestBitIdenticalMock:
    def test_infer_matches_training_side(self, tmp_path: Path) -> None:
        from core.training.transfer_adapter import (
            FrozenBaseModel,
            ResidualTransferLearner,
        )

        rng = np.random.default_rng(42)
        X_base = rng.normal(size=(200, 41))
        y_base = 2.0 * X_base[:, 0] + 0.5 * X_base[:, 5] - 1.0 * X_base[:, 10]
        X_flow = rng.normal(size=(200, 5))
        y_flow = 1.5 * X_flow[:, 0] - 0.5 * X_flow[:, 2]

        base_path = tmp_path / "tower_test.txt"
        residual_path = tmp_path / "residual_test.txt"
        _train_and_save_booster(X_base, y_base, base_path)
        _train_and_save_booster(X_flow, y_flow, residual_path)

        adapter = TransferResidualBrainAdapter(_flow46_entry(residual_path, base_path))
        adapter.load()
        assert adapter._backend == "transfer:freeze_and_residual"
        assert adapter._num_features == 46

        # Training-side composition (model_file= load) vs runtime (model_str= load)
        base = FrozenBaseModel.from_file(base_path, base_id="mock-base")
        learner = ResidualTransferLearner(
            base=base,
            flow_feature_names=FLOW_FEATURES,
            residual_path=residual_path,
        )

        for _ in range(5):
            X46 = rng.normal(size=46)
            raw = adapter.infer(X46)
            assert not raw["fallback"], "dimension guard must not trip on 46-dim"
            train_pred = float(learner.predict(X46)[0])
            # bit-identical: same boosters, same slice, same composition
            assert abs(raw["raw_score"] - train_pred) < 1e-9
            # decomposition is exposed for repairability
            assert abs(raw["base_score"] + raw["residual_score"] - raw["raw_score"]) < 1e-9

    def test_run_path_builds_46dim_vector(self, tmp_path: Path) -> None:
        """run() extracts 46 features from a source dict and composes normally."""
        from core.training.transfer_adapter import FrozenBaseModel, ResidualTransferLearner

        rng = np.random.default_rng(7)
        X_base = rng.normal(size=(150, 41))
        X_flow = rng.normal(size=(150, 5))
        base_path = tmp_path / "tower_run.txt"
        residual_path = tmp_path / "residual_run.txt"
        _train_and_save_booster(X_base, X_base[:, 0] * 2.0, base_path)
        _train_and_save_booster(X_flow, X_flow[:, 1] - X_flow[:, 3], residual_path)

        adapter = TransferResidualBrainAdapter(_flow46_entry(residual_path, base_path))
        adapter.load()

        vec = rng.normal(size=46)
        feature_source = {f"f{i:02d}": float(v) for i, v in enumerate(vec)}
        signal = adapter.run(None, feature_source=feature_source)
        assert not signal.fallback

        base = FrozenBaseModel.from_file(base_path, base_id="mock-base")
        learner = ResidualTransferLearner(
            base=base, flow_feature_names=FLOW_FEATURES, residual_path=residual_path
        )
        assert abs(signal.raw_score - float(learner.predict(vec)[0])) < 1e-9


# ────────────────────────────────────────────────────────────────────────────
# 2. Real-artifact integration — the Flow46 "dead object" is revived
# ────────────────────────────────────────────────────────────────────────────


def _real_artifacts_present() -> bool:
    return all(p.exists() for p in (FLOW46_CONFIG, V5_CONFIG, RESIDUAL_META))


pytestmark = pytest.mark.skipif(
    not _real_artifacts_present(),
    reason="real BTC artifacts not present (runtime data, not in VCS)",
)


class TestRealArtifactIntegration:
    def test_brain_factory_revives_flow46(self) -> None:
        entry = json.loads(FLOW46_CONFIG.read_text(encoding="utf-8"))
        adapter = BrainFactory().build(entry)
        assert type(adapter) is TransferResidualBrainAdapter
        assert adapter._num_features == 46
        assert adapter._backend == "transfer:freeze_and_residual"

        # Non-zero, non-fallback composition on a deterministic 46-dim input.
        vec = np.linspace(-0.1, 0.1, 46)
        raw = adapter.infer(vec)
        assert not raw["fallback"]
        assert np.isfinite(raw["raw_score"])
        assert abs(raw["base_score"] + raw["residual_score"] - raw["raw_score"]) < 1e-9

    def test_real_artifacts_bit_identical(self) -> None:
        from core.training.transfer_adapter import FrozenBaseModel, ResidualTransferLearner

        entry = json.loads(FLOW46_CONFIG.read_text(encoding="utf-8"))
        adapter = BrainFactory().build(entry)

        meta = json.loads(RESIDUAL_META.read_text(encoding="utf-8"))
        flow_names = meta["flow_feature_names"]
        base = FrozenBaseModel.from_file(
            entry["transfer"]["frozen_base_artifact_path"], base_id="real-base"
        )
        learner = ResidualTransferLearner(
            base=base, flow_feature_names=flow_names, residual_path=entry["artifact_path"]
        )

        for vec in (np.linspace(-0.2, 0.2, 46), np.linspace(0.0, 0.1, 46)):
            raw = adapter.infer(vec)
            assert not raw["fallback"]
            assert abs(raw["raw_score"] - float(learner.predict(vec)[0])) < 1e-9

    def test_get_signal_short_only_path5(self) -> None:
        """expected_r_short objective → Path 5: raw_score > thr votes SHORT only."""
        entry = json.loads(FLOW46_CONFIG.read_text(encoding="utf-8"))
        adapter = BrainFactory().build(entry)

        signal = adapter.get_signal({"raw_score": 0.5, "fallback": False})
        assert signal.direction == "short"
        signal_neutral = adapter.get_signal({"raw_score": -0.5, "fallback": False})
        assert signal_neutral.direction == "neutral"  # never votes LONG

    # ── Regression: existing expected_r towers still route to LightGBM ──
    def test_v5_tower_still_routes_to_lightgbm(self) -> None:
        entry = json.loads(V5_CONFIG.read_text(encoding="utf-8"))
        adapter = BrainFactory().build(entry)
        assert type(adapter) is LightGBMBrainAdapter
        assert adapter._num_features == 41


# ────────────────────────────────────────────────────────────────────────────
# 3. Fail-closed paths
# ────────────────────────────────────────────────────────────────────────────


class TestFailClosed:
    def test_missing_base_artifact_fail_closed(self, tmp_path: Path) -> None:
        entry = _flow46_entry(tmp_path / "residual_absent.txt", tmp_path / "base_absent.txt")
        adapter = TransferResidualBrainAdapter(entry)
        adapter.load()
        assert adapter._backend.startswith("stub")
        assert adapter._num_features is None
        raw = adapter.infer(np.zeros(46, dtype=np.float64))
        assert raw["fallback"] is True

    def test_dimension_mismatch_fallback(self, tmp_path: Path) -> None:
        rng = np.random.default_rng(3)
        X_base = rng.normal(size=(150, 41))
        X_flow = rng.normal(size=(150, 5))
        base_path = tmp_path / "tower_dim.txt"
        residual_path = tmp_path / "residual_dim.txt"
        _train_and_save_booster(X_base, X_base[:, 0], base_path)
        _train_and_save_booster(X_flow, X_flow[:, 1], residual_path)

        adapter = TransferResidualBrainAdapter(_flow46_entry(residual_path, base_path))
        adapter.load()
        assert adapter._num_features == 46

        # non-zero 41-dim → dimension guard (zero-vector guard must not pre-empt)
        raw = adapter.infer(np.ones(41, dtype=np.float64))
        assert raw["fallback"] is True
        assert "dim_mismatch" in raw["fallback_reason"]
