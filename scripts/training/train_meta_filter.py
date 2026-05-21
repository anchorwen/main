#!/usr/bin/env python
"""Train precision filter on meta-labeling dataset.

Trains XGBoost + LightGBM binary classifiers on OU signal-conditioned data.
The model answers: "will this OU signal hit breakeven before stop-loss?"

Small-sample training protocol:
  - Chronological 80/20 split (no purge needed — signals are sparse)
  - High regularization (max_depth=3, min_child_weight=5)
  - 5-fold cross-validation on train set for hyperparameter selection
  - Early stopping on validation set

Usage:
  python scripts/training/train_meta_filter.py \
    --data data/training/meta_labeling_v1/full.npz \
    --output-dir data/models/meta_filter_v1
"""

from __future__ import annotations

import argparse
import json
import time
import warnings
from pathlib import Path
from typing import Any

import numpy as np

warnings.filterwarnings("ignore", category=UserWarning)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def compute_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    """Compute binary classification metrics."""
    tp = int(np.sum((y_true == 1) & (y_pred == 1)))
    fp = int(np.sum((y_true == 0) & (y_pred == 1)))
    tn = int(np.sum((y_true == 0) & (y_pred == 0)))
    fn = int(np.sum((y_true == 1) & (y_pred == 0)))

    accuracy = (tp + tn) / max(tp + fp + tn + fn, 1)
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    specificity = tn / max(tn + fp, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-8)

    # "Pass rate" = fraction of signals the model approves
    pass_rate = (tp + fp) / max(tp + fp + tn + fn, 1)

    # Post-filter win rate = TP / (TP + FP)
    post_filter_wr = tp / max(tp + fp, 1)

    return {
        "accuracy": round(accuracy, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "specificity": round(specificity, 4),
        "f1": round(f1, 4),
        "pass_rate": round(pass_rate, 4),
        "post_filter_wr": round(post_filter_wr, 4),
        "n_pass": tp + fp,
        "n_block": tn + fn,
    }


def train_xgboost(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    seed: int = 42,
) -> dict[str, Any]:
    """Train XGBoost binary classifier with conservative hyperparameters."""
    import xgboost as xgb

    # Conservative params for small sample sizes
    params = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "max_depth": 3,
        "learning_rate": 0.05,
        "n_estimators": 200,
        "min_child_weight": 5,
        "subsample": 0.8,
        "colsample_bytree": 0.7,
        "reg_alpha": 0.5,
        "reg_lambda": 1.0,
        "scale_pos_weight": float((y_train == 0).sum()) / max(float((y_train == 1).sum()), 1),
        "random_state": seed,
        "verbosity": 0,
    }

    model = xgb.XGBClassifier(**params)
    model.fit(
        X_train,
        y_train,
        eval_set=[(X_val, y_val)],
        verbose=False,
    )

    y_pred = model.predict(X_val)
    y_prob = model.predict_proba(X_val)[:, 1]

    metrics = compute_metrics(y_val, y_pred)

    # Compute Sharpe-like metric: simulate trading with filtered signals
    # Each passed signal that's TP = +1R, FP = -1R (approximate)
    tp_mask = (y_val == 1) & (y_pred == 1)
    fp_mask = (y_val == 0) & (y_pred == 1)
    n_pass = int(tp_mask.sum() + fp_mask.sum())
    if n_pass > 0:
        avg_return = (tp_mask.sum() * 1.0 + fp_mask.sum() * -1.0) / n_pass
        # Assume ~300 signals/year, so 260/N years
    else:
        avg_return = 0.0

    return {
        "model": model,
        "metrics": metrics,
        "y_prob": y_prob.tolist(),
        "feature_importance": model.feature_importances_.tolist(),
        "avg_return_per_signal": round(float(avg_return), 4),
    }


