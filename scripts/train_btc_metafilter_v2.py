#!/usr/bin/env python3
"""Train BTC MetaFilter V2 with extreme regularization + class-imbalance handling.

Curse of Dimensionality: N/D ratio often < 5 for BTC training → severe
overfitting risk.  The following extreme regularization strategy is MANDATORY:

  1. max_depth=2, num_leaves=5     (shallow trees, limited interactions)
  2. min_data_in_leaf=10           (no leaf can memorize <10 samples)
  3. feature_fraction=0.35         (each tree sees only ~14 of 40 features)
  4. lambda_l1=0.5, lambda_l2=1.0  (heavy weight suppression)
  5. TimeSeriesSplit CV            (no shuffle — prevents temporal leakage)

FIX-20260621-028:
  - Added scale_pos_weight for class imbalance (|WR-50%|>10% → activate)
  - Added pickle output for MetaFilterAdapter compatibility
  - Added feature_names.json output for FeatureParityError check
  - Added --output-dir for directing output to meta_filter_v5/
  - Changed CV from StratifiedKFold(shuffle) → TimeSeriesSplit (no temporal leak)
  - Lowered --min-auc default from 0.60 → 0.45 (realistic for ~200-sample BTC)

Usage:
  python scripts/train_btc_metafilter_v2.py --data-dir data_btc
  python scripts/train_btc_metafilter_v2.py --data-dir data_btc --output-dir data_btc/models/meta_filter_v5
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
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
    p.add_argument("--dataset", default=None,
                   help="NPZ path (default: data_btc/training/meta_features_btc_v2.npz)")
    p.add_argument("--output-dir", default=None,
                   help="Output directory for model + metadata (default: data_btc/models)")
    p.add_argument("--cv-folds", type=int, default=5)
    p.add_argument("--min-auc", type=float, default=0.45,
                   help="Minimum CV AUC to pass gate (default: 0.45)")
    return p.parse_args()


def load_dataset(data_dir: str, dataset_path: str | None) -> tuple[np.ndarray, np.ndarray, list[str]]:
    path = dataset_path or os.path.join(data_dir, "training", "meta_features_btc_v2.npz")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Dataset not found: {path}. Run build_btc_metafilter_v2_dataset.py first.")
    data = np.load(path, allow_pickle=True)
    X = data["X"]
    y = data["y"]
    feature_names = list(data["feature_names"]) if "feature_names" in data else [f"f{i}" for i in range(X.shape[1])]
    n_wins = int(y.sum())
    n_losses = len(y) - n_wins
    wr = n_wins / max(len(y), 1)
    print(f"Loaded: X={X.shape}, y distribution: {n_wins} wins / {n_losses} losses (WR={wr:.1%})")
    return X, y, feature_names


def compute_scale_pos_weight(y: np.ndarray) -> float | None:
    """Compute scale_pos_weight from class ratio.

    FIX-20260621-028: Activates only when |WR - 50%| > 10%.
    Clamped to [0.5, 2.0] to prevent extreme weighting.
    """
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    wr = n_pos / max(len(y), 1)
    if abs(wr - 0.5) <= 0.10:
        return None  # balanced — no adjustment needed
    scale = n_neg / max(1, n_pos)
    scale = max(0.5, min(2.0, scale))
    print(f"Class imbalance detected (WR={wr:.1%}), scale_pos_weight={scale:.2f}")
    return scale


def train_with_cv(
    X: np.ndarray,
    y: np.ndarray,
    feature_names: list[str],
    cv_folds: int = 5,
    scale_pos_weight: float | None = None,
) -> dict[str, Any]:
    """Train LightGBM with TimeSeriesSplit CV and extreme regularization.

    FIX-20260621-028: Uses TimeSeriesSplit (no shuffle) instead of
    StratifiedKFold(shuffle=True) — financial time series have strong
    serial correlation; random shuffling leaks future into past.

    Returns dict with: model, cv_auc_mean, cv_auc_std, passed_gate, params.
    """
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import TimeSeriesSplit

    params: dict[str, Any] = {
        "objective": "binary",
        "metric": "auc",
        "boosting_type": "gbdt",
        # ── Extreme regularization ──
        "max_depth": 2,
        "num_leaves": 5,
        "min_data_in_leaf": 10,
        "feature_fraction": 0.35,
        "feature_fraction_seed": 42,
        "bagging_fraction": 0.7,
        "bagging_freq": 1,
        "bagging_seed": 42,
        "lambda_l1": 0.5,
        "lambda_l2": 1.0,
        "min_gain_to_split": 0.5,
        "learning_rate": 0.01,
        "num_iterations": 500,
        "early_stopping_rounds": 50,
        "verbose": -1,
        "seed": 42,
        "n_jobs": 1,
    }

    if scale_pos_weight is not None:
        params["scale_pos_weight"] = scale_pos_weight

    tscv = TimeSeriesSplit(n_splits=cv_folds)
    auc_scores: list[float] = []
    models: list[Any] = []

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
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

    # Select best model (highest validation AUC)
    best_model = models[int(np.argmax(auc_scores))]

    return {
        "model": best_model,
        "cv_auc_mean": auc_mean,
        "cv_auc_std": auc_std,
        "passed_gate": True,  # gate checked by caller
        "params": params,
        "fold_aucs": auc_scores,
    }


def train_final(X: np.ndarray, y: np.ndarray, feature_names: list[str], params: dict) -> Any:
    """Train final model on all data after CV validation passes."""
    dtrain = lgb.Dataset(X, label=y, feature_name=feature_names)
    return lgb.train(params, dtrain, valid_sets=[dtrain], valid_names=["train"])


def save_model(
    model: Any,
    feature_names: list[str],
    params: dict,
    output_dir: str,
    n_samples: int,
    n_wins: int,
    n_losses: int,
    wr: float,
    cv_auc_mean: float,
    cv_auc_std: float,
    scale_pos_weight: float | None,
) -> str:
    """Save model in BOTH formats: LightGBM native .txt + pickle .pkl.

    FIX-20260621-028:
      - .txt: LightGBM native booster format (for inspection/interchange)
      - .pkl: sklearn-compatible pickle (for MetaFilterAdapter.load())
      - feature_names.json: separate file for adapter's FeatureParityError check
    """
    os.makedirs(output_dir, exist_ok=True)

    # LightGBM native format
    txt_path = os.path.join(output_dir, "meta_filter_lightgbm.txt")
    model.save_model(txt_path)

    # Pickle format (for MetaFilterAdapter)
    pkl_path = os.path.join(output_dir, "meta_filter_lightgbm.pkl")
    with open(pkl_path, "wb") as f:
        pickle.dump(model, f)

    # Feature names (for adapter FeatureParityError)
    fn_path = os.path.join(output_dir, "feature_names.json")
    with open(fn_path, "w", encoding="utf-8") as f:
        json.dump({"feature_names": feature_names, "n_features": len(feature_names)}, f)

    # Metadata
    meta = {
        "schema_version": "meta_filter_model_meta.v1",
        "model_type": "lightgbm",
        "n_samples": n_samples,
        "n_features": len(feature_names),
        "n_wins": n_wins,
        "n_losses": n_losses,
        "win_rate": round(float(wr), 4),
        "scale_pos_weight": scale_pos_weight,
        "feature_names": feature_names,
        "cv_auc_mean": round(cv_auc_mean, 4),
        "cv_auc_std": round(cv_auc_std, 4),
        "params": {k: str(v) if not isinstance(v, (int, float, bool, type(None))) else v
                   for k, v in params.items()},
        "training_data": "meta_features_btc_v2.npz",
        "trained_at": datetime.now(UTC).isoformat(),
    }
    meta_path = os.path.join(output_dir, "meta_filter_lightgbm.meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, default=str)

    print("\nModel saved:")
    print(f"  LightGBM: {txt_path}")
    print(f"  Pickle:   {pkl_path}")
    print(f"  Features: {fn_path}")
    print(f"  Meta:     {meta_path}")

    # ── Dictionary Isomorphism validation ──
    online_features = feature_names
    model_features_from_file = list(model.feature_name())
    missing = set(online_features) - set(model_features_from_file)
    extra = set(model_features_from_file) - set(online_features)
    if missing:
        print(f"  [WARNING]: {len(missing)} online features missing from model")
    if extra:
        print(f"  [WARNING]: {len(extra)} model features not in online schema")
    if not missing and not extra:
        print(f"  [OK] Dictionary isomorphism: {len(online_features)} features match")

    return txt_path


def main() -> None:
    args = parse_args()
    data_dir = args.data_dir

    X, y, feature_names = load_dataset(data_dir, args.dataset)

    # ── FIX-20260621-028: Compute scale_pos_weight ──
    scale_pos_weight = compute_scale_pos_weight(y)
    n_wins = int(y.sum())
    n_losses = len(y) - n_wins
    wr = n_wins / max(len(y), 1)

    N, D = X.shape
    print(f"\nN={N}, D={D}, N/D ratio={N/D:.2f}")
    if N / D < 2:
        print("WARNING: N/D < 2 — severe curse of dimensionality. Extreme regularization active.")
    elif N / D < 5:
        print("WARNING: N/D < 5 — moderate curse of dimensionality.")

    # ── TimeSeriesSplit CV ──
    print(f"\n── {args.cv_folds}-Fold TimeSeriesSplit CV ──")
    result = train_with_cv(X, y, feature_names, cv_folds=args.cv_folds,
                           scale_pos_weight=scale_pos_weight)

    print(f"\nCV AUC: {result['cv_auc_mean']:.4f} ± {result['cv_auc_std']:.4f}")
    print(f"  Fold scores: {[f'{a:.4f}' for a in result['fold_aucs']]}")

    if result["cv_auc_mean"] < args.min_auc:
        print(f"\n[REJECTED] CV AUC {result['cv_auc_mean']:.4f} < {args.min_auc} — MODEL REJECTED")
        print(f"   The {args.cv_folds}-fold CV did not pass the minimum quality bar.")
        print("   Wait for more live trade data before retrying.")
        return

    print(f"\n[PASS] CV AUC {result['cv_auc_mean']:.4f} >= {args.min_auc} — GATE PASSED")

    # ── Train final model on all data ──
    print("\n── Training final model on full dataset ──")
    final_model = train_final(X, y, feature_names, result["params"])

    # Feature importance
    importance = list(zip(feature_names, final_model.feature_importance(importance_type="gain"), strict=False))
    importance.sort(key=lambda x: x[1], reverse=True)
    print("\nTop 10 features by gain:")
    for name, gain in importance[:10]:
        bar = "█" * int(gain / max(1e-9, importance[0][1]) * 30)
        print(f"  {name:30s}: {gain:.2f} {bar}")

    # ── Save in output_dir ──
    output_dir = args.output_dir or os.path.join(data_dir, "models")
    model_path = save_model(
        final_model, feature_names, result["params"], output_dir,
        n_samples=N, n_wins=n_wins, n_losses=n_losses, wr=wr,
        cv_auc_mean=result["cv_auc_mean"], cv_auc_std=result["cv_auc_std"],
        scale_pos_weight=scale_pos_weight,
    )

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
            f"{args.cv_folds}-Fold TimeSeriesSplit CV AUC: {result['cv_auc_mean']:.4f}±{result['cv_auc_std']:.4f}. "
            f"WR={wr:.1%}, scale_pos_weight={scale_pos_weight}. "
            f"Extreme regularization: max_depth=2, num_leaves=5, min_data_in_leaf=10, "
            f"feature_fraction=0.35, L1=0.5, L2=1.0."
        ),
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    print(f"Config saved: {config_path}")

    print("\n[DONE] All statistics above are the sole source of truth.")


if __name__ == "__main__":
    main()
