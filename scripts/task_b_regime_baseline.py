"""
Task B Phase 1 — Regime Classification Baseline.

Tests whether existing 40-dim v9_institutional features can predict FUTURE market
regime (Trend / Range / Toxic) over a 2-4 hour horizon. If baseline accuracy
significantly exceeds random, regime prediction is viable and Task B proceeds.

Regime labels are mechanically computed from future price action:
  - Trend:   strong directional move with high efficiency
  - Range:   contained, mean-reverting price action
  - Toxic:   extreme volatility or illiquidity
  - Trans:   transitional/ambiguous (excluded from training)

Usage:
    python scripts/task_b_regime_baseline.py --horizon 24
    python scripts/task_b_regime_baseline.py --horizon 48
"""

from __future__ import annotations

import argparse
import sys
import time
import warnings
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, confusion_matrix
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.training.build_calibrated_dataset import compute_features_at_bar

warnings.filterwarnings("ignore", category=UserWarning)

# ── Constants ─────────────────────────────────────────────────────────────────
ATR_PERIOD = 14
LOOKBACK = 480  # bars for feature computation
BAR_SPACING = 12  # sample every N bars
DEFAULT_HORIZON = 24  # M5 bars (2 hours)

# ── Regime label thresholds (data-calibrated on 352K XAUUSD M5 bars) ──────────
# Calibrated for 24-bar horizon. Adjust for other horizons via --horizon flag.
# Percentile references from empirical distribution:
#   range_atr: P25=3.49  P50=4.85  P75=6.93  P90=9.76  P95=12.09
#   move_atr:  P25=0.90  P50=1.99  P75=3.70  P90=6.09  P95=8.04
#   efficiency: P25=0.23 P50=0.44  P75=0.65  P90=0.80

# Toxic: extreme range (top 5%) — checked FIRST, mutually exclusive
TOXIC_MIN_RANGE_ATR = 12.0  # P95 = 12.09

# Trend: significant directional move (top 35% move × top 55% efficiency)
TREND_MIN_MOVE_ATR = 2.5  # ~P58
TREND_MIN_EFFICIENCY = 0.45  # ~P52

# Range: contained range (bottom 25% range) × low net move (bottom 30% move)
RANGE_MAX_RANGE_ATR = 3.5  # ~P25
RANGE_MAX_MOVE_ATR = 1.0  # ~P28
RANGE_MIN_RANGE_ATR = 0.5  # avoid dead/flat markets


def load_ohlc(path: str) -> dict[str, np.ndarray]:
    """Load OHLC CSV → numpy arrays."""
    print(f"[1/5] Loading data: {path}")
    data = np.loadtxt(
        path,
        delimiter=",",
        skiprows=1,
        dtype={
            "names": (
                "time",
                "open",
                "high",
                "low",
                "close",
                "tick_volume",
                "spread",
                "real_volume",
            ),
            "formats": ("U19", "f8", "f8", "f8", "f8", "i8", "i8", "i8"),
        },
    )
    print(f"       {len(data)} bars: {data['time'][0]} → {data['time'][-1]}")
    return {
        "o": data["open"],
        "h": data["high"],
        "l": data["low"],
        "c": data["close"],
        "v": data["tick_volume"],
        "s": data["spread"],
        "n": len(data),
    }


def compute_atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> np.ndarray:
    """Wilder's ATR."""
    n = len(close)
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr = np.full(n, np.nan)
    atr[period - 1] = np.mean(tr[:period])
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def classify_regime(
    future_open: np.ndarray,
    future_high: np.ndarray,
    future_low: np.ndarray,
    future_close: np.ndarray,
    atr_val: float,
) -> tuple[str, dict]:
    """Classify the regime of a future window into Trend/Range/Toxic/Trans.

    Returns (regime_label, metrics_dict).
    """
    if len(future_open) < 6:  # Need minimum bars
        return "transitional", {}

    entry_price = future_open[0]
    max_high = float(np.max(future_high))
    min_low = float(np.min(future_low))
    final_close = float(future_close[-1])

    total_range = max_high - min_low
    net_move = final_close - entry_price

    if atr_val <= 0:
        return "transitional", {}

    range_atr = total_range / atr_val
    move_atr = abs(net_move) / atr_val
    efficiency = abs(net_move) / total_range if total_range > 0 else 0.0

    metrics = {
        "range_atr": round(range_atr, 3),
        "move_atr": round(move_atr, 3),
        "efficiency": round(efficiency, 3),
        "net_move": round(float(net_move), 4),
    }

    # ── Toxic: extreme range ──
    if range_atr >= TOXIC_MIN_RANGE_ATR:
        return "toxic", metrics

    # ── Trend: significant directional move with high efficiency ──
    if move_atr >= TREND_MIN_MOVE_ATR and efficiency >= TREND_MIN_EFFICIENCY:
        return "trend", metrics

    # ── Range: contained, low net movement ──
    if move_atr <= RANGE_MAX_MOVE_ATR and RANGE_MIN_RANGE_ATR <= range_atr <= RANGE_MAX_RANGE_ATR:
        return "range", metrics

    # ── Transitional: everything else ──
    return "transitional", metrics


