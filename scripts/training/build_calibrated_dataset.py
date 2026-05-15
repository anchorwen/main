#!/usr/bin/env python
"""Build training dataset from calibrated labels + historically-computed features.

Computes v9_institutional_40 features directly from OHLC CSV data at each
label's entry bar. This bypasses the live feature store (which only contains
recent data) and enables training across the full historical label period.

Features are computed identically to V9LiveFeatureComputer so the training
distribution matches the live inference distribution.

Usage:
  python scripts/training/build_calibrated_dataset.py \
    --labels data/labels/calibrated_labels_180d.jsonl \
    --price-data data/raw/xauusdc_m5_180d.csv \
    --output-dir data/training/calibrated_v1
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np

# ── Feature computation constants (must match v9_live_computer.py) ──────────
ATR_PERIOD = 14
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
VOL_ZS_LOOKBACK = 20
OU_LOOKBACK = 20
HURST_MAX_LAG = 20

MIN_BARS = (
    max(
        ATR_PERIOD,
        RSI_PERIOD,
        MACD_SLOW + MACD_SIGNAL,
        VOL_ZS_LOOKBACK,
        OU_LOOKBACK,
        HURST_MAX_LAG,
    )
    + 2
)  # 37 M5 bars

# Multi-timeframe bar counts for resampling
TF_BAR_MULTS = {"M5": 1, "M15": 3, "M30": 6, "H1": 12}
TF_LABELS = ["M5", "M15", "M30", "H1"]

# Lookback in M5 bars: enough to resample H1 (MIN_BARS=37 × 12 = 444, use 480 for safety margin)
LOOKBACK_M5 = 480


# ── Feature computation (identical to v9_live_computer.py) ─────────────────


def _returns(c: np.ndarray) -> float:
    return (c[-1] - c[-2]) / c[-2] * 100.0 if len(c) >= 2 else 0.0


def _body_ratio(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray) -> float:
    denom = h[-1] - l[-1]
    if denom == 0:
        denom = 1e-8
    return float(np.clip((c[-1] - o[-1]) / denom, -1.0, 1.0))


def _atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = ATR_PERIOD) -> float:
    if len(c) < period + 1:
        return 0.0
    prev_c = c[-(period + 1) : -1]
    cur_h = h[-period:]
    cur_l = l[-period:]
    tr = np.maximum(cur_h - cur_l, np.maximum(np.abs(cur_h - prev_c), np.abs(cur_l - prev_c)))
    return float(np.mean(tr))


def _rsi(c: np.ndarray, period: int = RSI_PERIOD) -> float:
    if len(c) < period + 1:
        return 50.0
    deltas = np.diff(c[-(period + 1) :])
    gain = np.mean(np.maximum(deltas, 0))
    loss = np.mean(np.abs(np.minimum(deltas, 0)))
    if loss == 0:
        return 100.0
    rs = gain / loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _ema(data: np.ndarray, period: int) -> float:
    if len(data) < period:
        return float(np.mean(data))
    alpha = 2.0 / (period + 1.0)
    result = np.mean(data[:period])
    for val in data[period:]:
        result = alpha * val + (1 - alpha) * result
    return float(result)


def _macd(c: np.ndarray) -> float:
    need = MACD_SLOW + MACD_SIGNAL
    if len(c) < need:
        return 0.0
    ema12 = _ema(c, MACD_FAST)
    ema26 = _ema(c, MACD_SLOW)
    return float(ema12 - ema26)


def _vol_zscore(volume: np.ndarray, lookback: int = VOL_ZS_LOOKBACK) -> float:
    if len(volume) < lookback + 1:
        return 0.0
    window = volume[-lookback:]
    mean = np.mean(window)
    std = np.std(window)
    if std == 0:
        return 0.0
    return float((volume[-1] - mean) / std)


def _ou_theta(price: np.ndarray, lookback: int = OU_LOOKBACK) -> float:
    if len(price) < lookback + 1:
        return 0.0
    window = price[-lookback:]
    y = window[1:]
    x = window[:-1]
    x_mean = np.mean(x)
    y_mean = np.mean(y)
    beta_num: float = float(np.sum((x - x_mean) * (y - y_mean)))
    beta_den: float = float(np.sum((x - x_mean) ** 2))
    if beta_den == 0:
        return 0.0
    beta = beta_num / beta_den
    beta = np.clip(beta, 1e-8, 0.99999999)
    return float(-math.log(beta))


def _hurst(price: np.ndarray, max_lag: int = HURST_MAX_LAG) -> float:
    if len(price) < max_lag + 1:
        return 0.5
    series = np.asarray(price[-max_lag:], dtype=np.float64)
    mean = np.mean(series)
    deviations = series - mean
    z = np.cumsum(deviations)
    r = float(np.max(z) - np.min(z))
    s = float(np.std(series))
    if s == 0:
        return 0.5
    rs = r / s
    return float(math.log(rs) / math.log(max_lag)) if max_lag > 1 else 0.5


def _macro1_corr(price: np.ndarray, lookback: int = 20) -> float:
    if len(price) < lookback + 1:
        return 0.0
    window = price[-lookback:]
    ret = np.diff(window)
    if len(ret) < 2:
        return 0.0
    return float(np.corrcoef(ret[:-1], ret[1:])[0, 1])


def _macro_gold_silver_spread(price: np.ndarray, lookback: int = 20) -> float:
    if len(price) < lookback:
        return 0.0
    window = price[-lookback:]
    ma = np.mean(window)
    std = np.std(window)
    if std == 0:
        return 0.0
    return float((price[-1] - ma) / std)


# ── Multi-timeframe resampling ─────────────────────────────────────────────


def _resample_to_tf(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    v: np.ndarray,
    n_m5_per_tf: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Resample M5 OHLCV bars to a higher timeframe.

    Returns arrays where each element is the aggregated bar for one higher-TF period.
    Bars are aligned to the end of the M5 data (last N bars grouped into k TF bars).
    """
    n_bars = len(o)
    n_tf = n_bars // n_m5_per_tf
    if n_tf == 0:
        return np.array([]), np.array([]), np.array([]), np.array([]), np.array([])

    # Trim to exact multiple and reshape
    trim = n_bars - n_tf * n_m5_per_tf
    o_trim = o[trim:]
    h_trim = h[trim:]
    l_trim = l[trim:]
    c_trim = c[trim:]
    v_trim = v[trim:]

    o_rs = o_trim.reshape(n_tf, n_m5_per_tf)[:, 0]
    h_rs = h_trim.reshape(n_tf, n_m5_per_tf).max(axis=1)
    l_rs = l_trim.reshape(n_tf, n_m5_per_tf).min(axis=1)
    c_rs = c_trim.reshape(n_tf, n_m5_per_tf)[:, -1]
    v_rs = v_trim.reshape(n_tf, n_m5_per_tf).sum(axis=1)

    return o_rs, h_rs, l_rs, c_rs, v_rs


