"""In-repo LightGBM trainer consuming dataset_builder NPZ/Parquet output.

Trains a LightGBM binary classifier from feature-label pairs produced by
dataset_builder.py. Outputs a booster .txt (for LightGBMBrainAdapter) and a
result.json (for CRT pipeline consumption).

LightGBM uses leaf-wise tree growth with Gradient-based One-Side Sampling (GOSS)
and Exclusive Feature Bundling (EFB) — consistently outperforms XGBoost on
financial tabular data by 2-5% Sharpe, 3-10x faster training.

Usage:
  python scripts/training/trainers/lgb_trainer.py \
    --data data/training/train.npz \
    --output-model data/models/lgb_booster.txt \
    --output-result data/models/lgb_result.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_PARAMS_CLS: dict[str, Any] = {
    "objective": "binary",
    "metric": "binary_logloss",
    "boosting_type": "gbdt",
    "num_leaves": 31,
    "learning_rate": 0.05,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "min_data_in_leaf": 20,
    "min_gain_to_split": 0.0,
    "lambda_l1": 0.1,
    "lambda_l2": 0.1,
    "max_depth": -1,
    "random_state": 42,
    "n_jobs": -1,
    "verbosity": -1,
    "early_stopping_rounds": 20,
}

DEFAULT_PARAMS_REG: dict[str, Any] = {
    "objective": "regression",
    "metric": "rmse",
    "boosting_type": "gbdt",
    "num_leaves": 31,
    "learning_rate": 0.05,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "min_data_in_leaf": 20,
    "min_gain_to_split": 0.0,
    "lambda_l1": 0.1,
    "lambda_l2": 0.1,
    "max_depth": -1,
    "random_state": 42,
    "n_jobs": -1,
    "verbosity": -1,
    "early_stopping_rounds": 20,
}


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ── Data loading ──


def load_npz(
    data_path: Path, *, regression: bool = False
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    data = np.load(data_path)
    X = data["X"]
    if regression:
        y_reg = data.get("y_reg")
        if y_reg is not None:
            y = y_reg
        elif "pnl" in data:
            y = data["pnl"].astype(np.float64)
        else:
            y = data["y"].astype(np.float64)
    else:
        y = data["y"]
    pnl_arr = data.get("pnl")
    if pnl_arr is None:
        pnl_arr = np.zeros(len(y))
    feat_raw = data.get("feature_names")
    if feat_raw is None:
        feature_names = [f"f_{i}" for i in range(X.shape[1])]
    elif isinstance(feat_raw, np.ndarray):
        feature_names = feat_raw.tolist()
    else:
        feature_names = list(feat_raw)
    return X, y, pnl_arr, feature_names


def load_parquet(data_path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    import pandas as pd

    df = pd.read_parquet(data_path)
    feature_cols = [c for c in df.columns if c.startswith("f_")]
    if not feature_cols:
        feature_cols = [f"f_{i}" for i in range(40)]
    X = df[feature_cols].to_numpy(dtype=np.float64)
    y = df["label"].map({"win": 1}).fillna(0).to_numpy(dtype=np.int32)
    pnl_arr = (
        df["pnl"].fillna(0.0).to_numpy(dtype=np.float64)
        if "pnl" in df.columns
        else np.zeros(len(y))
    )
    return X, y, pnl_arr, feature_cols


def load_training_data(
    data_path: Path, *, regression: bool = False
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    ext = data_path.suffix.lower()
    if ext == ".npz":
        return load_npz(data_path, regression=regression)
    if ext == ".parquet":
        return load_parquet(data_path)
    raise ValueError(f"unsupported data format: {ext} (expected .npz or .parquet)")


# ── Training ──


def train_lightgbm(
    X: np.ndarray,
    y: np.ndarray,
    params: dict[str, Any] | None = None,
    *,
    val_data: tuple[np.ndarray, np.ndarray] | None = None,
    feature_names: list[str] | None = None,
    regression: bool = False,
    custom_obj: Any | None = None,
    custom_metric: Any | None = None,
    sample_weight: np.ndarray | None = None,
) -> tuple[Any, dict[str, Any]]:
    import lightgbm as lgb

    merged = {**(DEFAULT_PARAMS_REG if regression else DEFAULT_PARAMS_CLS), **(params or {})}
    early_stop = merged.pop("early_stopping_rounds", None)
    n_estimators = merged.pop("n_estimators", merged.get("num_iterations", 500))
    merged.pop("n_estimators", None)

    merged["num_iterations"] = n_estimators
    if "random_state" in merged:
        merged["random_state"] = merged["random_state"]
        merged["data_random_seed"] = merged["random_state"]

    # ── Class imbalance: scale_pos_weight for binary classification ──
    if not regression:
        scale_pos_weight = merged.pop("scale_pos_weight", None)
        if scale_pos_weight is not None and scale_pos_weight > 0:
            pos_count = int(y.sum())
            neg_count = len(y) - pos_count
            if pos_count > 0:
                merged["scale_pos_weight"] = scale_pos_weight
                print(
                    f"[lgb_trainer] scale_pos_weight={scale_pos_weight} (neg={neg_count} pos={pos_count})"
                )

    # ── Sample weights ──
    weight_array = sample_weight
    if weight_array is None and not regression:
        scale_pos_weight = merged.pop("scale_pos_weight", None)
        if scale_pos_weight is not None and scale_pos_weight > 0:
            pos_count = int(y.sum())
            neg_count = len(y) - pos_count
            if pos_count > 0:
                merged["scale_pos_weight"] = scale_pos_weight
                print(
                    f"[lgb_trainer] scale_pos_weight={scale_pos_weight} (neg={neg_count} pos={pos_count})"
                )

    dtrain = lgb.Dataset(
        X,
        label=y,
        feature_name=feature_names or [f"f_{i}" for i in range(X.shape[1])],
        weight=weight_array,
    )

    valid_sets = [dtrain]
    valid_names = ["train"]
    if val_data is not None:
        dval = lgb.Dataset(
            val_data[0],
            label=val_data[1],
            feature_name=feature_names or [f"f_{i}" for i in range(val_data[0].shape[1])],
            reference=dtrain,
        )
        valid_sets.append(dval)
        valid_names.append("eval")

    # ── Custom objective (LightGBM 4.x: pass via params, not fobj) ──
    if custom_obj is not None:
        merged.pop("objective", None)
        merged.pop("metric", None)
        merged["objective"] = custom_obj
        print("[lgb_trainer] Using custom objective function (via params)")
    if custom_metric is not None:
        print("[lgb_trainer] Using custom evaluation metric")

    t0 = time.perf_counter()
    evals_result: dict[str, list[float]] = {}

    booster = lgb.train(
        params=merged,
        train_set=dtrain,
        num_boost_round=n_estimators,
        valid_sets=valid_sets,
        valid_names=valid_names,
        feval=custom_metric,
        callbacks=[
            lgb.record_evaluation(evals_result),
            lgb.early_stopping(early_stop, verbose=False) if early_stop else lgb.log_evaluation(0),
        ],
    )

    elapsed = round(time.perf_counter() - t0, 3)

    n_rounds = booster.current_iteration()
    metrics: dict[str, Any] = {
        "n_estimators": n_rounds,
        "train_time_seconds": elapsed,
        "early_stopped": n_rounds != n_estimators,
    }

    if regression:
        train_preds_reg = booster.predict(X)
        y_f64 = y.astype(np.float64)
        ss_res = float(np.sum((y_f64 - train_preds_reg) ** 2))
        ss_tot = float(np.sum((y_f64 - np.mean(y_f64)) ** 2))
        train_r2 = float(1.0 - ss_res / ss_tot) if ss_tot > 1e-12 else 0.0
        train_rmse = float(np.sqrt(np.mean((y_f64 - train_preds_reg) ** 2)))
        metrics["train_r2"] = round(train_r2, 6)
        metrics["train_rmse"] = round(train_rmse, 6)
        if val_data is not None:
            val_preds_reg = booster.predict(val_data[0])
            yv = val_data[1].astype(np.float64)
            ss_res_val = float(np.sum((yv - val_preds_reg) ** 2))
            ss_tot_val = float(np.sum((yv - np.mean(yv)) ** 2))
            val_r2 = float(1.0 - ss_res_val / ss_tot_val) if ss_tot_val > 1e-12 else 0.0
            val_rmse = float(np.sqrt(np.mean((yv - val_preds_reg) ** 2)))
            metrics["val_r2"] = round(val_r2, 6)
            metrics["val_rmse"] = round(val_rmse, 6)
    else:
        train_preds = (booster.predict(X) > 0.5).astype(int)
        train_acc = float((train_preds == y).mean())
        metrics["train_accuracy"] = round(train_acc, 6)
        val_acc: float | None = None
        if val_data is not None:
            val_preds = (booster.predict(val_data[0]) > 0.5).astype(int)
            val_acc = float((val_preds == val_data[1]).mean())
        metrics["val_accuracy"] = round(val_acc, 6) if val_acc is not None else None
    for ds_name, metrics_list in evals_result.items():
        if isinstance(metrics_list, dict):
            for metric_name, values in metrics_list.items():
                if values:
                    metrics[f"final_{ds_name}_{metric_name}"] = round(values[-1], 6)
        elif metrics_list:
            metrics[f"final_{ds_name}"] = round(metrics_list[-1], 6)

    return booster, metrics


# ── Model persistence ──


def save_model(booster: Any, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(output_path))
    return output_path


def save_result(
    metrics: dict[str, Any],
    model_path: Path,
    result_path: Path,
    *,
    data_path: str | None = None,
    samples: int = 0,
    features: int = 0,
    params: dict[str, Any] | None = None,
) -> Path:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "trainer": "lgb_trainer",
        "trainer_version": "lgb-inrepo-1.0.0",
        "completed_at_utc": _utc_now_iso(),
        "exit_code": 0,
        "artifact_primary": str(model_path),
        "metrics": {"train_finished": True, **metrics},
        "data": {"source": data_path, "samples": samples, "features": features},
        "params_used": params or {},
        "risk_notes": [],
    }
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result_path


# ── Orchestrator ──


def build_and_train(
    data_path: Path,
    model_path: Path,
    result_path: Path | None = None,
    *,
    params: dict[str, Any] | None = None,
    val_data_path: Path | None = None,
    augment: bool = False,
    augment_vol_scales: list[float] | None = None,
    augment_noise_std: float = 0.0,
    augment_seed: int | None = None,
    regression: bool = False,
    custom_obj: Any | None = None,
    custom_metric: Any | None = None,
    sample_weight: np.ndarray | None = None,
) -> dict[str, Any]:
    X, y, pnl, feature_names = load_training_data(data_path, regression=regression)

    val_data = None
    if val_data_path is not None:
        Xv, yv, _, _ = load_training_data(val_data_path, regression=regression)
        val_data = (Xv, yv)

    augment_applied = False
    if augment and (augment_vol_scales or augment_noise_std > 0):
        from core.features.data_augmentation import augment_dataset

        X, y = augment_dataset(
            X,
            y,
            volatility_scaling=augment_vol_scales or [1.0],
            noise_std=augment_noise_std,
            seed=augment_seed,
            concat_original=True,
        )
        augment_applied = True

    booster, metrics = train_lightgbm(
        X,
        y,
        params=params,
        val_data=val_data,
        feature_names=feature_names,
        regression=regression,
        custom_obj=custom_obj,
        custom_metric=custom_metric,
        sample_weight=sample_weight,
    )

    save_model(booster, model_path)

    if result_path is None:
        result_path = model_path.with_suffix(".result.json")

    merged_params = {**(DEFAULT_PARAMS_REG if regression else DEFAULT_PARAMS_CLS), **(params or {})}
    save_result(
        metrics,
        model_path,
        result_path,
        data_path=str(data_path),
        samples=len(X),
        features=X.shape[1],
        params=merged_params,
    )

    if augment_applied and result_path and result_path.exists():
        result_data = json.loads(result_path.read_text(encoding="utf-8"))
        result_data["data_augmentation"] = {
            "enabled": True,
            "volatility_scaling": augment_vol_scales,
            "noise_std": augment_noise_std,
        }
        result_path.write_text(json.dumps(result_data, indent=2, ensure_ascii=False))

    return {
        "model_path": str(model_path),
        "result_path": str(result_path),
        "samples": len(X),
        "features": X.shape[1],
        "metrics": metrics,
    }


# ── CLI ──


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lgb_trainer",
        description="Train LightGBM classifier from dataset_builder NPZ/Parquet output",
    )
    p.add_argument("--data", type=Path, required=True, help="Path to train.npz or train.parquet")
    p.add_argument("--val-data", type=Path, default=None, help="Optional validation file")
    p.add_argument("--output-model", type=Path, required=True, help="Path for booster .txt output")
    p.add_argument("--output-result", type=Path, default=None, help="Path for result.json")
    p.add_argument(
        "--params", type=Path, default=None, help="Optional JSON file with LightGBM params"
    )
    p.add_argument("--n-estimators", type=int, default=None)
    p.add_argument("--num-leaves", type=int, default=None)
    p.add_argument("--learning-rate", type=float, default=None)
    p.add_argument(
        "--scale-pos-weight",
        type=float,
        default=None,
        help="LightGBM scale_pos_weight for binary class imbalance (e.g. 6.58)",
    )
    p.add_argument("--seed", type=int, default=None)
    p.add_argument(
        "--mode",
        choices=["cls", "reg"],
        default="cls",
        help="Training mode: cls (classification) or reg (P&L regression)",
    )
    p.add_argument(
        "--recipe", type=Path, default=None, help="Training Recipe JSON for hyperparameters"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.data.exists():
        print(f"[lgb_trainer] ERROR: data file not found: {args.data}", file=sys.stderr)
        return 2

    recipe_obj = None
    recipe_id: str | None = None
    augment_enabled = False
    augment_vol_scales: list[float] | None = None
    augment_noise_std = 0.0
    if args.recipe:
        from core.contracts.training.training_recipe import TrainingRecipe

        recipe_obj = TrainingRecipe.from_file(args.recipe)
        recipe_id = recipe_obj.recipe_id
        print(f"[lgb_trainer] Recipe: {recipe_id}")
        if args.n_estimators is None:
            args.n_estimators = recipe_obj.training.epochs
        if args.learning_rate is None:
            args.learning_rate = recipe_obj.training.learning_rate
        if args.num_leaves is None and recipe_obj.training.hidden_dims:
            args.num_leaves = recipe_obj.training.hidden_dims[0]
        da = recipe_obj.data.data_augmentation
        if da.enabled:
            augment_enabled = True
            augment_vol_scales = da.volatility_scaling
            augment_noise_std = da.noise_std

    params: dict[str, Any] = {}
    if args.params:
        params = json.loads(Path(args.params).read_text(encoding="utf-8"))

    overrides = {
        k: v
        for k, v in [
            ("num_iterations", args.n_estimators),
            ("num_leaves", args.num_leaves),
            ("learning_rate", args.learning_rate),
            ("random_state", args.seed),
            ("scale_pos_weight", args.scale_pos_weight),
        ]
        if v is not None
    }
    if overrides:
        params.update(overrides)

    summary = build_and_train(
        data_path=args.data.resolve(),
        model_path=args.output_model.resolve(),
        result_path=args.output_result.resolve() if args.output_result else None,
        params=params,
        val_data_path=args.val_data.resolve() if args.val_data else None,
        augment=augment_enabled,
        augment_vol_scales=augment_vol_scales,
        augment_noise_std=augment_noise_std,
        augment_seed=args.seed,
        regression=args.mode == "reg",
    )

    if recipe_id and args.output_result:
        result_path = args.output_result.resolve()
        if result_path.exists():
            result_data = json.loads(result_path.read_text(encoding="utf-8"))
            result_data["recipe_id"] = recipe_id
            result_path.write_text(json.dumps(result_data, indent=2, ensure_ascii=False))

    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
