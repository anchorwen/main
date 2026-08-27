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


def compute_features_vectorized(df, tf_minutes=5.0, schema_name="btc_expected_r_37"):
    """
    Compute the feature matrix via the SHARED pure assembly (SSOT path).

    DQAF-20260827-002 / Phase 2 (The Great Unification): the inline hand-rolled
    41-dim builder (a THIRD divergent implementation with its own slot mapping,
    OU/Hurst and regime-delta logic) is REMOVED.  Features now flow through
    ``core/training/feature_replay`` → ``assemble_41_series`` — the exact code
    path live inference uses.

    ``tf_minutes`` is the bar timeframe (5=M5, 15=M15, 30=M30, 60=H1).  When
    ``tf_minutes > 5`` the M5 bars are resampled to the target TF via the live
    ``MicrostructureFeatureComputer._resample_ohlc`` before feature computation
    (A3: the training slice is mathematically identical to what
    ``_mtf_price_service`` reconstructs for live).

    Returns:
        (X, o, h, l, c, spreads) where X is (n, dim) float32 and ``o/h/l/c`` /
        ``spreads`` are the (resampled, when tf_minutes > 5) bars used to build
        the features — so the caller computes LABELS on the SAME slice.
    """
    from core.training.feature_replay import (
        compute_replay_components,
        extract_schema_subset,
        replay_features_41,
    )

    comp = compute_replay_components(df, tf_minutes=tf_minutes)
    x41 = replay_features_41(comp)
    X = extract_schema_subset(x41, schema_name).astype(np.float32)
    return X, comp.o, comp.h, comp.l, comp.c, comp.spreads


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
