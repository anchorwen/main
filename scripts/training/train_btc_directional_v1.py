#!/usr/bin/env python3
"""BTC Directional Brain Training — retrain V6-V10 with bidirectional labels.

DQAF-20260611-001 resolution: ALL BTC swing brains are binary classifiers
trained on 90% LONG-biased survival labels.  This script rebuilds labels as
DIRECTIONAL signals:

  +1.0 = LONG trade wins (TP hit before SL at LONG entry)
  -1.0 = SHORT trade wins (TP hit before SL at SHORT entry)
   0.0 = timeout / no signal

The model is trained as regression (signed score output), compatible with
_score_to_direction()'s "regression" path → naturally produces both LONG
and SHORT predictions.

Label construction:
  For each bar, simulate BOTH a LONG and SHORT trade with the strategy's
  SL/TP parameters.  If LONG hits TP first → +1.  If SHORT hits TP first →
  -1.  If neither hits TP (timeout or both hit SL) → 0.

Usage:
  python scripts/training/train_btc_directional_v1.py --timeframe H1
  python scripts/training/train_btc_directional_v1.py --timeframe M15
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

UTC = UTC
ROOT = Path(__file__).resolve().parent.parent.parent

# ── Strategy Parameters (from live_btc.yaml btc_swing) ──
SL_ATR_MULT = 2.0
TP_ATR_MULT = 2.5
SPREAD_POINTS = 200  # BTC spread ~$200
SLIPPAGE_POINTS = 50


def load_btc_ohlc(filepath: str = "data/training/btc_m5_8year.csv") -> np.ndarray:
    """Load BTC OHLC data.  Expects CSV with columns: time,open,high,low,close,volume."""
    path = ROOT / filepath
    if not path.exists():
        print(f"[FATAL] Data file not found: {path}")
        print("  Expected: 8-year BTC M5 OHLC CSV")
        sys.exit(1)
    data = np.genfromtxt(
        path,
        delimiter=",",
        skip_header=1,
        dtype=np.float64,
        usecols=(0, 1, 2, 3, 4, 5),
        names=["time", "open", "high", "low", "close", "volume"],
    )
    return data


def compute_features(
    ohlc: np.ndarray,
    timeframe: str = "H1",
) -> tuple[np.ndarray, np.ndarray]:
    """Compute 37-dim btc_macro_enhanced features from OHLC data.

    Returns (X, timestamps) where X has shape (n_samples, 37).
    """
    n = len(ohlc)
    close = ohlc["close"]
    high = ohlc["high"]
    low = ohlc["low"]
    volume = ohlc["volume"]

    # Resample to target timeframe if needed
    if timeframe != "M5":
        tf_minutes = {"M15": 15, "M30": 30, "H1": 60, "H4": 240}.get(timeframe, 60)
        bars_per_tf = tf_minutes // 5
        # Simple resample: take every Nth bar
        indices = np.arange(0, n, bars_per_tf, dtype=int)
        close_tf = close[indices]
        high_tf = high[indices]
        low_tf = low[indices]
        volume_tf = volume[indices]
        timestamps = ohlc["time"][indices]
    else:
        close_tf = close
        high_tf = high
        low_tf = low
        volume_tf = volume
        timestamps = ohlc["time"]

    n_tf = len(close_tf)

    # ── Feature buffers ──
    X = np.zeros((n_tf, 37), dtype=np.float32)

    # Helper: rolling window functions
    def _ema(x, period):
        alpha = 2.0 / (period + 1)
        result = np.zeros_like(x)
        result[0] = x[0]
        for i in range(1, len(x)):
            result[i] = alpha * x[i] + (1 - alpha) * result[i - 1]
        return result

    def _rsi(x, period=14):
        delta = np.diff(x, prepend=x[0])
        gain = np.maximum(delta, 0)
        loss = np.maximum(-delta, 0)
        avg_gain = np.zeros_like(x)
        avg_loss = np.zeros_like(x)
        for i in range(period, len(x)):
            avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i]) / period
            avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i]) / period
        rs = np.divide(avg_gain, avg_loss, out=np.zeros_like(avg_gain), where=avg_loss != 0)
        return 100.0 - 100.0 / (1.0 + rs)

    def _rolling(x, window, func):
        result = np.zeros_like(x)
        for i in range(len(x)):
            start = max(0, i - window + 1)
            result[i] = func(x[start : i + 1])
        return result

    # ── D1 features (slots 0-11) ──
    atr14 = _rolling(np.maximum(high_tf - low_tf, 0), 14, np.mean)
    rsi14 = _rsi(close_tf, 14)
    ema12 = _ema(close_tf, 12)
    ema26 = _ema(close_tf, 26)
    macd = ema12 - ema26
    sma20 = _rolling(close_tf, 20, np.mean)
    std20 = np.array([np.std(close_tf[max(0, i - 19) : i + 1]) for i in range(n_tf)])
    bb_width = np.divide(2 * std20, sma20, out=np.zeros_like(sma20), where=sma20 != 0)
    adx_raw = np.abs(close_tf[1:] - close_tf[:-1])
    adx14 = _rolling(np.append(adx_raw, adx_raw[-1]), 14, np.mean)
    vol_sma20 = _rolling(volume_tf, 20, np.mean)
    vol_std20 = np.array([np.std(volume_tf[max(0, i - 19) : i + 1]) for i in range(n_tf)])
    vol_zscore = np.divide(
        volume_tf - vol_sma20, vol_std20, out=np.zeros_like(vol_sma20), where=vol_std20 != 0
    )
    body_ratio = np.divide(
        np.abs(close_tf - np.roll(close_tf, 1)),
        np.maximum(high_tf - low_tf, 1e-9),
    )

    # Slots 0-11: macro features
    X[:, 0] = np.divide(close_tf - np.roll(close_tf, 1), np.maximum(np.roll(close_tf, 1), 1e-9))  # D1_Ret_1
    X[:, 1] = body_ratio
    X[:, 2] = atr14
    X[:, 3] = rsi14
    X[:, 4] = macd
    X[:, 5] = vol_zscore
    X[:, 6] = bb_width
    X[:, 8] = adx14

    # ── Microstructure features (slots 24-32) ──
    tick_ret = np.divide(close_tf - np.roll(close_tf, 1), np.maximum(np.roll(close_tf, 1), 1e-9))
    X[:, 24] = tick_ret  # tick_return
    hl_ratio = np.divide(high_tf - low_tf, np.maximum(np.roll(close_tf, 1), 1e-9))
    X[:, 25] = hl_ratio
    co_ratio = np.divide(np.abs(close_tf - np.roll(close_tf, 1)), np.maximum(high_tf - low_tf, 1e-9))
    X[:, 26] = co_ratio

    # ── Derived features (slots 18-23) ──
    # Weekday sin/cos
    # Note: timestamps are Unix timestamps
    for i in range(n_tf):
        dt = datetime.fromtimestamp(timestamps[i], tz=UTC)
        weekday = dt.weekday()
        X[i, 18] = math.sin(2 * math.pi * weekday / 7.0)  # Derived_Weekday_Sin
        X[i, 19] = math.cos(2 * math.pi * weekday / 7.0)  # Derived_Weekday_Cos

    # ── TF features (slots 33-34) ──
    X[:, 33] = 0.0  # TF_OU_Theta (placeholder)
    X[:, 34] = 0.5  # TF_Hurst (placeholder)

    # ── NaN safety ──
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

    return X, timestamps


def compute_directional_labels(
    ohlc: np.ndarray,
    timeframe: str = "H1",
    horizon: int = 24,
) -> np.ndarray:
    """Compute DIRECTIONAL labels from OHLC data.

    For each bar, simulate BOTH a LONG and SHORT trade:
      - LONG: entry at open[i+1]+spread, SL=entry-SL_dist, TP=entry+TP_dist
      - SHORT: entry at open[i+1]-spread, SL=entry+SL_dist, TP=entry-TP_dist

    Returns labels:
      +1.0 = LONG wins (LONG TP hit before LONG SL, before SHORT TP)
      -1.0 = SHORT wins (SHORT TP hit before SHORT SL, before LONG TP)
       0.0 = no clear winner (timeout or both lose)
    """
    n = len(ohlc)
    open_p = ohlc["open"]
    high_p = ohlc["high"]
    low_p = ohlc["low"]
    close_p = ohlc["close"]

    # Resample
    if timeframe != "M5":
        tf_minutes = {"M15": 15, "M30": 30, "H1": 60, "H4": 240}.get(timeframe, 60)
        bars_per_tf = tf_minutes // 5
        indices = np.arange(0, n, bars_per_tf, dtype=int)
        open_tf = open_p[indices]
        high_tf = high_p[indices]
        low_tf = low_p[indices]
        close_tf = close_p[indices]
    else:
        open_tf = open_p
        high_tf = high_p
        low_tf = low_p
        close_tf = close_p

    n_tf = len(open_tf)
    atr = np.zeros(n_tf)
    for i in range(14, n_tf):
        tr = np.maximum(
            high_tf[i - 14 : i] - low_tf[i - 14 : i],
            np.abs(high_tf[i - 14 : i] - np.roll(close_tf[i - 14 : i], 1)),
        )
        atr[i] = np.mean(tr)

    labels = np.zeros(n_tf, dtype=np.float32)

    for i in range(n_tf - horizon - 1):
        if atr[i] <= 0:
            continue

        # Entry price = open of next bar
        entry = open_tf[i + 1]
        sl_dist = SL_ATR_MULT * atr[i] + SPREAD_POINTS
        tp_dist = TP_ATR_MULT * atr[i]

        # Simulate LONG
        sl_long = entry - sl_dist
        tp_long = entry + tp_dist
        long_win_bar = -1

        # Simulate SHORT
        sl_short = entry + sl_dist
        tp_short = entry - tp_dist
        short_win_bar = -1

        for j in range(i + 1, min(i + horizon + 1, n_tf)):
            cur_h = high_tf[j]
            cur_l = low_tf[j]

            # LONG check
            if long_win_bar < 0:
                if cur_l <= sl_long:
                    long_win_bar = j  # SL hit (LONG lost)
                elif cur_h >= tp_long:
                    long_win_bar = j  # TP hit (LONG won)

            # SHORT check
            if short_win_bar < 0:
                if cur_h >= sl_short:
                    short_win_bar = j  # SL hit (SHORT lost)
                elif cur_l <= tp_short:
                    short_win_bar = j  # TP hit (SHORT won)

            if long_win_bar >= 0 and short_win_bar >= 0:
                break

        # Determine directional label
        long_won = long_win_bar >= 0 and high_tf[long_win_bar] >= tp_long
        short_won = short_win_bar >= 0 and low_tf[short_win_bar] <= tp_short

        if long_won and not short_won:
            labels[i] = 1.0  # LONG wins
        elif short_won and not long_won:
            labels[i] = -1.0  # SHORT wins
        elif long_won and short_won:
            # Both won — use the one that won FIRST
            if long_win_bar < short_win_bar:
                labels[i] = 1.0
            else:
                labels[i] = -1.0
        # else: both lost or timeout → label stays 0.0

    return labels


def main():
    parser = argparse.ArgumentParser(description="BTC Directional Brain Training")
    parser.add_argument("--timeframe", default="H1", choices=["H1", "M15", "M30", "H4"])
    parser.add_argument("--horizon", type=int, default=24)
    parser.add_argument("--data", default="data/training/btc_m5_8year.csv")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    tf = args.timeframe
    output_dir = args.output_dir or f"data/training/btc_directional_{tf.lower()}"
    os.makedirs(output_dir, exist_ok=True)

    print(f"=== BTC Directional Brain Training: {tf} ===")
    print(f"    SL={SL_ATR_MULT}×ATR  TP={TP_ATR_MULT}×ATR  horizon={args.horizon}")

    # ── Load data ──
    print("[1/5] Loading BTC OHLC data...")
    ohlc = load_btc_ohlc(args.data)
    print(f"      {len(ohlc)} M5 bars loaded")

    # ── Compute features ──
    print("[2/5] Computing 37-dim features...")
    X, timestamps = compute_features(ohlc, timeframe=tf)

    # ── Compute directional labels ──
    print("[3/5] Computing DIRECTIONAL labels (+1=LONG, -1=SHORT, 0=neutral)...")
    labels = compute_directional_labels(ohlc, timeframe=tf, horizon=args.horizon)

    # ── Filter valid samples ──
    valid = np.where(labels != 0.0)[0]
    X_filtered = X[valid]
    y_filtered = labels[valid]
    ts_filtered = timestamps[valid]

    n_pos = int(np.sum(y_filtered > 0))
    n_neg = int(np.sum(y_filtered < 0))
    print(f"      Total valid samples: {len(valid)}")
    print(f"      LONG (+1): {n_pos} ({n_pos / len(valid) * 100:.1f}%)")
    print(f"      SHORT (-1): {n_neg} ({n_neg / len(valid) * 100:.1f}%)")

    # ── Time-based split (80/20, chronologically) ──
    split_idx = int(len(X_filtered) * 0.8)
    X_train, X_val = X_filtered[:split_idx], X_filtered[split_idx:]
    y_train, y_val = y_filtered[:split_idx], y_filtered[split_idx:]

    print(f"[4/5] Training: {len(X_train)} samples, Validation: {len(X_val)} samples")

    # ── Train LightGBM regressor ──
    print("[5/5] Training LightGBM regression model...")
    try:
        import lightgbm as lgb
    except ImportError:
        print("[SKIP] LightGBM not installed")
        lgb = None

    if lgb is not None:
        train_data = lgb.Dataset(X_train, label=y_train)
        val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

        params = {
            "objective": "regression",
            "metric": "rmse",
            "boosting_type": "gbdt",
            "num_leaves": 31,
            "learning_rate": 0.02,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "verbose": -1,
            "seed": 42,
        }

        model = lgb.train(
            params,
            train_data,
            valid_sets=[val_data],
            num_boost_round=500,
            callbacks=[lgb.early_stopping(50), lgb.log_evaluation(50)],
        )

        # Evaluate
        y_pred = model.predict(X_val)
        # Directional accuracy: sign(pred) == sign(actual)
        dir_correct: int = np.sum(np.sign(y_pred) == np.sign(y_val))
        dir_acc: float = dir_correct / len(y_val)
        # Binary: LONG vs SHORT accuracy
        long_correct: int = np.sum((y_pred > 0) & (y_val > 0))
        short_correct: int = np.sum((y_pred < 0) & (y_val < 0))
        long_total: int = np.sum(y_val > 0)
        short_total: int = np.sum(y_val < 0)

        print(f"      Directional accuracy: {dir_acc:.4f}")
        print(f"      LONG recall: {long_correct}/{long_total} ({long_correct / max(long_total, 1):.4f})")
        print(f"      SHORT recall: {short_correct}/{short_total} ({short_correct / max(short_total, 1):.4f})")

        # Check prediction balance
        pred_long: int = np.sum(y_pred > 0.1)
        pred_short: int = np.sum(y_pred < -0.1)
        pred_neutral = len(y_pred) - pred_long - pred_short
        print(f"      Predicted LONG: {pred_long} SHORT: {pred_short} NEUTRAL: {pred_neutral}")

        # Save model
        model_path = os.path.join(output_dir, "lightgbm_regressor.txt")
        model.save_model(model_path)
        print(f"      Model saved: {model_path}")

    # ── Save dataset ──
    dataset_path = os.path.join(output_dir, "train.npz")
    np.savez_compressed(
        dataset_path,
        X=X_filtered,
        y=y_filtered,
        timestamps=ts_filtered,
    )
    print(f"      Dataset saved: {dataset_path}")

    # ── Summary ──
    summary = {
        "schema_version": "btc_directional_training.v1",
        "timeframe": tf,
        "horizon": args.horizon,
        "sl_atr_mult": SL_ATR_MULT,
        "tp_atr_mult": TP_ATR_MULT,
        "n_samples": int(len(valid)),
        "n_long": n_pos,
        "n_short": n_neg,
        "directional_acc": float(dir_acc) if lgb else 0.0,
        "trained_at": datetime.now(UTC).isoformat(),
    }
    with open(os.path.join(output_dir, "training_summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n=== Done: {tf} ===")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