def generate_regime_labels(ohlc: dict, atr: np.ndarray, horizon: int) -> list[dict]:
    """Generate regime labels by looking at future price action at each sample bar."""
    print(f"[2/5] Generating regime labels (horizon={horizon} bars)...")
    n = ohlc["n"]
    o, h, l, c = ohlc["o"], ohlc["h"], ohlc["l"], ohlc["c"]

    labels = []
    counts = {"trend": 0, "range": 0, "toxic": 0, "transitional": 0}
    n_skipped = 0

    for entry_idx in range(LOOKBACK, n - horizon, BAR_SPACING):
        atr_val = atr[entry_idx]
        if np.isnan(atr_val) or atr_val <= 0:
            n_skipped += 1
            continue

        end_idx = entry_idx + horizon + 1

        regime, metrics = classify_regime(
            o[entry_idx:end_idx],
            h[entry_idx:end_idx],
            l[entry_idx:end_idx],
            c[entry_idx:end_idx],
            float(atr_val),
        )

        counts[regime] += 1
        labels.append(
            {
                "entry_idx": entry_idx,
                "atr": float(atr_val),
                "regime": regime,
                "metrics": metrics,
            }
        )

    n_total = len(labels)
    print(f"       {n_total} samples, {n_skipped} skipped (NaN ATR)")
    for r in ["trend", "range", "toxic", "transitional"]:
        pct = counts[r] / n_total * 100 if n_total else 0
        print(f"       {r:>12s}: {counts[r]:>6} ({pct:5.1f}%)")
    return labels


