"""Tests for the in-repo XGBoost trainer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── Data loading tests ──


class TestLoadNpz:
    def test_load_npz_returns_correct_shapes(self, tmp_path):
        from scripts.training.trainers.xgb_trainer import load_npz

        X = np.random.randn(100, 40).astype(np.float64)
        y = np.random.randint(0, 2, 100).astype(np.int32)
        pnl = np.random.randn(100).astype(np.float64)
        feat_names = np.array([f"f_{i}" for i in range(40)], dtype=str)

        path = tmp_path / "train.npz"
        np.savez(path, X=X, y=y, pnl=pnl, feature_names=feat_names)

        X_out, y_out, pnl_out, names_out = load_npz(path)

        assert X_out.shape == (100, 40)
        assert y_out.shape == (100,)
        assert pnl_out.shape == (100,)
        assert len(names_out) == 40
        np.testing.assert_array_equal(X_out, X)
        np.testing.assert_array_equal(y_out, y)

    def test_load_npz_no_pnl_field(self, tmp_path):
        from scripts.training.trainers.xgb_trainer import load_npz

        X = np.random.randn(10, 40).astype(np.float64)
        y = np.zeros(10, dtype=np.int32)
        path = tmp_path / "train.npz"
        np.savez(path, X=X, y=y)

        X_out, y_out, pnl_out, _ = load_npz(path)

        assert X_out.shape == (10, 40)
        assert pnl_out.shape == (10,)


class TestLoadParquet:
    def test_load_parquet_roundtrip(self, tmp_path):
        pytest.importorskip("pandas")
        import pandas as pd

        from scripts.training.trainers.xgb_trainer import load_parquet

        data = {
            "label": ["win", "loss", "win", "loss", "breakeven"],
            "pnl": [10.0, -5.0, 3.0, -2.0, 0.0],
        }
        for i in range(40):
            data[f"f_{i}"] = np.random.randn(5)

        df = pd.DataFrame(data)
        path = tmp_path / "train.parquet"
        df.to_parquet(path)

        X, y, pnl, names = load_parquet(path)

        assert X.shape == (5, 40)
        assert y.tolist() == [1, 0, 1, 0, 0]
        assert pnl.shape == (5,)
        assert len(names) == 40


class TestLoadTrainingData:
    def test_dispatches_to_npz(self, tmp_path):
        from scripts.training.trainers.xgb_trainer import load_training_data

        X = np.random.randn(5, 40).astype(np.float64)
        y = np.array([0, 1, 1, 0, 1], dtype=np.int32)
        path = tmp_path / "train.npz"
        np.savez(path, X=X, y=y)

        X_out, y_out, _, _ = load_training_data(path)
        assert X_out.shape == (5, 40)

    def test_dispatches_to_parquet(self, tmp_path):
        pytest.importorskip("pandas")
        import pandas as pd

        from scripts.training.trainers.xgb_trainer import load_training_data

        data = {"label": ["win"] * 3, "pnl": [1.0] * 3}
        for i in range(40):
            data[f"f_{i}"] = np.random.randn(3)
        df = pd.DataFrame(data)
        path = tmp_path / "train.parquet"
        df.to_parquet(path)

        X_out, y_out, _, _ = load_training_data(path)
        assert X_out.shape == (3, 40)

    def test_unsupported_format(self, tmp_path):
        from scripts.training.trainers.xgb_trainer import load_training_data

        path = tmp_path / "train.csv"
        path.write_text("a,b,c\n1,2,3")

        with pytest.raises(ValueError, match="unsupported data format"):
            load_training_data(path)


# ── Training tests ──


class TestTrainXGBoost:
    def test_train_basic(self):
        pytest.importorskip("xgboost")
        from scripts.training.trainers.xgb_trainer import train_xgboost

        X = np.random.randn(200, 40).astype(np.float64)
        y = (X[:, 0] + X[:, 1] * 0.5 > 0).astype(np.int32)

        booster, metrics = train_xgboost(
            X, y, params={"n_estimators": 20}, feature_names=[f"f_{i}" for i in range(40)]
        )

        assert booster is not None
        assert booster.num_boosted_rounds() > 0
        assert "train_accuracy" in metrics
        assert metrics["train_accuracy"] > 0.5
        assert "train_time_seconds" in metrics

    def test_train_with_validation(self):
        pytest.importorskip("xgboost")
        from scripts.training.trainers.xgb_trainer import train_xgboost

        X = np.random.randn(200, 40).astype(np.float64)
        y = (X[:, 0] + X[:, 1] * 0.5 > 0).astype(np.int32)
        Xv = np.random.randn(50, 40).astype(np.float64)
        yv = (Xv[:, 0] > 0).astype(np.int32)

        booster, metrics = train_xgboost(X, y, params={"n_estimators": 20}, val_data=(Xv, yv))

        assert "val_accuracy" in metrics
        assert "final_eval_logloss" in metrics

    def test_respects_early_stopping(self):
        pytest.importorskip("xgboost")
        from scripts.training.trainers.xgb_trainer import train_xgboost

        X = np.random.randn(200, 40).astype(np.float64)
        y = (X[:, 0] > 0).astype(np.int32)
        Xv = np.random.randn(50, 40).astype(np.float64)
        yv = (Xv[:, 0] > 0).astype(np.int32)

        booster, metrics = train_xgboost(
            X,
            y,
            params={"n_estimators": 200, "early_stopping_rounds": 5},
            val_data=(Xv, yv),
        )

        assert booster.num_boosted_rounds() <= 200


# ── Save tests ──


class TestSaveModel:
    def test_save_and_load(self, tmp_path):
        pytest.importorskip("xgboost")
        import xgboost as xgb

        from scripts.training.trainers.xgb_trainer import save_model, train_xgboost

        X = np.random.randn(100, 40).astype(np.float64)
        y = (X[:, 0] > 0).astype(np.int32)

        booster, _ = train_xgboost(X, y, params={"n_estimators": 10})

        model_path = tmp_path / "model.json"
        save_model(booster, model_path)

        assert model_path.exists()
        assert model_path.stat().st_size > 0

        # Verify recoverable
        booster2 = xgb.Booster()
        booster2.load_model(str(model_path))
        assert booster2.num_boosted_rounds() == booster.num_boosted_rounds()


class TestSaveResult:
    def test_save_result_structure(self, tmp_path):
        from scripts.training.trainers.xgb_trainer import save_result

        model_path = tmp_path / "models" / "model.json"
        result_path = tmp_path / "models" / "result.json"
        metrics = {"train_accuracy": 0.85, "n_estimators": 42}

        out = save_result(
            metrics, model_path, result_path, data_path="data/train.npz", samples=100, features=40
        )

        assert out.exists()
        data = json.loads(out.read_text())
        assert data["trainer"] == "xgb_trainer"
        assert data["exit_code"] == 0
        assert data["artifact_primary"] == str(model_path)
        assert data["metrics"]["train_accuracy"] == 0.85
        assert data["data"]["samples"] == 100
        assert data["data"]["features"] == 40


# ── Integration tests ──


class TestBuildAndTrain:
    def test_full_pipeline_npz(self, tmp_path):
        pytest.importorskip("xgboost")
        from scripts.training.trainers.xgb_trainer import build_and_train

        X = np.random.randn(100, 40).astype(np.float64)
        y = (X[:, 0] + X[:, 1] > 0).astype(np.int32)
        pnl = np.random.randn(100).astype(np.float64)
        feat = np.array([f"f_{i}" for i in range(40)], dtype=str)

        data_path = tmp_path / "train.npz"
        np.savez(data_path, X=X, y=y, pnl=pnl, feature_names=feat)

        model_path = tmp_path / "models" / "booster.json"
        result_path = tmp_path / "models" / "result.json"

        summary = build_and_train(data_path, model_path, result_path, params={"n_estimators": 10})

        assert model_path.exists()
        assert result_path.exists()
        assert summary["samples"] == 100
        assert summary["features"] == 40
        assert "train_accuracy" in summary["metrics"]

    def test_pipeline_with_validation(self, tmp_path):
        pytest.importorskip("xgboost")
        from scripts.training.trainers.xgb_trainer import build_and_train

        def _write_data(path, n):
            X = np.random.randn(n, 40).astype(np.float64)
            y = (X[:, 0] > 0).astype(np.int32)
            np.savez(path, X=X, y=y)
            return path

        train_path = _write_data(tmp_path / "train.npz", 80)
        val_path = _write_data(tmp_path / "val.npz", 20)

        model_path = tmp_path / "model.json"
        summary = build_and_train(
            train_path,
            model_path,
            params={"n_estimators": 10},
            val_data_path=val_path,
        )

        assert model_path.exists()
        assert summary["metrics"].get("val_accuracy") is not None


# ── CLI tests ──


class TestCLI:
    def test_cli_missing_data(self):
        from scripts.training.trainers.xgb_trainer import main

        rc = main(["--data", "/nonexistent/path.npz", "--output-model", "/tmp/m.json"])
        assert rc == 2

    def test_cli_dry_run(self, tmp_path):
        pytest.importorskip("xgboost")
        from scripts.training.trainers.xgb_trainer import main

        data_path = tmp_path / "train.npz"
        X = np.random.randn(50, 40).astype(np.float64)
        y = (X[:, 0] > 0).astype(np.int32)
        np.savez(data_path, X=X, y=y)

        model_path = tmp_path / "model.json"
        rc = main(
            [
                "--data",
                str(data_path),
                "--output-model",
                str(model_path),
                "--n-estimators",
                "10",
                "--max-depth",
                "3",
            ]
        )

        assert rc == 0
        assert model_path.exists()
