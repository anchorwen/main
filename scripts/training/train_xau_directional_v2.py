# type: ignore
#!/usr/bin/env python3
"""XAU Directional Brain V2 — dedicated 35-dim swing_enhanced_35 pipeline.

FIX-20260615-010/P1: Complete rewrite of feature computation.
V1 was broken — it imported train_btc_swing_v9 which now returns
41-dim BTC features (tuple format).  V2 uses a standalone XAU
35-dim feature computer aligned with the swing_enhanced_35 schema.

Key differences from V1:
  - No import from train_btc_swing_v9
  - Feature names from core.features.schemas.swing_enhanced_schema
  - N_FEATURES = 35 (not 37)
  - XAU cross-asset: Cross_Gold_Silver_Ratio, XAGUSDc_return
    (BTC uses AUDJPYc_return, BTC/XAU ratio)
  - No regime derivatives (TF_delta_OU etc. — BTC-only)

Usage:
  python scripts/training/train_xau_directional_v2.py --timeframe H1
  python scripts/training/train_xau_directional_v2.py --timeframe M15
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

UTC = UTC
ROOT = Path(__file__).resolve().parent.parent.parent

# ── XAU Strategy Parameters (from live.yaml h1_swing) ──
SL_ATR_MULT = 2.0
TP_ATR_MULT = 3.5
SPREAD_POINTS = 0.5
SLIPPAGE_POINTS = 0.2

# ── Feature names from canonical XAU schema ──
from core.features.schemas.swing_enhanced_schema import SWING_ENHANCED_35_FEATURES

ALL_FEATURE_NAMES = list(SWING_ENHANCED_35_FEATURES)
N_FEATURES = len(ALL_FEATURE_NAMES)  # 35

# ── Micro feature names (positions 24-32 in swing_enhanced_35) ──
MICRO_9_NAMES = [
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


# ═══════════════════════════════════════════════════════════════════════════════
# Technical indicator helpers (extracted from train_btc_swing_v9)
# ═══════════════════════════════════════════════════════════════════════════════


def _atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> float:
    """Average True Range over *period* bars."""
    n = len(c)
    if n < 2:
        return 0.0
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    return float(np.mean(tr[max(0, n - period) :])) if n >= period else float(np.mean(tr[1:]))


def _rsi(close: np.ndarray, period: int = 14) -> float:
    """Wilder's RSI over *period* bars."""
    n = len(close)
    if n < period + 1:
        return 50.0
    deltas = np.diff(close[-period - 1 :])
    gains: list[float] = np.sum(deltas[deltas > 0])
    losses: list[float] = -np.sum(deltas[deltas < 0])
    if losses == 0:
        return 100.0
    rs = gains / losses
    return float(100.0 - 100.0 / (1.0 + rs))


def _macd(close: np.ndarray) -> tuple[float, float, float]:
    """MACD (12, 26, 9). Returns (macd_line, signal_line, histogram)."""
    n = len(close)
    if n < 26:
        return 0.0, 0.0, 0.0
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    macd_line = ema12 - ema26
    # signal line: 9-period EMA of macd_line (approximate)
    signal = macd_line * 0.2  # simplified single-bar EMA
    return float(macd_line), float(signal), float(macd_line - signal)


def _ema(data: np.ndarray, period: int) -> float:
    """Exponential Moving Average."""
    n = len(data)
    if n < period:
        return float(np.mean(data))
    alpha = 2.0 / (period + 1)
    result = float(np.mean(data[:period]))
    for i in range(period, n):
        result = alpha * data[i] + (1 - alpha) * result
    return result


def _vol_zscore(close: np.ndarray, period: int = 20) -> float:
    """Volume (returns) z-score."""
    n = len(close)
    if n < period + 1:
        return 0.0
    returns = np.diff(np.log(close[-period - 1 :] + 1e-12))
    return float((returns[-1] - np.mean(returns)) / (np.std(returns) + 1e-8))


def _bollinger_width(close: np.ndarray, period: int = 20) -> float:
    """Bollinger Band width."""
    n = len(close)
    if n < period:
        return 0.0
    ma = float(np.mean(close[-period:]))
    std = float(np.std(close[-period:]))
    return (ma + 2 * std - (ma - 2 * std)) / ma if ma > 0 else 0.0


