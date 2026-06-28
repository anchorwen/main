#!/usr/bin/env python3
"""Train XAU MetaFilter — ML signal quality gate for XAUUSDc.

Unlike BTC (N=54, N/D=1.4, extreme overfitting risk), XAU has abundant
PIT-aligned data (N=20,142, N/D=429).  Moderate regularization suffices.

Key differences from BTC MetaFilter trainer:
  - No scale_pos_weight (dataset is naturally balanced 50/50)
  - More expressive trees (max_depth=5, num_leaves=31 vs BTC's 2/5)
  - Higher feature fraction (0.80 vs BTC's 0.35)
  - Lower L1/L2 (0.1/0.5 vs BTC's 0.5/1.0)

Usage:
  python scripts/train_xau_metafilter.py
  python scripts/train_xau_metafilter.py --dataset data/training/meta_features_v2_sl3_tp1.5.npz
  python scripts/train_xau_metafilter.py --output-dir data/models/meta_filter_v4
  python scripts/train_xau_metafilter.py --cv-folds 5 --min-auc 0.55
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


# ── CLI ────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train XAU MetaFilter LightGBM")
    p.add_argument(
        "--dataset",
        default="data/training/meta_features_runtime_v2.npz",
        help="NPZ dataset path (default: data/training/meta_features_runtime_v2.npz)",
    )
    p.add_argument(
        "--output-dir",
        default="data/models/meta_filter_v4",
        help="Output directory for model + metadata",
    )
    p.add_argument("--cv-folds", type=int, default=5)
    p.add_argument(
        "--min-auc",
        type=float,
        default=0.55,
        help="Minimum CV AUC to pass quality gate (default: 0.55)",
    )
    p.add_argument(
        "--no-config-update", action="store_true", help="Skip updating brain config (testing)"
    )
    return p.parse_args()


# ── Data loading ───────────────────────────────────────────────────────


def load_dataset(dataset_path: str) -> tuple[np.ndarray, np.ndarray, list[str]]:
    path = dataset_path
    if not os.path.isabs(path):
        path = str(Path(__file__).resolve().parent.parent / path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Dataset not found: {path}")
    data = np.load(path, allow_pickle=True)
    X = data["X"]
    y = data["y"]
    feature_names = (
        list(data["feature_names"])
        if "feature_names" in data
        else [f"f{i}" for i in range(X.shape[1])]
    )
    # Basic validation
    assert X.ndim == 2, f"Expected 2D X, got shape {X.shape}"
    assert len(y) == X.shape[0], f"X/y length mismatch: {X.shape[0]} vs {len(y)}"
    assert not np.any(np.isnan(X)), "NaN in features"
    assert not np.any(np.isinf(X)), "Inf in features"

    n_wins = int(y.sum())
    n_losses = len(y) - n_wins
    wr = n_wins / max(len(y), 1)
    print(f"Loaded: X={X.shape}, y distribution: {n_wins} wins / {n_losses} losses (WR={wr:.1%})")
    return X, y, list(feature_names)


# ── Training ───────────────────────────────────────────────────────────


def compute_scale_pos_weight(y: np.ndarray) -> float | None:
    """Compute scale_pos_weight from class ratio.

    Activates only when |WR - 50%| > 10%.  Clamped to [0.5, 3.0].
    XAU typically has WR=30-40% → scale=1.5-2.3.
    """
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    wr = n_pos / max(len(y), 1)
    if abs(wr - 0.50) <= 0.10:
        return None
    scale = n_neg / max(1, n_pos)
    scale = max(0.5, min(3.0, scale))
    print(f"Class imbalance detected (WR={wr:.1%}), scale_pos_weight={scale:.2f}")
    return scale


def get_training_params(scale_pos_weight: float | None = None) -> dict[str, Any]:
    """XAU MetaFilter training hyperparameters.

    Adapted to actual data volume (N≈1,300-1,500, D=40, N/D≈35).
    Moderate regularization — manageable sample size.
    """
    params: dict[str, Any] = {
        "objective": "binary",
        "metric": "auc",
        "boosting_type": "gbdt",
        # ── Conservative capacity (N=1,339, D=40, N/D=33) ──
        "max_depth": 3,
        "num_leaves": 7,
        "min_data_in_leaf": 30,
        # ── Feature sampling (stronger regularization) ──
        "feature_fraction": 0.50,
        "feature_fraction_seed": 42,
        "bagging_fraction": 0.60,
        "bagging_freq": 1,
        "bagging_seed": 42,
        # ── Regularization ──
        "lambda_l1": 0.5,
        "lambda_l2": 1.0,
        "min_gain_to_split": 0.10,
        # ── Learning rate ──
        "learning_rate": 0.02,
        "num_iterations": 500,
        "early_stopping_rounds": 30,
        # ── Reproducibility ──
        "verbose": -1,
        "seed": 42,
        "n_jobs": 1,
    }
    if scale_pos_weight is not None:
        params["scale_pos_weight"] = scale_pos_weight
    return params


def train_with_cv(
    X: np.ndarray,
    y: np.ndarray,
    feature_names: list[str],
    cv_folds: int = 5,
    scale_pos_weight: float | None = None,
) -> dict[str, Any]:
    """Train LightGBM with TimeSeriesSplit CV.

    TimeSeriesSplit (no shuffle) prevents temporal leakage — financial
    time series have strong serial correlation.
    """
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import TimeSeriesSplit

    params = get_training_params(scale_pos_weight)
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
        print(
            f"  Fold {fold+1}/{cv_folds}: AUC={auc:.4f} "
            f"(train={len(train_idx)}, val={len(val_idx)})"
        )

    auc_mean = float(np.mean(auc_scores))
    auc_std = float(np.std(auc_scores))

    # Best model by validation AUC
    best_model = models[int(np.argmax(auc_scores))]

    return {
        "model": best_model,
        "cv_auc_mean": auc_mean,
        "cv_auc_std": auc_std,
        "params": params,
        "fold_aucs": auc_scores,
    }


def train_final(X: np.ndarray, y: np.ndarray, feature_names: list[str], params: dict) -> Any:
    """Train final model on all data after CV validation passes.

    Returns an LGBMClassifier (sklearn-compatible) for MetaFilterAdapter
    compatibility.  The adapter calls predict_proba() which is only available
    on sklearn wrappers, not native LightGBM Boosters.
    """
    from lightgbm import LGBMClassifier

    # Extract sklearn-compatible params from native params
    sklearn_params = {
        "objective": "binary",
        "metric": "auc",
        "boosting_type": "gbdt",
        "max_depth": params.get("max_depth", 3),
        "num_leaves": params.get("num_leaves", 7),
        "min_data_in_leaf": params.get("min_data_in_leaf", 30),
        "feature_fraction": params.get("feature_fraction", 0.50),
        "bagging_fraction": params.get("bagging_fraction", 0.60),
        "bagging_freq": params.get("bagging_freq", 1),
        "lambda_l1": params.get("lambda_l1", 0.5),
        "lambda_l2": params.get("lambda_l2", 1.0),
        "min_gain_to_split": params.get("min_gain_to_split", 0.10),
        "learning_rate": params.get("learning_rate", 0.02),
        "n_estimators": params.get("num_iterations", 500),
        "verbose": -1,
        "random_state": params.get("seed", 42),
        "n_jobs": 1,
    }
    if "scale_pos_weight" in params:
        sklearn_params["scale_pos_weight"] = params["scale_pos_weight"]

    model = LGBMClassifier(**sklearn_params)
    model.fit(X, y)
    return model


# ── Evaluation ─────────────────────────────────────────────────────────


def evaluate_model(
    model: Any, X: np.ndarray, y: np.ndarray, feature_names: list[str]
) -> dict[str, Any]:
    """Compute post-filter metrics at various thresholds."""
    y_pred = model.predict_proba(X)[:, 1]  # P(win) for class 1

    # Evaluate at default threshold 0.50
    threshold = 0.50
    y_hat = (y_pred >= threshold).astype(int)
    passed = int(y_hat.sum())
    blocked = len(y_hat) - passed

    # Post-filter win rate
    if passed > 0:
        post_filter_wins = int((y_hat & (y == 1)).sum())
        post_filter_wr = post_filter_wins / passed
    else:
        post_filter_wr = 0.0

    # Baseline WR
    baseline_wr = float(y.mean())

    # AUC
    from sklearn.metrics import roc_auc_score

    try:
        auc = float(roc_auc_score(y, y_pred))
    except ValueError:
        auc = 0.0

    # Feature importance (sklearn API: use booster_)
    booster = model.booster_
    importance_pairs = list(
        zip(feature_names, booster.feature_importance(importance_type="gain"), strict=False)
    )
    importance_pairs.sort(key=lambda x: x[1], reverse=True)

    return {
        "threshold": threshold,
        "n_passed": passed,
        "n_blocked": blocked,
        "pass_rate": passed / max(len(y), 1),
        "post_filter_wr": post_filter_wr,
        "baseline_wr": baseline_wr,
        "wr_improvement": post_filter_wr - baseline_wr,
        "auc": auc,
        "top_features": importance_pairs[:15],
    }


# ── Save ───────────────────────────────────────────────────────────────


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
    eval_metrics: dict[str, Any],
    dataset_path: str,
) -> str:
    """Save model in LightGBM .txt + pickle .pkl + feature_names.json."""
    os.makedirs(output_dir, exist_ok=True)

    # LightGBM native format (access booster_ for sklearn wrapper)
    booster = model.booster_ if hasattr(model, "booster_") else model
    txt_path = os.path.join(output_dir, "meta_filter_lgb.txt")
    booster.save_model(txt_path)

    # Pickle format (for MetaFilterAdapter)
    pkl_path = os.path.join(output_dir, "meta_filter_lgb.pkl")
    with open(pkl_path, "wb") as f:
        pickle.dump(model, f)

    # Feature names (for MetaFilterAdapter FeatureParityError check)
    # IMPORTANT: adapter expects a JSON array, NOT a dict — writes plain list
    fn_path = os.path.join(output_dir, "feature_names.json")
    with open(fn_path, "w", encoding="utf-8") as f:
        json.dump(feature_names, f)

    # Metadata
    meta = {
        "schema_version": "meta_filter_model_meta.v1",
        "model_type": "lightgbm",
        "n_samples": n_samples,
        "n_features": len(feature_names),
        "n_wins": n_wins,
        "n_losses": n_losses,
        "win_rate": round(float(wr), 4),
        "feature_names": feature_names,
        "cv_folds": 5,
        "cv_auc_mean": round(cv_auc_mean, 4),
        "cv_auc_std": round(cv_auc_std, 4),
        "auc": round(eval_metrics["auc"], 4),
        "post_filter_wr": round(eval_metrics["post_filter_wr"], 4),
        "baseline_wr": round(eval_metrics["baseline_wr"], 4),
        "wr_improvement": round(eval_metrics["wr_improvement"], 4),
        "pass_rate": round(eval_metrics["pass_rate"], 4),
        "params": {
            k: str(v) if not isinstance(v, (int, float, bool, type(None))) else v
            for k, v in params.items()
        },
        "training_data": os.path.basename(dataset_path),
        "trained_at": datetime.now(UTC).isoformat(),
        "trainer": "train_xau_metafilter.py",
    }
    meta_path = os.path.join(output_dir, "meta_filter_lgb.meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, default=str)

    print("\nModel saved:")
    print(f"  LightGBM: {txt_path}")
    print(f"  Pickle:   {pkl_path}")
    print(f"  Features: {fn_path}")
    print(f"  Meta:     {meta_path}")

    # Dictionary isomorphism validation
    # sklearn LGBMClassifier strips feature names → verify dimension match only
    booster = model.booster_ if hasattr(model, "booster_") else model
    if hasattr(booster, "num_feature"):
        model_dim = booster.num_feature()
    else:
        model_dim = len(booster.feature_name())
    if model_dim != len(feature_names):
        print(f"  [WARNING]: model has {model_dim} features, online has {len(feature_names)}")
    else:
        print(f"  [OK] Feature count match: {len(feature_names)} features")

    return txt_path


def update_brain_config(model_path: str, meta: dict, eval_metrics: dict) -> None:
    """Update configs/brains/meta_stage2_filter_v3.json with new model."""
    repo_root = Path(__file__).resolve().parent.parent
    config_path = repo_root / "configs" / "brains" / "meta_stage2_filter_v3.json"

    # Read existing config to preserve any fields we don't manage
    existing = {}
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            existing = json.load(f)

    # Compute relative model path
    rel_model_path = str(Path(model_path).resolve().relative_to(repo_root))

    config = {
        "schema_version": "meta_filter_config.v1",
        "filter_id": "Meta_Stage2_Filter_XAU_V2",
        "model_path": rel_model_path,
        "threshold": 0.55,
        "mode": "binary",
        "feature_schema": "v9_institutional_40",
        "description": (
            f"XAU MetaFilter V2 — Trained on {meta['n_samples']} PIT-aligned samples "
            f"({meta['n_features']}-dim). {meta.get('cv_folds', 5)}-Fold TimeSeriesSplit "
            f"CV AUC={meta['cv_auc_mean']:.4f}±{meta['cv_auc_std']:.4f}. "
            f"Post-filter WR={eval_metrics['post_filter_wr']:.1%} "
            f"(baseline={eval_metrics['baseline_wr']:.1%}, "
            f"+{eval_metrics['wr_improvement']:+.1%}). "
            f"N/D={meta['n_samples']/meta['n_features']:.0f} — moderate regularization."
        ),
    }
    os.makedirs(config_path.parent, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    print(f"\nConfig updated: {config_path}")


# ── Main ───────────────────────────────────────────────────────────────


def main() -> None:
    args = parse_args()

    print("=" * 60)
    print("  XAU MetaFilter — ML Signal Quality Gate Training")
    print(f"  Dataset: {args.dataset}")
    print(f"  Output:  {args.output_dir}")
    print(f"  CV:      {args.cv_folds}-fold TimeSeriesSplit")
    print(f"  Gate:    AUC >= {args.min_auc}")
    print("=" * 60)

    # 1. Load
    X, y, feature_names = load_dataset(args.dataset)
    n_wins = int(y.sum())
    n_losses = len(y) - n_wins
    wr = n_wins / max(len(y), 1)
    N, D = X.shape

    print(f"\nN={N}, D={D}, N/D={N/D:.1f}")
    if N / D < 5:
        print("[WARNING] N/D < 5 — potential curse of dimensionality")
    else:
        print("[OK] N/D >= 5 — sufficient samples per feature")

    # 2. Compute scale_pos_weight for class imbalance
    scale_pos_weight = compute_scale_pos_weight(y)

    # 3. CV training
    print(f"\n── {args.cv_folds}-Fold TimeSeriesSplit CV ──")
    result = train_with_cv(
        X, y, feature_names, cv_folds=args.cv_folds, scale_pos_weight=scale_pos_weight
    )

    print(f"\nCV AUC: {result['cv_auc_mean']:.4f} ± {result['cv_auc_std']:.4f}")
    print(f"  Fold scores: {[f'{a:.4f}' for a in result['fold_aucs']]}")

    # 3. Quality gate
    if result["cv_auc_mean"] < args.min_auc:
        print(f"\n[REJECTED] CV AUC {result['cv_auc_mean']:.4f} < {args.min_auc}")
        print("  Model does not meet minimum quality bar.")
        raise SystemExit(1)

    print(f"\n[PASS] CV AUC {result['cv_auc_mean']:.4f} >= {args.min_auc} — GATE PASSED")

    # 4. Train final model
    print("\n── Training final model on full dataset ──")
    final_model = train_final(X, y, feature_names, result["params"])

    # 5. Evaluate
    print("\n── Evaluation ──")
    eval_metrics = evaluate_model(final_model, X, y, feature_names)

    print(f"  Baseline WR:     {eval_metrics['baseline_wr']:.1%}")
    print(f"  Post-filter WR:  {eval_metrics['post_filter_wr']:.1%}")
    print(f"  WR Improvement:  {eval_metrics['wr_improvement']:+.1%}")
    print(f"  AUC:             {eval_metrics['auc']:.4f}")
    print(f"  Pass rate:       {eval_metrics['pass_rate']:.1%}")
    print(f"  Signals passed:  {eval_metrics['n_passed']}")
    print(f"  Signals blocked: {eval_metrics['n_blocked']}")

    # 6. Feature importance
    print("\n  Top 10 features by gain:")
    for name, gain in eval_metrics["top_features"][:10]:
        bar = "█" * int(gain / max(1e-9, eval_metrics["top_features"][0][1]) * 30)
        print(f"  {name:30s}: {gain:.2f} {bar}")

    # 7. Save
    model_path = save_model(
        final_model,
        feature_names,
        result["params"],
        args.output_dir,
        n_samples=N,
        n_wins=n_wins,
        n_losses=n_losses,
        wr=wr,
        cv_auc_mean=result["cv_auc_mean"],
        cv_auc_std=result["cv_auc_std"],
        eval_metrics=eval_metrics,
        dataset_path=args.dataset,
    )

    # 8. Update config
    meta = {
        "n_samples": N,
        "n_features": D,
        "cv_folds": args.cv_folds,
        "cv_auc_mean": result["cv_auc_mean"],
        "cv_auc_std": result["cv_auc_std"],
    }
    if not args.no_config_update:
        update_brain_config(model_path, meta, eval_metrics)

    print(f"\n{'=' * 60}")
    print("  TRAINING COMPLETE — XAU MetaFilter V2")
    print(f"  CV AUC: {result['cv_auc_mean']:.4f}")
    print(f"  Post-filter WR: {eval_metrics['post_filter_wr']:.1%}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
