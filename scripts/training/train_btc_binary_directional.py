#!/usr/bin/env python
"""BTC Binary Directional Training — LONG vs SHORT (no NEUTRAL).

Loads a multi-class train.npz, filters out NEUTRAL samples, remaps
SHORT→0 / LONG→1, and trains binary XGBoost + LightGBM classifiers.

This solves the NEUTRAL-dominance problem: with symmetric SL=TP=1.5
and a 12-bar horizon, ~90% of bars have a clear directional outcome.
Removing the NEUTRAL class forces the model to pick a side.

Usage:
  python scripts/training/train_btc_binary_directional.py \
    --npz data_btc/training/btc_swing_h1_retrain/train.npz \
    --model-dir data_btc/models/btc_swing_h1_binary \
    --label H1

  python scripts/training/train_btc_binary_directional.py \
    --npz data_btc/training/btc_swing_m5_retrain/train.npz \
    --model-dir data_btc/models/btc_swing_m5_binary \
    --label M5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def build_binary_dataset(npz_path: str) -> dict[str, Any]:
    """Load multi-class NPZ, filter NEUTRAL, return binary X/y."""
    data = np.load(npz_path)
    X_all = data["X"]
    y_all = data["y"]

    # Multi-class encoding: 0=SHORT, 1=NEUTRAL, 2=LONG
    # Keep only non-NEUTRAL samples
    binary_mask = y_all != 1  # exclude NEUTRAL
    X = X_all[binary_mask]
    y_raw = y_all[binary_mask]

    # Remap: SHORT(0)→0, LONG(2)→1
    y = np.where(y_raw == 0, 0, 1)  # SHORT=0, LONG=1

    n_short = int((y == 0).sum())
    n_long = int((y == 1).sum())

    print(f"  Binary dataset: {len(y):,} samples (SHORT={n_short}, LONG={n_long})")
    print(f"  Balance: {n_short/len(y)*100:.1f}% SHORT / {n_long/len(y)*100:.1f}% LONG")

    return {
        "X": X,
        "y": y,
        "n_samples": len(y),
        "n_features": X.shape[1],
        "n_short": n_short,
        "n_long": n_long,
    }


def train_xgboost_binary(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    seed: int = 42,
) -> tuple[Any, dict[str, float]]:
    """Train binary XGBoost classifier."""
    import xgboost as xgb

    # Scale positive weight to handle class imbalance
    n_pos = int(y_train.sum())
    n_neg = len(y_train) - n_pos
    scale_pos_weight = n_neg / n_pos if n_pos > 0 else 1.0

    dtrain = xgb.DMatrix(X_train, label=y_train)
    dval = xgb.DMatrix(X_val, label=y_val)

    params = {
        "objective": "binary:logistic",
        "eval_metric": "auc",
        "max_depth": 5,
        "learning_rate": 0.02,
        "n_estimators": 500,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "scale_pos_weight": scale_pos_weight,
        "seed": seed,
        "n_jobs": -2,
    }

    model = xgb.train(
        params,
        dtrain,
        num_boost_round=500,
        evals=[(dval, "val")],
        early_stopping_rounds=50,
        verbose_eval=False,
    )

    # Evaluate
    y_pred_prob = model.predict(dval)
    y_pred = (y_pred_prob >= 0.5).astype(int)

    acc = float((y_pred == y_val).mean())
    # Win rate: of correct predictions, how many were correct
    tp = int(((y_pred == 1) & (y_val == 1)).sum())
    tn = int(((y_pred == 0) & (y_val == 0)).sum())
    fp = int(((y_pred == 1) & (y_val == 0)).sum())
    fn = int(((y_pred == 0) & (y_val == 1)).sum())

    precision_long = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    precision_short = tn / (tn + fn) if (tn + fn) > 0 else 0.0
    wr = (precision_long + precision_short) / 2.0  # balanced WR

    metrics = {
        "val_acc": acc,
        "val_wr": wr,
        "val_precision_long": precision_long,
        "val_precision_short": precision_short,
        "n_trees": model.best_iteration if hasattr(model, "best_iteration") else 500,
        "scale_pos_weight": scale_pos_weight,
    }

    return model, metrics


def train_lightgbm_binary(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    seed: int = 42,
) -> tuple[Any, dict[str, float]]:
    """Train binary LightGBM classifier."""
    import lightgbm as lgb

    n_pos = int(y_train.sum())
    n_neg = len(y_train) - n_pos
    scale_pos_weight = n_neg / n_pos if n_pos > 0 else 1.0

    dtrain = lgb.Dataset(X_train, label=y_train)
    dval = lgb.Dataset(X_val, label=y_val, reference=dtrain)

    params = {
        "objective": "binary",
        "metric": "auc",
        "max_depth": 5,
        "learning_rate": 0.02,
        "num_leaves": 31,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "scale_pos_weight": scale_pos_weight,
        "seed": seed,
        "n_jobs": -2,
        "verbosity": -1,
    }

    model = lgb.train(
        params,
        dtrain,
        num_boost_round=500,
        valid_sets=[dval],
        callbacks=[lgb.early_stopping(50), lgb.log_evaluation(0)],
    )

    y_pred_prob = model.predict(X_val)
    y_pred = (y_pred_prob >= 0.5).astype(int)

    acc = float((y_pred == y_val).mean())
    tp = int(((y_pred == 1) & (y_val == 1)).sum())
    tn = int(((y_pred == 0) & (y_val == 0)).sum())
    fp = int(((y_pred == 1) & (y_val == 0)).sum())
    fn = int(((y_pred == 0) & (y_val == 1)).sum())

    precision_long = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    precision_short = tn / (tn + fn) if (tn + fn) > 0 else 0.0
    wr = (precision_long + precision_short) / 2.0

    metrics = {
        "val_acc": acc,
        "val_wr": wr,
        "val_precision_long": precision_long,
        "val_precision_short": precision_short,
        "n_trees": model.best_iteration if hasattr(model, "best_iteration") else 500,
        "scale_pos_weight": scale_pos_weight,
    }

    return model, metrics


def walk_forward_splits(n_samples: int, n_folds: int = 5) -> list[dict]:
    """Chronological walk-forward splits."""
    splits = []
    fold_size = n_samples // (n_folds + 1)
    for fold in range(n_folds):
        test_start = n_samples - (n_folds - fold) * fold_size
        test_end = min(n_samples, test_start + fold_size)
        if test_start <= 0:
            continue
        train_idx = np.arange(0, test_start)
        test_idx = np.arange(test_start, test_end)
        splits.append({"fold": fold, "train_idx": train_idx, "test_idx": test_idx})
    return splits


def main():
    parser = argparse.ArgumentParser(description="BTC Binary Directional Training")
    parser.add_argument("--npz", required=True, help="Path to multi-class train.npz")
    parser.add_argument("--model-dir", required=True, help="Output directory for models")
    parser.add_argument("--label", default="BTC", help="Label for summary")
    parser.add_argument("--n-folds", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print(f"[BINARY] Loading {args.npz}...")
    ds = build_binary_dataset(args.npz)

    X = ds["X"]
    y = ds["y"]
    n = ds["n_samples"]

    print(f"[BINARY] Walk-forward CV ({args.n_folds} folds)...")
    splits = walk_forward_splits(n, args.n_folds)

    os.makedirs(args.model_dir, exist_ok=True)

    xgb_results: list[dict] = []
    lgb_results: list[dict] = []

    for split in splits:
        fold = split["fold"]
        train_idx = split["train_idx"]
        test_idx = split["test_idx"]

        X_train, y_train = X[train_idx], y[train_idx]
        X_val, y_val = X[test_idx], y[test_idx]

        n_train = len(train_idx)
        n_val = len(test_idx)
        val_short = int((y_val == 0).sum())
        val_long = int((y_val == 1).sum())

        print(f"\n{'='*60}")
        print(f"[BINARY] Fold {fold}: train={n_train:,}, test={n_val:,}")
        print(f"[BINARY] Test dist: SHORT={val_short}, LONG={val_long}")

        # XGBoost
        print(f"[BINARY] --- XGBoost (fold {fold}) ---")
        xgb_model, xgb_metrics = train_xgboost_binary(
            X_train, y_train, X_val, y_val, seed=args.seed
        )
        print(
            f"  acc={xgb_metrics['val_acc']:.3f}, wr={xgb_metrics['val_wr']:.3f}, "
            f"trees={xgb_metrics['n_trees']}"
        )
        xgb_path = os.path.join(args.model_dir, f"xgboost_fold{fold}_s{args.seed}.json")
        xgb_model.save_model(xgb_path)
        print(f"  Saved: {xgb_path}")
        xgb_results.append({"fold": fold, "metrics": xgb_metrics})

        # LightGBM
        print(f"[BINARY] --- LightGBM (fold {fold}) ---")
        lgb_model, lgb_metrics = train_lightgbm_binary(
            X_train, y_train, X_val, y_val, seed=args.seed
        )
        print(
            f"  acc={lgb_metrics['val_acc']:.3f}, wr={lgb_metrics['val_wr']:.3f}, "
            f"trees={lgb_metrics['n_trees']}"
        )
        lgb_path = os.path.join(args.model_dir, f"lightgbm_fold{fold}_s{args.seed}.txt")
        lgb_model.save_model(lgb_path)
        print(f"  Saved: {lgb_path}")
        lgb_results.append({"fold": fold, "metrics": lgb_metrics})

    # ── Summary ──
    print(f"\n{'='*60}")
    print(f"[BINARY] === Walk-Forward CV Summary ({args.label}) ===")

    xgb_wrs = [r["metrics"]["val_wr"] for r in xgb_results]
    xgb_accs = [r["metrics"]["val_acc"] for r in xgb_results]
    lgb_wrs = [r["metrics"]["val_wr"] for r in lgb_results]
    lgb_accs = [r["metrics"]["val_acc"] for r in lgb_results]

    print(
        f"  XGBoost: WR={np.mean(xgb_wrs)*100:.1f}% ± {np.std(xgb_wrs)*100:.1f}%, "
        f"Acc={np.mean(xgb_accs)*100:.1f}% ± {np.std(xgb_accs)*100:.1f}%"
    )
    print(
        f"  LightGBM: WR={np.mean(lgb_wrs)*100:.1f}% ± {np.std(lgb_wrs)*100:.1f}%, "
        f"Acc={np.mean(lgb_accs)*100:.1f}% ± {np.std(lgb_accs)*100:.1f}%"
    )

    # Direction diversity check
    print("\n[BINARY] === Direction Diversity Gate ===")
    # Load best model and check prediction distribution
    best_fold = max(xgb_results, key=lambda r: r["metrics"]["val_wr"])
    best_model_path = os.path.join(
        args.model_dir, f"xgboost_fold{best_fold['fold']}_s{args.seed}.json"
    )
    import xgboost as xgb

    best_xgb = xgb.Booster()
    best_xgb.load_model(best_model_path)
    d_all = xgb.DMatrix(X)
    y_prob = best_xgb.predict(d_all)
    y_pred = (y_prob >= 0.5).astype(int)
    n_long_pred = int(y_pred.sum())
    n_short_pred = len(y_pred) - n_long_pred
    print(f"  Predictions: LONG={n_long_pred}, SHORT={n_short_pred}")
    print(f"  LONG ratio: {n_long_pred/len(y_pred)*100:.1f}%")
    if 30 <= n_long_pred / len(y_pred) * 100 <= 70:
        print("  VERDICT: Direction-balanced ✅")
    else:
        print("  VERDICT: Direction-biased ⚠️")

    # Save summary
    summary = {
        "schema_version": "btc_binary_directional.v1",
        "label": args.label,
        "npz_source": args.npz,
        "objective": "binary:logistic",
        "n_samples": n,
        "n_features": ds["n_features"],
        "n_short": ds["n_short"],
        "n_long": ds["n_long"],
        "cv_summary": {
            "xgboost": {
                "mean_val_wr": float(np.mean(xgb_wrs)),
                "std_val_wr": float(np.std(xgb_wrs)),
                "mean_val_acc": float(np.mean(xgb_accs)),
                "std_val_acc": float(np.std(xgb_accs)),
                "folds": len(xgb_results),
            },
            "lightgbm": {
                "mean_val_wr": float(np.mean(lgb_wrs)),
                "std_val_wr": float(np.std(lgb_wrs)),
                "mean_val_acc": float(np.mean(lgb_accs)),
                "std_val_acc": float(np.std(lgb_accs)),
                "folds": len(lgb_results),
            },
        },
        "direction_diversity": {
            "n_long_pred": n_long_pred,
            "n_short_pred": n_short_pred,
            "long_ratio": float(n_long_pred / len(y_pred)),
        },
        "trained_at": datetime.now(UTC).isoformat(),
    }
    summary_path = os.path.join(args.model_dir, "training_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\n[DONE] Summary saved to {summary_path}")


if __name__ == "__main__":
    main()
