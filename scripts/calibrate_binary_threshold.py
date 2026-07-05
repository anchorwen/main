#!/usr/bin/env python
"""Calibrate binary classifier decision threshold for directional balance.

Problem: Default 0.5 threshold produces 77.7% LONG predictions because the
model's probability distribution is shifted — LONG samples have higher scores.
We scan thresholds to find the one that maximizes balanced win rate while
keeping direction diversity in [40%, 60%] range.

Usage:
  python scripts/calibrate_binary_threshold.py \
    --model-dir data_btc/models/btc_swing_m5_symmetric \
    --npz data_btc/training/btc_swing_m5_symmetric/train.npz
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_fold_models(model_dir: str, seed: int = 42) -> list[dict]:
    """Load all fold models and their validation indices."""
    import xgboost as xgb

    models = []
    for fold in range(5):
        path = os.path.join(model_dir, f"xgboost_fold{fold}_s{seed}.json")
        if not os.path.exists(path):
            continue
        model = xgb.Booster()
        model.load_model(path)
        models.append({"fold": fold, "model": model, "path": path})
    return models


def walk_forward_splits(n_samples: int, n_folds: int = 5) -> list[dict]:
    """Chronological walk-forward splits (must match training)."""
    splits = []
    fold_size = n_samples // (n_folds + 1)
    for fold in range(n_folds):
        test_start = n_samples - (n_folds - fold) * fold_size
        test_end = min(n_samples, test_start + fold_size)
        if test_start <= 0:
            continue
        splits.append(
            {
                "fold": fold,
                "train_start": 0,
                "train_end": test_start,
                "test_start": test_start,
                "test_end": test_end,
            }
        )
    return splits


def evaluate_at_threshold(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    threshold: float,
) -> dict:
    """Compute metrics at a given decision threshold."""
    y_pred = (y_prob >= threshold).astype(int)

    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    tn = int(((y_pred == 0) & (y_true == 0)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())

    n_long_pred = int(y_pred.sum())
    n_short_pred = len(y_pred) - n_long_pred

    precision_long = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    precision_short = tn / (tn + fn) if (tn + fn) > 0 else 0.0
    recall_long = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    recall_short = tn / (tn + fp) if (tn + fp) > 0 else 0.0

    wr = (precision_long + precision_short) / 2.0
    acc = float((y_pred == y_true).mean())
    f1_long = (
        2 * precision_long * recall_long / (precision_long + recall_long)
        if (precision_long + recall_long) > 0
        else 0.0
    )
    f1_short = (
        2 * precision_short * recall_short / (precision_short + recall_short)
        if (precision_short + recall_short) > 0
        else 0.0
    )

    return {
        "threshold": threshold,
        "acc": acc,
        "wr": wr,
        "precision_long": precision_long,
        "precision_short": precision_short,
        "recall_long": recall_long,
        "recall_short": recall_short,
        "f1_long": f1_long,
        "f1_short": f1_short,
        "f1_macro": (f1_long + f1_short) / 2.0,
        "n_long_pred": n_long_pred,
        "n_short_pred": n_short_pred,
        "long_ratio": n_long_pred / len(y_pred),
    }


def main():
    parser = argparse.ArgumentParser(description="Calibrate binary decision threshold")
    parser.add_argument("--model-dir", required=True, help="Directory with fold models")
    parser.add_argument("--npz", required=True, help="Path to training NPZ")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--n-thresholds", type=int, default=101)
    parser.add_argument("--target-long-ratio-min", type=float, default=0.40)
    parser.add_argument("--target-long-ratio-max", type=float, default=0.60)
    args = parser.parse_args()

    print(f"[CALIBRATE] Loading models from {args.model_dir}...")
    models = load_fold_models(args.model_dir, args.seed)
    print(f"  Loaded {len(models)} fold models")

    print(f"[CALIBRATE] Loading dataset {args.npz}...")
    data = np.load(args.npz)
    X = data["X"]
    y_all = data["y"]

    # Handle binary barrier encoding {-1, 1} → {0, 1}
    unique_vals = set(np.unique(y_all))
    if unique_vals == {-1, 1}:
        y = np.where(y_all == -1, 0, 1)
        print("  Detected barrier encoding {-1, 1} → binary {0, 1}")
    elif unique_vals == {0, 1}:
        y = y_all.copy()
        print("  Detected binary encoding {0, 1}")
    elif 1 in unique_vals and 2 in unique_vals:
        # Multi-class: filter NEUTRAL
        binary_mask = y_all != 1
        X = X[binary_mask]
        y = np.where(y_all[binary_mask] == 0, 0, 1)
        print("  Detected multi-class encoding → binary (NEUTRAL filtered)")
    else:
        raise ValueError(f"Unknown encoding: {unique_vals}")

    n = len(y)
    n_short_true = int((y == 0).sum())
    n_long_true = int((y == 1).sum())
    print(f"  Samples: {n:,} (SHORT={n_short_true}, LONG={n_long_true})")
    print(f"  True balance: LONG {n_long_true/n*100:.1f}%")

    splits = walk_forward_splits(n)

    # ── Per-fold threshold sweep ──
    thresholds = np.linspace(0.05, 0.95, args.n_thresholds)
    fold_best: list[dict] = []
    all_per_fold: list[dict] = []

    for model_info in models:
        fold = model_info["fold"]
        model = model_info["model"]
        split = splits[fold]
        test_start = split["test_start"]
        test_end = split["test_end"]

        y_val = y[test_start:test_end]
        X_val = X[test_start:test_end]

        import xgboost as xgb

        dval = xgb.DMatrix(X_val)
        y_prob = model.predict(dval)

        print(f"\n{'='*60}")
        print(f"[CALIBRATE] Fold {fold}: {len(y_val):,} validation samples")
        print(f"  True SHORT={int((y_val == 0).sum())}, LONG={int((y_val == 1).sum())}")

        # Default threshold results
        default = evaluate_at_threshold(y_val, y_prob, 0.50)
        print(
            f"  Default (0.50): WR={default['wr']:.3f}, "
            f"LONG_ratio={default['long_ratio']:.1%}, "
            f"P_LONG={default['precision_long']:.3f}, P_SHORT={default['precision_short']:.3f}"
        )

        # Sweep thresholds
        results = []
        for t in thresholds:
            r = evaluate_at_threshold(y_val, y_prob, t)
            results.append(r)

        fold_data = {"fold": fold, "default": default, "sweep": results}
        all_per_fold.append(fold_data)

        # Find best threshold: maximize WR subject to direction balance constraint
        # Score = WR * direction_penalty (penalty if outside [40%, 60%])
        best_constrained = None
        best_score = -999.0
        for r in results:
            lr = r["long_ratio"]
            # Direction penalty: 1.0 inside [40%, 60%], falls off linearly outside
            if args.target_long_ratio_min <= lr <= args.target_long_ratio_max:
                direction_penalty = 1.0
            elif lr < args.target_long_ratio_min:
                direction_penalty = max(0.0, 1.0 - (args.target_long_ratio_min - lr) * 5)
            else:
                direction_penalty = max(0.0, 1.0 - (lr - args.target_long_ratio_max) * 5)
            score = r["wr"] * direction_penalty
            if score > best_score:
                best_score = score
                best_constrained = r

        if best_constrained:
            print(
                f"  Best constrained: t={best_constrained['threshold']:.2f}, "
                f"WR={best_constrained['wr']:.3f}, "
                f"LONG_ratio={best_constrained['long_ratio']:.1%}, "
                f"P_LONG={best_constrained['precision_long']:.3f}, P_SHORT={best_constrained['precision_short']:.3f}"
            )

        # Also find global best WR (unconstrained)
        best_wr = max(results, key=lambda r: r["wr"])
        print(
            f"  Best WR (unconstrained): t={best_wr['threshold']:.2f}, "
            f"WR={best_wr['wr']:.3f}, LONG_ratio={best_wr['long_ratio']:.1%}"
        )

        fold_best.append(
            {
                "fold": fold,
                "best_constrained": best_constrained,
                "best_wr": best_wr,
                "default": default,
            }
        )

    # ── Global calibration (all validation sets pooled) ──
    print(f"\n{'='*60}")
    print("[CALIBRATE] === Global Calibration (all val sets pooled) ===")

    all_y_val = []
    all_y_prob = []

    for model_info in models:
        fold = model_info["fold"]
        model = model_info["model"]
        split = splits[fold]
        test_start = split["test_start"]
        test_end = split["test_end"]

        import xgboost as xgb

        dval = xgb.DMatrix(X[test_start:test_end])
        y_prob = model.predict(dval)
        y_val = y[test_start:test_end]

        all_y_val.append(y_val)
        all_y_prob.append(y_prob)

    pooled_y = np.concatenate(all_y_val)
    pooled_prob = np.concatenate(all_y_prob)

    pooled_default = evaluate_at_threshold(pooled_y, pooled_prob, 0.50)
    print(
        f"  Default (0.50): WR={pooled_default['wr']:.3f}, "
        f"LONG_ratio={pooled_default['long_ratio']:.1%}"
    )

    # Find optimal threshold on pooled data
    pooled_best = None
    pooled_best_score = -999.0
    pooled_sweep = []

    for t in thresholds:
        r = evaluate_at_threshold(pooled_y, pooled_prob, t)
        pooled_sweep.append(r)
        lr = r["long_ratio"]
        if args.target_long_ratio_min <= lr <= args.target_long_ratio_max:
            direction_penalty = 1.0
        elif lr < args.target_long_ratio_min:
            direction_penalty = max(0.0, 1.0 - (args.target_long_ratio_min - lr) * 5)
        else:
            direction_penalty = max(0.0, 1.0 - (lr - args.target_long_ratio_max) * 5)
        score = r["wr"] * direction_penalty
        if score > pooled_best_score:
            pooled_best_score = score
            pooled_best = r

    if pooled_best:
        print(
            f"  Optimal: t={pooled_best['threshold']:.2f}, "
            f"WR={pooled_best['wr']:.3f}, "
            f"LONG_ratio={pooled_best['long_ratio']:.1%}, "
            f"P_LONG={pooled_best['precision_long']:.3f}, P_SHORT={pooled_best['precision_short']:.3f}"
        )

    # ── Top-5 thresholds near 50:50 direction balance ──
    print("\n[CALIBRATE] === Top-5 thresholds closest to 50:50 direction ===")
    by_balance = sorted(pooled_sweep, key=lambda r: abs(r["long_ratio"] - 0.50))
    for i, r in enumerate(by_balance[:5]):
        print(
            f"  #{i+1}: t={r['threshold']:.2f}, WR={r['wr']:.3f}, "
            f"LONG={r['long_ratio']:.1%}, P_LONG={r['precision_long']:.3f}, P_SHORT={r['precision_short']:.3f}"
        )

    # ── Summary ──
    summary = {
        "calibrated_at": datetime.now(UTC).isoformat(),
        "model_dir": args.model_dir,
        "npz_source": args.npz,
        "n_samples": n,
        "n_short_true": n_short_true,
        "n_long_true": n_long_true,
        "true_long_ratio": float(n_long_true / n),
        "default_threshold": 0.50,
        "default_metrics": {
            "wr": pooled_default["wr"],
            "acc": pooled_default["acc"],
            "long_ratio": pooled_default["long_ratio"],
            "precision_long": pooled_default["precision_long"],
            "precision_short": pooled_default["precision_short"],
            "f1_long": pooled_default["f1_long"],
            "f1_short": pooled_default["f1_short"],
            "f1_macro": pooled_default["f1_macro"],
        },
        "optimal_constrained": {
            "threshold": pooled_best["threshold"],
            "wr": pooled_best["wr"],
            "acc": pooled_best["acc"],
            "long_ratio": pooled_best["long_ratio"],
            "precision_long": pooled_best["precision_long"],
            "precision_short": pooled_best["precision_short"],
            "f1_long": pooled_best["f1_long"],
            "f1_short": pooled_best["f1_short"],
            "f1_macro": pooled_best["f1_macro"],
        }
        if pooled_best
        else None,
        "per_fold": [
            {
                "fold": fb["fold"],
                "default_wr": fb["default"]["wr"],
                "default_long_ratio": fb["default"]["long_ratio"],
                "best_constrained_threshold": fb["best_constrained"]["threshold"],
                "best_constrained_wr": fb["best_constrained"]["wr"],
                "best_constrained_long_ratio": fb["best_constrained"]["long_ratio"],
            }
            for fb in fold_best
        ],
    }

    summary_path = os.path.join(args.model_dir, "threshold_calibration.json")
    os.makedirs(args.model_dir, exist_ok=True)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\n[DONE] Calibration saved to {summary_path}")

    # ── Recommendation ──
    print(f"\n{'='*60}")
    print("[RECOMMENDATION]")
    if pooled_best:
        wr_delta = (pooled_best["wr"] - pooled_default["wr"]) * 100
        lr_delta = (pooled_best["long_ratio"] - pooled_default["long_ratio"]) * 100
        print(f"  Threshold: {pooled_default['threshold']:.2f} → {pooled_best['threshold']:.2f}")
        print(f"  WR change: {wr_delta:+.1f}%")
        print(
            f"  LONG ratio: {pooled_default['long_ratio']:.1%} → {pooled_best['long_ratio']:.1%} ({lr_delta:+.1f}pp)"
        )
        print(
            f"  P_LONG: {pooled_default['precision_long']:.3f} → {pooled_best['precision_long']:.3f}"
        )
        print(
            f"  P_SHORT: {pooled_default['precision_short']:.3f} → {pooled_best['precision_short']:.3f}"
        )
        if abs(lr_delta) > 10:
            print("  ⚠️  Direction balance significantly improved")
        if wr_delta > 0:
            print("  ✅ WR improved")
        elif wr_delta > -1.0:
            print("  →  WR essentially unchanged")
        else:
            print("  ⚠️  WR degraded — check if direction balance gain is worth it")


if __name__ == "__main__":
    main()
