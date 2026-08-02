"""
BTC Expected R Two-Tower Dataset Builder.

Phase 1 of the Expected R Regression paradigm:
  - Tower LONG: target_r_long (independent, -1.0 to +R_max)
  - Tower SHORT: target_r_short (independent, -1.0 to +R_max)
  - NO max() merging — each tower learns its own expected return

Key design (per Institutional Review):
  1. Simulate LONG trade: entry=open[i+1]+half_spread+slippage → R_long
  2. Simulate SHORT trade: entry=open[i+1]-half_spread-slippage → R_short
  3. Both outcomes stored independently as two target columns
  4. Curation: ADX>=20 + exclude weekends + ATR p5-p95

Iron Law #11: stdout = only legal evidence source.

Usage:
  python scripts/training/build_btc_expected_r_dataset.py \
    --input data/training/aligned_btc_multitf/btc_m5_aligned_multitf.csv \
    --output-dir data_btc/training/btc_expected_r_v1 \
    --horizon 12 --sl-atr-mult 1.5 --tp-atr-mult 1.5
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
import pandas as pd

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Constants ──
ATR_PERIOD = 14
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
VOL_ZS_LOOKBACK = 20
BB_PERIOD = 20
ADX_PERIOD = 14
OU_LOOKBACK = 20
HURST_MAX_LAG = 20
MIN_WARMUP = (
    max(
        ATR_PERIOD,
        RSI_PERIOD,
        MACD_SLOW + MACD_SIGNAL,
        VOL_ZS_LOOKBACK,
        BB_PERIOD,
        ADX_PERIOD,
        OU_LOOKBACK,
        HURST_MAX_LAG,
    )
    + 5
)


# ═══════════════════════════════════════════════════════════════════
# Technical indicators (extracted from train_btc_swing_v9)
# ═══════════════════════════════════════════════════════════════════


def _atr(h, l, c, period=14):
    n = len(c)
    if n < 2:
        return 0.0
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    return float(np.mean(tr[max(0, n - period) :]))


def _rsi(close, period=14):
    n = len(close)
    if n < period + 1:
        return 50.0
    deltas = np.diff(close[-period - 1 :])
    gains = float(np.sum(deltas[deltas > 0]))
    losses = float(-np.sum(deltas[deltas < 0]))
    if losses == 0:
        return 100.0
    return float(100.0 - 100.0 / (1.0 + gains / losses))


def _macd(close):
    n = len(close)
    if n < 26:
        return 0.0, 0.0, 0.0
    ema12_vals = [float(np.mean(close[:12]))]
    ema26_vals = [float(np.mean(close[:26]))]
    a12, a26 = 2 / 13, 2 / 27
    for i in range(1, n):
        ema12_vals.append(a12 * close[i] + (1 - a12) * ema12_vals[-1])
        ema26_vals.append(a26 * close[i] + (1 - a26) * ema26_vals[-1])
    macd_line = ema12_vals[-1] - ema26_vals[-1]
    return float(macd_line), 0.0, float(macd_line)


def _vol_zscore(close, period=20):
    n = len(close)
    if n < period + 1:
        return 0.0
    returns = np.diff(np.log(close[-period - 1 :] + 1e-12))
    return float((returns[-1] - np.mean(returns)) / (np.std(returns) + 1e-8))


def _bollinger_width(close, period=20):
    n = len(close)
    if n < period:
        return 0.0
    ma = float(np.mean(close[-period:]))
    std = float(np.std(close[-period:]))
    return (ma + 2 * std - (ma - 2 * std)) / ma if ma > 0 else 0.0


def _adx(h, l, c, period=14):
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
    if tr_smooth == 0:
        return 25.0
    plus_di = 100.0 * np.sum(plus_dm[-period:]) / tr_smooth
    minus_di = 100.0 * np.sum(minus_dm[-period:]) / tr_smooth
    di_sum = plus_di + minus_di
    return float(100.0 * abs(plus_di - minus_di) / di_sum) if di_sum > 0 else 25.0


def _ou_theta(price, max_n=200):
    n = len(price)
    if n < 10:
        return 0.0
    log_p = np.log(price[-min(n, max_n) :] + 1e-12)
    y, x = log_p[1:], log_p[:-1]
    mu_x, mu_y = np.mean(x), np.mean(y)
    num = float(np.sum((x - mu_x) * (y - mu_y)))
    den = float(np.sum((x - mu_x) ** 2))
    if den == 0:
        return 0.0
    rho = num / den
    return float(-np.log(max(rho, 1e-8))) if rho > 0 else 0.0


def _hurst(price, max_lag=20, max_n=500):
    n = len(price)
    if n < max_lag * 2:
        return 0.5
    log_p = np.log(price[-min(n, max_n) :] + 1e-12)
    lags = np.arange(2, max_lag + 1)
    rs = np.zeros(len(lags))
    for j, lag in enumerate(lags):
        segs = len(log_p) // lag
        if segs < 2:
            continue
        r_sum = 0.0
        for s in range(min(segs, 10)):
            seg = log_p[s * lag : (s + 1) * lag]
            if len(seg) < 2:
                continue
            mean_seg = np.mean(seg)
            cum_dev = np.cumsum(seg - mean_seg)
            r_val = float(np.max(cum_dev) - np.min(cum_dev))
            s_val = np.std(seg) + 1e-8
            r_sum += r_val / float(s_val)
        rs[j] = r_sum / max(segs, 1)
    valid = rs > 0
    if np.sum(valid) < 3:
        return 0.5
    coeffs = np.polyfit(np.log(lags[valid]), np.log(rs[valid]), 1)
    return float(max(0.0, min(1.0, coeffs[0])))


# ═══════════════════════════════════════════════════════════════════
# Label Simulation: Two-Tower Expected R
# ═══════════════════════════════════════════════════════════════════


def simulate_trade(
    o,
    h,
    l,
    c,
    i,
    horizon,
    entry_price,
    sl_price,
    tp_price,
    direction,
):
    """
    Simulate ONE directional trade.
    TP checked BEFORE SL (favorable outcome first).
    Same-bar TP+SL → ambiguous → outcome=0.

    Returns:
      R_multiple: actual R outcome (continuous)
        - TP hit: positive R = (tp - entry) / (entry - sl)
        - SL hit: -1.0
        - Timeout: (close_at_horizon - entry) / (entry - sl)
    """
    n = len(o)
    end_bar = min(i + 1 + horizon, n)

    for j in range(i + 1, end_bar):
        cur_h, cur_l = h[j], l[j]

        if direction == "long":
            tp_ok = cur_h >= tp_price
            sl_ok = cur_l <= sl_price
        else:
            tp_ok = cur_l <= tp_price
            sl_ok = cur_h >= sl_price

        # Same-bar both → ambiguous
        if tp_ok and sl_ok:
            return 0.0
        if tp_ok:
            r = abs(tp_price - entry_price) / max(abs(entry_price - sl_price), 1e-9)
            return r
        if sl_ok:
            return -1.0

    # Timeout: partial outcome
    close_at_end = c[min(i + horizon, n - 1)]
    if direction == "long":
        r = (close_at_end - entry_price) / max(abs(entry_price - sl_price), 1e-9)
    else:
        r = (entry_price - close_at_end) / max(abs(entry_price - sl_price), 1e-9)
    return r


def compute_two_tower_labels(
    o,
    h,
    l,
    c,
    spreads,
    horizon,
    sl_atr_mult,
    tp_atr_mult,
    slippage_points=0,
    atr_arr=None,  # Pre-computed ATR (vectorized)
):
    """
    Compute Two-Tower labels: target_r_long and target_r_short.
    Uses pre-computed ATR array (vectorized) for O(n) performance.
    """
    n = len(o)
    r_long = np.full(n, np.nan, dtype=np.float32)
    r_short = np.full(n, np.nan, dtype=np.float32)

    if atr_arr is None:
        atr_arr = np.zeros(n)
        for i in range(ATR_PERIOD, n):
            atr_arr[i] = _atr(h[: i + 1], l[: i + 1], c[: i + 1])

    n_long_tp = n_long_sl = n_long_timeout = 0
    n_short_tp = n_short_sl = n_short_timeout = 0

    for i in range(MIN_WARMUP, n - horizon - 1):
        atr_val = atr_arr[i]
        if atr_val <= 0:
            continue

        sl_dist = sl_atr_mult * atr_val
        tp_dist = max(tp_atr_mult * atr_val, sl_dist * 0.3)
        half_sp = spreads[i] / 2.0 if i < len(spreads) else 100.0

        # ── Tower LONG ──
        entry_long = o[i + 1] + half_sp + slippage_points
        sl_long = entry_long - sl_dist
        tp_long = entry_long + tp_dist
        rl = simulate_trade(o, h, l, c, i, horizon, entry_long, sl_long, tp_long, "long")

        if rl > 0:
            n_long_tp += 1
        elif rl < -0.5:
            n_long_sl += 1
        else:
            n_long_timeout += 1
        r_long[i] = rl

        # ── Tower SHORT ──
        entry_short = o[i + 1] - half_sp - slippage_points
        sl_short = entry_short + sl_dist
        tp_short = entry_short - tp_dist
        rs = simulate_trade(o, h, l, c, i, horizon, entry_short, sl_short, tp_short, "short")

        if rs > 0:
            n_short_tp += 1
        elif rs < -0.5:
            n_short_sl += 1
        else:
            n_short_timeout += 1
        r_short[i] = rs

    total_long = n_long_tp + n_long_sl + n_long_timeout
    total_short = n_short_tp + n_short_sl + n_short_timeout

    print("\n  Tower LONG labels:")
    print(f"    TP hit:     {n_long_tp:6d} ({n_long_tp/total_long*100:5.1f}%)")
    print(f"    SL hit:     {n_long_sl:6d} ({n_long_sl/total_long*100:5.1f}%)")
    print(f"    Timeout:    {n_long_timeout:6d} ({n_long_timeout/total_long*100:5.1f}%)")
    print(f"    Mean R:     {np.nanmean(r_long):+.4f}")

    print("\n  Tower SHORT labels:")
    print(f"    TP hit:     {n_short_tp:6d} ({n_short_tp/total_short*100:5.1f}%)")
    print(f"    SL hit:     {n_short_sl:6d} ({n_short_sl/total_short*100:5.1f}%)")
    print(f"    Timeout:    {n_short_timeout:6d} ({n_short_timeout/total_short*100:5.1f}%)")
    print(f"    Mean R:     {np.nanmean(r_short):+.4f}")

    # Bimodal distribution check
    r_long_valid = r_long[~np.isnan(r_long)]
    r_short_valid = r_short[~np.isnan(r_short)]
    print("\n  R distribution (valid samples):")
    print(
        f"    LONG:  n={len(r_long_valid):,},  min={np.min(r_long_valid):.3f},  "
        f"p50={np.median(r_long_valid):.3f},  max={np.max(r_long_valid):.3f}"
    )
    print(
        f"    SHORT: n={len(r_short_valid):,}, min={np.min(r_short_valid):.3f},  "
        f"p50={np.median(r_short_valid):.3f},  max={np.max(r_short_valid):.3f}"
    )

    return r_long, r_short, atr_arr


# ═══════════════════════════════════════════════════════════════════
# Feature Computation (41-dim btc_macro_enhanced)
# ═══════════════════════════════════════════════════════════════════


def compute_features_vectorized(df, tf_minutes=5.0):
    """
    Compute 41-dim btc_macro_enhanced features — VECTORIZED version.
    Uses rolling windows instead of per-bar loops for O(n) performance.

    tf_minutes: timeframe in minutes (5=M5, 15=M15, 30=M30).
                Used to compute bars_per_day for D1/Momentum/Vol_Regime lookbacks.
    """
    n = len(df)
    o = df["open"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64)
    c = df["close"].values.astype(np.float64)
    v = df.get("tick_volume", pd.Series(np.zeros(n))).values.astype(np.float64)
    spreads_raw = df.get("spread", pd.Series([200] * n)).values.astype(np.float64)
    # FIX: MT5 raw points → price dollars (same as train_btc_swing_v9.py:693)
    spreads = spreads_raw / 100.0

    # Dynamic bars-per-day based on timeframe
    bars_per_day = max(1, int(24 * 60 / tf_minutes))

    X = np.zeros((n, 41), dtype=np.float32)

    # Cross-asset return columns (pre-computed in alignment)
    xau_return = df.get("XAUUSDc_return", pd.Series(np.zeros(n))).values
    eur_return = df.get("EURUSDc_return", pd.Series(np.zeros(n))).values
    audjpy_return = df.get("AUDJPYc_return", pd.Series(np.zeros(n))).values
    usdjpy_return = df.get("USDJPYc_return", pd.Series(np.zeros(n))).values

    # BTC-Gold ratio
    xau_close = df.get("XAUUSDc_close", pd.Series(np.ones(n))).values
    btc_gold_ratio = np.where(xau_close > 0, c / xau_close, 0.0)
    btc_gold_ratio_roc = np.zeros(n)
    valid_ratio = btc_gold_ratio[:-1] > 0
    btc_gold_ratio_roc[1:][valid_ratio] = (
        btc_gold_ratio[1:][valid_ratio] - btc_gold_ratio[:-1][valid_ratio]
    ) / btc_gold_ratio[:-1][valid_ratio]

    # ── Calendar features (vectorized) ──
    weekdays = np.array([ts.weekday() if hasattr(ts, "weekday") else 0 for ts in df.index])
    X[:, 16] = np.sin(2 * np.pi * weekdays / 7.0)
    X[:, 17] = np.cos(2 * np.pi * weekdays / 7.0)
    X[:, 20] = np.where(weekdays >= 4, 1.0, 0.0)  # Weekend_Gap
    X[:, 18] = 0.0  # Days_To_MonthEnd
    X[:, 19] = 0.0  # Is_MonthEnd_Week

    # ── Micro features (vectorized) ──
    X[:, 1] = np.abs(c - o) / np.maximum(h - l, 1e-9)  # Body_Ratio
    X[:, 24] = (c - o) / np.maximum(o, 1e-9)  # tick_return
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    X[:, 25] = (h - l) / np.maximum(prev_c, 1e-9)  # hl_ratio
    X[:, 26] = np.abs(c - o) / np.maximum(h - l, 1e-9)  # co_ratio
    X[:, 27] = spreads  # avg_spread
    X[:, 28] = (c - o) / np.maximum(h - l, 1e-9)  # OIM

    # tick_velocity: volume / mean(volume[-20:])
    vol_mean_20 = pd.Series(v).rolling(20, min_periods=1).mean().values
    X[:, 29] = v / np.maximum(vol_mean_20, 1e-8)

    # ── Cross-asset (direct mapping) ──
    X[:, 12] = xau_return  # XAUUSDc_return
    X[:, 14] = eur_return  # Cross_EURUSD_Return
    X[:, 30] = audjpy_return  # AUDJPYc_return
    X[:, 31] = eur_return  # EURUSDc_return
    X[:, 32] = usdjpy_return  # USDJPYc_return

    # ── Rolling technicals (vectorized via pandas) ──
    # True Range
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr_series = pd.Series(tr).rolling(14, min_periods=1).mean()
    X[:, 2] = atr_series.values  # ATR_14

    # RSI (simplified rolling implementation)
    delta = np.diff(c, prepend=c[0])
    gain = np.maximum(delta, 0)
    loss = np.maximum(-delta, 0)
    avg_gain = pd.Series(gain).rolling(14, min_periods=1).mean().values
    avg_loss = pd.Series(loss).rolling(14, min_periods=1).mean().values
    rs = np.divide(avg_gain, avg_loss, out=np.ones_like(avg_gain) * 100, where=avg_loss > 0)
    X[:, 3] = 100.0 - 100.0 / (1.0 + rs)  # RSI_14

    # MACD
    ema12 = pd.Series(c).ewm(span=12, adjust=False).mean().values
    ema26 = pd.Series(c).ewm(span=26, adjust=False).mean().values
    X[:, 4] = ema12 - ema26  # MACD

    # Bollinger Width
    bb_ma = pd.Series(c).rolling(20, min_periods=1).mean().values
    bb_std = pd.Series(c).rolling(20, min_periods=1).std().fillna(0).values
    X[:, 6] = np.divide(2 * bb_std, bb_ma, out=np.zeros_like(bb_ma), where=bb_ma > 0)

    # ADX (simplified)
    plus_dm = np.where((h - prev_c > prev_c - l) & (h - prev_c > 0), h - prev_c, 0.0)
    minus_dm = np.where((prev_c - l > h - prev_c) & (prev_c - l > 0), prev_c - l, 0.0)
    tr_smooth = pd.Series(tr).rolling(14, min_periods=1).sum().values
    plus_smooth = pd.Series(plus_dm).rolling(14, min_periods=1).sum().values
    minus_smooth = pd.Series(minus_dm).rolling(14, min_periods=1).sum().values
    plus_di = 100.0 * plus_smooth / np.maximum(tr_smooth, 1e-8)
    minus_di = 100.0 * minus_smooth / np.maximum(tr_smooth, 1e-8)
    di_sum = plus_di + minus_di
    X[:, 7] = np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / di_sum, 25.0)

    # Vol ZScore
    returns = np.diff(np.log(np.maximum(c, 1e-12)), prepend=0)
    ret_mean_20 = pd.Series(returns).rolling(20, min_periods=1).mean().values
    ret_std_20 = pd.Series(returns).rolling(20, min_periods=1).std().fillna(1e-8).values
    X[:, 5] = (returns - ret_mean_20) / np.maximum(ret_std_20, 1e-8)

    # D1_Ret_1: 1-day return (bars_per_day bars)
    d1_ago = np.roll(c, bars_per_day)
    d1_ago[:bars_per_day] = c[0]
    X[:, 0] = (c - d1_ago) / np.maximum(d1_ago, 1e-9)

    # Vol Regime: ATR / ATR_5d
    atr_5d = pd.Series(tr).rolling(bars_per_day * 5, min_periods=1).mean().values
    X[:, 21] = np.divide(atr_series.values, atr_5d, out=np.ones(n), where=atr_5d > 0)

    # Momentum 5D, 20D
    lookback_5d = bars_per_day * 5
    lookback_20d = bars_per_day * 20
    c_5d_ago = np.roll(c, lookback_5d)
    c_5d_ago[:lookback_5d] = c[0]
    c_20d_ago = np.roll(c, lookback_20d)
    c_20d_ago[:lookback_20d] = c[0]
    X[:, 22] = np.divide(
        c - c_5d_ago, np.maximum(c_5d_ago, 1e-9), out=np.zeros(n), where=c_5d_ago > 0
    )
    X[:, 23] = np.divide(
        c - c_20d_ago, np.maximum(c_20d_ago, 1e-9), out=np.zeros(n), where=c_20d_ago > 0
    )

    # ── TF-specific (OU Theta + Hurst) — every 50th bar, interpolate ──
    ou_vals = np.zeros(n)
    hurst_vals = np.zeros(n)
    last_ou, last_hurst = 0.0, 0.5
    for i in range(0, n, 50):  # Every 50 bars
        end = max(i + 1, 20)
        price_slice = c[max(0, i - 200) : end]
        ou_vals[i] = _ou_theta(price_slice)
        hurst_vals[i] = _hurst(price_slice)
    # Forward fill
    for i in range(1, n):
        if ou_vals[i] == 0.0:
            ou_vals[i] = ou_vals[i - 1]
        if hurst_vals[i] == 0.0:
            hurst_vals[i] = hurst_vals[i - 1]
    X[:, 33] = ou_vals
    X[:, 34] = hurst_vals

    # Regime derivatives
    X[0, 35] = 0.0
    X[0, 36] = 0.0
    X[1:, 35] = X[1:, 33] - X[:-1, 33]  # TF_delta_OU
    X[1:, 36] = X[1:, 34] - X[:-1, 34]  # TF_delta_Hurst
    X[:, 37] = X[:, 33] * X[:, 34]  # TF_OU_x_Hurst
    X[:, 38] = np.divide(X[:, 33], X[:, 7] + 1e-8)  # TF_OU_div_ADX

    # ── BTC-specific ──
    X[:, 39] = btc_gold_ratio
    X[:, 40] = btc_gold_ratio_roc

    # V4: Fix Cross_DXY_Return — DXY ≈ -EURUSD (DXY not directly available on MT5 retail)
    X[:, 13] = -eur_return  # Cross_DXY_Return (was placeholder zero-fill)

    # V4: Fix Cross_Risk_On_Off — XAU 5d momentum vs BTC 5d momentum (risk appetite proxy)
    xau_close_raw = df.get("XAUUSDc_close", pd.Series(np.ones(n))).values
    xau_5d_ago = np.roll(xau_close_raw, lookback_5d)
    xau_5d_ago[:lookback_5d] = xau_close_raw[0]
    xau_5d_mom = np.divide(
        xau_close_raw - xau_5d_ago,
        np.maximum(xau_5d_ago, 1e-9),
        out=np.zeros(n),
        where=xau_5d_ago > 0,
    )
    X[:, 15] = xau_5d_mom - X[:, 22]  # Cross_Risk_On_Off = XAU_5d_mom - BTC_5d_mom

    # V4: Fix calendar features (were zero-fill)
    # Days_To_MonthEnd
    month_ends = pd.Series(df.index).apply(lambda ts: ts.days_in_month - ts.day).values
    X[:, 18] = month_ends.astype(np.float32) / 31.0  # normalize to [0, 1]
    # Is_MonthEnd_Week: 1.0 if last 5 trading days of month
    X[:, 19] = np.where(month_ends <= 5, 1.0, 0.0).astype(np.float32)

    # V4: Physically delete 4 H4 placeholder features (indices 8-11).
    # These require multi-TF H4 alignment infrastructure not yet built.
    # Keep indices 0-7, 12-40 → 37 features.
    X = X[:, list(range(8)) + list(range(12, 41))]

    # NaN safety
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

    return X, o, h, l, c, spreads


# ═══════════════════════════════════════════════════════════════════
# Main pipeline
# ═══════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Build BTC Two-Tower Expected R dataset")
    parser.add_argument(
        "--input", default="data/training/aligned_btc_multitf/btc_m5_aligned_multitf.csv"
    )
    parser.add_argument("--output-dir", default="data_btc/training/btc_expected_r_v1")
    parser.add_argument("--horizon", type=int, default=12, help="Forward horizon in bars")
    parser.add_argument("--sl-atr-mult", type=float, default=1.5, help="SL multiplier × ATR")
    parser.add_argument("--tp-atr-mult", type=float, default=1.5, help="TP multiplier × ATR")
    parser.add_argument("--slippage-points", type=float, default=0, help="Slippage in price points")
    parser.add_argument("--min-adx", type=float, default=20, help="Min ADX for curation")
    parser.add_argument("--exclude-weekends", action="store_true", default=True)
    parser.add_argument("--atr-pctile-low", type=float, default=5)
    parser.add_argument("--atr-pctile-high", type=float, default=95)
    parser.add_argument(
        "--tf-minutes", type=float, default=5.0, help="Timeframe in minutes (5=M5, 15=M15, 30=M30)"
    )
    args = parser.parse_args()

    input_path = PROJECT_ROOT / args.input
    output_dir = PROJECT_ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("Phase 1: Two-Tower Expected R Dataset Builder")
    print(f"  Input:    {input_path}")
    print(f"  Output:   {output_dir}")
    print(f"  Horizon:  {args.horizon} bars")
    print(f"  SL/TP:    {args.sl_atr_mult}× / {args.tp_atr_mult}× ATR")
    print(f"  Time:     {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 80)

    # Load aligned data
    print("\n[1] Loading aligned data...")
    df = pd.read_csv(input_path)
    df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time").sort_index()
    print(f"    {len(df):,} bars: {df.index[0]} → {df.index[-1]}")

    # V4: Extract timestamps for time-decay weighting
    timestamps = df.index.astype(np.int64).values // 10**9  # ns → unix seconds
    timestamps = timestamps.astype(np.float64)
    # Compute features
    print(
        f"\n[2] Computing 41-dim features (TF={args.tf_minutes:.0f}min, {int(24*60/args.tf_minutes)} bars/day)..."
    )
    X, o, h, l, c, spreads = compute_features_vectorized(df, tf_minutes=args.tf_minutes)
    print(f"    Feature matrix: {X.shape}")

    # Compute Two-Tower labels (use pre-computed vectorized ATR)
    print("\n[3] Computing Two-Tower labels...")
    precomputed_atr = X[:, 2].copy()  # ATR_14 from feature matrix
    r_long, r_short, atr_arr = compute_two_tower_labels(
        o,
        h,
        l,
        c,
        spreads,
        horizon=args.horizon,
        sl_atr_mult=args.sl_atr_mult,
        tp_atr_mult=args.tp_atr_mult,
        slippage_points=args.slippage_points,
        atr_arr=precomputed_atr,
    )

    # ── Curation ──
    print("\n[4] Applying curation filters...")
    n_before = len(X)

    # Mask: valid labels (not NaN)
    valid_mask = ~np.isnan(r_long) & ~np.isnan(r_short)

    # ADX filter
    adx_vals = X[:, 7]
    adx_mask = adx_vals >= args.min_adx

    # Weekend exclusion
    if args.exclude_weekends:
        weekday_sin = X[:, 16]
        weekday_cos = X[:, 17]
        # weekday=5 (Sat) or 6 (Sun): sin/cos where cos > 0.62 or sin near 0.78/-0.78
        # Simpler: check both sin and cos ranges
        weekend_mask = ~(
            ((weekday_sin > 0.7) & (weekday_cos > 0.6))  # Saturday
            | ((weekday_sin > 0.7) & (weekday_cos < -0.2))  # Sunday
        )
        # Even simpler: check Weekend_Gap feature
        weekend_mask = X[:, 20] < 0.5  # Weekend_Gap < 0.5 → weekday
    else:
        weekend_mask = np.ones(n_before, dtype=bool)

    # ATR percentile filter
    atr_values = X[:, 2]
    atr_low = np.percentile(atr_values[atr_values > 0], args.atr_pctile_low)
    atr_high = np.percentile(atr_values[atr_values > 0], args.atr_pctile_high)
    atr_mask = (atr_values >= atr_low) & (atr_values <= atr_high)

    # Warmup mask (first MIN_WARMUP bars have unconverged features)
    warmup_mask = np.arange(n_before) >= MIN_WARMUP

    # Combined mask
    curation_mask = valid_mask & adx_mask & weekend_mask & atr_mask & warmup_mask

    X_curated = X[curation_mask]
    r_long_curated = r_long[curation_mask]
    r_short_curated = r_short[curation_mask]
    atr_curated = atr_arr[curation_mask]
    timestamps_curated = timestamps[curation_mask]  # V4: time-decay weighting

    n_after = len(X_curated)
    print(f"    Before curation: {n_before:,}")
    print(f"    After curation:  {n_after:,} ({n_after/n_before*100:.1f}%)")
    print("    Dropped by:")
    print(f"      Invalid labels: {(~valid_mask).sum():,}")
    print(f"      ADX < {args.min_adx}: {(~adx_mask & valid_mask).sum():,}")
    print(f"      Weekend:         {(~weekend_mask & valid_mask).sum():,}")
    print(f"      ATR percentile:  {(~atr_mask & valid_mask).sum():,}")
    print(f"      Warmup:          {(~warmup_mask).sum():,}")

    # ── Train/Val/Test split (time-based, no look-ahead) ──
    print("\n[5] Creating time-based splits...")
    n_total = n_after
    train_end = int(n_total * 0.70)
    val_end = int(n_total * 0.85)

    # Add purge gap at boundaries
    purge = args.horizon

    X_train = X_curated[: train_end - purge]
    y_train_long = r_long_curated[: train_end - purge]
    y_train_short = r_short_curated[: train_end - purge]
    ts_train = timestamps_curated[: train_end - purge]  # V4

    X_val = X_curated[train_end : val_end - purge]
    y_val_long = r_long_curated[train_end : val_end - purge]
    y_val_short = r_short_curated[train_end : val_end - purge]
    ts_val = timestamps_curated[train_end : val_end - purge]  # V4

    X_test = X_curated[val_end:]
    y_test_long = r_long_curated[val_end:]
    y_test_short = r_short_curated[val_end:]
    ts_test = timestamps_curated[val_end:]  # V4

    print(f"    Train: {len(X_train):,} ({len(X_train)/n_total*100:.0f}%)")
    print(f"    Val:   {len(X_val):,} ({len(X_val)/n_total*100:.0f}%)")
    print(f"    Test:  {len(X_test):,} ({len(X_test)/n_total*100:.0f}%)")

    # ── Save ──
    print("\n[6] Saving NPZ datasets...")
    np.savez_compressed(
        output_dir / "train.npz",
        X=X_train,
        y_long=y_train_long,
        y_short=y_train_short,
        timestamps=ts_train,  # V4: time-decay weighting
    )
    np.savez_compressed(
        output_dir / "val.npz",
        X=X_val,
        y_long=y_val_long,
        y_short=y_val_short,
        timestamps=ts_val,
    )
    np.savez_compressed(
        output_dir / "test.npz",
        X=X_test,
        y_long=y_test_long,
        y_short=y_test_short,
        timestamps=ts_test,
    )

    # Metadata
    meta = {
        "schema_version": "expected_r_two_tower.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "input_file": str(input_path),
        "horizon": args.horizon,
        "sl_atr_mult": args.sl_atr_mult,
        "tp_atr_mult": args.tp_atr_mult,
        "tf_minutes": args.tf_minutes,
        "n_features": 37,  # V4: 41 → 37 (deleted 4 H4 placeholders)
        "n_train": len(X_train),
        "n_val": len(X_val),
        "n_test": len(X_test),
        "feature_names": [
            "D1_Ret_1",
            "D1_Body_Ratio",
            "D1_ATR_14",
            "D1_RSI_14",
            "D1_MACD",
            "D1_Vol_ZScore",
            "D1_Bollinger_Width",
            "D1_ADX_14",
            # V4: "H4_Trend_Strength", "H4_ATR_Ratio", "H4_RSI_Divergence", "H4_vs_D1_Alignment" PHYSICALLY DELETED
            "XAUUSDc_return",
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
            "tick_return",
            "hl_ratio",
            "co_ratio",
            "avg_spread",
            "OIM",
            "tick_velocity",
            "AUDJPYc_return",
            "EURUSDc_return",
            "USDJPYc_return",
            "TF_OU_Theta",
            "TF_Hurst",
            "TF_delta_OU",
            "TF_delta_Hurst",
            "TF_OU_x_Hurst",
            "TF_OU_div_ADX",
            "Cross_BTC_Gold_Ratio",
            "Cross_BTC_Gold_Ratio_ROC",
        ],
        "targets": {
            "y_long": "Expected R for LONG trade (continuous, -1.0 to +R_max)",
            "y_short": "Expected R for SHORT trade (continuous, -1.0 to +R_max)",
        },
        "curation": {
            "min_adx": args.min_adx,
            "exclude_weekends": args.exclude_weekends,
            "atr_pctile_low": args.atr_pctile_low,
            "atr_pctile_high": args.atr_pctile_high,
        },
    }
    with open(output_dir / "meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    print(f"    {output_dir}/train.npz: {X_train.shape}")
    print(f"    {output_dir}/val.npz:   {X_val.shape}")
    print(f"    {output_dir}/test.npz:  {X_test.shape}")
    print(f"    {output_dir}/meta.json")

    print("\n[Phase 1 COMPLETE] Two-Tower dataset ready.")
    print("  Next: python scripts/training/train_btc_expected_r.py")


if __name__ == "__main__":
    main()
