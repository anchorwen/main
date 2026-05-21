"""Train initial weights for OnlineLearnerAdapter using barrier labels.

Uses the same data pipeline as train_from_csv.py (barrier labels + 40-dim features)
but trains a linear SGDClassifier instead of an MLP. The output is a lightweight JSON
weight file consumable by OnlineLearnerAdapter.

Usage:
  python scripts/training/train_online_init.py --csv data/raw/xauusdc_m5_1y.csv
  python scripts/training/train_online_init.py --csv data/raw/xauusdc_m5_1y.csv --dry-run
  python scripts/training/train_online_init.py --output data/models/online_learner_v1.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sklearn.linear_model import SGDClassifier
from sklearn.metrics import accuracy_score, classification_report
from sklearn.preprocessing import StandardScaler


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="train_online_init")
    p.add_argument("--csv", default="data/raw/xauusdc_m5_1y.csv", help="OHLC CSV path")
    p.add_argument(
        "--label-contract",
        default="configs/training/label_contracts/label-survival-barrier-1.0.0.json",
        help="Label contract JSON",
    )
    p.add_argument(
        "--output", default="data/models/online_learner_weights.json", help="Output JSON path"
    )
    p.add_argument("--val-split", type=float, default=0.20, help="Validation fraction")
    p.add_argument("--entry-stride", type=int, default=5, help="Bars between label entries")
    p.add_argument("--dry-run", action="store_true", help="Validate data only, skip training")
    return p


def load_csv(csv_path: str) -> np.ndarray:
    """Load OHLC CSV, return (n_bars, 5) array [open, high, low, close, tick_volume]."""
    import pandas as pd

    df = pd.read_csv(csv_path)
    cols_ohlc = {
        "time": "time",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "tick_volume": "tick_volume",
        "spread": "spread",
    }
    # Rename common variants
    for c in df.columns:
        cl = c.strip().lower()
        if cl == "date" or cl == "datetime":
            cols_ohlc["time"] = c
        elif cl == "volume" and "tick_volume" not in df.columns:
            cols_ohlc["tick_volume"] = c
    ohlc = df[["open", "high", "low", "close"]].values.astype(np.float64)
    vol = (
        df[cols_ohlc["tick_volume"]].values.astype(np.float64)
        if cols_ohlc["tick_volume"] in df.columns
        else np.ones(len(df))
    )
    return np.column_stack([ohlc, vol.reshape(-1, 1)])


def compute_atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> np.ndarray:
    n = len(close)
    tr = np.maximum(high[1:] - low[1:], np.abs(high[1:] - close[:-1]))
    tr = np.maximum(tr, np.abs(low[1:] - close[:-1]))
    atr = np.full(n, np.nan)
    atr[period] = np.mean(tr[:period])
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i - 1]) / period
    return atr


def resample_to_timeframe(data_5m: np.ndarray, factor: int) -> np.ndarray:
    """Resample M5 data to a higher timeframe by aggregating every `factor` bars."""
    n = len(data_5m)
    out_len = n // factor
    out = np.zeros((out_len, data_5m.shape[1]))
    for i in range(out_len):
        chunk = data_5m[i * factor : (i + 1) * factor]
        out[i, 0] = chunk[0, 0]  # open = first open
        out[i, 1] = np.max(chunk[:, 1])  # high
        out[i, 2] = np.min(chunk[:, 2])  # low
        out[i, 3] = chunk[-1, 3]  # close = last close
        out[i, 4] = np.sum(chunk[:, 4])  # volume sum
    return out


def compute_features(data: np.ndarray) -> np.ndarray:
    """Compute 10 V9 institutional features from OHLCV data."""
    o, h, l, c, v = data[:, 0], data[:, 1], data[:, 2], data[:, 3], data[:, 4]
    n = len(c)
    eps = 1e-12

    # Ret_1
    ret_1 = np.zeros(n)
    ret_1[1:] = (c[1:] - c[:-1]) / (c[:-1] + eps)

    # Body Ratio
    hl_range = h - l + eps
    body_ratio = np.clip((c - o) / hl_range, -1, 1)

    # ATR(14)
    atr = compute_atr(h, l, c, 14)

    # RSI(14)
    delta = np.diff(c, prepend=c[0])
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = np.zeros(n)
    avg_loss = np.zeros(n)
    period = 14
    avg_gain[period] = np.mean(gain[1 : period + 1])
    avg_loss[period] = np.mean(loss[1 : period + 1])
    for i in range(period + 1, n):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i]) / period
    rs = avg_gain / (avg_loss + eps)
    rsi = np.clip(100.0 - 100.0 / (1.0 + rs), 0, 100)

    # MACD
    ema12 = np.zeros(n)
    ema26 = np.zeros(n)
    ema12[0] = c[0]
    ema26[0] = c[0]
    a12 = 2.0 / 13.0
    a26 = 2.0 / 27.0
    for i in range(1, n):
        ema12[i] = c[i] * a12 + ema12[i - 1] * (1 - a12)
        ema26[i] = c[i] * a26 + ema26[i - 1] * (1 - a26)
    macd = ema12 - ema26

    # Vol_ZScore (20-bar rolling)
    vol_zscore = np.zeros(n)
    window = 20
    for i in range(window, n):
        w = v[i - window : i]
        vol_zscore[i] = (v[i] - np.mean(w)) / (np.std(w) + eps)

    # Macro1_Corr (20-bar lag-1 autocorrelation of returns)
    macro1_corr = np.zeros(n)
    for i in range(window, n):
        w = ret_1[i - window : i]
        if len(w) > 1 and np.std(w) > 0:
            macro1_corr[i] = np.corrcoef(w[:-1], w[1:])[0, 1] if len(w) > 2 else 0.0

    # Price_ZScore (20-bar price z-score)
    macro_gold = np.zeros(n)
    for i in range(window, n):
        w = c[i - window : i]
        macro_gold[i] = (c[i] - np.mean(w)) / (np.std(w) + eps)

    # OU_Theta (20-bar OLS)
    ou_theta = np.zeros(n)
    for i in range(window, n):
        w = c[i - window : i + 1]
        y = np.diff(w)
        x = w[:-1]
        if np.std(x) > 0:
            beta = np.sum((x - np.mean(x)) * (y - np.mean(y))) / (
                np.sum((x - np.mean(x)) ** 2) + eps
            )
            ou_theta[i] = -beta

    # Hurst (20-bar rescaled range)
    hurst = np.zeros(n)
    for i in range(window, n):
        w = c[i - window : i + 1]
        r = np.max(w) - np.min(w)
        s = np.std(w) + eps
        hurst[i] = np.log(r / s) / np.log(window) if r > 0 else 0.0

    return np.column_stack(
        [ret_1, body_ratio, atr, rsi, macd, vol_zscore, macro1_corr, macro_gold, ou_theta, hurst]
    )


def build_label_contract(contract_path: str) -> dict[str, Any]:
    with open(contract_path, encoding="utf-8") as f:
        return json.load(f)


def build_barrier_labels(
    data: np.ndarray,
    atr: np.ndarray,
    contract: dict[str, Any],
    stride: int = 5,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Generate barrier labels: entry every `stride` bars, forward-scan for TP/SL hit.

    Returns (entry_indices, labels, entry_prices).
    """
    c = data[:, 3]
    sl_mult = float(contract.get("sl_atr_mult", 2.0))
    tp_mult = float(contract.get("tp_atr_mult", 3.5))
    horizon = int(contract.get("horizon_bars", 12))
    min_atr = float(contract.get("min_atr", 0.5))

    entries = []
    labels = []
    entry_prices = []

    for i in range(atr.shape[0] - horizon - 1):
        if i % stride != 0:
            continue
        if np.isnan(atr[i]) or atr[i] < min_atr:
            continue
        entry = c[i]
        sl_dist = sl_mult * atr[i]
        tp_dist = tp_mult * atr[i]
        sl_long = entry - sl_dist
        tp_long = entry + tp_dist
        sl_short = entry + sl_dist
        tp_short = entry - tp_dist

        # Long side
        for j in range(i + 1, min(i + horizon + 1, len(c))):
            if c[j] <= sl_long:
                entries.append(i)
                labels.append(-1)  # sl_hit_first = loss
                entry_prices.append(entry)
                break
            elif c[j] >= tp_long:
                entries.append(i)
                labels.append(1)  # tp_hit_first = win
                entry_prices.append(entry)
                break
        else:
            entries.append(i)
            labels.append(0)  # timeout
            entry_prices.append(entry)

        # Short side — add more entries for label balance
        if (i // stride) % 2 == 0:
            if i + 1 < len(c):
                entries.append(i + 1)
                entry_prices.append(c[i + 1])
                for j in range(i + 2, min(i + horizon + 2, len(c))):
                    if c[j] >= sl_short:
                        labels.append(-1)  # sl_hit_first (for short) = loss
                        break
                    elif c[j] <= tp_short:
                        labels.append(1)  # tp_hit_first (for short) = win
                        break
                else:
                    labels.append(0)

    return np.array(entries), np.array(labels), np.array(entry_prices)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    print("=" * 60)
    print("  ONLINE SGD LEARNER — INITIAL TRAINING")
    print(f"  Data: {args.csv}")
    print(f"  Label Contract: {args.label_contract}")
    print("=" * 60)

    # 1. Load data
    print("\nLoading CSV...")
    data_5m = load_csv(args.csv)
    print(f"  {len(data_5m)} bars")

    # 2. Compute ATR
    print("Computing ATR(14)...")
    atr_5m = compute_atr(data_5m[:, 1], data_5m[:, 2], data_5m[:, 3], 14)

    # 3. Build barrier labels
    print(f"Building barrier labels (stride={args.entry_stride})...")
    contract = build_label_contract(args.label_contract)
    entries, labels, _ = build_barrier_labels(data_5m, atr_5m, contract, stride=args.entry_stride)
    n_tp = int(np.sum(labels == 1))
    n_sl = int(np.sum(labels == -1))
    n_to = int(np.sum(labels == 0))
    print(
        f"  {len(labels)} labels: {n_tp} TP ({n_tp/max(len(labels),1)*100:.1f}%), {n_sl} SL ({n_sl/max(len(labels),1)*100:.1f}%), {n_to} timeout ({n_to/max(len(labels),1)*100:.1f}%)"
    )

    # 4. Compute features at all 4 timeframes
    print("Building 40-dim V9 Institutional features (M5/M15/M30/H1)...")
    m5_feats = compute_features(data_5m)
    data_m15 = resample_to_timeframe(data_5m, 3)
    data_m30 = resample_to_timeframe(data_5m, 6)
    data_h1 = resample_to_timeframe(data_5m, 12)
    m15_feats = compute_features(data_m15)
    m30_feats = compute_features(data_m30)
    h1_feats = compute_features(data_h1)

    # 5. Map M5 entry indices to resampled feature indices
    def _tf_index(m5_idx: int, factor: int) -> int:
        return min(m5_idx // factor, data_5m.shape[0] // factor - 1)

    feature_list = []
    valid_label_list = []
    for k, entry_idx in enumerate(entries):
        idx = int(entry_idx)
        if idx >= len(m5_feats):
            continue
        row: list[float] = []
        # All 10 features per timeframe (V9 institutional 40-dim)
        m15_idx = _tf_index(idx, 3)
        m30_idx = _tf_index(idx, 6)
        h1_idx = _tf_index(idx, 12)
        row.extend(m5_feats[idx].tolist())  # M5: 10 features
        row.extend(m15_feats[m15_idx].tolist())  # M15: 10 features
        row.extend(m30_feats[m30_idx].tolist())  # M30: 10 features
        row.extend(h1_feats[h1_idx].tolist())  # H1: 10 features
        feature_list.append(row)
        valid_label_list.append(labels[k])

    X = np.array(feature_list, dtype=np.float64)
    y = np.array(valid_label_list, dtype=np.int32)
    print(f"  Feature matrix: {X.shape}, labels: {y.shape}")

    # 6. Clean NaNs
    valid = ~np.isnan(X).any(axis=1)
    X = X[valid]
    y = y[valid]
    print(f"  After NaN removal: {X.shape}")

    # 7. Train/Val split (chronological)
    split_idx = int(len(X) * (1.0 - args.val_split))
    X_train, X_val = X[:split_idx], X[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]
    print(f"  Train: {len(X_train)}, Val: {len(X_val)}")

    if args.dry_run:
        print("\n  [DRY RUN] Data validation complete. Skipping training.")
        return 0

    # 8. Scale features
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_val_scaled = scaler.transform(X_val)

    # 9. Train SGDClassifier
    print("\nTraining SGDClassifier (log loss, L2, online-capable)...")
    t0 = time.perf_counter()
    clf = SGDClassifier(
        loss="log_loss",
        penalty="l2",
        alpha=0.0001,
        max_iter=1000,
        tol=1e-4,
        learning_rate="optimal",
        random_state=42,
    )
    clf.fit(X_train_scaled, y_train)

    train_acc = accuracy_score(y_train, clf.predict(X_train_scaled))
    val_acc = accuracy_score(y_val, clf.predict(X_val_scaled))
    elapsed = time.perf_counter() - t0
    print(f"  Training complete in {elapsed:.1f}s")
    print(f"  Train accuracy: {train_acc:.4f}")
    print(f"  Val accuracy:   {val_acc:.4f}")
    print(
        f"\n{classification_report(y_val, clf.predict(X_val_scaled), target_names=['short/loss', 'neutral', 'long/win'])}"
    )

    # 10. Export weights JSON
    artifact = {
        "coef_": clf.coef_.tolist(),
        "intercept_": clf.intercept_.tolist(),
        "classes_": clf.classes_.tolist(),
        "n_features": X.shape[1],
        "scaler_mean": scaler.mean_.tolist(),
        "scaler_scale": scaler.scale_.tolist(),
        "train_samples": int(len(X_train)),
        "val_samples": int(len(X_val)),
        "train_accuracy": round(float(train_acc), 4),
        "val_accuracy": round(float(val_acc), 4),
        "total_updates": int(len(X_train)),
        "label_distribution": {"tp_win": n_tp, "sl_loss": n_sl, "timeout_neutral": n_to},
        "data_source": args.csv,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWeights saved: {out_path} ({out_path.stat().st_size} bytes)")

    print("\n" + "=" * 60)
    print("  TRAINING COMPLETE")
    print(f"  Val accuracy: {val_acc:.4f}")
    print(f"  Output: {out_path}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
