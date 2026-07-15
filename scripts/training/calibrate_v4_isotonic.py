#!/usr/bin/env python
"""V4 Post-Training Isotonic Calibration — fix confidence inversion.

After 3-class XGBoost training, this script:
1. Loads CV predictions across all folds
2. Fits isotonic regression per-class: maps predicted prob → empirical frequency
3. Fits a per-direction reliability curve (LONG confidence vs actual LONG WR)
4. Outputs calibration params for the brain config's confidence_params

The old V4 had inverted calibration: high confidence → LOW win rate.
Isotonic regression is non-parametric — it learns the actual mapping
from predicted probabilities to empirical frequencies on held-out data.

Usage:
  python scripts/training/calibrate_v4_isotonic.py \
    --npz data_btc/training/btc_swing_v4_3class_v2/train.npz \
    --model-dir data_btc/models/btc_swing_v4_3class_v2 \
    --output data_btc/models/btc_swing_v4_3class_v2/calibration.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_cv_predictions(
    npz_path: str,
    model_dir: str,
    cv_splits_path: str | None = None,
    model_type: str = "xgboost",
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Load CV predictions for all folds.

    Args:
        npz_path: Path to train.npz
        model_dir: Directory with per-fold model files
        cv_splits_path: Path to cv_splits.json (default: derived from npz_path)
        model_type: "xgboost" or "lightgbm"

    Returns:
        y_true_all: shape (n,) — class labels 0/1/2 for SHORT/NEUTRAL/LONG
        y_prob_all: shape (n, 3) — predicted probabilities
        y_pred_all: shape (n,) — predicted class
    """

    data = np.load(npz_path)
    X = data["X"]
    y = data["y"]  # 0=SHORT, 1=NEUTRAL, 2=LONG

    # Load CV splits
    if cv_splits_path is None:
        cv_splits_path = npz_path.replace("train.npz", "cv_splits.json")
    with open(cv_splits_path) as f:
        cv_data = json.load(f)

    y_prob_all = np.zeros((len(y), 3), dtype=np.float32)
    y_pred_all = np.zeros(len(y), dtype=np.int8)

    if model_type == "lightgbm":
        import lightgbm as lgb

        model_suffix = "txt"
    else:
        import xgboost as xgb

        model_suffix = "json"

    for split in cv_data["splits"]:
        fold = split["fold"]
        test_idx = np.array(split["test_idx"])

        model_path = os.path.join(model_dir, f"{model_type}_fold{fold}_s42.{model_suffix}")
        if not os.path.exists(model_path):
            print(f"  [WARN] Model not found: {model_path}, skipping fold {fold}")
            continue

        if model_type == "lightgbm":
            model = lgb.Booster(model_file=model_path)
            probs = model.predict(X[test_idx])  # shape (n_test, 3) for multiclass
        else:
            model = xgb.Booster()
            model.load_model(model_path)
            dtest = xgb.DMatrix(X[test_idx])
            probs = model.predict(dtest)  # shape (n_test, 3)

        y_prob_all[test_idx] = probs
        y_pred_all[test_idx] = probs.argmax(axis=1).astype(np.int8)

    return y, y_prob_all, y_pred_all