def compute_features_batch(
    ohlc: dict, labels: list[dict]
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Compute features at each labeled entry point."""
    print("[3/5] Computing features at sample points...")
    o, h, l, c, v = ohlc["o"], ohlc["h"], ohlc["l"], ohlc["c"], ohlc["v"]
    n_labels = len(labels)

    feature_list: list[dict] = []
    y_list: list[str] = []
    t0 = time.time()

    for i, lab in enumerate(labels):
        feats = compute_features_at_bar(o, h, l, c, v, lab["entry_idx"])
        feature_list.append(feats)
        y_list.append(lab["regime"])

        if (i + 1) % 5000 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta = (n_labels - i - 1) / rate
            print(f"       {i+1}/{n_labels} ({rate:.0f} bar/s, ETA {eta:.0f}s)")

    elapsed = time.time() - t0
    print(f"       Done: {n_labels} samples in {elapsed:.0f}s ({n_labels/elapsed:.0f} bar/s)")

    # Build feature matrix
    feature_names = sorted(feature_list[0].keys())
    X = np.array([[f[n] for n in feature_names] for f in feature_list], dtype=np.float64)

    # Drop NaN/Inf
    nan_mask = ~np.isfinite(X).all(axis=1)
    if nan_mask.any():
        print(f"       WARNING: {nan_mask.sum()} rows with NaN/Inf — dropping")
        X = X[~nan_mask]
        y_list = [lab for i, lab in enumerate(y_list) if not nan_mask[i]]

    return X, np.array(y_list), feature_names


def split_time_series(X, y, train_pct=0.60, val_pct=0.20):
    """Time-series split."""
    n = len(X)
    train_end = int(n * train_pct)
    val_end = int(n * (train_pct + val_pct))

    print(
        f"[4/5] Time-series split: train[0:{train_end}], val[{train_end}:{val_end}], "
        f"test[{val_end}:{n}]"
    )
    print(f"       Train class balance: {np.unique(y[:train_end], return_counts=True)}")
    print(f"       Test  class balance: {np.unique(y[val_end:], return_counts=True)}")

    return (
        (X[:train_end], y[:train_end]),
        (X[train_end:val_end], y[train_end:val_end]),
        (X[val_end:], y[val_end:]),
    )


def evaluate_model(
    model, scaler, X_train, y_train, X_val, y_val, X_test, y_test, class_names, horizon
):
    """Evaluate multi-class regime classifier.

    y_train, y_val, y_test are integer-encoded (0..K-1), mapping to class_names.
    """
    print(f"\n[5/5] Evaluation (horizon={horizon} bars)")
    print("=" * 70)

    y_train_pred = model.predict(X_train)
    y_val_pred = model.predict(X_val)
    y_test_pred = model.predict(X_test)

    train_acc = accuracy_score(y_train, y_train_pred)
    val_acc = accuracy_score(y_val, y_val_pred)
    test_acc = accuracy_score(y_test, y_test_pred)

    # Baseline: always predict majority class
    n_classes = len(class_names)
    majority_class = int(np.argmax(np.bincount(y_train.astype(int))))
    baseline_pred = np.full(len(y_test), majority_class)
    baseline_acc = accuracy_score(y_test, baseline_pred)

    print(f"\n  Classes: {class_names}")
    print(f"  Train accuracy:     {train_acc*100:.1f}%")
    print(f"  Val accuracy:       {val_acc*100:.1f}%")
    print(f"  Test accuracy:      {test_acc*100:.1f}%")
    print(
        f"  Majority baseline:  {baseline_acc*100:.1f}% (always predict '{class_names[majority_class]}')"
    )
    print(f"  Lift over baseline: {(test_acc - baseline_acc)*100:+.1f} pp")

    # Confusion matrix — use integer labels
    cm = confusion_matrix(y_test, y_test_pred)
    print("\n  ── Confusion Matrix (Test) ──")
    header = " " * 10 + "".join(f"{n:>8s}" for n in class_names)
    print(header)
    for i, name in enumerate(class_names):
        print(f"  {name:>8s}  " + "".join(f"{cm[i,j]:>8d}" for j in range(len(class_names))))

    # Per-class metrics
    print("\n  ── Per-Class Precision/Recall ──")
    for i, name in enumerate(class_names):
        tp = cm[i, i]
        pred_total = cm[:, i].sum()
        true_total = cm[i, :].sum()
        precision = tp / pred_total * 100 if pred_total > 0 else 0
        recall = tp / true_total * 100 if true_total > 0 else 0
        print(f"  {name:>8s}:  precision={precision:5.1f}%  recall={recall:5.1f}%")

    # Feasibility assessment
    print("\n  ══ REGIME PREDICTION FEASIBILITY ══")
    lift = (test_acc - baseline_acc) * 100
    if lift > 5:
        print(f"  Lift={lift:.1f}pp → [VIABLE] Regime classification has clear signal.")
        print("  Proceed to Task B Phase 2: God's Eye architecture design.")
    elif lift > 2:
        print(f"  Lift={lift:.1f}pp → [MARGINAL] Weak but detectable signal.")
        print("  Proceed with caution — consider ensemble or simpler label definitions.")
    else:
        print(f"  Lift={lift:.1f}pp → [NOT VIABLE] Features cannot predict future regime.")
        print("  Consider: (a) shorter horizon, (b) different label definitions,")
        print("            (c) regime prediction may also be infeasible at M5 scale.")

    return {
        "train_acc": train_acc,
        "val_acc": val_acc,
        "test_acc": test_acc,
        "baseline_acc": baseline_acc,
        "lift_pp": lift,
        "class_names": class_names,
    }


def main():
    parser = argparse.ArgumentParser(description="Task B — Regime Classification Baseline")
    parser.add_argument("--data", default="data/raw/xauusdc_m5_merged.csv")
    parser.add_argument(
        "--horizon",
        type=int,
        default=DEFAULT_HORIZON,
        help="Future horizon in M5 bars (default: 24 = 2h)",
    )
    parser.add_argument("--train-pct", type=float, default=0.60)
    parser.add_argument("--val-pct", type=float, default=0.20)
    args = parser.parse_args()

    print(f"Task B Phase 1 — Regime Classification Baseline (horizon={args.horizon})")
    print("=" * 70)

    # ── 1. Load ──
    ohlc = load_ohlc(args.data)
    atr = compute_atr(ohlc["h"], ohlc["l"], ohlc["c"], ATR_PERIOD)

    # ── 2. Generate regime labels ──
    labels = generate_regime_labels(ohlc, atr, args.horizon)

    # ── 3. Compute features ──
    X, y_str, feature_names = compute_features_batch(ohlc, labels)

    # Remove "transitional" samples (ambiguous labels)
    nontrans_mask = y_str != "transitional"
    X_filtered = X[nontrans_mask]
    y_filtered = y_str[nontrans_mask]
    print(
        f"       After removing 'transitional': {len(y_filtered)} samples "
        f"(removed {(~nontrans_mask).sum()})"
    )

    # Encode labels
    class_names = sorted(set(y_filtered))
    y = np.array([class_names.index(lab) for lab in y_filtered], dtype=np.int32)
    print(f"       Classes: {class_names}")
    for i, name in enumerate(class_names):
        print(f"         {name}: {(y == i).sum()} samples")

    # ── 4. Split ──
    (X_train, y_train), (X_val, y_val), (X_test, y_test) = split_time_series(
        X_filtered, y, args.train_pct, args.val_pct
    )

    # ── 5. Train ──
    print("\n[5/5] Training Logistic Regression (multi-class, C=1.0)...")
    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_val_s = scaler.transform(X_val)
    X_test_s = scaler.transform(X_test)

    model = LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced", random_state=42)
    model.fit(X_train_s, y_train)
    print(f"       Converged in {model.n_iter_[0]} iterations")

    # ── 6. Evaluate ──
    result = evaluate_model(
        model,
        scaler,
        X_train_s,
        y_train,
        X_val_s,
        y_val,
        X_test_s,
        y_test,
        class_names,
        args.horizon,
    )

    return 0 if result["lift_pp"] > 2 else 1


if __name__ == "__main__":
    sys.exit(main())
