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
M5_PER_TF: dict[str, int] = {"M15": 3, "M30": 6}

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


def _returns(c: np.ndarray) -> float:
    return float((c[-1] - c[-2]) / c[-2] * 100.0) if len(c) >= 2 else 0.0


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
    return float(100.0 - 100.0 / (1.0 + gain / loss))


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
    return float(_ema(c, MACD_FAST) - _ema(c, MACD_SLOW))


def _vol_zscore(volume: np.ndarray, lookback: int = VOL_ZS_LOOKBACK) -> float:
    if len(volume) < lookback + 1:
        return 0.0
    window = volume[-lookback:]
    mean_v = float(np.mean(window))
    std_v = float(np.std(window))
    if std_v == 0:
        return 0.0
    return float((volume[-1] - mean_v) / std_v)


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


def _adx(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> float:
    """Simplified ADX: ratio of directional movement to true range."""
    if len(c) < period + 1:
        return 20.0
    tr_arr = np.maximum(
        h[-period:] - l[-period:],
        np.maximum(
            np.abs(h[-period:] - c[-(period + 1) : -1]),
            np.abs(l[-period:] - c[-(period + 1) : -1]),
        ),
    )
    atr_val = float(np.mean(tr_arr))
    if atr_val == 0:
        return 20.0
    up_move = np.maximum(h[-period:] - h[-(period + 1) : -1], 0)
    down_move = np.maximum(l[-(period + 1) : -1] - l[-period:], 0)
    plus_di = float(np.mean(up_move) / atr_val * 100)
    minus_di = float(np.mean(down_move) / atr_val * 100)
    dx = abs(plus_di - minus_di) / max(plus_di + minus_di, 1e-12) * 100
    return float(np.clip(dx, 0, 100))


def _bollinger_width(c: np.ndarray, period: int = 20) -> float:
    if len(c) < period:
        return 0.0
    ma = float(np.mean(c[-period:]))
    std = float(np.std(c[-period:]))
    if ma == 0:
        return 0.0
    return float(std / ma * 100)


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


def load_higher_tf(tf: str, tf_dir: Path) -> dict[str, np.ndarray] | None:
    """Load D1 or H4 OHLC data aligned by timestamp."""
    csv_path = tf_dir / f"xauusdc_{tf.lower()}_merged.csv"
    if not csv_path.exists():
        print(f"  [WARN] {csv_path} not found, higher-TF features will be zero")
        return None
    return load_ohlc_csv(csv_path)


# ── Swing macro feature computation ───────────────────────────────────────────


def _align_higher_tf_value(
    bar_ts: pd.Timestamp, tf_ts: list[pd.Timestamp], tf_values: np.ndarray
) -> float:
    """Get the most recent higher-TF value at or before bar_ts."""
    for i in range(len(tf_ts) - 1, -1, -1):
        if tf_ts[i] <= bar_ts:
            return float(tf_values[i])
    return 0.0


def compute_swing_macro_features(
    bar_idx: int,
    tf_ohlc: dict[str, Any],
    d1_ohlc: dict[str, Any] | None,
    h4_ohlc: dict[str, Any] | None,
    gold_ts: list[pd.Timestamp],
    silver_close: np.ndarray | None,
    silver_ts: list[pd.Timestamp] | None,
    eur_close: np.ndarray | None,
    eur_ts: list[pd.Timestamp] | None,
    dxy_close: np.ndarray | None,
    dxy_ts: list[pd.Timestamp] | None,
) -> dict[str, float]:
    """Compute swing_24 macro features for a single bar."""
    winsize = 60
    start = max(0, bar_idx - winsize + 1)
    sl = slice(start, bar_idx + 1)

    o = tf_ohlc["open"][sl]
    h = tf_ohlc["high"][sl]
    l = tf_ohlc["low"][sl]
    c = tf_ohlc["close"][sl]
    v = tf_ohlc["volume"][sl]
    bar_ts = tf_ohlc["timestamp"][bar_idx]

    result: dict[str, float] = {}

    # ── D1 features ──
    if d1_ohlc is not None:
        d1_c = d1_ohlc["close"]
        d1_o = d1_ohlc["open"]
        d1_h = d1_ohlc["high"]
        d1_l = d1_ohlc["low"]
        d1_v = d1_ohlc["volume"]
        d1_ts = d1_ohlc["timestamp"]

        d1_idx = len(d1_c) - 1
        for i in range(len(d1_ts) - 1, -1, -1):
            if d1_ts[i] <= bar_ts:
                d1_idx = i
                break

        d1_sl = slice(max(0, d1_idx - winsize + 1), d1_idx + 1)
        result["D1_Ret_1"] = _returns(d1_c[d1_sl])
        result["D1_Body_Ratio"] = _body_ratio(d1_o[d1_sl], d1_h[d1_sl], d1_l[d1_sl], d1_c[d1_sl])
        result["D1_ATR_14"] = _atr(d1_h[d1_sl], d1_l[d1_sl], d1_c[d1_sl])
        result["D1_RSI_14"] = _rsi(d1_c[d1_sl])
        result["D1_MACD"] = _macd(d1_c[d1_sl])
        result["D1_Vol_ZScore"] = _vol_zscore(d1_v[d1_sl])
        result["D1_Bollinger_Width"] = _bollinger_width(d1_c[d1_sl])
        result["D1_ADX_14"] = _adx(d1_h[d1_sl], d1_l[d1_sl], d1_c[d1_sl])
    else:
        for k in [
            "D1_Ret_1",
            "D1_Body_Ratio",
            "D1_ATR_14",
            "D1_RSI_14",
            "D1_MACD",
            "D1_Vol_ZScore",
            "D1_Bollinger_Width",
            "D1_ADX_14",
        ]:
            result[k] = 0.0

    # ── H4 features ──
    if h4_ohlc is not None:
        h4_c = h4_ohlc["close"]
        h4_h = h4_ohlc["high"]
        h4_l = h4_ohlc["low"]
        h4_ts = h4_ohlc["timestamp"]

        h4_idx = len(h4_c) - 1
        for i in range(len(h4_ts) - 1, -1, -1):
            if h4_ts[i] <= bar_ts:
                h4_idx = i
                break

        h4_sl = slice(max(0, h4_idx - winsize + 1), h4_idx + 1)
        result["H4_Trend_Strength"] = _adx(h4_h[h4_sl], h4_l[h4_sl], h4_c[h4_sl]) / 100.0
        result["H4_ATR_Ratio"] = _atr(h4_h[h4_sl], h4_l[h4_sl], h4_c[h4_sl]) / max(
            _atr(h[sl], l[sl], c[sl]), 1e-8
        )
        # H4 RSI divergence: difference between D1 and H4 RSI
        d1_rsi = result.get("D1_RSI_14", 50.0)
        h4_rsi = _rsi(h4_c[h4_sl])
        result["H4_RSI_Divergence"] = d1_rsi - h4_rsi
        result["H4_vs_D1_Alignment"] = (
            1.0
            if (
                h4_c[h4_sl][-1] > h4_c[h4_sl][-2]
                and d1_ohlc is not None
                and d1_ohlc["close"][-1] > d1_ohlc["close"][-2]
            )
            or (
                h4_c[h4_sl][-1] < h4_c[h4_sl][-2]
                and d1_ohlc is not None
                and d1_ohlc["close"][-1] < d1_ohlc["close"][-2]
            )
            else 0.0
        )
    else:
        for k in ["H4_Trend_Strength", "H4_ATR_Ratio", "H4_RSI_Divergence", "H4_vs_D1_Alignment"]:
            result[k] = 0.0

    # ── Cross-market features ──
    gold_close = float(c[-1])
    _empty_close = np.array([], dtype=np.float64)
    _empty_ts: list[pd.Timestamp] = []
    _s_close = silver_close if silver_close is not None else _empty_close
    _s_ts = silver_ts if silver_ts is not None else _empty_ts
    _e_close = eur_close if eur_close is not None else _empty_close
    _e_ts = eur_ts if eur_ts is not None else _empty_ts
    _d_close = dxy_close if dxy_close is not None else _empty_close
    _d_ts = dxy_ts if dxy_ts is not None else _empty_ts

    if gold_close > 0:
        silver_val = _align_higher_tf_value(bar_ts, _s_ts, _s_close)
        result["Cross_Gold_Silver_Ratio"] = (
            gold_close / max(silver_val, 1e-9) * 0.01 if silver_val > 0 else 0.0
        )

        eur_val = _align_higher_tf_value(bar_ts, _e_ts, _e_close)
        eur_prev = _align_higher_tf_value(bar_ts - pd.Timedelta(hours=4), _e_ts, _e_close)
        result["Cross_DXY_Return"] = 0.0  # DXY inverse proxy via EUR
        result["Cross_EURUSD_Return"] = (
            (eur_val - eur_prev) / max(eur_prev, 1e-9) * 100.0
            if eur_prev > 0 and eur_val > 0
            else 0.0
        )

        dxy_val = _align_higher_tf_value(bar_ts, _d_ts, _d_close)
        dxy_prev = _align_higher_tf_value(bar_ts - pd.Timedelta(hours=4), _d_ts, _d_close)
        result["Cross_Risk_On_Off"] = (
            (dxy_val - dxy_prev) / max(dxy_prev, 1e-9) * 100.0
            if dxy_prev > 0 and dxy_val > 0
            else 0.0
        )

    # ── Calendar / derived features ──
    weekday = bar_ts.dayofweek  # 0=Mon..6=Sun
    result["Derived_Weekday_Sin"] = math.sin(2 * math.pi * weekday / 7.0)
    result["Derived_Weekday_Cos"] = math.cos(2 * math.pi * weekday / 7.0)

    # Days to month end (0..15, normalized to [0,1])
    days_in_month = bar_ts.days_in_month
    result["Derived_Days_To_MonthEnd"] = (days_in_month - bar_ts.day) / max(days_in_month, 1)

    # Is month-end week (last 5 trading days)
    result["Derived_Is_MonthEnd_Week"] = 1.0 if (days_in_month - bar_ts.day) < 5 else 0.0

    # Weekend gap (Friday close vs Monday open estimate)
    result["Derived_Weekend_Gap"] = 0.0  # simplified

    # Vol regime: ratio of short-term ATR to long-term ATR
    st_atr = _atr(h[sl], l[sl], c[sl], period=5)
    lt_atr = _atr(h[sl], l[sl], c[sl], period=20)
    result["Derived_Vol_Regime"] = st_atr / max(lt_atr, 1e-8) if lt_atr > 0 else 1.0

    # Momentum
    if len(c) >= 5:
        result["Derived_Momentum_5D"] = float((c[-1] - c[-5]) / max(c[-5], 1e-8) * 100.0)
    else:
        result["Derived_Momentum_5D"] = 0.0
    if len(c) >= 20:
        result["Derived_Momentum_20D"] = float((c[-1] - c[-20]) / max(c[-20], 1e-8) * 100.0)
    else:
        result["Derived_Momentum_20D"] = 0.0

    return result


# ── Micro feature computation (aggregated from M5 to swing TF) ───────────────


def compute_micro_features_at_bar(
    ohlc_m5: dict[str, np.ndarray],
    cross_data: dict[str, np.ndarray] | None,
    bar_idx: int,
    n_m5_per_tf: int,
) -> dict[str, float]:
    """Compute 9 micro features aggregated from M5 bars within the swing bar."""
    result: dict[str, float] = {}
    m5_start = max(0, bar_idx - n_m5_per_tf + 1)
    m5_sl = slice(m5_start, bar_idx + 1)

    m5_c = ohlc_m5["close"][m5_sl]
    m5_h = ohlc_m5["high"][m5_sl]
    m5_l = ohlc_m5["low"][m5_sl]
    m5_o = ohlc_m5["open"][m5_sl]
    m5_v = ohlc_m5["volume"][m5_sl]
    m5_s = ohlc_m5["spread"][m5_sl]

    if len(m5_c) < 2:
        for k in MICRO_FEATURES:
            result[k] = 0.0
        return result

    # tick_return: aggregate M5 returns
    m5_rets = np.diff(m5_c) / m5_c[:-1] * 100.0
    result["tick_return"] = float(np.mean(m5_rets))

    # hl_ratio: sum across M5 bars within swing TF bar
    result["hl_ratio"] = float(np.mean((m5_h - m5_l) / np.clip(m5_c, 1e-9, None)))

    # co_ratio: mean close/open
    result["co_ratio"] = float(np.mean(m5_c / np.clip(m5_o, 1e-9, None)))

    # avg_spread: mean spread / close
    result["avg_spread"] = float(np.mean(m5_s / np.clip(m5_c, 1e-9, None)))

    # OIM: order imbalance metric (mean of per-bar OIM)
    hl_diff = m5_h - m5_l
    oim_vals = np.where(hl_diff > 1e-12, (m5_c - m5_o) / hl_diff, 0.0)
    result["OIM"] = float(np.mean(oim_vals))

    # tick_velocity: aggregate volume
    result["tick_velocity"] = float(np.sum(m5_v)) / 1000.0

    # Cross-symbol returns (from cross_data aligned to M5)
    if cross_data is not None:
        for sym_key, feat_key in [
            ("xag_close", "XAGUSDc_return"),
            ("eur_close", "EURUSDc_return"),
            ("jpy_close", "USDJPYc_return"),
        ]:
            sym_close = cross_data.get(sym_key)
            if sym_close is not None and len(sym_close) > bar_idx:
                sym_sl = slice(m5_start, bar_idx + 1)
                sym_c = sym_close[sym_sl]
                if len(sym_c) >= 2:
                    result[feat_key] = float((sym_c[-1] - sym_c[0]) / max(sym_c[0], 1e-9) * 100.0)
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
) -> np.ndarray:
    """Compute barrier labels: -1=SL, 0=timeout, 1=TP.

    For each bar, look ahead `horizon` bars. If price hits SL first → -1,
    TP first → 1, neither → 0.
    """
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

        sl_dist = atr_mult * atr_i
        tp_dist = atr_mult * atr_i  # symmetric RR=1:1
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

    # ── Load higher TF ──
    d1_ohlc = load_higher_tf("D1", _data_dir)
    h4_ohlc = load_higher_tf("H4", _data_dir)
    if d1_ohlc:
        print(f"  D1: {d1_ohlc['n_bars']} bars loaded")
    if h4_ohlc:
        print(f"  H4: {h4_ohlc['n_bars']} bars loaded")

    # ── Load cross-symbol data ──
    cross_data: dict[str, tuple[np.ndarray, list[pd.Timestamp]]] = {}
    for sym_name, csv_name in [
        ("silver", "xagusdc_m5_merged.csv"),
        ("eur", "eurusdc_m5_merged.csv"),
        ("dxy", "usdjpyc_m5_merged.csv"),  # proxy for DXY
    ]:
        sym_path = _data_dir / csv_name
        if sym_path.exists():
            sym_ohlc = load_ohlc_csv(sym_path)
            cross_data[sym_name] = (sym_ohlc["close"], sym_ohlc["timestamp"])
            print(f"  {sym_name}: {sym_ohlc['n_bars']} bars loaded")

    # ── Align M5 micro features to target-TF bars ──
    # For each target-TF bar, find the aggregated micro features
    # Map TF bar timestamps to nearest M5 indices
    tf_ts_arr = np.array([ts.timestamp() for ts in tf_ohlc["timestamp"]], dtype=np.float64)
    m5_ts_arr = np.array([ts.timestamp() for ts in m5_ohlc["timestamp"]], dtype=np.float64)

    # ── Compute labels ──
    print(f"  Computing barrier labels (horizon={horizon}, SL=TP=1.5xATR)...")
    labels = compute_barrier_labels(tf_ohlc, atr_mult=1.5, horizon=horizon)

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

    for i in range(start_idx, n_bars):
        bar_ts = tf_ohlc["timestamp"][i]

        # Find corresponding M5 bar index (last M5 bar within this TF bar)
        bar_ts_epoch = bar_ts.timestamp()
        m5_idx = int(np.searchsorted(m5_ts_arr, bar_ts_epoch, side="right") - 1)
        if m5_idx < n_m5:
            continue

        # ── Swing macro features ──
        macro = compute_swing_macro_features(
            bar_idx=i,
            tf_ohlc=tf_ohlc,
            d1_ohlc=d1_ohlc,
            h4_ohlc=h4_ohlc,
            gold_ts=tf_ohlc["timestamp"],
            silver_close=cross_data.get("silver", (None, None))[0],
            silver_ts=cross_data.get("silver", (None, None))[1],
            eur_close=cross_data.get("eur", (None, None))[0],
            eur_ts=cross_data.get("eur", (None, None))[1],
            dxy_close=cross_data.get("dxy", (None, None))[0],
            dxy_ts=cross_data.get("dxy", (None, None))[1],
        )

        # ── Micro features (from M5 aggregation) ──
        micro = compute_micro_features_at_bar(m5_ohlc, micro_cross, m5_idx, n_m5)

        # ── TF-specific features ──
        tf_c = tf_ohlc["close"][max(0, i - 60) : i + 1]
        tf_ou = _ou_theta(tf_c, lookback=20)
        tf_hurst = _hurst(tf_c, max_lag=20)

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

    # ── Chronological split ──
    n_val = int(valid_count * val_ratio)
    n_test = int(valid_count * test_ratio)
    n_train = valid_count - n_val - n_test

    X_train = X[:n_train]
    y_train = y[:n_train]
    X_val = X[n_train : n_train + n_val]
    y_val = y[n_train : n_train + n_val]
    X_test = X[n_train + n_val :]
    y_test = y[n_train + n_val :]

    # Dummy PnL (synthetic: 1.5 for TP, -1.5 for SL, 0 for timeout)
    pnl_r = np.where(y == 1, 1.5, np.where(y == -1, -1.5, 0.0)).astype(np.float32)
    pnl_r_train = pnl_r[:n_train]
    pnl_r_val = pnl_r[n_train : n_train + n_val]
    pnl_r_test = pnl_r[n_train + n_val :]
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
        "n_train": int(n_train),
        "n_val": int(n_val),
        "n_test": int(n_test),
        "sl_atr_mult": 1.5,
        "tp_atr_mult": 1.5,
        "label_dist": {str(k): int(v) for k, v in zip(unique, counts, strict=False)},
        "built_at": datetime.now().isoformat(),
    }
    with open(output_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print(f"  Saved to {output_dir}/")
    print(f"    train: {n_train} samples")
    print(f"    val:   {n_val} samples")
    print(f"    test:  {n_test} samples")

    return meta


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Build enhanced swing training dataset")
    parser.add_argument(
        "--tf", default="M30", choices=["M15", "M30"], help="Target timeframe (default: M30)"
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
    args = parser.parse_args()

    # Auto-adjust horizon defaults
    if args.horizon == 12 and args.tf == "M15":
        args.horizon = 24  # M15 needs more bars for same real-time horizon (~6h)
        print(f"[auto] M15 horizon adjusted to {args.horizon}")

    output = Path(args.output_dir)
    if output.name == "swing_enhanced":
        output = Path(f"data/training/swing_{args.tf.lower()}_enhanced")

    build_swing_dataset(
        tf=args.tf,
        horizon=args.horizon,
        output_dir=output,
        raw_dir=Path(args.raw_dir),
    )


if __name__ == "__main__":
    main()