def fit_isotonic_calibration(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    n_bins: int = 20,
) -> dict[str, Any]:
    """Fit isotonic calibration for each class and directional confidence.

    For 3-class (SHORT=0, NEUTRAL=1, LONG=2):
    - Calibrate P(SHORT) vs empirical SHORT frequency
    - Calibrate P(LONG) vs empirical LONG frequency
    - Calibrate directional confidence: P(LONG) vs actual LONG WR (ignoring NEUTRAL)
    """
    from sklearn.isotonic import IsotonicRegression

    results: dict[str, Any] = {
        "calibrated_at": datetime.now(UTC).isoformat(),
        "n_samples": len(y_true),
        "n_bins": n_bins,
    }

    # ── Per-class calibration ──
    class_names = ["SHORT", "NEUTRAL", "LONG"]
    for cls in range(3):
        mask = y_true == cls
        y_binary = mask.astype(np.float64)
        prob_cls = y_prob[:, cls].astype(np.float64)

        # Fit isotonic regression: prob → empirical frequency
        iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        try:
            iso.fit(prob_cls, y_binary)
        except ValueError as e:
            print(f"  [WARN] Isotonic fit failed for {class_names[cls]}: {e}")
            results[f"isotonic_{class_names[cls].lower()}"] = None
            continue

        # Store calibration curve as (x, y) pairs for inference
        # Sample at regular intervals
        x_grid = np.linspace(0.0, 1.0, n_bins + 1)
        y_grid = iso.predict(x_grid)

        results[f"isotonic_{class_names[cls].lower()}"] = {
            "x_grid": x_grid.tolist(),
            "y_grid": y_grid.tolist(),
            "method": "isotonic_regression",
        }

        print(
            f"  {class_names}: isotonic fitted, " f"range=[{y_grid.min():.3f}, {y_grid.max():.3f}]"
        )

    # ── Directional confidence calibration (the CRITICAL one) ──
    # For LONG direction: when model says P(LONG)=X, what's the actual LONG win rate?
    # Only evaluate on non-NEUTRAL samples (true label != NEUTRAL)
    # and on samples where the model predicted LONG
    non_neutral_mask = y_true != 1  # exclude NEUTRAL true labels
    long_pred_mask = y_prob[:, 2] > y_prob[:, 0]  # model favors LONG over SHORT
    short_pred_mask = y_prob[:, 0] > y_prob[:, 2]  # model favors SHORT

    # LONG calibration: P(LONG) vs actual LONG win rate
    long_conf = y_prob[:, 2]  # model's LONG probability
    # On non-neutral samples where model prefers LONG:
    long_mask = non_neutral_mask & long_pred_mask
    if long_mask.sum() > 50:
        long_true_win = (y_true[long_mask] == 2).astype(np.float64)
        iso_long = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        iso_long.fit(long_conf[long_mask], long_true_win)
        x_long = np.linspace(0.0, 1.0, n_bins + 1)
        y_long = iso_long.predict(x_long)
        results["directional_long"] = {
            "x_grid": x_long.tolist(),
            "y_grid": y_long.tolist(),
            "n_samples": int(long_mask.sum()),
            "method": "isotonic_regression",
        }
        print(f"  Directional LONG: fitted on {long_mask.sum()} samples")
    else:
        results["directional_long"] = None
        print(f"  Directional LONG: insufficient samples ({long_mask.sum()})")

    # SHORT calibration
    short_mask = non_neutral_mask & short_pred_mask
    if short_mask.sum() > 50:
        short_conf = y_prob[:, 0]
        short_true_win = (y_true[short_mask] == 0).astype(np.float64)
        iso_short = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        iso_short.fit(short_conf[short_mask], short_true_win)
        x_short = np.linspace(0.0, 1.0, n_bins + 1)
        y_short = iso_short.predict(x_short)
        results["directional_short"] = {
            "x_grid": x_short.tolist(),
            "y_grid": y_short.tolist(),
            "n_samples": int(short_mask.sum()),
            "method": "isotonic_regression",
        }
        print(f"  Directional SHORT: fitted on {short_mask.sum()} samples")
    else:
        results["directional_short"] = None

    # ── Confidence band analysis ──
    # Bin by confidence and compute actual win rate in each bin
    bands = [(0.0, 0.25), (0.25, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 1.0)]
    band_analysis = []
    for lo, hi in bands:
        in_band = (long_conf >= lo) & (long_conf < hi)
        n_band = int(in_band.sum())
        if n_band > 0:
            actual_wr = float(np.mean(y_true[in_band] == 2))
            band_analysis.append(
                {
                    "confidence_band": f"[{lo:.2f}, {hi:.2f})",
                    "n_samples": n_band,
                    "predicted_wr_mean": float(np.mean(long_conf[in_band])),
                    "actual_wr": round(actual_wr, 4),
                    "calibration_error": round(float(np.mean(long_conf[in_band])) - actual_wr, 4),
                }
            )

    results["confidence_band_analysis"] = band_analysis

    # Print band analysis
    print("\n  Confidence Band Calibration:")
    print(f"  {'Band':<16} {'N':<8} {'Pred WR':<10} {'Actual WR':<10} {'Error':<10}")
    for b in band_analysis:
        print(
            f"  {b['confidence_band']:<16} {b['n_samples']:<8} "
            f"{b['predicted_wr_mean']:<10.3f} {b['actual_wr']:<10.3f} "
            f"{b['calibration_error']:<10.3f}"
        )

    # Calibration quality: is it still inverted?
    max_error: float = max(abs(cast(float, b["calibration_error"])) for b in band_analysis)
    inverted = any(
        cast(float, band_analysis[i]["actual_wr"]) < cast(float, band_analysis[i + 1]["actual_wr"])
        and cast(float, band_analysis[i]["predicted_wr_mean"])
        > cast(float, band_analysis[i + 1]["predicted_wr_mean"])
        for i in range(len(band_analysis) - 1)
    )
    results["calibration_quality"] = {
        "max_abs_error": round(max_error, 4),
        "is_inverted": inverted,
        "verdict": "PASS" if max_error < 0.15 and not inverted else "FAIL",
    }
    print(f"\n  Verdict: max_error={max_error:.4f}, inverted={inverted}")

    return results


