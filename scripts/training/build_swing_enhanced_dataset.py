#!/usr/bin/env python
"""Build enhanced swing datasets: swing_24 macro + V9 micro features + OU/Hurst.

Combines:
  - swing_24 features (D1/H4 macro, cross-market, calendar/momentum)
  - 9 microstructure features (tick_return, hl_ratio, co_ratio, avg_spread,
    OIM, tick_velocity, XAGUSDc_return, EURUSDc_return, USDJPYc_return)
  - Trading-TF OU Theta + Hurst exponent

Labels: symmetric SL=TP=1.5xATR barrier, horizon matched to strategy TF.

Usage:
  python scripts/training/build_swing_enhanced_dataset.py \
    --tf M30 --horizon 12 --output-dir data/training/swing_m30_enhanced

  python scripts/training/build_swing_enhanced_dataset.py \
    --tf M15 --horizon 24 --output-dir data/training/swing_m15_enhanced
"""

from __future__ import annotations

import argparse
import csv
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.features.computers.daily_computer import DailyFeatureComputer

# ── Constants ─────────────────────────────────────────────────────────────────

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
        ATR_PERIOD, RSI_PERIOD, MACD_SLOW + MACD_SIGNAL, VOL_ZS_LOOKBACK, OU_LOOKBACK, HURST_MAX_LAG
    )
    + 2
)

# Number of M5 bars per higher timeframe (for micro aggregation)
M5_PER_TF: dict[str, int] = {"M5": 1, "M15": 3, "M30": 6}

# Feature order (must match training schema)
SWING_MACRO_FEATURES = [
    "D1_Ret_1",
    "D1_Body_Ratio",
    "D1_ATR_14",
    "D1_RSI_14",
    "D1_MACD",
    "D1_Vol_ZScore",
    "D1_Bollinger_Width",
    "D1_ADX_14",
    "H4_Trend_Strength",
    "H4_ATR_Ratio",
    "H4_RSI_Divergence",
    "H4_vs_D1_Alignment",
    "Cross_Gold_Silver_Ratio",
    "Cross_DXY_Return",
    "Cross_EURUSD_Return",
    "Cross_Risk_On_Off",
    "Derived_Weekday_Sin",
    "Derived_Weekday_Cos",
    "Derived_Days_To_MonthEnd",
    "Derived_Is_MonthEnd_Week",
    "Derived_Weekend_Gap",
    "Derived_Vol_Regime",
    "Derived_Momentum_5D",
    "Derived_Momentum_20D",
]

MICRO_FEATURES = [
    "tick_return",
    "hl_ratio",
    "co_ratio",
    "avg_spread",
    "OIM",
    "tick_velocity",
    "XAGUSDc_return",
    "EURUSDc_return",
    "USDJPYc_return",
]

TF_SPECIFIC_FEATURES = ["TF_OU_Theta", "TF_Hurst"]

ALL_FEATURE_NAMES = SWING_MACRO_FEATURES + MICRO_FEATURES + TF_SPECIFIC_FEATURES
N_FEATURES = len(ALL_FEATURE_NAMES)  # 35


# ── Feature computation functions ─────────────────────────────────────────────


def _atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = ATR_PERIOD) -> float:
    if len(c) < period + 1:
        return 0.0
    prev_c = c[-(period + 1) : -1]
    cur_h = h[-period:]
    cur_l = l[-period:]
    tr = np.maximum(cur_h - cur_l, np.maximum(np.abs(cur_h - prev_c), np.abs(cur_l - prev_c)))
    return float(np.mean(tr))


def _ou_theta(price: np.ndarray, lookback: int = OU_LOOKBACK) -> float:
    if len(price) < lookback + 1:
        return 0.0
    window = price[-lookback:]
    y = window[1:]
    x = window[:-1]
    x_mean = float(np.mean(x))
    y_mean = float(np.mean(y))
    beta_num = float(np.sum((x - x_mean) * (y - y_mean)))
    beta_den = float(np.sum((x - x_mean) ** 2))
    if beta_den == 0:
        return 0.0
    beta = np.clip(beta_num / beta_den, 1e-8, 0.99999999)
    return float(-math.log(beta))