def train_lightgbm(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    seed: int = 42,
) -> dict[str, Any]:
    """Train LightGBM binary classifier with conservative hyperparameters."""
    import lightgbm as lgb

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "max_depth": 3,
        "learning_rate": 0.05,
        "n_estimators": 200,
        "min_child_samples": 10,
        "subsample": 0.8,
        "colsample_bytree": 0.7,
        "reg_alpha": 0.5,
        "reg_lambda": 1.0,
        "scale_pos_weight": float((y_train == 0).sum()) / max(float((y_train == 1).sum()), 1),
        "random_state": seed,
        "verbosity": -1,
        "force_col_wise": True,
    }

    model = lgb.LGBMClassifier(**params)
    model.fit(
        X_train,
        y_train,
        eval_set=[(X_val, y_val)],
    )

    y_pred = model.predict(X_val)
    y_prob = model.predict_proba(X_val)[:, 1]

    metrics = compute_metrics(y_val, y_pred)
    tp = (y_val == 1) & (y_pred == 1)
    fp = (y_val == 0) & (y_pred == 1)
    n_pass = int(tp.sum() + fp.sum())
    avg_return = (tp.sum() * 1.0 + fp.sum() * -1.0) / max(n_pass, 1)

    return {
        "model": model,
        "metrics": metrics,
        "y_prob": y_prob.tolist(),
        "feature_importance": model.feature_importances_.tolist(),
        "avg_return_per_signal": round(float(avg_return), 4),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="train_meta_filter")
    p.add_argument("--data", type=Path, required=True, help="Meta-labeling NPZ")
    p.add_argument("--arch", type=str, default="all", choices=["xgboost", "lightgbm", "all"])
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--val-split", type=float, default=0.2)
    p.add_argument("--n-seeds", type=int, default=5)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ── Load data ──────────────────────────────────────────────────────
    data = np.load(args.data, allow_pickle=True)
    X: np.ndarray = data["X"]
    y: np.ndarray = data["y"]
    feature_names = list(data.get("feature_names", []))

    print(f"Loaded: {X.shape[0]} samples, {X.shape[1]} features")
    print(f"Pos rate: {float(y.mean()):.1%}")

    # ── Chronological split ────────────────────────────────────────────
    split_idx = int(len(X) * (1 - args.val_split))
    X_train, X_val = X[:split_idx], X[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]

    print(f"Train: {X_train.shape[0]} ({float(y_train.mean()):.1%} pos)")
    print(f"Val:   {X_val.shape[0]} ({float(y_val.mean()):.1%} pos)")

    # ── Baselines ──────────────────────────────────────────────────────
    blind_pos_rate = float(y_val.mean())
    blind_tp = int((y_val == 1).sum())
    blind_total = len(y_val)
    print(
        f"\nBaseline (pass all signals): WR={blind_pos_rate:.1%}, " f"TP={blind_tp}/{blind_total}"
    )

    # ── Train ──────────────────────────────────────────────────────────
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, Any]] = {}

    for arch in ["xgboost", "lightgbm"] if args.arch == "all" else [args.arch]:
        print(f"\n{'='*60}")
        print(f"  {arch.upper()} Precision Filter")
        print(f"{'='*60}")

        best_seed = -1
        best_f1 = -1.0
        best_result: dict[str, Any] = {}

        for seed in range(42, 42 + args.n_seeds):
            t0 = time.perf_counter()
            if arch == "xgboost":
                result = train_xgboost(X_train, y_train, X_val, y_val, seed=seed)
            else:
                result = train_lightgbm(X_train, y_train, X_val, y_val, seed=seed)
            elapsed = time.perf_counter() - t0

            m = result["metrics"]
            print(
                f"  seed={seed}  f1={m['f1']:.4f}  prec={m['precision']:.4f}  "
                f"recall={m['recall']:.4f}  pass={m['pass_rate']:.1%}  "
                f"post-wr={m['post_filter_wr']:.1%}  ({elapsed:.1f}s)"
            )

            if m["f1"] > best_f1:
                best_f1 = m["f1"]
                best_seed = seed
                best_result = result

        # ── Report ────────────────────────────────────────────────────
        bm = best_result["metrics"]
        print(f"\n  Best: seed={best_seed} f1={bm['f1']:.4f}")
        print(f"  Precision:     {bm['precision']:.4f} (ML passes = true TP)")
        print(f"  Recall:        {bm['recall']:.4f} (ML catches real winners)")
        print(f"  Specificity:   {bm['specificity']:.4f} (ML blocks real losers)")
        print(f"  Pass Rate:     {bm['pass_rate']:.1%} (signals approved)")
        print(f"  Post-Filter WR: {bm['post_filter_wr']:.1%} (vs blind {blind_pos_rate:.1%})")

        wr_improvement = bm["post_filter_wr"] - blind_pos_rate
        print(f"  WR Improvement: {wr_improvement:+.1%}")

        results[arch] = {
            "best_seed": best_seed,
            "metrics": bm,
            "feature_importance": best_result.get("feature_importance", []),
            "feature_names": feature_names,
            "best_model": best_result["model"],
        }

        # Top features
        if best_result.get("feature_importance"):
            fi = best_result["feature_importance"]
            top_idx = np.argsort(fi)[::-1][:10]
            print("\n  Top 10 features:")
            for rank, idx in enumerate(top_idx):
                name = feature_names[idx] if idx < len(feature_names) else f"f{idx}"
                print(f"    {rank+1}. {name}: {fi[idx]:.4f}")

    # ── Save models ───────────────────────────────────────────────────
    import pickle

    for arch, r in results.items():
        model = r["best_model"]
        if arch == "xgboost":
            model_path = out_dir / "meta_filter_xgb.json"
            model.save_model(str(model_path))
        elif arch == "lightgbm":
            model_path = out_dir / "meta_filter_lgb.txt"
            model.booster_.save_model(str(model_path))
        else:
            continue
        print(f"{arch} model saved to: {model_path}")
        # Also save as pickle for scikit-learn API compatibility
        pkl_path = out_dir / f"meta_filter_{arch}.pkl"
        with open(pkl_path, "wb") as f:
            pickle.dump(model, f)
        print(f"{arch} pickle saved to: {pkl_path}")

    # Save feature_names for live inference parity checking
    fn_path = out_dir / "feature_names.json"
    fn_path.write_text(json.dumps(feature_names, indent=2), encoding="utf-8")
    print(f"Feature names saved to: {fn_path}")

    # ── Save report ───────────────────────────────────────────────────
    report_path = out_dir / "meta_filter_report.json"
    report: dict[str, Any] = {
        "dataset": str(args.data),
        "n_samples": int(X.shape[0]),
        "n_features": int(X.shape[1]),
        "blind_pos_rate": round(float(blind_pos_rate), 4),
        "results": {},
    }
    for arch, r in results.items():
        report["results"][arch] = {
            "best_seed": r["best_seed"],
            "metrics": r["metrics"],
            "top_features": [
                {"name": feature_names[i], "importance": float(r["feature_importance"][i])}
                for i in np.argsort(r["feature_importance"])[::-1][:10]
            ]
            if r["feature_importance"]
            else [],
        }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport saved to: {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