def compute_quantile_gaussian_params(y_prob: np.ndarray, y_true: np.ndarray) -> dict:
    """Fit quantile_gaussian calibration parameters from predictions.

    This replicates the existing confidence calibration method but with
    fresh data from the retrained model.
    """
    # Directional score: LONG prob - SHORT prob
    raw_score = y_prob[:, 2] - y_prob[:, 0]

    # Filter to non-neutral predictions
    non_neutral = np.abs(raw_score) > 0.05
    if non_neutral.sum() < 50:
        return {
            "p95": 0.5,
            "peak_conf": 0.5,
            "lambda_decay": 80.0,
            "_warning": "insufficient_samples",
        }

    scores = raw_score[non_neutral]

    # p95: 95th percentile of directional score magnitude
    p95 = float(np.percentile(np.abs(scores), 95))

    # peak_conf: mean of top 10% scores → where confidence peaks
    top_10 = np.abs(scores) >= np.percentile(np.abs(scores), 90)
    peak_conf = float(np.mean(np.abs(scores[top_10]))) if top_10.sum() > 0 else p95

    # lambda_decay: controls how fast confidence drops with score distance
    # Fit: confidence = peak_conf * (1 - exp(-|score| / lambda_decay))
    # Simplified: lambda_decay = p95 / 2 (steeper for M5)
    lambda_decay = p95 / 2.0

    return {
        "p95": round(p95, 4),
        "peak_conf": round(peak_conf, 4),
        "lambda_decay": round(lambda_decay, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="V4 Isotonic Calibration")
    parser.add_argument("--npz", required=True, help="Path to multi-class train.npz")
    parser.add_argument("--model-dir", required=True, help="Directory with CV models")
    parser.add_argument("--output", required=True, help="Output JSON path for calibration")
    parser.add_argument("--n-bins", type=int, default=20)
    parser.add_argument(
        "--model-type",
        default="xgboost",
        choices=["xgboost", "lightgbm"],
        help="Model type to calibrate (default: xgboost)",
    )
    args = parser.parse_args()

    print(f"[CALIBRATE] Loading {args.model_type} CV predictions from {args.model_dir}...")
    y_true, y_prob, y_pred = load_cv_predictions(
        args.npz, args.model_dir, model_type=args.model_type
    )

    # Label distribution
    n_short = int((y_true == 0).sum())
    n_neutral = int((y_true == 1).sum())
    n_long = int((y_true == 2).sum())
    print(f"  True labels: SHORT={n_short}, NEUTRAL={n_neutral}, LONG={n_long}")

    n_pred_short = int((y_pred == 0).sum())
    n_pred_neutral = int((y_pred == 1).sum())
    n_pred_long = int((y_pred == 2).sum())
    print(f"  Predictions: SHORT={n_pred_short}, NEUTRAL={n_pred_neutral}, LONG={n_pred_long}")

    # Overall accuracy
    acc = float(np.mean(y_pred == y_true))
    print(f"  Accuracy: {acc:.4f}")

    # Directional WR (on non-neutral predictions)
    directional_mask = y_pred != 1  # model didn't predict NEUTRAL
    true_directional = y_true != 1  # true label is not NEUTRAL
    both_directional = directional_mask & true_directional
    if both_directional.sum() > 0:
        directional_wr = float(np.mean(y_pred[both_directional] == y_true[both_directional]))
        print(f"  Directional WR: {directional_wr:.4f} (n={both_directional.sum()})")

    # LONG precision
    long_pred = y_pred == 2
    long_correct = long_pred & (y_true == 2)
    long_precision = float(long_correct.sum() / max(long_pred.sum(), 1))
    print(
        f"  LONG precision: {long_precision:.4f} (pred={long_pred.sum()}, correct={long_correct.sum()})"
    )

    # SHORT precision
    short_pred = y_pred == 0
    short_correct = short_pred & (y_true == 0)
    short_precision = float(short_correct.sum() / max(short_pred.sum(), 1))
    print(
        f"  SHORT precision: {short_precision:.4f} (pred={short_pred.sum()}, correct={short_correct.sum()})"
    )

    print("\n[CALIBRATE] Fitting isotonic calibration...")
    calibration = fit_isotonic_calibration(y_true, y_prob, args.n_bins)

    # Compute quantile_gaussian params
    qg_params = compute_quantile_gaussian_params(y_prob, y_true)
    calibration["quantile_gaussian"] = qg_params
    print(f"\n  Quantile Gaussian: {qg_params}")

    # Add summary stats
    calibration["model_stats"] = {
        "accuracy": round(acc, 4),
        "long_precision": round(long_precision, 4),
        "short_precision": round(short_precision, 4),
        "n_short_true": n_short,
        "n_neutral_true": n_neutral,
        "n_long_true": n_long,
        "n_short_pred": n_pred_short,
        "n_neutral_pred": n_pred_neutral,
        "n_long_pred": n_pred_long,
        "long_pred_ratio": round(n_pred_long / max(len(y_pred), 1), 4),
    }

    # Save
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(calibration, f, indent=2, ensure_ascii=False)
    print(f"\n[DONE] Calibration saved to {args.output}")

    # Final verdict
    if calibration.get("calibration_quality", {}).get("is_inverted"):
        print("\n[FAIL] Confidence calibration is still INVERTED — isotonic fix needed in serving.")
        return 1
    elif calibration.get("calibration_quality", {}).get("max_abs_error", 1.0) > 0.15:
        print("\n[WARN] Calibration error > 0.15 — consider isotonic calibration in serving.")
        return 2
    else:
        print("\n[PASS] Confidence calibration is acceptable.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