def _hurst(price: np.ndarray, max_lag: int = HURST_MAX_LAG) -> float:
    if len(price) < max_lag + 1:
        return 0.5
    series = np.asarray(price[-max_lag:], dtype=np.float64)
    s = float(np.std(series))
    if s == 0:
        return 0.5
    mean_v = float(np.mean(series))
    z = np.cumsum(series - mean_v)
    r = float(np.max(z) - np.min(z))
    rs = r / s
    return float(math.log(rs) / math.log(max_lag)) if max_lag > 1 else 0.5


# ── Data loading ──────────────────────────────────────────────────────────────


def load_ohlc_csv(csv_path: Path) -> dict[str, Any]:
    """Load OHLC CSV into numpy arrays."""
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    opens, highs, lows, closes, volumes, spreads = [], [], [], [], [], []
    timestamps: list[pd.Timestamp] = []

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
                ts = pd.Timestamp(row[0])
                if ts.tzinfo is not None:
                    ts = ts.tz_convert(None)
                o = float(row[1])
                h = float(row[2])
                l = float(row[3])
                c = float(row[4])
                v = float(row[5]) if len(row) > 5 else 0.0
                s = float(row[6]) if len(row) > 6 else 0.0
            except (ValueError, IndexError):
                continue
            if h < l or c <= 0:
                continue
            opens.append(o)
            highs.append(h)
            lows.append(l)
            closes.append(c)
            volumes.append(v)
            spreads.append(s)
            timestamps.append(ts)

    return {
        "open": np.array(opens, dtype=np.float64),
        "high": np.array(highs, dtype=np.float64),
        "low": np.array(lows, dtype=np.float64),
        "close": np.array(closes, dtype=np.float64),
        "volume": np.array(volumes, dtype=np.float64),
        "spread": np.array(spreads, dtype=np.float64),
        "timestamp": timestamps,
        "n_bars": len(closes),
    }


# ── Micro feature computation (single M5 snapshot — matches inference) ─────


def compute_micro_features_at_bar(
    ohlc_m5: dict[str, np.ndarray],
    cross_data: dict[str, np.ndarray] | None,
    bar_idx: int,
) -> dict[str, float]:
    """Compute 9 micro features from a single M5 bar snapshot (matches inference)."""
    result: dict[str, float] = {}
    m5_sl = slice(bar_idx, bar_idx + 1)

    m5_c = ohlc_m5["close"][m5_sl]
    m5_h = ohlc_m5["high"][m5_sl]
    m5_l = ohlc_m5["low"][m5_sl]
    m5_o = ohlc_m5["open"][m5_sl]
    m5_v = ohlc_m5["volume"][m5_sl]
    m5_s = ohlc_m5["spread"][m5_sl]

    # tick_return: single bar has no return (0.0)
    result["tick_return"] = 0.0

    # hl_ratio: per-bar high-low range / close
    result["hl_ratio"] = float(np.mean((m5_h - m5_l) / np.clip(m5_c, 1e-9, None)))

    # co_ratio: close/open
    result["co_ratio"] = float(np.mean(m5_c / np.clip(m5_o, 1e-9, None)))

    # avg_spread: spread / close
    result["avg_spread"] = float(np.mean(m5_s / np.clip(m5_c, 1e-9, None)))

    # OIM: order imbalance metric
    hl_diff = m5_h - m5_l
    oim_vals = np.where(hl_diff > 1e-12, (m5_c - m5_o) / hl_diff, 0.0)
    result["OIM"] = float(np.mean(oim_vals))

    # tick_velocity: per-bar volume (÷1000 for scaling)
    result["tick_velocity"] = float(np.sum(m5_v)) / 1000.0

    # Cross-symbol returns (single bar — use per-bar return from prev bar)
    if cross_data is not None:
        for sym_key, feat_key in [
            ("xag_close", "XAGUSDc_return"),
            ("eur_close", "EURUSDc_return"),
            ("jpy_close", "USDJPYc_return"),
        ]:
            sym_close = cross_data.get(sym_key)
            if sym_close is not None and bar_idx > 0 and bar_idx < len(sym_close):
                sym_sl = slice(max(0, bar_idx - 1), bar_idx + 1)
                sym_c = sym_close[sym_sl]
                if len(sym_c) >= 2 and sym_c[0] > 0:
                    result[feat_key] = float((sym_c[-1] - sym_c[0]) / sym_c[0] * 100.0)
                else:
                    result[feat_key] = 0.0
            else:
                result[feat_key] = 0.0
    else:
        for feat_key in ["XAGUSDc_return", "EURUSDc_return", "USDJPYc_return"]:
            result[feat_key] = 0.0

    return result


