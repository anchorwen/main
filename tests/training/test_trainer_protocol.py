"""Unit tests for training protocol — TrainResult + registry.

Part of Test 4: training dedicated test suite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from core.training.trainer_protocol import TRAINER_REGISTRY, TrainResult, register_trainer


class TestTrainResult:
    def test_create_minimal(self):
        r = TrainResult(
            architecture="xgboost",
            model_path=Path("/tmp/model.xgb"),
            metrics={"auc": 0.85},
            completed_at_utc=datetime.now(UTC).isoformat(),
        )
        assert r.architecture == "xgboost"
        assert r.metrics["auc"] == 0.85

    def test_defaults(self):
        r = TrainResult(
            architecture="lightgbm",
            model_path=Path("/tmp/model.lgb"),
            metrics={},
            completed_at_utc="2026-06-19T00:00:00",
        )
        assert r.n_parameters == 0
        assert r.train_samples == 0
        assert r.val_metrics == {}
        assert r.scaler_path is None
        assert r.extra == {}

    def test_full_creation(self):
        r = TrainResult(
            architecture="deep_res_mlp",
            model_path=Path("/tmp/model.onnx"),
            metrics={"loss": 0.12, "accuracy": 0.91},
            completed_at_utc="2026-06-19T00:00:00",
            n_parameters=15_000,
            train_samples=50_000,
            val_metrics={"val_loss": 0.15},
            scaler_path=Path("/tmp/scaler.pkl"),
            extra={"epochs": 100},
        )
        assert r.n_parameters == 15_000
        assert r.val_metrics["val_loss"] == 0.15
        assert r.scaler_path == Path("/tmp/scaler.pkl")


class TestRegistry:
    def test_register_and_lookup(self):
        @register_trainer("test_arch_v1")
        def _test_trainer(dataset, recipe, output_dir=None):
            return TrainResult(
                architecture="test_arch_v1",
                model_path=Path("/tmp/model"),
                metrics={"score": 1.0},
                completed_at_utc="2026-01-01T00:00:00",
            )

        assert "test_arch_v1" in TRAINER_REGISTRY
        trainer = TRAINER_REGISTRY["test_arch_v1"]
        result = trainer(None, None)
        assert result.architecture == "test_arch_v1"
        assert result.metrics["score"] == 1.0
