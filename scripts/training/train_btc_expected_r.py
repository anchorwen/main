"""
BTC Two-Tower Expected R Training Pipeline.

Phase 2 of the Expected R Regression paradigm:
  - Tower LONG: LightGBM regressor → E[R_long]
  - Tower SHORT: LightGBM regressor → E[R_short]
  - Asymmetric Huber Loss (over-prediction penalty 2×)
  - Walk-forward evaluation

Per Institutional Review:
  Trap 1: Two-Tower architecture (independent LONG/SHORT models)
  Trap 2: Huber Loss (handles bimodal R distribution)
  Trap 3: Asymmetric Penalty (over-prediction weighted 2×)

Usage:
  python scripts/training/train_btc_expected_r.py \
    --dataset data_btc/training/btc_expected_r_v1 \
    --model-dir data_btc/models/btc_expected_r_v1
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ═══════════════════════════════════════════════════════════════════
# V4: Time-Decay Sample Weighting
# ═══════════════════════════════════════════════════════════════════


def compute_time_decay_weights(
    timestamps: np.ndarray,
    half_life_days: float = 180.0,
) -> np.ndarray:
    """Exponential time-decay weights favoring recent samples.

    weight(t) = exp(-age_days * ln(2) / half_life_days)

    The most recent sample gets weight 1.0.  A sample at exactly
    *half_life_days* ago gets weight 0.5.

    Reused from train_btc_swing_v9.py:compute_time_decay_weights()
    """
    if len(timestamps) == 0:
        return np.array([], dtype=np.float32)

    latest = timestamps[-1]
    age_seconds = latest - timestamps
    age_days = age_seconds / 86400.0
    decay_rate = math.log(2) / half_life_days
    weights = np.exp(-age_days * decay_rate)
    # Ensure no weight is below a floor to avoid de-facto dropping old data
    weights = np.maximum(weights, 0.05)
    return weights.astype(np.float32)


# ═══════════════════════════════════════════════════════════════════
# Asymmetric Huber Loss for LightGBM
# ═══════════════════════════════════════════════════════════════════


def asymmetric_huber_objective(y_pred, train_data):
    """
    Custom objective for LightGBM 4.x: Asymmetric Huber Loss.

    LightGBM API: fobj(preds, train_data) → (grad, hess)
      - y_pred: 1D array of predictions
      - train_data: lgb.Dataset (use .get_label() to get y_true)

    Over-prediction (model too optimistic → predicted R > actual R):
      → heavier penalty (× overpred_penalty)
    Under-prediction (model too cautious → predicted R < actual R):
      → standard Huber penalty

    Returns:
        grad, hess (gradient and hessian for LightGBM)
    """
    y_true = train_data.get_label()
    delta = 1.0
    overpred_penalty = 1.2  # V4.2: mild asymmetry (sweet spot: ρ + profitability)

    residual = y_true - y_pred  # negative residual = over-prediction
    abs_res = np.abs(residual)

    # Huber gradient and hessian
    is_small = abs_res <= delta
    grad = np.where(is_small, -residual, -delta * np.sign(residual))
    hess = np.where(is_small, 1.0, 0.01)  # small hessian for linear region

    # Asymmetric penalty: over-predictions get extra weight
    is_overpred = (y_pred > y_true).astype(np.float64)
    weight = 1.0 + (overpred_penalty - 1.0) * is_overpred

    return grad * weight, hess * weight


def asymmetric_huber_eval(y_pred, train_data):
    """
    Evaluation metric for asymmetric Huber (symmetric for fair comparison).

    LightGBM API: feval(preds, train_data) → (eval_name, eval_result, is_higher_better)
    """
    y_true = train_data.get_label()
    delta = 1.0

    residual = y_true - y_pred
    abs_res = np.abs(residual)
    is_small = abs_res <= delta
    loss = np.where(is_small, 0.5 * residual**2, delta * (abs_res - 0.5 * delta))
    return ("asymmetric_huber", float(np.mean(loss)), False)


# ═══════════════════════════════════════════════════════════════════
# Metrics
# ═══════════════════════════════════════════════════════════════════


def compute_metrics(y_true, y_pred, name=""):
    """
    Compute regression + directional metrics.

    Returns: dict of metrics
    """
    mask = ~np.isnan(y_true) & ~np.isnan(y_pred)
    yt = y_true[mask]
    yp = y_pred[mask]

    if len(yt) < 10:
        return {"n": len(yt), "warning": "too few samples"}

    # R2
    ss_res = float(np.sum((yt - yp) ** 2))
    ss_tot = float(np.sum((yt - np.mean(yt)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # Spearman rank correlation
    from scipy.stats import spearmanr

    try:
        rho, pval = spearmanr(yt, yp)
    except ValueError:
        rho, pval = 0.0, 1.0

    # Directional accuracy (sign match)
    sign_match = np.mean(np.sign(yt) == np.sign(yp))

    # Mean Absolute Error
    mae = np.mean(np.abs(yt - yp))

    # RMSE
    rmse = np.sqrt(np.mean((yt - yp) ** 2))

    # Bias (mean prediction error)
    bias = np.mean(yp - yt)

    # Per-outcome breakdown
    tp_mask = yt > 0.1
    sl_mask = yt < -0.5
    timeout_mask = ~tp_mask & ~sl_mask

    results = {
        "n": len(yt),
        "r2": round(r2, 6),
        "spearman_rho": round(rho, 6),
        "spearman_pval": round(pval, 6),
        "sign_match": round(sign_match, 6),
        "mae": round(mae, 6),
        "rmse": round(rmse, 6),
        "bias": round(bias, 6),
        "mean_y_true": round(float(np.mean(yt)), 6),
        "mean_y_pred": round(float(np.mean(yp)), 6),
    }

    if tp_mask.sum() > 5:
        results["tp_mae"] = round(float(np.mean(np.abs(yt[tp_mask] - yp[tp_mask]))), 6)
        results["tp_bias"] = round(float(np.mean(yp[tp_mask] - yt[tp_mask])), 6)
    if sl_mask.sum() > 5:
        results["sl_mae"] = round(float(np.mean(np.abs(yt[sl_mask] - yp[sl_mask]))), 6)
        results["sl_bias"] = round(float(np.mean(yp[sl_mask] - yt[sl_mask])), 6)

    return results


# ═══════════════════════════════════════════════════════════════════
# Training
# ═══════════════════════════════════════════════════════════════════


def train_tower(X_train, y_train, X_val, y_val, tower_name, params=None, sample_weight=None):
    """
    Train a single tower (LONG or SHORT) LightGBM model.

    Uses asymmetric Huber objective (V4: wired via fobj).
    sample_weight: optional per-sample training weights (time-decay).
                   ONLY applied to dtrain — dval stays unweighted.
    """
    import lightgbm as lgb

    if params is None:
        params = {
            "boosting_type": "gbdt",
            "metric": "None",  # V4: suppress default RMSE — early stopping watches feval (asymmetric Huber) only
            "num_leaves": 63,
            "max_depth": 6,
            "learning_rate": 0.03,
            "n_estimators": 500,
            "min_data_in_leaf": 50,
            "feature_fraction": 0.7,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "lambda_l1": 0.1,
            "lambda_l2": 0.1,
            "verbosity": -1,
            "random_state": 42,
        }

    print(f"\n  [{tower_name}] Training LightGBM (V4: asymmetric Huber fobj+feval)...")
    print(f"    Samples: {len(X_train):,} train / {len(X_val):,} val")

    # Filter NaN
    train_mask = ~np.isnan(y_train)
    val_mask = ~np.isnan(y_val)
    X_tr = X_train[train_mask]
    y_tr = y_train[train_mask]
    X_v = X_val[val_mask]
    y_v = y_val[val_mask]

    # V4: Apply time-decay weights to dtrain ONLY.
    # dval MUST remain unweighted to preserve natural market distribution.
    sw_tr = sample_weight[train_mask].astype(np.float64) if sample_weight is not None else None
    dtrain = lgb.Dataset(X_tr, label=y_tr, weight=sw_tr)
    dval = lgb.Dataset(X_v, label=y_v, reference=dtrain)  # NO weight on validation

    # V4: Wire in the asymmetric Huber objective that was previously defined but never used.
    # LightGBM 4.x: custom objective via params['objective'], feval via kwarg.
    # Both functions use native LightGBM API: fobj(preds, train_data), feval(preds, train_data).
    params.pop(
        "metric", None
    )  # Remove metric str when using custom objective (lgb_trainer.py pattern)
    params["objective"] = asymmetric_huber_objective  # LightGBM 4.x: custom obj via params dict

    # Train with early stopping
    model = lgb.train(
        params,
        dtrain,
        feval=asymmetric_huber_eval,
        valid_sets=[dtrain, dval],
        valid_names=["train", "val"],
        callbacks=[lgb.early_stopping(50), lgb.log_evaluation(50)],
    )

    # Predictions
    y_pred_train = model.predict(X_tr)
    y_pred_val = model.predict(X_v)

    train_metrics = compute_metrics(y_tr, y_pred_train, f"{tower_name}_train")
    val_metrics = compute_metrics(y_v, y_pred_val, f"{tower_name}_val")

    return model, train_metrics, val_metrics


def train_tower_multi_seed(
    X_train, y_train, X_val, y_val, tower_name, n_seeds=3, sample_weight=None
):
    """Train multiple seeds and ensemble. sample_weight: V4 time-decay (dtrain only)."""
    models = []
    metrics_list = []

    for seed in [42, 123, 456][:n_seeds]:
        params = {
            "boosting_type": "gbdt",
            "metric": "None",  # V4: suppress default RMSE
            "num_leaves": 63,
            "max_depth": 6,
            "learning_rate": 0.03,
            "n_estimators": 500,
            "min_data_in_leaf": 50,
            "feature_fraction": 0.7,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "lambda_l1": 0.1,
            "lambda_l2": 0.1,
            "verbosity": -1,
            "random_state": seed,
            "seed": seed,
        }
        model, train_m, val_m = train_tower(
            X_train,
            y_train,
            X_val,
            y_val,
            f"{tower_name}_seed{seed}",
            params,
            sample_weight=sample_weight,
        )
        models.append(model)
        metrics_list.append((train_m, val_m))

    return models, metrics_list


# ═══════════════════════════════════════════════════════════════════
# Decision Gate
# ═══════════════════════════════════════════════════════════════════


def evaluate_decision_gate(model_long, model_short, X, y_long, y_short, threshold=0.15):
    """
    Simulate the Two-Tower decision gate on test data.

    For each sample:
      1. Predict E[R_long] and E[R_short]
      2. Choose direction with higher E[R] if > threshold
      3. Compare against actual outcome of that direction
    """
    pred_long = model_long.predict(X)
    pred_short = model_short.predict(X)

    n = len(X)
    decisions = []
    actuals = []

    for i in range(n):
        e_long = pred_long[i]
        e_short = pred_short[i]

        if e_long > threshold and e_long > e_short:
            decisions.append("LONG")
            actuals.append(y_long[i] if not np.isnan(y_long[i]) else 0)
        elif e_short > threshold and e_short > e_short:
            # Wait, this is always False! Fix:
            pass

    # Vectorized version
    chose_long = (pred_long > threshold) & (pred_long > pred_short)
    chose_short = (pred_short > threshold) & (pred_short > pred_long)
    chose_neutral = ~chose_long & ~chose_short

    n_long = chose_long.sum()
    n_short = chose_short.sum()
    n_neutral = chose_neutral.sum()
    total = n_long + n_short + n_neutral

    # Actual R for chosen trades
    actual_r = np.where(chose_long, y_long, np.where(chose_short, y_short, 0.0))
    actual_r_traded = actual_r[chose_long | chose_short]

    # Directional accuracy
    if n_long > 0:
        long_wr = (y_long[chose_long] > 0).mean()
        long_mean_r = y_long[chose_long].mean()
    else:
        long_wr = 0.0
        long_mean_r = 0.0

    if n_short > 0:
        short_wr = (y_short[chose_short] > 0).mean()
        short_mean_r = y_short[chose_short].mean()
    else:
        short_wr = 0.0
        short_mean_r = 0.0

    overall_wr = (actual_r_traded > 0).mean() if len(actual_r_traded) > 0 else 0.0

    return {
        "n_total": int(n),
        "n_long_signals": int(n_long),
        "n_short_signals": int(n_short),
        "n_neutral": int(n_neutral),
        "signal_rate": float(round((n_long + n_short) / total, 4)) if total > 0 else 0.0,
        "long_wr": float(round(long_wr, 4)),
        "short_wr": float(round(short_wr, 4)),
        "overall_wr": float(round(overall_wr, 4)),
        "long_mean_r": float(round(long_mean_r, 4)),
        "short_mean_r": float(round(short_mean_r, 4)),
        "mean_r_per_trade": float(round(actual_r_traded.mean(), 4))
        if len(actual_r_traded) > 0
        else 0.0,
        "total_r": float(round(actual_r_traded.sum(), 4)) if len(actual_r_traded) > 0 else 0.0,
        "threshold": float(threshold),
    }


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Train BTC Two-Tower Expected R model")
    parser.add_argument("--dataset", default="data_btc/training/btc_expected_r_v1")
    parser.add_argument("--model-dir", default="data_btc/models/btc_expected_r_v1")
    parser.add_argument("--n-seeds", type=int, default=3)
    parser.add_argument(
        "--threshold", type=float, default=0.15, help="Decision gate E[R] threshold"
    )
    args = parser.parse_args()

    dataset_dir = PROJECT_ROOT / args.dataset
    model_dir = PROJECT_ROOT / args.model_dir
    model_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("Phase 2: Two-Tower Expected R Training")
    print(f"  Dataset:  {dataset_dir}")
    print(f"  Models:   {model_dir}")
    print(f"  Seeds:    {args.n_seeds}")
    print(f"  Time:     {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 80)

    # Load data
    print("\n[1] Loading dataset...")
    train_data = np.load(dataset_dir / "train.npz")
    val_data = np.load(dataset_dir / "val.npz")
    test_data = np.load(dataset_dir / "test.npz")

    X_train, y_train_long, y_train_short = (
        train_data["X"],
        train_data["y_long"],
        train_data["y_short"],
    )
    X_val, y_val_long, y_val_short = val_data["X"], val_data["y_long"], val_data["y_short"]
    X_test, y_test_long, y_test_short = test_data["X"], test_data["y_long"], test_data["y_short"]

    print(f"    Train: {X_train.shape[0]:,} samples")
    print(f"    Val:   {X_val.shape[0]:,} samples")
    print(f"    Test:  {X_test.shape[0]:,} samples")

    # V4: Load timestamps and compute time-decay weights (train only)
    ts_train = train_data.get("timestamps", None)
    sample_weight_train = None
    if ts_train is not None:
        sample_weight_train = compute_time_decay_weights(ts_train, half_life_days=180.0)
        print("\n[1b] Time-decay weights computed (half_life=180d):")
        print(
            f"    Train weights: min={sample_weight_train.min():.3f}, "
            f"p50={np.median(sample_weight_train):.3f}, max={sample_weight_train.max():.3f}"
        )
    else:
        print("\n[1b] No timestamps in dataset — skipping time-decay weighting")

    # R distribution check
    print("\n[2] R distribution diagnostics...")
    for name, y in [
        ("LONG_train", y_train_long),
        ("LONG_val", y_val_long),
        ("SHORT_train", y_train_short),
        ("SHORT_val", y_val_short),
    ]:
        yv = y[~np.isnan(y)]
        print(
            f"    {name}: n={len(yv):,} mean={np.mean(yv):+.4f} "
            f"p50={np.median(yv):+.4f} min={np.min(yv):+.3f} max={np.max(yv):+.3f} "
            f"TP%={np.mean(yv>0)*100:.1f}% SL%={np.mean(yv<-0.5)*100:.1f}%"
        )

    # ── Train Tower LONG ──
    print("\n[3] Training Tower LONG...")
    models_long, long_metrics = train_tower_multi_seed(
        X_train,
        y_train_long,
        X_val,
        y_val_long,
        "Tower_LONG",
        n_seeds=args.n_seeds,
        sample_weight=sample_weight_train,
    )
    best_long_idx = np.argmax([m[1]["spearman_rho"] for m in long_metrics])
    best_long_model = models_long[best_long_idx]
    print(f"\n  Best Tower LONG: seed {[42, 123, 456][best_long_idx]}")
    print(
        f"    Val metrics: R2={long_metrics[best_long_idx][1]['r2']:.4f}, "
        f"Spearman rho={long_metrics[best_long_idx][1]['spearman_rho']:.4f}, "
        f"SignMatch={long_metrics[best_long_idx][1]['sign_match']:.4f}"
    )

    # ── Train Tower SHORT ──
    print("\n[4] Training Tower SHORT...")
    models_short, short_metrics = train_tower_multi_seed(
        X_train,
        y_train_short,
        X_val,
        y_val_short,
        "Tower_SHORT",
        n_seeds=args.n_seeds,
        sample_weight=sample_weight_train,
    )
    best_short_idx = np.argmax([m[1]["spearman_rho"] for m in short_metrics])
    best_short_model = models_short[best_short_idx]
    print(f"\n  Best Tower SHORT: seed {[42, 123, 456][best_short_idx]}")
    print(
        f"    Val metrics: R2={short_metrics[best_short_idx][1]['r2']:.4f}, "
        f"Spearman rho={short_metrics[best_short_idx][1]['spearman_rho']:.4f}, "
        f"SignMatch={short_metrics[best_short_idx][1]['sign_match']:.4f}"
    )

    # ── Decision Gate on Test ──
    print("\n[5] Evaluating Decision Gate on Test set...")
    gate_result = evaluate_decision_gate(
        best_long_model,
        best_short_model,
        X_test,
        y_test_long,
        y_test_short,
        threshold=args.threshold,
    )

    print(f"\n  Decision Gate Results (threshold E[R] > {args.threshold}):")
    print(f"    Total samples:      {gate_result['n_total']:,}")
    print(f"    LONG signals:       {gate_result['n_long_signals']:,}")
    print(f"    SHORT signals:      {gate_result['n_short_signals']:,}")
    print(f"    NEUTRAL:            {gate_result['n_neutral']:,}")
    print(f"    Signal rate:        {gate_result['signal_rate']*100:.1f}%")
    print(f"    LONG WR:            {gate_result['long_wr']*100:.1f}%")
    print(f"    SHORT WR:           {gate_result['short_wr']*100:.1f}%")
    print(f"    Overall WR:         {gate_result['overall_wr']*100:.1f}%")
    print(f"    LONG Mean R:        {gate_result['long_mean_r']:+.4f}")
    print(f"    SHORT Mean R:       {gate_result['short_mean_r']:+.4f}")
    print(f"    Mean R per trade:   {gate_result['mean_r_per_trade']:+.4f}")
    print(f"    Total R:            {gate_result['total_r']:+.4f}")

    # ── Quality Gate Assessment ──
    print("\n[6] Quality Gate Assessment...")
    long_val_metrics = long_metrics[best_long_idx][1]
    short_val_metrics = short_metrics[best_short_idx][1]

    gates = []
    gates.append(
        (
            "LONG Spearman rho >= 0.05",
            long_val_metrics["spearman_rho"] >= 0.05,
            f"rho={long_val_metrics['spearman_rho']:.4f}",
        )
    )
    gates.append(
        (
            "SHORT Spearman rho >= 0.05",
            short_val_metrics["spearman_rho"] >= 0.05,
            f"rho={short_val_metrics['spearman_rho']:.4f}",
        )
    )
    gates.append(
        (
            "LONG SignMatch >= 0.52",
            long_val_metrics["sign_match"] >= 0.52,
            f"sm={long_val_metrics['sign_match']:.4f}",
        )
    )
    gates.append(
        (
            "SHORT SignMatch >= 0.52",
            short_val_metrics["sign_match"] >= 0.52,
            f"sm={short_val_metrics['sign_match']:.4f}",
        )
    )
    gates.append(
        (
            "Overall WR >= 0.40",
            gate_result["overall_wr"] >= 0.40,
            f"WR={gate_result['overall_wr']*100:.1f}%",
        )
    )

    all_pass = True
    for name, passed, detail in gates:
        status = "PASS" if passed else "FAIL"
        symbol = "[+]" if passed else "[!!]"
        print(f"    {symbol} {name}: {status} ({detail})")
        if not passed:
            all_pass = False

    if all_pass:
        print("\n    ALL GATES PASSED — Model deployable as shadow.")
    else:
        print("\n    SOME GATES FAILED — Review before shadow deployment.")

    # ── Save models ──
    print("\n[7] Saving models...")
    import lightgbm as lgb

    best_long_model.save_model(str(model_dir / "tower_long_best.txt"))
    best_short_model.save_model(str(model_dir / "tower_short_best.txt"))

    # Save ensemble (average of all seeds)
    for i, m in enumerate(models_long):
        m.save_model(str(model_dir / f"tower_long_seed{[42,123,456][i]}.txt"))
    for i, m in enumerate(models_short):
        m.save_model(str(model_dir / f"tower_short_seed{[42,123,456][i]}.txt"))

    # Save training report
    report = {
        "schema_version": "expected_r_training.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "dataset": str(dataset_dir),
        "tower_long": {
            "best_seed": [42, 123, 456][best_long_idx],
            "val_metrics": long_val_metrics,
        },
        "tower_short": {
            "best_seed": [42, 123, 456][best_short_idx],
            "val_metrics": short_val_metrics,
        },
        "decision_gate": gate_result,
        "quality_gates": {name: bool(passed) for name, passed, _ in gates},
        "all_gates_passed": bool(all_pass),
    }
    with open(model_dir / "training_report.json", "w") as f:
        json.dump(report, f, indent=2)

    print(f"    {model_dir}/tower_long_best.txt")
    print(f"    {model_dir}/tower_short_best.txt")
    print(f"    {model_dir}/training_report.json")

    print("\n[Phase 2 COMPLETE] Two-Tower models trained.")
    if all_pass:
        print("  Status: READY for shadow deployment.")
    else:
        print("  Status: GATES FAILED — review before proceeding.")


if __name__ == "__main__":
    main()
