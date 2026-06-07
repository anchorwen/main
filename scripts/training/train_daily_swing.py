"""Train D1 daily-swing models (XGBoost + LightGBM) for long-cycle trading.

High-standard training pipeline:
  - Chronological train/val/test split with Purge & Embargo
  - Early stopping on validation set
  - PnL-magnitude-weighted loss (samples with larger |pnl_r| get higher weight)
  - Permutation feature importance (shuffle-column accuracy drop)
  - Multi-contract training (5d, 10d, 20d)
  - Saves models in brain-compatible formats with full provenance metadata

Usage:
    python scripts/training/train_daily_swing.py \\
        --dataset data/training/d1_swing_10d.npz \\
        --model xgboost \\
        --output-dir data/models/daily
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

AVAILABLE_MODELS = ["xgboost", "lightgbm", "all"]


def _load_dataset(npz_path: Path) -> dict[str, Any]:
    """Load training dataset from NPZ."""
    if not npz_path.exists():
        raise FileNotFoundError(f"Dataset not found: {npz_path}")
    data = np.load(npz_path, allow_pickle=True)
    meta_path = npz_path.with_suffix(".meta.json")
    if meta_path.exists():
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    else:
        meta = {}
    return {
        "X": data["X"],
        "y": data["y"],
        "pnl_r": data.get("pnl_r", np.zeros(len(data["y"]), dtype=np.float32)),
        "X_val": data["X_val"],
        "y_val": data["y_val"],
        "pnl_r_val": data.get("pnl_r_val", np.zeros(len(data["y_val"]), dtype=np.float32)),
        "X_test": data["X_test"],
        "y_test": data["y_test"],
        "meta": meta,
    }


# ── Sample weights: class balance × PnL magnitude (Fix #6) ──


def _compute_sample_weights(
    y_flat: np.ndarray, pnl_r: np.ndarray, num_classes: int = 3
) -> np.ndarray:
    """Compute sample weights combining class balancing with PnL magnitude.

    weight[i] = class_weight[class_i] * max(1.0, |pnl_r[i]| / mean_nonzero_pnl)

    Samples with larger |pnl_r| receive higher weight, making the model
    prioritise learning from high-conviction moves.
    """
    n = len(y_flat)
    y_shifted = y_flat + 1  # [-1,0,1] → [0,1,2]

    # Class balancing
    class_counts = np.bincount(y_shifted, minlength=num_classes)
    class_weights = np.zeros(num_classes)
    for c in range(num_classes):
        if class_counts[c] > 0:
            class_weights[c] = n / (num_classes * class_counts[c])
        else:
            class_weights[c] = 1.0

    base_weights = np.array([class_weights[int(yi)] for yi in y_shifted], dtype=np.float64)

    # P&L magnitude scaling — log-smooth to prevent fat-tail explosion
    abs_pnl = np.abs(pnl_r.astype(np.float64))
    nonzero_mask = abs_pnl > 1e-8
    if np.any(nonzero_mask):
        mean_abs_pnl = np.mean(abs_pnl[nonzero_mask])
        if mean_abs_pnl > 1e-8:
            pnl_factor = np.ones(n, dtype=np.float64)
            # log1p smoothing: squashes 10R+ outliers to ~2.4× instead of 10×
            raw_factor = abs_pnl[nonzero_mask] / mean_abs_pnl
            pnl_factor[nonzero_mask] = np.maximum(1.0, np.log1p(raw_factor))
            # Hard cap at 3.0 — prevents any single sample from dominating
            pnl_factor = np.clip(pnl_factor, 1.0, 3.0)
            base_weights = base_weights * pnl_factor

    return base_weights.astype(np.float32)


# ── Permutation feature importance (Fix #7) ──


def _permutation_importance(
    model: Any,
    X: np.ndarray,
    y_flat: np.ndarray,
    feature_names: list[str],
    *,
    n_repeats: int = 5,
    seed: int = 42,
) -> list[tuple[str, float, float]]:
    """Permutation-based feature importance.

    For each feature, shuffle its column and measure the accuracy drop.
    More reliable than tree-native gain/split-count importance, especially
    for high-cardinality or cyclical features.
    """
    rng = np.random.default_rng(seed)
    n_features = X.shape[1]

    # Map predictions back from [0,1,2] to [-1,0,1]
    _y_shifted = y_flat + 1
    baseline_pred = model.predict(X)
    if baseline_pred.min() >= 0:  # model outputs [0,1,2]
        baseline_pred = baseline_pred - 1
    baseline_acc = float(np.mean(baseline_pred == y_flat))

    results: list[tuple[str, float, float]] = []

    for col in range(min(n_features, len(feature_names))):
        drops: list[float] = []
        X_perm = X.copy()
        for _ in range(n_repeats):
            rng.shuffle(X_perm[:, col])
            perm_pred = model.predict(X_perm)
            if perm_pred.min() >= 0:
                perm_pred = perm_pred - 1
            acc = float(np.mean(perm_pred == y_flat))
            drops.append(baseline_acc - acc)

        mean_drop = float(np.mean(drops))
        std_drop = float(np.std(drops))
        results.append((feature_names[col], mean_drop, std_drop))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


# ── XGBoost Trainer ──


def train_xgboost(
    dataset: dict[str, Any],
    *,
    seed: int = 42,
    verbosity: int = 1,
) -> tuple[Any, dict[str, Any]]:
    """Train an XGBoost classifier with PnL-weighted loss."""
    import xgboost as xgb

    X_train = dataset["X"]
    y_train_flat = dataset["y"]
    X_val = dataset["X_val"]
    y_val_flat = dataset["y_val"]
    X_test = dataset["X_test"]
    y_test_flat = dataset["y_test"]
    train_pnl = dataset.get("pnl_r", np.zeros(len(y_train_flat), dtype=np.float32))
    meta = dataset.get("meta", {})

    y_train = (y_train_flat + 1).astype(np.int32)
    y_val = (y_val_flat + 1).astype(np.int32)
    y_test = (y_test_flat + 1).astype(np.int32)
    num_classes = 3
    n_train = len(y_train)

    class_counts = np.bincount(y_train, minlength=num_classes)
    sample_weights = _compute_sample_weights(y_train_flat, train_pnl, num_classes)

    print(f"[xgboost] Training on {n_train} samples, {X_train.shape[1]} features")
    print(
        f"[xgboost] Class distribution: short={class_counts[0]} neutral={class_counts[1]} long={class_counts[2]}"
    )
    print(
        f"[xgboost] PnL-weighted sample weights: mean={np.mean(sample_weights):.3f} "
        f"std={np.std(sample_weights):.3f} max={np.max(sample_weights):.2f}"
    )

    params = {
        "objective": "multi:softprob",
        "num_class": num_classes,
        "eval_metric": ["mlogloss", "merror"],
        "max_depth": 4,
        "learning_rate": 0.03,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "min_child_weight": 5,
        "gamma": 0.1,
        "reg_alpha": 0.5,
        "reg_lambda": 1.0,
        "n_estimators": 500,
        "early_stopping_rounds": 50,
        "random_state": seed,
    }

    t0 = time.perf_counter()

    model = xgb.XGBClassifier(**params)
    if verbosity > 0:
        model.set_params(verbosity=1)

    model.fit(
        X_train,
        y_train,
        sample_weight=sample_weights,
        eval_set=[(X_val, y_val)],
        verbose=(verbosity > 1),
    )

    train_time = time.perf_counter() - t0

    train_pred = model.predict(X_train) - 1
    val_pred = model.predict(X_val) - 1
    test_pred = model.predict(X_test) - 1

    train_acc = float(np.mean(train_pred == y_train_flat))
    val_acc = float(np.mean(val_pred == y_val_flat))
    test_acc = float(np.mean(test_pred == y_test_flat))

    val_metrics = _per_class_metrics(y_val_flat, val_pred)

    # Permutation feature importance (Fix #7)
    feature_names = meta.get("feature_names", [f"f{i}" for i in range(X_train.shape[1])])
    perm_importances = _permutation_importance(
        model,
        X_val,
        y_val_flat,
        feature_names,
        n_repeats=5,
        seed=seed,
    )
    top_features = perm_importances[:10]

    # Also include native importance for comparison
    native_importance = model.feature_importances_
    native_top = sorted(
        zip(feature_names, native_importance, strict=False),
        key=lambda x: x[1],
        reverse=True,
    )[:10]

    test_pnl_r_sim = _simulate_barrier_pnl(y_test_flat, test_pred, meta.get("contract_id", ""))

    metrics = {
        "model_type": "xgboost",
        "num_classes": num_classes,
        "n_features": X_train.shape[1],
        "n_train": n_train,
        "n_val": len(y_val),
        "n_test": len(y_test),
        "train_accuracy": round(train_acc, 4),
        "val_accuracy": round(val_acc, 4),
        "test_accuracy": round(test_acc, 4),
        "val_per_class": val_metrics,
        "train_time_seconds": round(train_time, 2),
        "best_iteration": int(getattr(model, "best_iteration", -1)),
        "permutation_importance": [
            {"name": n, "importance": round(float(imp), 6), "std": round(float(std), 6)}
            for n, imp, std in top_features
        ],
        "native_importance_top10": [
            {"name": n, "importance": round(float(imp), 4)} for n, imp in native_top
        ],
        "simulated_test_pnl_r": test_pnl_r_sim,
        "loss_type": "pnl_weighted_cross_entropy",
        "params": {k: v for k, v in params.items() if k not in ("eval_metric",)},
    }

    if verbosity > 0:
        print(
            f"[xgboost] train_acc={train_acc:.4f}  val_acc={val_acc:.4f}  test_acc={test_acc:.4f}  "
            f"time={train_time:.1f}s  best_iter={metrics['best_iteration']}"
        )
        print(
            f"[xgboost] Top 5 perm importance: "
            f"{', '.join(f'{n}({imp:+.4f})' for n, imp, _ in top_features[:5])}"
        )
        print(
            f"[xgboost] Simulated test PnL: sharpe={test_pnl_r_sim.get('sharpe', 0):.2f}  "
            f"total_r={test_pnl_r_sim.get('total_pnl_r', 0):.2f}  "
            f"win_rate={test_pnl_r_sim.get('win_rate', 0):.1%}"
        )

    return model, metrics


# ── LightGBM Trainer ──


def train_lightgbm(
    dataset: dict[str, Any],
    *,
    seed: int = 42,
    verbosity: int = 1,
) -> tuple[Any, dict[str, Any]]:
    """Train a LightGBM classifier with PnL-weighted loss."""
    import lightgbm as lgb

    X_train = dataset["X"]
    y_train_flat = dataset["y"]
    X_val = dataset["X_val"]
    y_val_flat = dataset["y_val"]
    X_test = dataset["X_test"]
    y_test_flat = dataset["y_test"]
    train_pnl = dataset.get("pnl_r", np.zeros(len(y_train_flat), dtype=np.float32))
    meta = dataset.get("meta", {})

    y_train = (y_train_flat + 1).astype(np.int32)
    y_val = (y_val_flat + 1).astype(np.int32)
    y_test = (y_test_flat + 1).astype(np.int32)
    num_classes = 3
    n_train = len(y_train)

    class_counts = np.bincount(y_train, minlength=num_classes)
    sample_weights = _compute_sample_weights(y_train_flat, train_pnl, num_classes)

    print(f"[lightgbm] Training on {n_train} samples, {X_train.shape[1]} features")
    print(
        f"[lightgbm] Class distribution: short={class_counts[0]} neutral={class_counts[1]} long={class_counts[2]}"
    )
    print(
        f"[lightgbm] PnL-weighted sample weights: mean={np.mean(sample_weights):.3f} "
        f"std={np.std(sample_weights):.3f} max={np.max(sample_weights):.2f}"
    )

    params = {
        "objective": "multiclass",
        "num_class": num_classes,
        "metric": "multi_logloss",
        "boosting_type": "gbdt",
        "max_depth": 5,
        "num_leaves": 31,
        "learning_rate": 0.03,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "min_child_samples": 10,
        "min_child_weight": 0.001,
        "reg_alpha": 0.3,
        "reg_lambda": 0.5,
        "n_estimators": 500,
        "early_stopping_rounds": 50,
        "random_state": seed,
        "verbose": -1,
    }

    t0 = time.perf_counter()

    model = lgb.LGBMClassifier(**params)
    model.fit(
        X_train,
        y_train,
        sample_weight=sample_weights,
        eval_set=[(X_val, y_val)],
        eval_metric="multi_logloss",
    )

    train_time = time.perf_counter() - t0

    train_pred = model.predict(X_train) - 1
    val_pred = model.predict(X_val) - 1
    test_pred = model.predict(X_test) - 1

    train_acc = float(np.mean(train_pred == y_train_flat))
    val_acc = float(np.mean(val_pred == y_val_flat))
    test_acc = float(np.mean(test_pred == y_test_flat))

    val_metrics = _per_class_metrics(y_val_flat, val_pred)

    # Permutation feature importance (Fix #7)
    feature_names = meta.get("feature_names", [f"f{i}" for i in range(X_train.shape[1])])
    perm_importances = _permutation_importance(
        model,
        X_val,
        y_val_flat,
        feature_names,
        n_repeats=5,
        seed=seed,
    )
    top_features = perm_importances[:10]

    native_importance = model.feature_importances_
    native_top = sorted(
        zip(feature_names, native_importance, strict=False),
        key=lambda x: x[1],
        reverse=True,
    )[:10]

    test_pnl_r_sim = _simulate_barrier_pnl(y_test_flat, test_pred, meta.get("contract_id", ""))

    metrics = {
        "model_type": "lightgbm",
        "num_classes": num_classes,
        "n_features": X_train.shape[1],
        "n_train": n_train,
        "n_val": len(y_val),
        "n_test": len(y_test),
        "train_accuracy": round(train_acc, 4),
        "val_accuracy": round(val_acc, 4),
        "test_accuracy": round(test_acc, 4),
        "val_per_class": val_metrics,
        "train_time_seconds": round(train_time, 2),
        "best_iteration": int(getattr(model, "best_iteration_", -1)),
        "permutation_importance": [
            {"name": n, "importance": round(float(imp), 6), "std": round(float(std), 6)}
            for n, imp, std in top_features
        ],
        "native_importance_top10": [
            {"name": n, "importance": round(float(imp), 4)} for n, imp in native_top
        ],
        "simulated_test_pnl_r": test_pnl_r_sim,
        "loss_type": "pnl_weighted_cross_entropy",
        "params": params,
    }

    if verbosity > 0:
        print(
            f"[lightgbm] train_acc={train_acc:.4f}  val_acc={val_acc:.4f}  test_acc={test_acc:.4f}  "
            f"time={train_time:.1f}s  best_iter={metrics['best_iteration']}"
        )
        print(
            f"[lightgbm] Top 5 perm importance: "
            f"{', '.join(f'{n}({imp:+.4f})' for n, imp, _ in top_features[:5])}"
        )
        print(
            f"[lightgbm] Simulated test PnL: sharpe={test_pnl_r_sim.get('sharpe', 0):.2f}  "
            f"total_r={test_pnl_r_sim.get('total_pnl_r', 0):.2f}  "
            f"win_rate={test_pnl_r_sim.get('win_rate', 0):.1%}"
        )

    return model, metrics


# ── Metrics helpers ──


def _per_class_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, Any]:
    """Per-class precision, recall, f1."""
    metrics: dict[str, Any] = {}
    for label, name in [(-1, "short"), (0, "neutral"), (1, "long")]:
        tp = int(np.sum((y_true == label) & (y_pred == label)))
        fp = int(np.sum((y_true != label) & (y_pred == label)))
        fn = int(np.sum((y_true == label) & (y_pred != label)))
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        f1 = 2 * precision * recall / max(precision + recall, 1e-12)
        metrics[name] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": int(np.sum(y_true == label)),
        }
    return metrics


def _simulate_barrier_pnl(
    y_true: np.ndarray, y_pred: np.ndarray, contract_id: str
) -> dict[str, Any]:
    """Simulate P&L by trading only when model predicts non-neutral."""
    if "5d" in contract_id:
        sl_mult, tp_mult = 2.0, 3.5
    elif "10d" in contract_id:
        sl_mult, tp_mult = 2.0, 4.0
    elif "20d" in contract_id:
        sl_mult, tp_mult = 2.5, 5.0
    else:
        sl_mult, tp_mult = 2.0, 3.5

    pnl_list: list[float] = []
    wins = 0
    losses = 0

    for true_lbl, pred_lbl in zip(y_true, y_pred, strict=False):
        if pred_lbl == 0:
            continue
        if pred_lbl == true_lbl == 1:
            pnl_list.append(tp_mult)
            wins += 1
        elif pred_lbl == true_lbl == -1:
            pnl_list.append(tp_mult)
            wins += 1
        elif pred_lbl == 1 and true_lbl == -1:
            pnl_list.append(-sl_mult)
            losses += 1
        elif pred_lbl == -1 and true_lbl == 1:
            pnl_list.append(-sl_mult)
            losses += 1
        elif true_lbl == 0:
            pnl_list.append(0.0)

    total_pnl = sum(pnl_list) if pnl_list else 0.0
    n_trades = len(pnl_list)
    win_rate = wins / n_trades if n_trades > 0 else 0.0

    pnl_arr = np.array(pnl_list) if pnl_list else np.zeros(1)
    sharpe = (
        float(np.mean(pnl_arr) / np.std(pnl_arr) * np.sqrt(252 / 5))
        if np.std(pnl_arr) > 1e-12
        else 0.0
    )
    max_dd = 0.0
    cumsum = 0.0
    peak = -1e18
    for p in pnl_arr:
        cumsum += p
        if cumsum > peak:
            peak = cumsum
        dd = peak - cumsum
        if dd > max_dd:
            max_dd = dd

    return {
        "total_pnl_r": round(total_pnl, 2),
        "n_trades": n_trades,
        "win_rate": round(win_rate, 4),
        "sharpe": round(sharpe, 2),
        "max_drawdown_r": round(max_dd, 2),
        "avg_pnl_per_trade": round(total_pnl / max(n_trades, 1), 4),
        "sl_mult": sl_mult,
        "tp_mult": tp_mult,
    }


# ── Model persistence ──


def save_model_xgboost(
    model: Any, metrics: dict[str, Any], output_dir: Path, contract_id: str
) -> Path:
    """Save XGBoost model in brain-compatible JSON format."""
    output_dir.mkdir(parents=True, exist_ok=True)

    model_path = output_dir / f"xgboost_d1_{contract_id}.json"
    model.save_model(str(model_path))

    metrics_path = output_dir / f"xgboost_d1_{contract_id}.result.json"
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)

    print(f"[xgboost] Model saved to {model_path}")
    return model_path


def save_model_lightgbm(
    model: Any, metrics: dict[str, Any], output_dir: Path, contract_id: str
) -> Path:
    """Save LightGBM model in brain-compatible TXT format."""
    output_dir.mkdir(parents=True, exist_ok=True)

    model_path = output_dir / f"lightgbm_d1_{contract_id}.txt"
    model.booster_.save_model(str(model_path))

    metrics_path = output_dir / f"lightgbm_d1_{contract_id}.result.json"
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)

    print(f"[lightgbm] Model saved to {model_path}")
    return model_path


# ── Main ──


def main():
    parser = argparse.ArgumentParser(prog="train_daily_swing")
    parser.add_argument("--dataset", type=Path, required=True, help="Path to training NPZ file")
    parser.add_argument(
        "--model", type=str, default="all", help=f"Model type: {', '.join(AVAILABLE_MODELS)}"
    )
    parser.add_argument("--output-dir", type=Path, default=Path("data/models/daily"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--verbose", type=int, default=1)
    args = parser.parse_args()

    if args.model not in AVAILABLE_MODELS:
        print(f"Unknown model '{args.model}'. Available: {AVAILABLE_MODELS}")
        return 2

    if not args.dataset.exists():
        print(f"Dataset not found: {args.dataset}")
        return 2

    print(f"{'='*60}")
    print(f"[train_daily_swing] Dataset: {args.dataset}")
    print(f"[train_daily_swing] Model: {args.model}")
    print(f"[train_daily_swing] Output: {args.output_dir}")

    dataset = _load_dataset(args.dataset)
    contract_id = dataset["meta"].get("contract_id", args.dataset.stem)
    print(f"[train_daily_swing] Contract: {contract_id}")

    models_to_train = ["xgboost", "lightgbm"] if args.model == "all" else [args.model]

    all_metrics: dict[str, Any] = {
        "contract_id": contract_id,
        "dataset_path": str(args.dataset),
        "trained_at_utc": datetime.now(UTC).isoformat(),
        "models": {},
    }

    for model_type in models_to_train:
        print(f"\n{'─'*40}")
        if model_type == "xgboost":
            try:
                model, metrics = train_xgboost(dataset, seed=args.seed, verbosity=args.verbose)
                save_model_xgboost(model, metrics, args.output_dir, contract_id)
                all_metrics["models"]["xgboost"] = metrics
            except Exception as exc:  # noqa: BLE001
                print(f"[xgboost] Training failed: {exc}")
                all_metrics["models"]["xgboost"] = {"error": str(exc)}

        elif model_type == "lightgbm":
            try:
                model, metrics = train_lightgbm(dataset, seed=args.seed, verbosity=args.verbose)
                save_model_lightgbm(model, metrics, args.output_dir, contract_id)
                all_metrics["models"]["lightgbm"] = metrics
            except Exception as exc:  # noqa: BLE001
                print(f"[lightgbm] Training failed: {exc}")
                all_metrics["models"]["lightgbm"] = {"error": str(exc)}

    summary_path = args.output_dir / f"training_summary_{contract_id}.json"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(all_metrics, f, indent=2, ensure_ascii=False)
    print(f"\n[train_daily_swing] Summary saved to {summary_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