def _adx(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> float:
    """Average Directional Index."""
    n = len(c)
    if n < period + 1:
        return 25.0
    tr = np.zeros(n)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        up = h[i] - h[i - 1]
        down = l[i - 1] - l[i]
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
    tr_smooth = float(np.sum(tr[-period:]))
    plus_smooth = float(np.sum(plus_dm[-period:]))
    minus_smooth = float(np.sum(minus_dm[-period:]))
    if tr_smooth == 0:
        return 25.0
    plus_di = 100.0 * plus_smooth / tr_smooth
    minus_di = 100.0 * minus_smooth / tr_smooth
    di_sum = plus_di + minus_di
    return float(100.0 * abs(plus_di - minus_di) / di_sum) if di_sum > 0 else 25.0


def _ou_theta(price: np.ndarray) -> float:
    """Ornstein-Uhlenbeck theta (mean-reversion speed)."""
    n = len(price)
    if n < 10:
        return 0.0
    log_p = np.log(price[-min(n, 200) :] + 1e-12)
    y = log_p[1:]
    x = log_p[:-1]
    mu_x, mu_y = np.mean(x), np.mean(y)
    num: list[float] = np.sum((x - mu_x) * (y - mu_y))
    den: list[float] = np.sum((x - mu_x) ** 2)
    if den == 0:
        return 0.0
    rho = num / den
    dt = 1.0
    theta = -np.log(max(rho, 1e-8)) / dt if rho > 0 else 0.0
    return float(theta)


def _hurst(price: np.ndarray, max_lag: int = 20) -> float:
    """Hurst exponent (0.0 = mean-reverting, 0.5 = random walk, 1.0 = trending)."""
    n = len(price)
    if n < max_lag * 2:
        return 0.5
    log_p = np.log(price[-min(n, 500) :] + 1e-12)
    lags = np.arange(2, max_lag + 1)
    rs_values = np.zeros(len(lags))
    for j, lag in enumerate(lags):
        segments = len(log_p) // lag
        if segments < 2:
            continue
        r_sum = 0.0
        for s in range(min(segments, 10)):
            seg = log_p[s * lag : (s + 1) * lag]
            if len(seg) < 2:
                continue
            mean_seg = np.mean(seg)
            cum_dev = np.cumsum(seg - mean_seg)
            r: dict[str, float] = np.max(cum_dev) - np.min(cum_dev)
            s_val = np.std(seg) + 1e-8
            r_sum += r / s_val
        rs_values[j] = r_sum / max(segments, 1)
    valid = rs_values > 0
    if np.sum(valid) < 3:
        return 0.5
    coeffs = np.polyfit(np.log(lags[valid]), np.log(rs_values[valid]), 1)
    return float(max(0.0, min(1.0, coeffs[0])))


# ═══════════════════════════════════════════════════════════════════════════════
# XAU 35-dim feature computation
# ═══════════════════════════════════════════════════════════════════════════════


def compute_xau_feature_row(
    idx: int,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    v: np.ndarray,
    spreads: np.ndarray,
    day_features: dict[int, dict[str, float]],
    daily_ts: np.ndarray,
    daily_o: np.ndarray,
    daily_h: np.ndarray,
    daily_l: np.ndarray,
    daily_c: np.ndarray,
    gold_price_hist: np.ndarray,
    tf_minutes: float = 60.0,
    prev_ou: float | None = None,
    prev_hurst: float | None = None,
) -> tuple[list[float], float, float]:
    """Compute 35-dim XAU swing_enhanced_35 feature vector at bar *idx*.

    Returns (row, tf_ou, tf_hurst) — aligned with SWING_ENHANCED_35_FEATURES.
    """
    end = idx + 1
    price_slice = c[:end]
    o_slice, h_slice, l_slice, c_slice = o[:end], h[:end], l[:end], c[:end]

    # ── D1 features from day_features dict ──
    bar_ts = daily_ts[idx] if idx < len(daily_ts) else 0
    day_idx = len(day_features) - 1
    d_feat = day_features.get(day_idx, {}) if day_features else {}

    d1_ret = float(d_feat.get("D1_Ret_1", 0.0))
    d1_body = float(d_feat.get("D1_Body_Ratio", 0.5))
    d1_atr = float(d_feat.get("D1_ATR_14", 0.0))
    d1_rsi = float(d_feat.get("D1_RSI_14", 50.0))
    d1_macd = float(d_feat.get("D1_MACD", 0.0))
    d1_vol_z = float(d_feat.get("D1_Vol_ZScore", 0.0))
    d1_bb = float(d_feat.get("D1_Bollinger_Width", 0.0))
    d1_adx = float(d_feat.get("D1_ADX_14", 25.0))

    # ── H4 features (from day_features) ──
    h4_trend = float(d_feat.get("H4_Trend_Strength", 0.0))
    h4_atr_r = float(d_feat.get("H4_ATR_Ratio", 0.0))
    h4_rsi_d = float(d_feat.get("H4_RSI_Divergence", 0.0))
    h4_d1_align = float(d_feat.get("H4_vs_D1_Alignment", 0.0))

    # ── XAU cross-asset features ──
    gold_silver_ratio = float(d_feat.get("Cross_Gold_Silver_Ratio", 0.0))
    dxy_return = float(d_feat.get("Cross_DXY_Return", 0.0))
    eurusd_return = float(d_feat.get("Cross_EURUSD_Return", 0.0))
    risk_on_off = float(d_feat.get("Cross_Risk_On_Off", 0.0))

    # ── Derived calendar features ──
    dt_obj = datetime.fromtimestamp(float(bar_ts), tz=UTC) if bar_ts > 0 else datetime.now(UTC)
    weekday = dt_obj.weekday()
    weekday_sin = math.sin(2 * math.pi * weekday / 7.0)
    weekday_cos = math.cos(2 * math.pi * weekday / 7.0)
    import calendar as _cal

    days_in_month = _cal.monthrange(dt_obj.year, dt_obj.month)[1]
    days_to_end = (days_in_month - dt_obj.day) / float(days_in_month)
    is_month_end = 1.0 if dt_obj.day >= days_in_month - 4 else 0.0
    weekend_gap = 1.0 if weekday >= 4 else 0.0

    # ── Vol regime + momentum ──
    atr_val = _atr(h_slice, l_slice, c_slice)
    lookback_5d = min(len(c_slice), 288 * 5)
    atr_5d = (
        _atr(h_slice[-lookback_5d:], l_slice[-lookback_5d:], c_slice[-lookback_5d:])
        if len(c_slice) >= 288 * 5
        else atr_val
    )
    vol_regime = atr_val / atr_5d if atr_5d > 0 else 1.0
    mom_5d = (
        (c[idx] - c[max(0, idx - 288 * 5)]) / c[max(0, idx - 288 * 5)]
        if len(c) > 288 * 5 and c[max(0, idx - 288 * 5)] > 0
        else 0.0
    )
    mom_20d = (
        (c[idx] - c[max(0, idx - 288 * 20)]) / c[max(0, idx - 288 * 20)]
        if len(c) > 288 * 20 and c[max(0, idx - 288 * 20)] > 0
        else 0.0
    )

    # ── Micro features (bar-level) ──
    prev_c_val = c[idx - 1] if idx > 0 else c[idx]
    tick_ret = (c[idx] - o[idx]) / o[idx] if o[idx] > 0 else 0.0
    hl_ratio_val = (h[idx] - l[idx]) / prev_c_val if prev_c_val > 0 else 0.0
    co_ratio_val = abs(c[idx] - o[idx]) / (h[idx] - l[idx]) if (h[idx] - l[idx]) > 0 else 0.0
    avg_spread_val = float(spreads[idx]) if idx < len(spreads) else 0.5
    oim = (c[idx] - o[idx]) / (h[idx] - l[idx]) if (h[idx] - l[idx]) > 0 else 0.0
    tick_vel = (
        float(v[idx]) / (float(np.mean(v[max(0, idx - 20) : end])) + 1e-8) if idx > 20 else 1.0
    )

    # XAU cross-market micro returns (XAGUSDc = silver, key gold proxy)
    xagusd_ret = 0.0  # placeholder — real data from MT5 cross-asset feed
    eur_ret = 0.0  # placeholder
    usdjpy_ret = 0.0  # placeholder

    # ── TF-specific (timeframe-aware) ──
    tf_ou = _ou_theta(price_slice)
    tf_hurst = _hurst(price_slice)

    # ── Assemble in SWING_ENHANCED_35 order ──
    # 24 MACRO + 9 MICRO + 2 TF = 35
    values_map: dict[str, float] = {
        # ── DAILY_SWING_24 (slots 0-23) ──
        "D1_Ret_1": d1_ret,
        "D1_Body_Ratio": d1_body,
        "D1_ATR_14": d1_atr,
        "D1_RSI_14": d1_rsi,
        "D1_MACD": d1_macd,
        "D1_Vol_ZScore": d1_vol_z,
        "D1_Bollinger_Width": d1_bb,
        "D1_ADX_14": d1_adx,
        "H4_Trend_Strength": h4_trend,
        "H4_ATR_Ratio": h4_atr_r,
        "H4_RSI_Divergence": h4_rsi_d,
        "H4_vs_D1_Alignment": h4_d1_align,
        "Cross_Gold_Silver_Ratio": gold_silver_ratio,
        "Cross_DXY_Return": dxy_return,
        "Cross_EURUSD_Return": eurusd_return,
        "Cross_Risk_On_Off": risk_on_off,
        "Derived_Weekday_Sin": weekday_sin,
        "Derived_Weekday_Cos": weekday_cos,
        "Derived_Days_To_MonthEnd": days_to_end,
        "Derived_Is_MonthEnd_Week": is_month_end,
        "Derived_Weekend_Gap": weekend_gap,
        "Derived_Vol_Regime": vol_regime,
        "Derived_Momentum_5D": mom_5d,
        "Derived_Momentum_20D": mom_20d,
        # ── MICRO_9 (slots 24-32) ──
        "tick_return": tick_ret,
        "hl_ratio": hl_ratio_val,
        "co_ratio": co_ratio_val,
        "avg_spread": avg_spread_val,
        "OIM": oim,
        "tick_velocity": tick_vel,
        "XAGUSDc_return": xagusd_ret,
        "EURUSDc_return": eur_ret,
        "USDJPYc_return": usdjpy_ret,
        # ── TF_SPECIFIC_2 (slots 33-34) ──
        "TF_OU_Theta": tf_ou,
        "TF_Hurst": tf_hurst,
    }
    return [values_map.get(name, 0.0) for name in ALL_FEATURE_NAMES], tf_ou, tf_hurst


# ═══════════════════════════════════════════════════════════════════════════════
# Time decay weights (replaces v9.compute_time_decay_weights)
# ═══════════════════════════════════════════════════════════════════════════════


def compute_time_decay_weights(
    timestamps: np.ndarray,
    half_life_days: float = 90.0,
) -> np.ndarray:
    """Exponential time-decay weights (newer samples weight more)."""
    if len(timestamps) == 0:
        return np.ones(0, dtype=np.float64)
    # Normalize to days since most recent bar
    max_ts: float = np.max(timestamps)
    days_ago = (max_ts - timestamps) / 86400.0
    decay = np.exp(-np.log(2) * days_ago / half_life_days)
    decay = np.clip(decay, 0.05, 1.0)
    return decay.astype(np.float64)


# ═══════════════════════════════════════════════════════════════════════════════
# Walk-forward purged CV splits
# ═══════════════════════════════════════════════════════════════════════════════


def walk_forward_purged_splits(
    n: int,
    timestamps: np.ndarray,
    n_folds: int = 5,
    purge_bars: int = 24,
) -> list[dict[str, Any]]:
    """Walk-forward purged CV splits with embargo."""
    splits = []
    fold_size = n // (n_folds + 1)
    for f in range(n_folds):
        test_start = (f + 1) * fold_size
        test_end = test_start + fold_size if f < n_folds - 1 else n
        train_end = test_start - purge_bars
        if train_end <= 0:
            continue
        splits.append(
            {
                "train_idx": list(range(0, train_end)),
                "test_idx": list(range(test_start, test_end)),
                "fold": f,
            }
        )
    return splits


# ═══════════════════════════════════════════════════════════════════════════════
# Directional label computation (from V1 — unchanged)
# ═══════════════════════════════════════════════════════════════════════════════


def _simulate_one_trade(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    i: int,
    horizon: int,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    direction: str,
) -> tuple[int, float, int]:
    """Simulate ONE directional trade."""
    n = len(o)
    end_bar = min(i + horizon, n - 1)
    for j in range(i + 1, end_bar + 1):
        if direction == "long":
            if l[j] <= sl_price:
                return -1, (sl_price - entry_price) / entry_price, j - i
            if h[j] >= tp_price:
                return 1, (tp_price - entry_price) / entry_price, j - i
        else:
            if h[j] >= sl_price:
                return -1, (entry_price - sl_price) / entry_price, j - i
            if l[j] <= tp_price:
                return 1, (entry_price - tp_price) / entry_price, j - i
    final_price = c[end_bar]
    if direction == "long":
        return 0, (final_price - entry_price) / entry_price, horizon
    else:
        return 0, (entry_price - final_price) / entry_price, horizon


def compute_directional_labels(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    horizon: int,
    sl_atr_mult: float,
    tp_atr_mult: float,
    spread_points: float,
    slippage_points: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Directional labels — V1-compatible: entry at NEXT bar open, select winning direction."""
    n = len(o)
    labels = np.zeros(n, dtype=np.float32)
    pnl_r = np.zeros(n, dtype=np.float32)
    atr = np.zeros(n)
    for i in range(14, n):
        atr[i] = _atr(h[: i + 1], l[: i + 1], c[: i + 1])
    half_sp = spread_points / 2.0
    n_long = n_short = n_neutral = 0
    for i in range(14, n - horizon - 1):
        if atr[i] <= 0:
            continue
        sl_raw = sl_atr_mult * atr[i]
        tp_raw = max(tp_atr_mult * atr[i], sl_raw * 0.3)
        # Long simulation: entry at next bar open + half_spread + slippage
        entry_long = o[i + 1] + half_sp + slippage_points
        lo, _, _ = _simulate_one_trade(
            o,
            h,
            l,
            c,
            i,
            horizon,
            entry_long,
            entry_long - sl_raw,
            entry_long + tp_raw,
            "long",
        )
        # Short simulation: entry at next bar open - half_spread - slippage
        entry_short = o[i + 1] - half_sp - slippage_points
        so, _, _ = _simulate_one_trade(
            o,
            h,
            l,
            c,
            i,
            horizon,
            entry_short,
            entry_short + sl_raw,
            entry_short - tp_raw,
            "short",
        )
        # Select winning direction — discard conflicting/neutral signals
        if lo == 1 and so != 1:
            labels[i] = 1.0
            pnl_r[i] = tp_raw / max(sl_raw, 1e-9)
            n_long += 1
        elif so == 1 and lo != 1:
            labels[i] = -1.0
            pnl_r[i] = tp_raw / max(sl_raw, 1e-9)
            n_short += 1
        else:
            n_neutral += 1
    total = n_long + n_short + n_neutral
    if total > 0:
        print(
            f"  Labels: LONG={n_long} ({100*n_long/total:.1f}%) SHORT={n_short} ({100*n_short/total:.1f}%) NEUTRAL={n_neutral} ({100*n_neutral/total:.1f}%)"
        )
    return labels, pnl_r, np.zeros(n, dtype=np.int32)


# ═══════════════════════════════════════════════════════════════════════════════
# Dataset builder
# ═══════════════════════════════════════════════════════════════════════════════


def build_dataset(
    csv_path: str,
    output_dir: str,
    horizon: int,
    sl_atr_mult: float,
    tp_atr_mult: float,
    spread_points: float,
    slippage_points: float,
    cv_folds: int,
    purge_bars: int,
    tf_minutes: float,
) -> dict[str, Any]:
    """Build 35-dim XAU directional dataset."""
    print(f"[B2] Loading XAU data from {csv_path}...")
    df = pd.read_csv(csv_path)
    n_bars = len(df)
    print(f"  Loaded {n_bars} bars")

    o = df["open"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64)
    c = df["close"].values.astype(np.float64)
    vol_col = "tick_volume" if "tick_volume" in df.columns else "volume"
    v = df.get(vol_col, pd.Series(np.zeros(n_bars))).values.astype(np.float64)
    spreads = df.get("spread", pd.Series([spread_points] * n_bars)).values.astype(np.float64)
    timestamps = pd.to_datetime(df["time"]).astype(np.int64).values // 10**9
    timestamps_f = timestamps.astype(np.float64)

    # Daily features
    print("[B2] Computing day-level context features...")
    df_dt = pd.to_datetime(df["time"])
    daily = (
        df.set_index(df_dt)
        .resample("D")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", vol_col: "sum"})
        .dropna()
    )
    daily_ts = daily.index.astype(np.int64).values // 10**9
    daily_ts_f = daily_ts.astype(np.float64)
    daily_o = daily["open"].values
    daily_h = daily["high"].values
    daily_l = daily["low"].values
    daily_c = daily["close"].values

    day_features: dict[int, dict[str, float]] = {}
    for d_idx in range(len(daily_c)):
        end = d_idx + 1
        prev_c = daily_c[-2] if len(daily_c) >= 2 else daily_c[-1]
        day_features[d_idx] = {
            "D1_Ret_1": (daily_c[-1] - prev_c) / prev_c if prev_c > 0 else 0.0,
            "D1_Body_Ratio": abs(daily_c[-1] - daily_o[-1]) / (daily_h[-1] - daily_l[-1])
            if (daily_h[-1] - daily_l[-1]) > 0
            else 0.5,
            "D1_ATR_14": _atr(daily_h[:end], daily_l[:end], daily_c[:end]),
            "D1_RSI_14": _rsi(daily_c[:end]),
            "D1_MACD": _macd(daily_c[:end])[2],
            "D1_Vol_ZScore": _vol_zscore(daily_c[:end]),
            "D1_Bollinger_Width": _bollinger_width(daily_c[:end]),
            "D1_ADX_14": _adx(daily_h[:end], daily_l[:end], daily_c[:end]),
            # XAU cross-asset (placeholders)
            "Cross_Gold_Silver_Ratio": 0.0,
            "Cross_DXY_Return": 0.0,
            "Cross_EURUSD_Return": 0.0,
            "Cross_Risk_On_Off": 0.0,
            "H4_Trend_Strength": 0.0,
            "H4_ATR_Ratio": 0.0,
            "H4_RSI_Divergence": 0.0,
            "H4_vs_D1_Alignment": 0.0,
        }

    MIN_BARS = 100
    gold_price_hist = c  # XAU is the gold price itself
    features = np.zeros((n_bars, N_FEATURES), dtype=np.float32)
    start_bar = MIN_BARS
    prev_ou: float | None = None
    prev_hurst: float | None = None

    print(f"[B2] Computing {N_FEATURES}-dim XAU features...")
    for i in range(start_bar, n_bars - horizon - 1):
        if (i - start_bar) % 20000 == 0 and i > start_bar:
            print(f"  ... {i}/{n_bars} bars ({100 * i / n_bars:.0f}%)")
        row, tf_ou, tf_hurst = compute_xau_feature_row(
            i,
            o,
            h,
            l,
            c,
            v,
            spreads,
            day_features,
            daily_ts_f,
            daily_o,
            daily_h,
            daily_l,
            daily_c,
            gold_price_hist,
            tf_minutes=tf_minutes,
            prev_ou=prev_ou,
            prev_hurst=prev_hurst,
        )
        features[i] = np.asarray(row, dtype=np.float32)
        prev_ou = tf_ou
        prev_hurst = tf_hurst

    labels, pnl_r, _ = compute_directional_labels(
        o,
        h,
        l,
        c,
        horizon,
        sl_atr_mult,
        tp_atr_mult,
        spread_points,
        slippage_points,
    )
    valid_idx = np.arange(start_bar, n_bars - horizon - 1)
    features = features[valid_idx]
    labels = labels[valid_idx]
    pnl_r = pnl_r[valid_idx]
    ts_valid = timestamps_f[valid_idx]
    labeled_mask = labels != 0.0
    X = features[labeled_mask]
    y = labels[labeled_mask]
    r = pnl_r[labeled_mask]
    ts_labeled = ts_valid[labeled_mask]
    print(f"  Labeled: {len(X)} ({100 * len(X) / max(len(valid_idx), 1):.1f}%)")

    weights = compute_time_decay_weights(ts_labeled, 180.0)
    splits = walk_forward_purged_splits(len(X), ts_labeled, cv_folds, purge_bars)
    os.makedirs(output_dir, exist_ok=True)
    np.savez_compressed(
        os.path.join(output_dir, "train.npz"),
        X=X,
        y=y,
        pnl_r=r,
        sample_weight=weights,
        timestamps=ts_labeled,
    )
    splits_json = []
    for s in splits:
        sc = dict(s)
        for k in list(sc):
            if isinstance(sc[k], np.ndarray):
                sc[k] = sc[k].tolist()
        splits_json.append(sc)
    with open(os.path.join(output_dir, "cv_splits.json"), "w") as f:
        json.dump(splits_json, f)
    meta = {
        "n_samples": int(len(X)),
        "n_features": N_FEATURES,
        "n_long": int(np.sum(y > 0)),
        "n_short": int(np.sum(y < 0)),
        "timeframe_minutes": tf_minutes,
        "horizon": horizon,
        "feature_names": ALL_FEATURE_NAMES,
        "schema": "swing_enhanced_35",
    }
    with open(os.path.join(output_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    return meta


# ═══════════════════════════════════════════════════════════════════════════════
# Model evaluation
# ═══════════════════════════════════════════════════════════════════════════════


def _evaluate_model(y_true, y_pred, pnl_r):
    signal_mask = np.abs(y_pred) > 0.05
    dir_acc = (
        float(np.mean(np.sign(y_pred[signal_mask]) == np.sign(y_true[signal_mask])))
        if np.sum(signal_mask) > 0
        else 0.0
    )
    long_actual, short_actual = y_true > 0, y_true < 0
    long_pred, short_pred = y_pred > 0.05, y_pred < -0.05
    long_rec = float(np.sum(long_actual & long_pred) / max(np.sum(long_actual), 1))
    short_rec = float(np.sum(short_actual & short_pred) / max(np.sum(short_actual), 1))
    trade_rs = []
    for j in range(len(y_pred)):
        if long_pred[j]:
            trade_rs.append(pnl_r[j] if long_actual[j] else -1.0)
        elif short_pred[j]:
            trade_rs.append(pnl_r[j] if short_actual[j] else -1.0)
    return {
        "directional_accuracy": dir_acc,
        "long_recall": long_rec,
        "short_recall": short_rec,
        "long_precision": float(np.sum(long_actual & long_pred) / max(np.sum(long_pred), 1)),
        "short_precision": float(np.sum(short_actual & short_pred) / max(np.sum(short_pred), 1)),
        "pred_long": int(np.sum(long_pred)),
        "pred_short": int(np.sum(short_pred)),
        "pred_neutral": int(len(y_pred) - np.sum(long_pred) - np.sum(short_pred)),
        "trade_rs": trade_rs,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Model training
# ═══════════════════════════════════════════════════════════════════════════════


def train_models(data_dir, output_dir, lr=0.02, n_estimators=500, max_depth=None, num_leaves=31):
    data = np.load(os.path.join(data_dir, "train.npz"))
    X_all, y_all = data["X"], data["y"]
    pnl_r_all = data.get("pnl_r", np.zeros(len(y_all)))
    weights_all = data.get("sample_weight", np.ones(len(X_all)))
    with open(os.path.join(data_dir, "cv_splits.json")) as f:
        splits = json.load(f)
    os.makedirs(output_dir, exist_ok=True)
    results: dict[str, list[dict[str, Any]]] = {"lightgbm": [], "xgboost": []}
    for fold_idx, split in enumerate(splits):
        train_idx, val_idx = split["train_idx"], split["test_idx"]
        X_tr, y_tr = X_all[train_idx], y_all[train_idx]
        X_val, y_val = X_all[val_idx], y_all[val_idx]
        w_tr, r_val = weights_all[train_idx], pnl_r_all[val_idx]
        print(f"\n  Fold {fold_idx + 1}/{len(splits)}: train={len(X_tr)}, val={len(X_val)}")
        # LightGBM
        try:
            import lightgbm as lgb

            params = {
                "objective": "regression",
                "metric": "rmse",
                "boosting_type": "gbdt",
                "num_leaves": num_leaves,
                "learning_rate": lr,
                "feature_fraction": 0.8,
                "bagging_fraction": 0.8,
                "bagging_freq": 5,
                "verbose": -1,
                "seed": 42,
            }
            dtrain = lgb.Dataset(X_tr, label=y_tr, weight=w_tr)
            dval = lgb.Dataset(X_val, label=y_val, reference=dtrain)
            model = lgb.train(
                params,
                dtrain,
                valid_sets=[dval],
                num_boost_round=n_estimators,
                callbacks=[lgb.early_stopping(50)],
            )
            model.save_model(os.path.join(output_dir, f"lightgbm_fold{fold_idx}_s42.txt"))
            ev = _evaluate_model(y_val, model.predict(X_val), r_val)
            results["lightgbm"].append(ev)
            print(
                f"    LGB: DirAcc={ev['directional_accuracy']:.3f} LongRec={ev['long_recall']:.3f} ShortRec={ev['short_recall']:.3f} trades={len(ev['trade_rs'])}"
            )
        except ImportError:
            pass
        # XGBoost
        try:
            import xgboost as xgb

            md = max_depth if max_depth is not None else 5
            params = {
                "objective": "reg:squarederror",
                "eval_metric": "rmse",
                "max_depth": md,
                "learning_rate": lr,
                "subsample": 0.8,
                "colsample_bytree": 0.8,
                "seed": 42,
                "verbosity": 0,
            }
            dtrain = xgb.DMatrix(X_tr, label=y_tr, weight=w_tr)
            dval = xgb.DMatrix(X_val, label=y_val)
            model = xgb.train(
                params,
                dtrain,
                num_boost_round=n_estimators,
                evals=[(dval, "val")],
                early_stopping_rounds=50,
                verbose_eval=False,
            )
            model.save_model(os.path.join(output_dir, f"xgboost_fold{fold_idx}_s42.json"))
            ev = _evaluate_model(y_val, model.predict(dval), r_val)
            results["xgboost"].append(ev)
            print(
                f"    XGB: DirAcc={ev['directional_accuracy']:.3f} LongRec={ev['long_recall']:.3f} ShortRec={ev['short_recall']:.3f} trades={len(ev['trade_rs'])}"
            )
        except ImportError:
            pass
    summary = {}
    for mn, fr in results.items():
        if not fr:
            continue
        agg = {}
        for k in ["directional_accuracy", "long_recall", "short_recall"]:
            vals = [f[k] for f in fr]
            agg[f"{k}_mean"] = float(np.mean(vals))
            agg[f"{k}_std"] = float(np.std(vals))
        all_rs: list[float] = []
        for entry in fr:
            all_rs.extend(entry.get("trade_rs", []))
        if all_rs:
            r_arr = np.array(all_rs)
            agg["bt_total_trades"] = len(r_arr)
            agg["bt_win_rate"] = float(np.mean(r_arr > 0))
            agg["bt_total_r"] = float(np.sum(r_arr))
        summary[mn] = agg
    return summary


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="XAU Directional Brain V2 — 35-dim swing_enhanced_35"
    )
    parser.add_argument("--timeframe", default="H1", choices=["H1", "M15", "M30", "H4"])
    parser.add_argument("--horizon", type=int, default=24)
    parser.add_argument(
        "--sl-atr-mult", type=float, default=None, help="Override SL ATR multiplier"
    )
    parser.add_argument(
        "--tp-atr-mult", type=float, default=None, help="Override TP ATR multiplier"
    )
    parser.add_argument("--csv", default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--cv-folds", type=int, default=5)
    parser.add_argument("--skip-train", action="store_true")
    parser.add_argument("--n-estimators", type=int, default=500, help="Max boosting rounds")
    parser.add_argument("--lr", type=float, default=0.02, help="Learning rate")
    parser.add_argument("--max-depth", type=int, default=None, help="Max depth (XGBoost)")
    parser.add_argument("--num-leaves", type=int, default=31, help="Num leaves (LightGBM)")
    args = parser.parse_args()
    tf = args.timeframe
    sl_atr = args.sl_atr_mult if args.sl_atr_mult is not None else SL_ATR_MULT
    tp_atr = args.tp_atr_mult if args.tp_atr_mult is not None else TP_ATR_MULT
    data_dir = args.data_dir or f"data/training/xau_directional_v2_{tf.lower()}"
    csv_path = args.csv or f"data/raw/xauusdc_{tf.lower()}_merged.csv"
    tf_minutes = {"H1": 60.0, "M15": 15.0, "M30": 30.0, "H4": 240.0}[tf]
    np.random.seed(42)
    print(f"{'=' * 60}")
    print(f"XAU Directional Brain V2 — {tf} (35-dim swing_enhanced_35)")
    print(f"  SL={sl_atr}×ATR  TP={tp_atr}×ATR  spread={SPREAD_POINTS}")
    print(f"  data: {csv_path}")
    print(f"  lr={args.lr} n_estimators={args.n_estimators}")
    print(f"{'=' * 60}")
    build_dataset(
        csv_path,
        data_dir,
        args.horizon,
        sl_atr,
        tp_atr,
        SPREAD_POINTS,
        SLIPPAGE_POINTS,
        args.cv_folds,
        args.horizon,
        tf_minutes,
    )
    if not args.skip_train:
        results = train_models(
            data_dir,
            data_dir,
            lr=args.lr,
            n_estimators=args.n_estimators,
            max_depth=args.max_depth,
            num_leaves=args.num_leaves,
        )
        summary = {
            "schema_version": "xau_directional_v2.v1",
            "timeframe": tf,
            "sl_atr_mult": sl_atr,
            "tp_atr_mult": tp_atr,
            "n_features": N_FEATURES,
            "feature_schema": "swing_enhanced_35",
            "models": results,
            "trained_at": datetime.now(UTC).isoformat(),
        }
        with open(os.path.join(data_dir, "training_summary.json"), "w") as f:
            json.dump(summary, f, indent=2)
        print(f"\n{'=' * 60}")
        for mn, mr in results.items():
            print(
                f"  {mn}: DirAcc={mr.get('directional_accuracy_mean', 0):.3f} "
                f"LongRec={mr.get('long_recall_mean', 0):.3f} "
                f"ShortRec={mr.get('short_recall_mean', 0):.3f} "
                f"bt_WR={mr.get('bt_win_rate', 0):.1%}"
            )
        print(f"  Summary: {data_dir}/training_summary.json")


if __name__ == "__main__":
    main()
