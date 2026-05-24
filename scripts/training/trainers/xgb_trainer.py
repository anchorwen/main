"""In-repo XGBoost trainer consuming dataset_builder NPZ/Parquet output.

Trains an XGBoost binary classifier from feature-label pairs produced by
dataset_builder.py. Outputs a booster JSON (for XGBoostBrainAdapter) and a
result.json (for CRT pipeline consumption).

Usage:
  # Train from NPZ (fastest)
  python scripts/training/trainers/xgb_trainer.py \\
    --data data/training/train.npz \\
    --output-model data/models/xgb_booster.json \\
    --output-result data/models/xgb_result.json

  # Train from Parquet
  python scripts/training/trainers/xgb_trainer.py \\
    --data data/training/train.parquet \\
    --output-model data/models/xgb_booster.json
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
    "n_estimators": 200,
    "max_depth": 5,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "objective": "binary:logistic",
    "eval_metric": "logloss",
    "random_state": 42,
    "n_jobs": -1,
    "early_stopping_rounds": 20,
}

DEFAULT_PARAMS_REG: dict[str, Any] = {
    "n_estimators": 200,
    "max_depth": 5,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "objective": "reg:squarederror",
    "eval_metric": "rmse",
    "random_state": 42,
    "n_jobs": -1,
    "early_stopping_rounds": 20,
}

DEFAULT_PARAMS_MULTI: dict[str, Any] = {
    "n_estimators": 200,
    "max_depth": 5,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "objective": "multi:softmax",
    "num_class": 3,
    "eval_metric": "mlogloss",
    "random_state": 42,
    "n_jobs": -1,
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
    data_path: Path, *, regression: bool = False, multi_class: bool = False
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Load training data from .npz file.

    Returns (X, y, pnl, feature_names).  y is y_reg when regression=True.
    When multi_class=True, y should be int class labels -1, 0, 1 (remapped to 0, 1, 2).
    """
    data = np.load(data_path)
    X = data["X"]
    used_flat = False
    # If X is 3D (sequence data), flatten to use last bar or X_flat key
    if X.ndim == 3:
        X_flat_key = data.get("X_flat")
        if X_flat_key is not None:
            X = X_flat_key
            used_flat = True
        else:
            X = X[:, -1, :]  # use last bar of each sequence
    if regression:
        y_reg = data.get("y_reg")
        if y_reg is not None:
            y = y_reg
        elif "pnl" in data:
            y = data["pnl"].astype(np.float64)
        else:
            y = data["y"].astype(np.float64)
    elif multi_class:
        y = data["y"].astype(np.int32)
        # Map -1,0,1 → 0,1,2 for XGBoost multi:softmax
        y = np.where(y == -1, 2, y)  # -1 → 2 (sl_hit_first)
        y = np.where(y == 1, 1, y)  #  1 → 1 (tp_hit_first)
        # 0 stays 0 (timeout)
    else:
        y = data["y"]
    pnl_arr = data.get("pnl")
    if pnl_arr is None:
        pnl_arr = np.zeros(len(y))
    if used_flat:
        feature_names = [f"f_{i}" for i in range(X.shape[1])]
    else:
        feat_raw = data.get("feature_names")
        if feat_raw is None:
            feature_names = [f"f_{i}" for i in range(X.shape[1])]
        elif isinstance(feat_raw, np.ndarray):
            feature_names = feat_raw.tolist()
        else:
            feature_names = list(feat_raw)
    return X, y, pnl_arr, feature_names