# ── Label computation ─────────────────────────────────────────────────────────


def compute_barrier_labels(
    ohlc: dict[str, np.ndarray],
    atr_mult: float = 1.5,
    horizon: int = 12,
    *,
    sl_atr_mult: float | None = None,
    tp_atr_mult: float | None = None,
) -> np.ndarray:
    """Compute barrier labels: -1=SL, 0=timeout, 1=TP.

    For each bar, look ahead `horizon` bars. If price hits SL first → -1,
    TP first → 1, neither → 0.

    When sl_atr_mult/tp_atr_mult are provided, they override atr_mult
    for asymmetric SL/TP (dual-track label generation).
    """
    _sl_mult = sl_atr_mult if sl_atr_mult is not None else atr_mult
    _tp_mult = tp_atr_mult if tp_atr_mult is not None else atr_mult
    n = ohlc["n_bars"]
    close = ohlc["close"]
    high = ohlc["high"]
    low = ohlc["low"]
    labels = np.zeros(n, dtype=np.int32)

    for i in range(n - horizon - 1):
        entry = close[i]
        atr_i = _atr(
            high[max(0, i - ATR_PERIOD) : i + 1],
            low[max(0, i - ATR_PERIOD) : i + 1],
            close[max(0, i - ATR_PERIOD) : i + 1],
        )
        if atr_i <= 0:
            continue

        sl_dist = _sl_mult * atr_i
        tp_dist = _tp_mult * atr_i
        sl_level = entry - sl_dist
        tp_level = entry + tp_dist

        for j in range(1, horizon + 1):
            idx = i + j
            if idx >= n:
                break
            if low[idx] <= sl_level:
                labels[i] = -1  # SL hit (SHORT would win)
                break
            if high[idx] >= tp_level:
                labels[i] = 1  # TP hit (LONG would win)
                break
        # else: stays 0 (timeout)

    return labels


# ── Main build function ───────────────────────────────────────────────────────


