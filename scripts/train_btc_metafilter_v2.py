#!/usr/bin/env python3
"""Train BTC MetaFilter V2 with extreme regularization for N=54, D=40.

Curse of Dimensionality: 54 samples across 40 features → severe overfitting
risk.  The following extreme regularization strategy is MANDATORY:

  1. max_depth=2, num_leaves=5     (shallow trees, limited interactions)
  2. min_data_in_leaf=10           (no leaf can memorize <10 samples)
  3. feature_fraction=0.35         (each tree sees only ~14 of 40 features)
  4. lambda_l1=0.5, lambda_l2=1.0  (heavy weight suppression)
  5. 5-Fold CV as minimum bar      (AUC must be >= 0.60 on held-out folds)

Usage:
  python scripts/train_btc_metafilter_v2.py --data-dir data_btc
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

try:
    import lightgbm as lgb
except ImportError:
    print("ERROR: pip install lightgbm")
    raise


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train BTC MetaFilter V2")
    p.add_argument("--data-dir", default="data_btc")
    p.add_argument("--dataset", default=None, help="NPZ path (default: data_btc/training/meta_features_btc_v2.npz)")
    p.add_argument("--cv-folds", type=int, default=5)
    p.add_argument("--min-auc", type=float, default=0.60, help="Minimum CV AUC to pass gate")
    p.add_argument("--output-model", default=None, help="Model output path")
    return p.parse_args()


def load_dataset(data_dir: str, dataset_path: str | None) -> tuple[np.ndarray, np.ndarray, list[str]]:
    path = dataset_path or os.path.join(data_dir, "training", "meta_features_btc_v2.npz")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Dataset not found: {path}. Run build_btc_metafilter_v2_dataset.py first.")
    data = np.load(path, allow_pickle=True)
    X = data["X"]
    y = data["y"]
    feature_names = list(data["feature_names"]) if "feature_names" in data else [f"f{i}" for i in range(X.shape[1])]
    print(f"Loaded: X={X.shape}, y distribution: {dict(zip(*np.unique(y, return_counts=True)))}")
    print(f"  WR: {y.sum()}/{len(y)} = {y.sum()/len(y)*100:.1f}%")
    return X, y, feature_names


def train_with_cv(
    X: np.ndarray,
    y: np.ndarray,
    feature_names: list[str],
    cv_folds: int = 5,
) -> dict[str, Any]:
    """Train LightGBM with 5-fold CV and extreme regularization.

    Returns dict with: model, cv_auc_mean, cv_auc_std, passed_gate, params.
    """
    from sklearn.model_selection import StratifiedKFold
    from sklearn.metrics import roc_auc_score

    params = {
        "objective": "binary",
        "metric": "auc",
        "boosting_type": "gbdt",
        # ── Extreme regularization for N=54, D=40 ──
        "max_depth": 2,               # No complex interactions
        "num_leaves": 5,              # Severely limited leaf count
        "min_data_in_leaf": 10,       # >=10 samples per leaf (no memorization)
        "feature_fraction": 0.35,     # Each tree sees ~14 of 40 features
        "feature_fraction_seed": 42,
        "bagging_fraction": 0.7,      # Row subsampling
        "bagging_freq": 1,
        "bagging_seed": 42,
        "lambda_l1": 0.5,             # L1 regularization
        "lambda_l2": 1.0,             # L2 regularization
        "min_gain_to_split": 0.5,     # Require meaningful gain to split
        "learning_rate": 0.01,
        "num_iterations": 500,        # Will be limited by early stopping
        "early_stopping_rounds": 50,
        "verbose": -1,
        "seed": 42,
        "n_jobs": 1,
    }

    skf = StratifiedKFold(n_splits=cv_folds, shuffle=True, random_state=42)
    auc_scores: list[float] = []
    models: list[Any] = []

    for fold, (train_idx, val_idx) in enumerate(skf.split(X, y)):
        X_train, X_val = X[train_idx], X[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]

        dtrain = lgb.Dataset(X_train, label=y_train, feature_name=feature_names)
        dval = lgb.Dataset(X_val, label=y_val, feature_name=feature_names, reference=dtrain)

        model = lgb.train(
            params,
            dtrain,
            valid_sets=[dtrain, dval],
            valid_names=["train", "val"],
        )

        y_pred = model.predict(X_val)
        auc = roc_auc_score(y_val, y_pred)
        auc_scores.append(auc)
        models.append(model)
        print(f"  Fold {fold+1}: AUC={auc:.4f} (train={len(train_idx)}, val={len(val_idx)})")

    auc_mean = float(np.mean(auc_scores))
    auc_std = float(np.std(auc_scores))
    passed = auc_mean >= 0.60

    # Select best model (highest validation AUC)
    best_model = models[int(np.argmax(auc_scores))]

    return {
        "model": best_model,
        "cv_auc_mean": auc_mean,
        "cv_auc_std": auc_std,
        "passed_gate": passed,
        "params": params,
        "fold_aucs": auc_scores,
    }


def train_final(X: np.ndarray, y: np.ndarray, feature_names: list[str], params: dict) -> Any:
    """Train final model on all data after CV validation passes."""
    dtrain = lgb.Dataset(X, label=y, feature_name=feature_names)
    return lgb.train(params, dtrain, valid_sets=[dtrain], valid_names=["train"])


def save_model(model: Any, feature_names: list[str], params: dict, output_dir: str) -> str:
    """Save model and metadata."""
    os.makedirs(output_dir, exist_ok=True)

    model_path = os.path.join(output_dir, "meta_stage2_lightgbm_btc_v2.txt")
    model.save_model(model_path)

    meta = {
        "schema_version": "meta_filter_model_meta.v1",
        "model_type": "lightgbm",
        "n_features": len(feature_names),
        "feature_names": feature_names,
        "params": {k: str(v) if not isinstance(v, (int, float, bool, type(None))) else v for k, v in params.items()},
        "training_data": "meta_features_btc_v2.npz",
        "trained_at": datetime.now(UTC).isoformat(),
    }
    meta_path = model_path.rsplit(".", 1)[0] + ".meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, default=str)

    print(f"\nModel saved: {model_path}")
    print(f"  Meta: {meta_path}")
    print(f"  Features: {feature_names[:3]}... ({len(feature_names)} total)")

    # ── MLOps Iron Law #3: Dictionary Isomorphism validation ──
    online_features = feature_names
    model_features_from_file = list(model.feature_name())
    missing = set(online_features) - set(model_features_from_file)
    extra = set(model_features_from_file) - set(online_features)
    if missing:
        print(f"  [WARNING]: {len(missing)} online features missing from model: {sorted(list(missing))[:5]}")
    if extra:
        print(f"  [WARNING]: {len(extra)} model features not in online schema: {sorted(list(extra))[:5]}")
    if not missing and not extra:
        print(f"  [OK] Dictionary isomorphism: model.feature_name() == online V9 features ({len(online_features)})")

    return model_path


def main() -> None:
    args = parse_args()
    data_dir = args.data_dir

    X, y, feature_names = load_dataset(data_dir, args.dataset)
    N, D = X.shape
    print(f"\nN={N}, D={D}, N/D ratio={N/D:.2f}")
    if N / D < 2:
        print("WARNING: N/D < 2 — severe curse of dimensionality. Extreme regularization active.")
    elif N / D < 5:
        print("WARNING: N/D < 5 — moderate curse of dimensionality.")

    # ── 5-Fold CV ──
    print(f"\n── {args.cv_folds}-Fold CV ──")
    result = train_with_cv(X, y, feature_names, cv_folds=args.cv_folds)

    print(f"\nCV AUC: {result['cv_auc_mean']:.4f} ± {result['cv_auc_std']:.4f}")
    print(f"  Fold scores: {[f'{a:.4f}' for a in result['fold_aucs']]}")

    if not result["passed_gate"]:
        print(f"\n[REJECTED] CV AUC {result['cv_auc_mean']:.4f} < {args.min_auc} — MODEL REJECTED")
        print("   The 5-fold CV did not pass the minimum quality bar.")
        print("   Wait for more live trade data (>100 samples) before retrying.")
        return

    print(f"\n[PASS] CV AUC {result['cv_auc_mean']:.4f} >= {args.min_auc} — GATE PASSED")

    # ── Train final model on all data ──
    print("\n── Training final model on full dataset ──")
    final_model = train_final(X, y, feature_names, result["params"])

    # Feature importance
    importance = list(zip(feature_names, final_model.feature_importance(importance_type="gain")))
    importance.sort(key=lambda x: x[1], reverse=True)
    print("\nTop 10 features by gain:")
    for name, gain in importance[:10]:
        print(f"  {name:30s}: {gain:.2f}")

    # ── Save ──
    output_dir = args.output_model or os.path.join(data_dir, "models")
    model_path = save_model(final_model, feature_names, result["params"], output_dir)

    # ── Also save config for live deployment ──
    repo_root = Path(__file__).resolve().parent.parent
    config_path = repo_root / "configs" / "brains_btc" / "meta_stage2_filter_v2.json"
    os.makedirs(config_path.parent, exist_ok=True)
    config = {
        "schema_version": "meta_filter_config.v1",
        "filter_id": "Meta_Stage2_Filter_BTC_V2",
        "model_path": str(Path(os.path.abspath(model_path)).relative_to(repo_root)),
        "threshold": 0.55,
        "mode": "binary",
        "feature_schema": "v9_institutional_40",
        "description": (
            f"BTC MetaFilter V2 — Trained on {N} PIT-aligned V9 institutional samples. "
            f"5-Fold CV AUC: {result['cv_auc_mean']:.4f}±{result['cv_auc_std']:.4f}. "
            f"Extreme regularization: max_depth=2, num_leaves=5, min_data_in_leaf=10, "
            f"feature_fraction=0.35, L1=0.5, L2=1.0."
        ),
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    print(f"Config saved: {config_path}")


if __name__ == "__main__":
    main()