def load_parquet(data_path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Load training data from .parquet file.

    Expects columns f_0..f_39, label, pnl.
    """
    import pandas as pd

    df = pd.read_parquet(data_path)
    feature_cols = [f"f_{i}" for i in range(40)]
    X = df[feature_cols].to_numpy(dtype=np.float64)
    y = df["label"].map({"win": 1}).fillna(0).to_numpy(dtype=np.int32)
    pnl_arr = df["pnl"].fillna(0.0).to_numpy(dtype=np.float64)
    return X, y, pnl_arr, feature_cols


def load_training_data(
    data_path: Path, *, regression: bool = False, multi_class: bool = False
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Load training data, dispatching by extension.

    Returns (X, y, pnl, feature_names).
    """
    ext = data_path.suffix.lower()
    if ext == ".npz":
        return load_npz(data_path, regression=regression, multi_class=multi_class)
    if ext == ".parquet":
        return load_parquet(data_path)
    raise ValueError(f"unsupported data format: {ext} (expected .npz or .parquet)")


# ── Training ──


def train_xgboost(
    X: np.ndarray,
    y: np.ndarray,
    params: dict[str, Any] | None = None,
    *,
    val_data: tuple[np.ndarray, np.ndarray] | None = None,
    feature_names: list[str] | None = None,
    regression: bool = False,
    multi_class: bool = False,
    custom_obj: Any | None = None,
    sample_weight: np.ndarray | None = None,
    pnl: np.ndarray | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Train an XGBoost model and return (booster, metrics).

    Args:
        X: Feature matrix (n_samples, n_features).
        y: Binary labels (0/1) for classification, int 0/1/2 for multi-class,
           or P&L values for regression.
        params: XGBoost parameters dict; merged with defaults per mode.
        val_data: Optional (X_val, y_val) for early stopping and eval.
        feature_names: Optional list of feature names.
        regression: If True, use reg:squarederror.
        multi_class: If True, use multi:softmax with num_class=3.
        custom_obj: Optional custom objective callable for XGBoost.
        sample_weight: Optional per-sample weights (e.g., return-magnitude).
        pnl: Optional P&L array for custom objective computation.

    Returns:
        (booster, metrics_dict).
    """
    import xgboost as xgb

    if regression:
        defaults = DEFAULT_PARAMS_REG
    elif multi_class:
        defaults = DEFAULT_PARAMS_MULTI
    else:
        defaults = DEFAULT_PARAMS_CLS
    merged = {**defaults, **(params or {})}

    early_stop = merged.pop("early_stopping_rounds", None)
    n_estimators = merged.pop("n_estimators", 200)

    dtrain = xgb.DMatrix(X, label=y)
    if feature_names:
        dtrain.feature_names = feature_names

    # ── Sample weights for class imbalance ──
    if sample_weight is not None:
        dtrain.set_weight(sample_weight)
        print(f"[xgb_trainer] Custom sample weights applied (mean={sample_weight.mean():.3f})")
    elif multi_class:
        cls_counts = np.bincount(y, minlength=3)
        cls_weights = len(y) / (3 * cls_counts.clip(min=1))
        multi_weights = cls_weights[y]
        dtrain.set_weight(multi_weights)
        print(
            f"[xgb_trainer] Multi-class weights: {dict(zip(['timeout','tp','sl'], cls_weights.round(4).tolist(), strict=False))}"
        )
    elif not regression:
        # Binary classification: scale_pos_weight for class imbalance
        scale_pos_weight = merged.pop("scale_pos_weight", None)
        if scale_pos_weight is not None and scale_pos_weight > 0:
            pos_count = int(y.sum())
            neg_count = len(y) - pos_count
            if pos_count > 0:
                merged["scale_pos_weight"] = scale_pos_weight
                print(
                    f"[xgb_trainer] scale_pos_weight={scale_pos_weight} (neg={neg_count} pos={pos_count})"
                )

    # ── Custom objective ──
    if custom_obj is not None:
        if "eval_metric" in merged:
            merged.pop("eval_metric")
        # Set a valid built-in objective; custom obj passed via xgb.train(obj=...)
        merged["objective"] = "binary:logistic"
        print("[xgb_trainer] Using custom objective function")

    evals: list[tuple[xgb.DMatrix, str]] = [(dtrain, "train")]
    if val_data is not None:
        dval = xgb.DMatrix(val_data[0], label=val_data[1])
        if feature_names:
            dval.feature_names = feature_names
        if multi_class:
            val_unique, val_counts = np.unique(val_data[1], return_counts=True)
            val_cls_weights = len(val_data[1]) / (len(val_unique) * np.maximum(val_counts, 1))
            dval.set_weight(val_cls_weights[val_data[1]])
        evals.append((dval, "eval"))

    t0 = time.perf_counter()
    evals_result: dict[str, list[float]] = {}

    booster = xgb.train(
        params=merged,
        dtrain=dtrain,
        num_boost_round=n_estimators,
        evals=evals,
        evals_result=evals_result,
        early_stopping_rounds=early_stop,
        verbose_eval=False,
        obj=custom_obj if custom_obj is not None else None,
    )

    elapsed = round(time.perf_counter() - t0, 3)

    n_rounds = booster.num_boosted_rounds()
    metrics: dict[str, Any] = {
        "n_estimators": n_rounds,
        "train_time_seconds": elapsed,
        "early_stopped": n_rounds != n_estimators,
    }

    if regression:
        train_preds_reg = booster.predict(dtrain)
        y_f64 = y.astype(np.float64)
        ss_res = float(np.sum((y_f64 - train_preds_reg) ** 2))
        ss_tot = float(np.sum((y_f64 - np.mean(y_f64)) ** 2))
        train_r2 = float(1.0 - ss_res / ss_tot) if ss_tot > 1e-12 else 0.0
        train_rmse = float(np.sqrt(np.mean((y_f64 - train_preds_reg) ** 2)))
        metrics["train_r2"] = round(train_r2, 6)
        metrics["train_rmse"] = round(train_rmse, 6)
        if val_data is not None:
            dval_post = xgb.DMatrix(val_data[0], label=val_data[1])
            if feature_names:
                dval_post.feature_names = feature_names
            val_preds_reg = booster.predict(dval_post)
            yv = val_data[1].astype(np.float64)
            ss_res_val = float(np.sum((yv - val_preds_reg) ** 2))
            ss_tot_val = float(np.sum((yv - np.mean(yv)) ** 2))
            val_r2 = float(1.0 - ss_res_val / ss_tot_val) if ss_tot_val > 1e-12 else 0.0
            val_rmse = float(np.sqrt(np.mean((yv - val_preds_reg) ** 2)))
            metrics["val_r2"] = round(val_r2, 6)
            metrics["val_rmse"] = round(val_rmse, 6)
    elif multi_class:
        # multi:softmax returns class predictions (0,1,2)
        train_preds = booster.predict(dtrain).astype(np.int32)
        train_acc = float((train_preds == y).mean())
        metrics["train_accuracy"] = round(train_acc, 6)
        # Per-class accuracy
        for cls_idx, cls_name in enumerate(["timeout", "tp_hit", "sl_hit"]):
            mask = y == cls_idx
            if mask.sum() > 0:
                metrics[f"train_acc_{cls_name}"] = round(
                    float((train_preds[mask] == y[mask]).mean()), 6
                )
        if val_data is not None:
            dval_post = xgb.DMatrix(val_data[0], label=val_data[1])
            if feature_names:
                dval_post.feature_names = feature_names
            val_preds = booster.predict(dval_post).astype(np.int32)
            multi_val_acc = float((val_preds == val_data[1]).mean())
            metrics["val_accuracy"] = round(multi_val_acc, 6)
            for cls_idx, cls_name in enumerate(["timeout", "tp_hit", "sl_hit"]):
                mask = val_data[1] == cls_idx
                if mask.sum() > 0:
                    metrics[f"val_acc_{cls_name}"] = round(
                        float((val_preds[mask] == val_data[1][mask]).mean()), 6
                    )
    else:
        train_preds = (booster.predict(dtrain) > 0.5).astype(int)
        train_acc = float((train_preds == y).mean())
        metrics["train_accuracy"] = round(train_acc, 6)
        val_acc: float | None = None
        if val_data is not None:
            dval_post = xgb.DMatrix(val_data[0], label=val_data[1])
            if feature_names:
                dval_post.feature_names = feature_names
            val_preds = (booster.predict(dval_post) > 0.5).astype(int)
            val_acc = float((val_preds == val_data[1]).mean())
        metrics["val_accuracy"] = round(val_acc, 6) if val_acc is not None else None
    # Include final eval logloss
    for ds_name, metrics_list in evals_result.items():
        if isinstance(metrics_list, dict):
            # xgboost >= 2.0: {"train": {"logloss": [...]}, ...}
            for metric_name, values in metrics_list.items():
                if values:
                    metrics[f"final_{ds_name}_{metric_name}"] = round(values[-1], 6)
        elif metrics_list:
            # xgboost < 2.0: {"train": [...]}
            metrics[f"final_{ds_name}"] = round(metrics_list[-1], 6)

    return booster, metrics


# ── Model persistence ──


def save_model(booster: Any, output_path: Path) -> Path:
    """Save XGBoost booster as JSON."""
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
    """Save training result.json for CRT pipeline consumption."""
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "trainer": "xgb_trainer",
        "trainer_version": "xgb-inrepo-1.0.0",
        "completed_at_utc": _utc_now_iso(),
        "exit_code": 0,
        "artifact_primary": str(model_path),
        "metrics": {
            "train_finished": True,
            **metrics,
        },
        "data": {
            "source": data_path,
            "samples": samples,
            "features": features,
        },
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
    multi_class: bool = False,
    custom_obj: Any | None = None,
    sample_weight: np.ndarray | None = None,
) -> dict[str, Any]:
    """Full pipeline: load → augment → train → save.

    Returns summary dict.
    """
    X, y, pnl, feature_names = load_training_data(
        data_path, regression=regression, multi_class=multi_class
    )

    val_data = None
    if val_data_path is not None:
        Xv, yv, _, _ = load_training_data(
            val_data_path, regression=regression, multi_class=multi_class
        )
        val_data = (Xv, yv)

    # ── Data augmentation ──
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

    booster, metrics = train_xgboost(
        X,
        y,
        params=params,
        val_data=val_data,
        feature_names=feature_names,
        regression=regression,
        multi_class=multi_class,
        custom_obj=custom_obj,
        sample_weight=sample_weight,
        pnl=pnl,
    )

    save_model(booster, model_path)

    if result_path is None:
        result_path = model_path.with_suffix(".result.json")

    if regression:
        defaults = DEFAULT_PARAMS_REG
    elif multi_class:
        defaults = DEFAULT_PARAMS_MULTI
    else:
        defaults = DEFAULT_PARAMS_CLS
    merged_params = {**defaults, **(params or {})}
    save_result(
        metrics,
        model_path,
        result_path,
        data_path=str(data_path),
        samples=len(X),
        features=X.shape[1],
        params=merged_params,
    )

    # Inject augmentation metadata into result
    if augment_applied and result_path and result_path.exists():
        result_data = json.loads(result_path.read_text(encoding="utf-8"))
        result_data["data_augmentation"] = {
            "enabled": True,
            "volatility_scaling": augment_vol_scales,
            "noise_std": augment_noise_std,
        }
        result_path.write_text(
            json.dumps(result_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

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
        prog="xgb_trainer",
        description="Train XGBoost classifier from dataset_builder NPZ/Parquet output",
    )
    p.add_argument("--data", type=Path, required=True, help="Path to train.npz or train.parquet")
    p.add_argument(
        "--val-data", type=Path, default=None, help="Optional validation file (val.npz/parquet)"
    )
    p.add_argument("--output-model", type=Path, required=True, help="Path for booster JSON output")
    p.add_argument(
        "--output-result", type=Path, default=None, help="Path for result.json (default: adjacent)"
    )
    p.add_argument(
        "--params", type=Path, default=None, help="Optional JSON file with XGBoost params"
    )
    p.add_argument("--n-estimators", type=int, default=None)
    p.add_argument("--max-depth", type=int, default=None)
    p.add_argument("--learning-rate", type=float, default=None)
    p.add_argument(
        "--scale-pos-weight",
        type=float,
        default=None,
        help="XGBoost scale_pos_weight for binary class imbalance (e.g. 6.58 for 86.8%% class 0)",
    )
    p.add_argument("--seed", type=int, default=None)
    p.add_argument(
        "--mode",
        choices=["cls", "reg", "multi"],
        default="cls",
        help="Training mode: cls (binary), reg (P&L regression), multi (3-class barrier)",
    )
    p.add_argument(
        "--recipe",
        type=Path,
        default=None,
        help="Path to Training Recipe JSON for hyperparameters and provenance",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.data.exists():
        print(f"[xgb_trainer] ERROR: data file not found: {args.data}", file=sys.stderr)
        return 2

    # ── Load recipe if provided ──
    recipe_obj = None
    recipe_id: str | None = None
    augment_enabled = False
    augment_vol_scales: list[float] | None = None
    augment_noise_std = 0.0
    if args.recipe:
        from core.contracts.training.training_recipe import TrainingRecipe

        recipe_obj = TrainingRecipe.from_file(args.recipe)
        recipe_id = recipe_obj.recipe_id
        print(f"[xgb_trainer] Recipe: {recipe_id}")

        # Recipe provides defaults; CLI overrides take precedence
        if args.n_estimators is None:
            args.n_estimators = recipe_obj.training.epochs
        if args.learning_rate is None:
            args.learning_rate = recipe_obj.training.learning_rate
        if args.max_depth is None and recipe_obj.training.hidden_dims:
            args.max_depth = recipe_obj.training.hidden_dims[0]

        # ── Augmentation config from recipe ──
        da = recipe_obj.data.data_augmentation
        if da.enabled:
            augment_enabled = True
            augment_vol_scales = da.volatility_scaling
            augment_noise_std = da.noise_std
            print(
                f"[xgb_trainer] Augmentation: vol_scales={augment_vol_scales}, noise={augment_noise_std}"
            )

    params: dict[str, Any] = {}
    if args.params:
        params = json.loads(Path(args.params).read_text(encoding="utf-8"))

    overrides = {
        k: v
        for k, v in [
            ("n_estimators", args.n_estimators),
            ("max_depth", args.max_depth),
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
        multi_class=args.mode == "multi",
    )

    # Inject recipe provenance into result
    if recipe_id and args.output_result:
        result_path = args.output_result.resolve()
        if result_path.exists():
            result_data = json.loads(result_path.read_text(encoding="utf-8"))
            result_data["recipe_id"] = recipe_id
            result_path.write_text(
                json.dumps(result_data, indent=2, ensure_ascii=False), encoding="utf-8"
            )

    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