def compute_features_at_bar(
    o_m5: np.ndarray,
    h_m5: np.ndarray,
    l_m5: np.ndarray,
    c_m5: np.ndarray,
    v_m5: np.ndarray,
    entry_idx: int,
) -> dict[str, float]:
    """Compute all 40 v9_institutional features at a specific bar index.

    Uses LOOKBACK_M5 bars before entry_idx (inclusive) to ensure enough
    data for all multi-timeframe feature computations.
    """
    start = max(0, entry_idx - LOOKBACK_M5 + 1)
    sl = slice(start, entry_idx + 1)

    o_win = o_m5[sl]
    h_win = h_m5[sl]
    l_win = l_m5[sl]
    c_win = c_m5[sl]
    v_win = v_m5[sl]

    result: dict[str, float] = {}

    for tf_name, mult in TF_BAR_MULTS.items():
        if mult == 1:
            o_tf, h_tf, l_tf, c_tf, v_tf = o_win, h_win, l_win, c_win, v_win
        else:
            o_tf, h_tf, l_tf, c_tf, v_tf = _resample_to_tf(o_win, h_win, l_win, c_win, v_win, mult)

        if len(c_tf) < MIN_BARS:
            for feat in [
                "Ret_1",
                "Body_Ratio",
                "ATR_14",
                "RSI_14",
                "MACD",
                "Vol_ZScore",
                "Macro1_Corr",
                "Macro_Gold_Silver_Spread",
                "OU_Theta",
                "Hurst",
            ]:
                result[f"{tf_name}_{feat}"] = 0.0
            continue

        result[f"{tf_name}_Ret_1"] = _returns(c_tf)
        result[f"{tf_name}_Body_Ratio"] = _body_ratio(o_tf, h_tf, l_tf, c_tf)
        result[f"{tf_name}_ATR_14"] = _atr(h_tf, l_tf, c_tf)
        result[f"{tf_name}_RSI_14"] = _rsi(c_tf)
        result[f"{tf_name}_MACD"] = _macd(c_tf)
        result[f"{tf_name}_Vol_ZScore"] = _vol_zscore(v_tf)
        result[f"{tf_name}_Macro1_Corr"] = _macro1_corr(c_tf)
        result[f"{tf_name}_Macro_Gold_Silver_Spread"] = _macro_gold_silver_spread(c_tf)
        result[f"{tf_name}_OU_Theta"] = _ou_theta(c_tf)
        result[f"{tf_name}_Hurst"] = _hurst(c_tf)

    return result