def build_swing_dataset(
    tf: str,
    horizon: int,
    output_dir: Path,
    val_ratio: float = 0.2,
    test_ratio: float = 0.15,
    raw_dir: Path | None = None,
    *,
    sl_atr_mult: float | None = None,
    tp_atr_mult: float | None = None,
) -> dict[str, Any]:
    """Build enhanced swing dataset.

    Args:
        tf: Target timeframe ("M15" or "M30").
        horizon: Label barrier horizon in bars.
        output_dir: Where to save NPZ files.
        val_ratio: Fraction for validation.
        test_ratio: Fraction for test.
        raw_dir: Directory containing raw CSV data.
    """
    _data_dir = raw_dir or Path("data/raw")
    n_m5 = M5_PER_TF[tf]

    # ── Load target-TF XAUUSD data ──
    tf_csv = _data_dir / f"xauusdc_{tf.lower()}_merged.csv"
    print(f"[swing:{tf}] Loading {tf_csv}...")
    tf_ohlc = load_ohlc_csv(tf_csv)
    print(f"  {tf_ohlc['n_bars']} bars loaded")

    # ── Load M5 XAUUSD for micro features ──
    m5_csv = _data_dir / "xauusdc_m5_merged.csv"
    m5_ohlc = load_ohlc_csv(m5_csv)
    print(f"  M5: {m5_ohlc['n_bars']} bars loaded")

    # ── Load cross-symbol data ──
    cross_data: dict[str, tuple[np.ndarray, list[pd.Timestamp]]] = {}
    for sym_name, csv_name in [
        ("silver", "xagusdc_m5_merged.csv"),
        ("eur", "eurusdc_m5_merged.csv"),
        ("dxy", "usdjpyc_m5_merged.csv"),  # USDJPY for micro features
    ]:
        sym_path = _data_dir / csv_name
        if sym_path.exists():
            sym_ohlc = load_ohlc_csv(sym_path)
            cross_data[sym_name] = (sym_ohlc["close"], sym_ohlc["timestamp"])
            print(f"  {sym_name}: {sym_ohlc['n_bars']} bars loaded")

    # ── Initialize DailyFeatureComputer for SSOT 24-dim macro features ──
    d1_csv_path = _data_dir / "xauusdc_d1_merged.csv"
    h4_csv_path = _data_dir / "xauusdc_h4_merged.csv"
    cross_asset_paths: dict[str, str | Path] = {}
    for name, csv_name in [
        ("XAGUSDc", "xagusdc_d1_merged.csv"),
        ("EURUSDc", "eurusdc_d1_merged.csv"),
    ]:
        p = _data_dir / csv_name
        if p.exists():
            cross_asset_paths[name] = p
    daily_computer: DailyFeatureComputer | None = None
    d1_min_lookback = 0
    if d1_csv_path.exists():
        try:
            daily_computer = DailyFeatureComputer(
                d1_csv=str(d1_csv_path),
                h4_csv=str(h4_csv_path) if h4_csv_path.exists() else None,
                cross_assets=cross_asset_paths if cross_asset_paths else None,
            )
            from core.features.computers.daily_computer import MIN_LOOKBACK as _D1_MIN_LOOK

            d1_min_lookback = _D1_MIN_LOOK
            print(f"  DailyFeatureComputer initialized: {daily_computer._n} D1 bars")
        except Exception as exc:
            print(f"  [WARN] DailyFeatureComputer init failed: {exc}")

    # ── Align M5 micro features to target-TF bars ──
    # For each target-TF bar, find the aggregated micro features
    # Map TF bar timestamps to nearest M5 indices
    tf_ts_arr = np.array([ts.timestamp() for ts in tf_ohlc["timestamp"]], dtype=np.float64)
    m5_ts_arr = np.array([ts.timestamp() for ts in m5_ohlc["timestamp"]], dtype=np.float64)

    # ── Compute labels ──
    _sl = sl_atr_mult if sl_atr_mult is not None else 1.5
    _tp = tp_atr_mult if tp_atr_mult is not None else 1.5
    print(f"  Computing barrier labels (horizon={horizon}, SL={_sl}xATR, TP={_tp}xATR)...")
    labels = compute_barrier_labels(
        tf_ohlc, atr_mult=1.5, horizon=horizon, sl_atr_mult=sl_atr_mult, tp_atr_mult=tp_atr_mult
    )

    # ── Build feature matrix ──
    n_bars = tf_ohlc["n_bars"]
    # Need enough history for features
    start_idx = max(100, n_m5 * 2)  # skip first bars for feature stability
    valid_count = 0

    X = np.zeros((n_bars - start_idx, N_FEATURES), dtype=np.float32)
    y = np.zeros(n_bars - start_idx, dtype=np.int32)
    bar_indices = np.zeros(n_bars - start_idx, dtype=np.int32)

    # Prepare cross-data for micro features
    micro_cross: dict[str, np.ndarray] = {}
    for sym_name, (sym_close, sym_ts) in cross_data.items():
        # Align to M5 timestamps
        key = f"{'xag' if sym_name == 'silver' else 'eur' if sym_name == 'eur' else 'jpy'}_close"
        # Use searchsorted for alignment
        sym_ts_arr = np.array([ts.timestamp() for ts in sym_ts], dtype=np.float64)
        idxs_raw = np.searchsorted(sym_ts_arr, m5_ts_arr, side="right") - 1
        idxs: np.ndarray = np.asarray(idxs_raw, dtype=np.intp)
        valid_mask = (idxs >= 0) & (idxs < len(sym_close))
        aligned = np.full(len(m5_ts_arr), np.nan, dtype=np.float64)
        aligned[valid_mask] = np.asarray(sym_close)[idxs[valid_mask]]
        micro_cross[key] = aligned

    # ── D1 index tracker (monotonic, both arrays are time-sorted) ──
    d1_idx_tracker: int = d1_min_lookback

    for i in range(start_idx, n_bars):
        bar_ts = tf_ohlc["timestamp"][i]

        # Find corresponding M5 bar index (last M5 bar within this TF bar)
        bar_ts_epoch = bar_ts.timestamp()
        m5_idx = int(np.searchsorted(m5_ts_arr, bar_ts_epoch, side="right") - 1)
        if m5_idx < n_m5:
            continue

        # ── Swing macro features (DailyFeatureComputer = SSOT, matches inference) ──
        if daily_computer is not None:
            bar_dt = bar_ts.to_pydatetime()
            while (
                d1_idx_tracker + 1 < daily_computer._n
                and daily_computer._d1_datetimes[d1_idx_tracker + 1] <= bar_dt
            ):
                d1_idx_tracker += 1
            if d1_idx_tracker >= d1_min_lookback and d1_idx_tracker < daily_computer._n:
                raw_row = daily_computer._gather_row(d1_idx_tracker)
                macro = dict(zip(SWING_MACRO_FEATURES, raw_row, strict=False))
            else:
                macro = dict.fromkeys(SWING_MACRO_FEATURES, 0.0)
        else:
            macro = dict.fromkeys(SWING_MACRO_FEATURES, 0.0)

        # ── Micro features (single M5 snapshot, matches inference) ──
        micro = compute_micro_features_at_bar(m5_ohlc, micro_cross, m5_idx)

        # ── TF-specific features (M5 close prices, matching inference _tf_close_buffer) ──
        m5_close_window = m5_ohlc["close"][max(0, m5_idx - OU_LOOKBACK) : m5_idx + 1]
        tf_ou = _ou_theta(m5_close_window, lookback=min(OU_LOOKBACK, len(m5_close_window)))
        tf_hurst = _hurst(m5_close_window, max_lag=min(HURST_MAX_LAG, len(m5_close_window)))

        # ── Assemble feature vector ──
        row_idx = valid_count
        for j, name in enumerate(SWING_MACRO_FEATURES):
            X[row_idx, j] = float(macro.get(name, 0.0))
        for j, name in enumerate(MICRO_FEATURES):
            X[row_idx, len(SWING_MACRO_FEATURES) + j] = float(micro.get(name, 0.0))
        X[row_idx, len(SWING_MACRO_FEATURES) + len(MICRO_FEATURES)] = tf_ou
        X[row_idx, len(SWING_MACRO_FEATURES) + len(MICRO_FEATURES) + 1] = tf_hurst

        # Handle NaN/inf
        X[row_idx] = np.nan_to_num(X[row_idx], nan=0.0, posinf=0.0, neginf=0.0)

        y[row_idx] = labels[i]
        bar_indices[row_idx] = i
        valid_count += 1

    # Trim to valid
    X = X[:valid_count]
    y = y[:valid_count]
    bar_indices = bar_indices[:valid_count]

    print(f"  {valid_count} feature rows built from {n_bars} bars")
    unique, counts = np.unique(y, return_counts=True)
    for u, c in zip(unique, counts, strict=False):
        print(f"    Label {u}: {c} ({100*c/valid_count:.1f}%)")

    # ── Purged chronological split (FIX-20260529-029) ──
    # Labels look ahead `horizon` bars. Without purge, the last training
    # sample's label window overlaps the first `horizon` validation bars,
    # causing label leakage and inflated val/test metrics.
    purge_bars = horizon

    n_val_init = int(valid_count * val_ratio)
    n_test_init = int(valid_count * test_ratio)
    n_train_init = valid_count - n_val_init - n_test_init

    train_end = max(0, n_train_init - purge_bars)
    val_start = n_train_init
    val_end = n_train_init + max(0, n_val_init - purge_bars)
    test_start = n_train_init + n_val_init

    purged_train = n_train_init - train_end
    purged_val = n_val_init - (val_end - val_start) if val_end > val_start else n_val_init

    X_train = X[:train_end]
    y_train = y[:train_end]
    X_val = X[val_start:val_end]
    y_val = y[val_start:val_end]
    X_test = X[test_start:]
    y_test = y[test_start:]

    n_train = len(y_train)
    n_val = len(y_val)
    n_test = len(y_test)

    print(f"  Purge zone: {purge_bars} bars (horizon={horizon})")
    print(f"  Purged: {purged_train} train + {purged_val} val samples")

    # Dummy PnL (synthetic: 1.5 for TP, -1.5 for SL, 0 for timeout)
    pnl_r = np.where(y == 1, 1.5, np.where(y == -1, -1.5, 0.0)).astype(np.float32)
    pnl_r_train = pnl_r[:train_end]
    pnl_r_val = pnl_r[val_start:val_end]
    pnl_r_test = pnl_r[test_start:]
    y_train_shifted = y_train + 1  # [-1,0,1] → [0,1,2] for multi-class
    y_val_shifted = y_val + 1
    y_test_shifted = y_test + 1

    # ── Export ──
    output_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        output_dir / "train.npz",
        X=X_train,
        y=y_train_shifted,
        pnl_r=pnl_r_train,
        X_val=X_val,
        y_val=y_val_shifted,
        pnl_r_val=pnl_r_val,
        X_test=X_test,
        y_test=y_test_shifted,
        pnl_r_test=pnl_r_test,
        feature_names=np.array(ALL_FEATURE_NAMES, dtype=str),
    )

    # Save metadata
    import json

    meta = {
        "tf": tf,
        "horizon": horizon,
        "n_features": N_FEATURES,
        "feature_names": ALL_FEATURE_NAMES,
        "n_train": n_train,
        "n_val": n_val,
        "n_test": n_test,
        "purge_bars": purge_bars,
        "n_train_init": n_train_init,
        "n_val_init": n_val_init,
        "n_test_init": n_test_init,
        "sl_atr_mult": _sl,
        "tp_atr_mult": _tp,
        "label_dist": {str(k): int(v) for k, v in zip(unique, counts, strict=False)},
        "built_at": datetime.now().isoformat(),
    }
    with open(output_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print(f"  Saved to {output_dir}/")
    print(f"    train: {n_train} samples (purged {purged_train})")
    print(f"    val:   {n_val} samples (purged {purged_val})")
    print(f"    test:  {n_test} samples")

    return meta


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Build enhanced swing training dataset")
    parser.add_argument(
        "--tf", default="M30", choices=["M5", "M15", "M30"], help="Target timeframe (default: M30)"
    )
    parser.add_argument(
        "--horizon",
        type=int,
        default=12,
        help="Barrier horizon in bars (default: 12 for M30, 24 for M15)",
    )
    parser.add_argument(
        "--output-dir",
        default="data/training/swing_enhanced",
        help="Output directory for NPZ files",
    )
    parser.add_argument("--raw-dir", default="data/raw", help="Directory containing raw CSV data")
    parser.add_argument(
        "--label-contract",
        default=None,
        help="Path to Label Contract JSON for asymmetric SL/TP (dual-track training)",
    )
    parser.add_argument(
        "--sl-atr-mult",
        type=float,
        default=None,
        help="SL ATR multiplier override (requires --tp-atr-mult)",
    )
    parser.add_argument(
        "--tp-atr-mult",
        type=float,
        default=None,
        help="TP ATR multiplier override (requires --sl-atr-mult)",
    )
    args = parser.parse_args()

    # Resolve asymmetric SL/TP from label contract or CLI flags
    _sl_mult: float | None = args.sl_atr_mult
    _tp_mult: float | None = args.tp_atr_mult
    if args.label_contract:
        import json as _json
        with open(args.label_contract) as _f:
            _lc = _json.load(_f)
        barriers = _lc.get("barriers", {})
        _sl_mult = _sl_mult or barriers.get("sl_atr_mult")
        _tp_mult = _tp_mult or barriers.get("tp_atr_mult")
        if _sl_mult and _tp_mult:
            print(
                f"[label_contract] {_lc['contract_id']}: "
                f"sl_atr={_sl_mult}, tp_atr={_tp_mult}"
            )

    # Auto-adjust horizon defaults
    if args.horizon == 12 and args.tf == "M15":
        args.horizon = 24  # M15 needs more bars for same real-time horizon (~6h)
        print(f"[auto] M15 horizon adjusted to {args.horizon}")

    output = Path(args.output_dir)
    if output.name == "swing_enhanced":
        _suffix = ""
        if _sl_mult and _tp_mult:
            _suffix = f"_sl{_sl_mult}_tp{_tp_mult}".replace(".", "p")
        output = Path(f"data/training/swing_{args.tf.lower()}_enhanced{_suffix}")

    build_swing_dataset(
        tf=args.tf,
        horizon=args.horizon,
        output_dir=output,
        sl_atr_mult=_sl_mult,
        tp_atr_mult=_tp_mult,
        raw_dir=Path(args.raw_dir),
    )


if __name__ == "__main__":
    main()
