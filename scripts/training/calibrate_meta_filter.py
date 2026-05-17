"""Train Platt scaling (logistic calibration) on held-out validation set.

Converts raw Stage 2 probabilities to log-odds space, then fits a
LogisticRegression calibrator.  This produces a smooth sigmoid mapping
that avoids the step-function collapse of IsotonicRegression.

Platt Scaling reference: Platt, J. (1999). "Probabilistic Outputs for
Support Vector Machines."

Why NOT IsotonicRegression:
  - PAVA produces piecewise constant steps → sparse regions create vertical
    cliffs (0.001 input change → 0.3 output jump)
  - Dense regions get flattened to constant → kills conformal percentile
  - Platt's sigmoid preserves micro-distinctions everywhere

Usage:
    python scripts/training/calibrate_meta_filter.py \
        --model data/models/institutional/meta_stage2_mlp_v2_20260516_135812.json \
        --data data/training/meta_features_pit_v3.npz \
        --output data/models/institutional/meta_stage2_mlp_v2_calibrator.pkl \
        --calibration-split 0.15
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression


def _load_model(model_path: Path) -> tuple[Any, str]:
    """Load a Stage 2 model, returning (model, model_type).

    model_type is 'lgb' or 'mlp'.
    """
    if model_path.suffix == ".json":
        from core.brains.online_mlp_model import OnlineMLP

        return OnlineMLP.load(str(model_path)), "mlp"
    elif model_path.suffix == ".txt":
        import lightgbm as lgb

        return lgb.Booster(model_file=str(model_path)), "lgb"
    else:
        print(f"[calibrate] ERROR: Unsupported model format: {model_path.suffix}")
        sys.exit(1)


def _predict(model: Any, model_type: str, X: np.ndarray) -> np.ndarray:
    """Get raw probability predictions from a Stage 2 model."""
    if model_type == "lgb":
        return model.predict(X)
    elif model_type == "mlp":
        from core.brains.online_mlp_model import OnlineMLP

        assert isinstance(model, OnlineMLP)
        probs = np.zeros(len(X), dtype=np.float64)
        for i in range(len(X)):
            raw = model.forward_numpy(X[i : i + 1].astype(np.float32))
            if raw.ndim == 2 and raw.shape[1] == 2:
                probs[i] = float(raw[0, 1])  # batch × 2: P(class=1)
            elif raw.ndim == 1 and len(raw) >= 2:
                probs[i] = float(raw[1])  # (2,): P(class=1) at index 1
            elif raw.ndim == 1:
                probs[i] = float(raw[0])  # single-output
            else:
                probs[i] = float(raw.ravel()[0])
        return probs
    else:
        raise ValueError(f"Unknown model type: {model_type}")


def calibrate(
    model_path: str | Path,
    data_path: str | Path,
    output_path: str | Path,
    *,
    calibration_split: float = 0.15,
    c_param: float = 1.0,
    seed: int = 42,
) -> None:
    """Train Platt scaling calibrator on held-out validation set.

    Args:
        model_path: Path to trained Stage 2 model (.txt or .json).
        data_path: Path to meta-features NPZ (must have X, y fields).
        output_path: Where to save the calibrator (.pkl).
        calibration_split: Fraction of data to hold out for calibration
            (last N% chronologically). Default 0.15.
        c_param: LogisticRegression regularization (lower = stronger).
            Default 1.0.
        seed: Random seed.
    """
    model_path = Path(model_path)
    data_path = Path(data_path)
    output_path = Path(output_path)

    if not model_path.exists():
        print(f"[calibrate] ERROR: Model not found: {model_path}")
        sys.exit(1)
    if not data_path.exists():
        print(f"[calibrate] ERROR: Data not found: {data_path}")
        sys.exit(1)

    # Load model
    print(f"[calibrate] Loading model: {model_path}")
    model, model_type = _load_model(model_path)
    print(f"[calibrate] Model type: {model_type}")

    # Load data
    print(f"[calibrate] Loading data: {data_path}")
    raw = np.load(data_path, allow_pickle=True)
    X = np.asarray(raw["X"], dtype=np.float64)
    y = np.asarray(raw["y"], dtype=np.int32).ravel()

    print(f"[calibrate] X: {X.shape}, y: {y.shape}")
    print(f"[calibrate] Label distribution: {np.sum(y==1)} TP, {np.sum(y==0)} non-TP")

    # Chronological split: last calibration_split% as calibration set
    n_cal = int(len(X) * calibration_split)
    if n_cal < 50:
        print(
            f"[calibrate] ERROR: Calibration set too small ({n_cal} samples). Reduce --calibration-split or use more data."
        )
        sys.exit(1)

    X_train, X_cal = X[:-n_cal], X[-n_cal:]
    y_cal = y[-n_cal:]

    print(f"[calibrate] Train set: {len(X_train)}, Calibration set: {len(X_cal)}")

    # Get raw probabilities on calibration set
    print("[calibrate] Computing raw probabilities on calibration set...")
    raw_probs = _predict(model, model_type, X_cal)
    print(
        f"[calibrate] Raw probs: mean={float(np.mean(raw_probs)):.4f}, "
        f"std={float(np.std(raw_probs)):.4f}, "
        f"range=[{float(np.min(raw_probs)):.4f}, {float(np.max(raw_probs)):.4f}]"
    )

    # Platt Scaling: convert to log-odds, fit logistic calibrator
    eps = 1e-6
    raw_probs_clipped = np.clip(raw_probs, eps, 1 - eps)
    log_odds = np.log(raw_probs_clipped / (1 - raw_probs_clipped)).reshape(-1, 1)

    calibrator = LogisticRegression(C=c_param, solver="lbfgs", random_state=seed)
    calibrator.fit(log_odds, y_cal)

    # Evaluate calibration quality
    cal_probs = calibrator.predict_proba(log_odds)[:, 1]
    print(
        f"[calibrate] Calibrated probs: mean={float(np.mean(cal_probs)):.4f}, "
        f"std={float(np.std(cal_probs)):.4f}, "
        f"range=[{float(np.min(cal_probs)):.4f}, {float(np.max(cal_probs)):.4f}]"
    )

    # Brier score before/after
    brier_raw = float(np.mean((raw_probs - y_cal) ** 2))
    brier_cal = float(np.mean((cal_probs - y_cal) ** 2))
    print(
        f"[calibrate] Brier score: {brier_raw:.4f} → {brier_cal:.4f} "
        f"({'improved' if brier_cal < brier_raw else 'worse'})"
    )

    # Platt params
    print(
        f"[calibrate] Platt params: coef_={calibrator.coef_[0][0]:.4f}, "
        f"intercept_={calibrator.intercept_[0]:.4f}"
    )

    # Save
    output_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(calibrator, str(output_path))
    print(f"[calibrate] Saved calibrator to: {output_path}")
    print("[calibrate] Done.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Train Platt scaling calibrator for Stage 2 meta filter"
    )
    ap.add_argument("--model", required=True, help="Path to Stage 2 model (.txt or .json)")
    ap.add_argument("--data", required=True, help="Path to meta-features NPZ")
    ap.add_argument("--output", required=True, help="Output path for calibrator (.pkl)")
    ap.add_argument(
        "--calibration-split",
        type=float,
        default=0.15,
        help="Fraction of data to hold out for calibration (default: 0.15 = last 15%%)",
    )
    ap.add_argument(
        "--c",
        type=float,
        default=1.0,
        dest="c_param",
        help="LogisticRegression regularization strength (default: 1.0)",
    )
    ap.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    args = ap.parse_args(argv)

    calibrate(
        model_path=args.model,
        data_path=args.data,
        output_path=args.output,
        calibration_split=args.calibration_split,
        c_param=args.c_param,
        seed=args.seed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