# ── Data loading ───────────────────────────────────────────────────────────


def load_ohlc_arrays(csv_path: Path) -> dict[str, np.ndarray]:
    """Load OHLC CSV into numpy arrays. Handles MT5 export format."""
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    opens, highs, lows, closes, volumes = [], [], [], [], []
    timestamps: list[str] = []

    with open(csv_path, encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
            has_header = csv.Sniffer().has_header(sample)
        except csv.Error:
            dialect = None
            has_header = True

        if dialect is not None:
            reader = csv.reader(f, dialect)
        else:
            reader = csv.reader(f)
        for i, row in enumerate(reader):
            if has_header and i == 0:
                continue
            if len(row) < 5:
                continue
            try:
                ts = row[0]
                o = float(row[1])
                h = float(row[2])
                l = float(row[3])
                c = float(row[4])
                v = float(row[5]) if len(row) > 5 else 0.0
            except (ValueError, IndexError):
                continue
            if h < l or c <= 0:
                continue

            opens.append(o)
            highs.append(h)
            lows.append(l)
            closes.append(c)
            volumes.append(v)
            timestamps.append(ts)

    return {
        "open": np.array(opens, dtype=np.float64),
        "high": np.array(highs, dtype=np.float64),
        "low": np.array(lows, dtype=np.float64),
        "close": np.array(closes, dtype=np.float64),
        "volume": np.array(volumes, dtype=np.float64),
        "timestamp": timestamps,
        "n_bars": len(closes),
    }


# ── Dataset assembly ───────────────────────────────────────────────────────


def pair_labels_with_computed_features(
    labels_path: Path,
    ohlc: dict[str, np.ndarray],
    *,
    warmup_bars: int = 500,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Join labels to historically-computed feature vectors.

    Returns (X, y, pnl, side, feature_names).
    side: 1=long, 0=short.
    """
    o = ohlc["open"]
    h = ohlc["high"]
    l = ohlc["low"]
    c = ohlc["close"]
    v = ohlc["volume"]
    n_bars = ohlc["n_bars"]

    feature_names = sorted(
        compute_features_at_bar(o, h, l, c, v, min(warmup_bars, n_bars - 1)).keys()
    )

    X_rows: list[list[float]] = []
    y_rows: list[int] = []
    pnl_rows: list[float] = []
    side_rows: list[int] = []
    matched = 0
    skipped_warmup = 0
    unmatched = 0

    with open(labels_path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if (i + 1) % 10000 == 0:
                print(
                    f"  ... {i + 1} labels processed ({matched} matched, "
                    f"{skipped_warmup} warmup, {unmatched} unmatched)"
                )

            line = line.strip()
            if not line:
                continue
            try:
                lab = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_idx = lab.get("entry_idx")
            if entry_idx is None:
                unmatched += 1
                continue

            entry_idx = int(entry_idx)
            if entry_idx >= n_bars or entry_idx < warmup_bars:
                skipped_warmup += 1
                continue

            feat_vec_dict = compute_features_at_bar(o, h, l, c, v, entry_idx)
            feat_vec = [float(feat_vec_dict.get(fn, 0.0)) for fn in feature_names]
            X_rows.append(feat_vec)

            numeric_label = lab.get("label", 0)
            y_rows.append(1 if numeric_label == 1 else 0)

            pnl_rows.append(float(lab.get("pnl_r", 0.0)))
            side_rows.append(1 if lab.get("side") == "long" else 0)
            matched += 1

    X = np.array(X_rows, dtype=np.float64)
    y = np.array(y_rows, dtype=np.int32)
    pnl = np.array(pnl_rows, dtype=np.float64)
    side = np.array(side_rows, dtype=np.int8)

    print(f"  Total: {matched} matched, {skipped_warmup} warmup, {unmatched} unmatched")
    return X, y, pnl, side, feature_names


# ── CLI ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="build_calibrated_dataset")
    p.add_argument(
        "--labels", type=Path, required=True, help="JSONL file of calibrated barrier labels"
    )
    p.add_argument(
        "--price-data",
        type=Path,
        required=True,
        help="OHLC CSV matching the label source (xauusdc_m5_180d.csv)",
    )
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--val-split", type=float, default=0.2)
    p.add_argument(
        "--warmup-bars",
        type=int,
        default=500,
        help="Skip first N bars (need lookback for feature computation)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ── Load OHLC data ─────────────────────────────────────────────────
    print("[1/3] Loading OHLC data...")
    try:
        ohlc = load_ohlc_arrays(args.price_data)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        return 1
    print(f"       {ohlc['n_bars']} bars loaded")

    # ── Compute features + pair with labels ─────────────────────────────
    print("[2/3] Computing features and pairing with labels...")
    X, y, pnl, side, feature_names = pair_labels_with_computed_features(
        args.labels,
        ohlc,
        warmup_bars=args.warmup_bars,
    )

    if len(X) < 100:
        print(f"[ERROR] Only {len(X)} samples matched — insufficient for training")
        return 1

    pos_rate = (y == 1).mean()
    avg_pnl = float(np.mean(pnl[pnl != 0])) if np.any(pnl != 0) else 0.0
    print(f"       Features: {X.shape[1]}, Samples: {X.shape[0]}")
    print(f"       Pos rate: {pos_rate:.1%}, Avg PnL (nonzero): {avg_pnl:.4f}R")

    # ── Split and save ─────────────────────────────────────────────────
    print("[3/3] Splitting and saving...")
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    split_idx = int(len(X) * (1 - args.val_split))
    X_train, X_val = X[:split_idx], X[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]
    pnl_train, pnl_val = pnl[:split_idx], pnl[split_idx:]
    side_train, side_val = side[:split_idx], side[split_idx:]

    np.savez_compressed(
        out_dir / "train.npz",
        X=X_train,
        y=y_train,
        pnl=pnl_train,
        side=side_train,
        feature_names=np.array(feature_names),
        schema="calibrated_barrier_v2",
    )
    np.savez_compressed(
        out_dir / "val.npz",
        X=X_val,
        y=y_val,
        pnl=pnl_val,
        side=side_val,
        feature_names=np.array(feature_names),
        schema="calibrated_barrier_v2",
    )

    train_pos = (y_train == 1).mean()
    val_pos = (y_val == 1).mean()
    train_long_pct = (side_train == 1).mean()
    print(
        f"       Train: {X_train.shape[0]} samples ({train_pos:.1%} pos, {train_long_pct:.1%} long)"
    )
    print(f"       Val:   {X_val.shape[0]} samples ({val_pos:.1%} pos)")
    print(f"       Saved to: {out_dir}")

    meta = {
        "schema_version": "calibrated_dataset.v2",
        "n_samples": int(X.shape[0]),
        "n_features": int(X.shape[1]),
        "feature_names": feature_names,
        "train_samples": int(X_train.shape[0]),
        "val_samples": int(X_val.shape[0]),
        "pos_rate": round(float(pos_rate), 4),
        "avg_pnl_r": round(float(avg_pnl), 4),
        "label_contract": "SL=3.0ATR, TP=1.0ATR, horizon=12",
        "feature_source": "historical_OHLC_computed",
    }
    with open(out_dir / "dataset_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
