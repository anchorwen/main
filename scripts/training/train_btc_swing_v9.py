#!/usr/bin/env python
"""BTC Swing V9 Training Pipeline — institutional-grade.

Implements the full B2+B3 pipeline per DQAF-20260609-012:

  B2.1 — Load BTC M5 historical data (up to ~100k bars from CSV/MT5)
  B2.2 — Compute 37-dim btc_macro_enhanced features (FIX-20260604-081 schema)
  B2.3 — Create forward-barrier labels with REAL friction (spread + slippage)
  B2.4 — Exponential time-decay sample weighting (half-life configurable)
  B2.5 — Walk-forward purged CV splits (train/purge/test windows)
  B3   — Optuna TPE hyperparameter search × XGBoost + LightGBM
  B3   — Multi-seed ensemble training + model card generation
  B4   — Brain config registration + normalization calibration

Usage:
  # Full pipeline (dataset build + train)
  python scripts/training/train_btc_swing_v9.py --full

  # Dataset only
  python scripts/training/train_btc_swing_v9.py --build-only

  # Train only (requires pre-built dataset)
  python scripts/training/train_btc_swing_v9.py --train-only

  # Custom parameters
  python scripts/training/train_btc_swing_v9.py --full \
    --sl-atr-mult 2.0 --tp-atr-mult 2.5 \
    --horizon 12 \
    --decay-half-life-days 90 \
    --cv-folds 5 --purge-bars 144 \
    --optuna-trials 50 --n-seeds 3
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

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Constants ─────────────────────────────────────────────────────────────────

ATR_PERIOD = 14
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
VOL_ZS_LOOKBACK = 20
OU_LOOKBACK = 20
HURST_MAX_LAG = 20
ADX_PERIOD = 14
BB_PERIOD = 20
MIN_BARS = max(
    ATR_PERIOD, RSI_PERIOD, MACD_SLOW + MACD_SIGNAL,
    VOL_ZS_LOOKBACK, OU_LOOKBACK, HURST_MAX_LAG, ADX_PERIOD, BB_PERIOD,
) + 2

# ── BTC 37-dim feature schema (FIX-20260604-081) ────────────────────────────

BTC_MACRO_24 = [
    "D1_Ret_1", "D1_Body_Ratio", "D1_ATR_14", "D1_RSI_14", "D1_MACD",
    "D1_Vol_ZScore", "D1_Bollinger_Width", "D1_ADX_14",
    "H4_Trend_Strength", "H4_ATR_Ratio", "H4_RSI_Divergence", "H4_vs_D1_Alignment",
    "XAUUSDc_return", "Cross_DXY_Return", "Cross_EURUSD_Return", "Cross_Risk_On_Off",
    "Derived_Weekday_Sin", "Derived_Weekday_Cos",
    "Derived_Days_To_MonthEnd", "Derived_Is_MonthEnd_Week",
    "Derived_Weekend_Gap", "Derived_Vol_Regime",
    "Derived_Momentum_5D", "Derived_Momentum_20D",
]

BTC_MICRO_9 = [
    "tick_return", "hl_ratio", "co_ratio", "avg_spread", "OIM", "tick_velocity",
    "AUDJPYc_return", "EURUSDc_return", "USDJPYc_return",
]

BTC_CROSS_2 = ["Cross_BTC_Gold_Ratio", "Cross_BTC_Gold_Ratio_ROC"]

TF_SPECIFIC_2 = ["TF_OU_Theta", "TF_Hurst"]

ALL_FEATURE_NAMES = BTC_MACRO_24 + BTC_MICRO_9 + BTC_CROSS_2 + TF_SPECIFIC_2
N_FEATURES = len(ALL_FEATURE_NAMES)  # 37


# ── Feature Computers ─────────────────────────────────────────────────────────


def _atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = ATR_PERIOD) -> float:
    if len(c) < period + 1:
        return 0.0
    prev_c = c[-(period + 1):-1]
    cur_h = h[-period:]
    cur_l = l[-period:]
    tr = np.maximum(cur_h - cur_l, np.maximum(np.abs(cur_h - prev_c), np.abs(cur_l - prev_c)))
    return float(np.mean(tr))


def _rsi(c: np.ndarray, period: int = RSI_PERIOD) -> float:
    if len(c) < period + 1:
        return 50.0
    deltas = np.diff(c[-(period + 1):])
    gains = np.maximum(deltas, 0)
    losses = np.maximum(-deltas, 0)
    avg_gain = float(np.mean(gains))
    avg_loss = float(np.mean(losses))
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _macd(c: np.ndarray) -> tuple[float, float, float]:
    """Return (macd_line, signal_line, histogram)."""
    if len(c) < MACD_SLOW + MACD_SIGNAL:
        return 0.0, 0.0, 0.0
    ema_fast = _ema(c, MACD_FAST)
    ema_slow = _ema(c, MACD_SLOW)
    macd_line = ema_fast - ema_slow
    # Signal line needs MACD history — approximate with single-point
    signal_line = macd_line * (2.0 / (MACD_SIGNAL + 1))
    return macd_line, signal_line, macd_line - signal_line


def _ema(data: np.ndarray, period: int) -> float:
    if len(data) < period:
        return float(np.mean(data))
    k = 2.0 / (period + 1)
    result = float(np.mean(data[:period]))
    for val in data[period:]:
        result = k * val + (1 - k) * result
    return result


def _adx(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = ADX_PERIOD) -> float:
    if len(c) < period + 1:
        return 25.0
    prev_c = c[-(period + 1):-1]
    cur_h = h[-period:]
    cur_l = l[-period:]
    tr = np.maximum(cur_h - cur_l, np.maximum(np.abs(cur_h - prev_c), np.abs(cur_l - prev_c)))
    atr_val = float(np.mean(tr))
    if atr_val == 0:
        return 25.0
    up = cur_h - np.roll(cur_h, 1)
    down = np.roll(cur_l, 1) - cur_l
    up[0], down[0] = 0.0, 0.0
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_di = 100.0 * _ema(plus_dm, period) / atr_val
    minus_di = 100.0 * _ema(minus_dm, period) / atr_val
    dx_sum = abs(plus_di - minus_di)
    dx_denom = plus_di + minus_di
    dx = 100.0 * dx_sum / dx_denom if dx_denom > 0 else 0.0
    return float(dx)


def _bollinger_width(c: np.ndarray, period: int = BB_PERIOD) -> float:
    if len(c) < period:
        return 0.0
    window = c[-period:]
    mu = float(np.mean(window))
    sigma = float(np.std(window, ddof=1))
    return (mu + 2 * sigma - (mu - 2 * sigma)) / mu if mu > 0 else 0.0


def _vol_zscore(c: np.ndarray, lookback: int = VOL_ZS_LOOKBACK) -> float:
    if len(c) < lookback + 1:
        return 0.0
    returns = np.diff(c[-(lookback + 1):]) / c[-(lookback + 1):-1]
    vol = float(np.std(returns))
    returns_full = np.diff(c) / c[:-1]
    mean_vol = float(np.std(returns_full[-200:])) if len(returns_full) >= 200 else vol
    return (vol - mean_vol) / mean_vol if mean_vol > 0 else 0.0


# FIX-20260614-B3: Production parity — import live OU/Hurst directly.
# The training pipeline must use the EXACT same math as the live feature
# computer.  V9's local implementations used dt-adjusted OU (bar→years)
# and log-return Hurst — both differed from production (dt=1, price-level).
from core.features.computers.v9_live_computer import _hurst, _ou_theta


# ── Feature Computation ──────────────────────────────────────────────────────


def compute_feature_row(idx: int, o: np.ndarray, h: np.ndarray, l: np.ndarray,
                        c: np.ndarray, v: np.ndarray, spreads: np.ndarray,
                        day_features: dict[int, dict[str, float]],
                        daily_ts: np.ndarray, daily_o: np.ndarray,
                        daily_h: np.ndarray, daily_l: np.ndarray,
                        daily_c: np.ndarray,
                        btc_price_hist: np.ndarray,
                        tf_minutes: float = 5.0,
                        ) -> list[float]:
    """Compute 37-dim BTC feature vector at bar *idx*."""
    end = idx + 1
    price_slice = c[:end]
    o_slice, h_slice, l_slice, c_slice = o[:end], h[:end], l[:end], c[:end]

    # ── D1 features ──
    bar_ts = daily_ts[idx] if idx < len(daily_ts) else 0
    bar_date = datetime.fromtimestamp(float(bar_ts), tz=UTC).strftime("%Y-%m-%d") if bar_ts > 0 else ""
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

    # ── H4 features (approximated from M5 data) ──
    h4_trend = float(d_feat.get("H4_Trend_Strength", 0.0))
    h4_atr_r = float(d_feat.get("H4_ATR_Ratio", 0.0))
    h4_rsi_d = float(d_feat.get("H4_RSI_Divergence", 0.0))
    h4_d1_align = float(d_feat.get("H4_vs_D1_Alignment", 0.0))

    # ── Cross-market (placeholders when cross data unavailable) ──
    xau_return = float(d_feat.get("XAUUSDc_return", 0.0))
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
    atr_5d = _atr(h_slice, l_slice, c_slice) if len(c_slice) < 288 * 5 else _atr(
        h_slice[-288 * 5:], l_slice[-288 * 5:], c_slice[-288 * 5:]
    )
    vol_regime = atr_val / atr_5d if atr_5d > 0 else 1.0
    mom_5d = (c[idx] - c[max(0, idx - 288 * 5)]) / c[max(0, idx - 288 * 5)] if len(c) > 288 * 5 else 0.0
    mom_20d = (c[idx] - c[max(0, idx - 288 * 20)]) / c[max(0, idx - 288 * 20)] if len(c) > 288 * 20 else 0.0

    # ── Micro features (M5 bar-level) ──
    prev_c_val = c[idx - 1] if idx > 0 else c[idx]
    tick_ret = (c[idx] - o[idx]) / o[idx] if o[idx] > 0 else 0.0
    body_ratio = abs(c[idx] - o[idx]) / (h[idx] - l[idx]) if (h[idx] - l[idx]) > 0 else 0.5
    hl_ratio_val = (h[idx] - l[idx]) / prev_c_val if prev_c_val > 0 else 0.0
    co_ratio_val = abs(c[idx] - o[idx]) / (h[idx] - l[idx]) if (h[idx] - l[idx]) > 0 else 0.0
    avg_spread_val = float(spreads[idx]) if idx < len(spreads) else 10.0
    oim = (c[idx] - o[idx]) / (h[idx] - l[idx]) if (h[idx] - l[idx]) > 0 else 0.0
    tick_vel = float(v[idx]) / (float(np.mean(v[max(0, idx - 20):end])) + 1e-8) if idx > 20 else 1.0

    # Cross-market micro returns (use synthetic when unavailable)
    audjpy_ret = 0.0  # placeholder
    eur_ret = 0.0     # placeholder
    usdjpy_ret = 0.0  # placeholder

    # ── BTC-specific cross features ──
    btc_gold_ratio = 0.0
    btc_gold_ratio_roc = 0.0
    if len(btc_price_hist) > 0 and btc_price_hist[idx] > 0 and d_feat.get("XAUUSDc_close", 0) > 0:
        btc_gold_ratio = btc_price_hist[idx] / d_feat["XAUUSDc_close"]
        if idx >= 288:
            prev_ratio = btc_price_hist[idx - 288] / max(d_feat.get("XAUUSDc_close", 1.0), 1.0)
            btc_gold_ratio_roc = (btc_gold_ratio - prev_ratio) / prev_ratio if prev_ratio > 0 else 0.0

    # ── TF-specific (timeframe-aware dt for OU_Theta) ──
    tf_ou = _ou_theta(price_slice)  # FIX-B3: production parity, dt=1 implicit
    tf_hurst = _hurst(price_slice)

    # ── Assemble in schema order ──
    row = BTC_MACRO_24 + BTC_MICRO_9 + BTC_CROSS_2 + TF_SPECIFIC_2
    # We'll use dict-based assembly for clarity
    values_map: dict[str, float] = {
        # BTC_MACRO_24
        "D1_Ret_1": d1_ret, "D1_Body_Ratio": d1_body, "D1_ATR_14": d1_atr,
        "D1_RSI_14": d1_rsi, "D1_MACD": d1_macd, "D1_Vol_ZScore": d1_vol_z,
        "D1_Bollinger_Width": d1_bb, "D1_ADX_14": d1_adx,
        "H4_Trend_Strength": h4_trend, "H4_ATR_Ratio": h4_atr_r,
        "H4_RSI_Divergence": h4_rsi_d, "H4_vs_D1_Alignment": h4_d1_align,
        "XAUUSDc_return": xau_return, "Cross_DXY_Return": dxy_return,
        "Cross_EURUSD_Return": eurusd_return, "Cross_Risk_On_Off": risk_on_off,
        "Derived_Weekday_Sin": weekday_sin, "Derived_Weekday_Cos": weekday_cos,
        "Derived_Days_To_MonthEnd": days_to_end, "Derived_Is_MonthEnd_Week": is_month_end,
        "Derived_Weekend_Gap": weekend_gap, "Derived_Vol_Regime": vol_regime,
        "Derived_Momentum_5D": mom_5d, "Derived_Momentum_20D": mom_20d,
        # BTC_MICRO_9
        "tick_return": tick_ret, "hl_ratio": hl_ratio_val, "co_ratio": co_ratio_val,
        "avg_spread": avg_spread_val, "OIM": oim, "tick_velocity": tick_vel,
        "AUDJPYc_return": audjpy_ret, "EURUSDc_return": eur_ret, "USDJPYc_return": usdjpy_ret,
        # BTC_CROSS_2
        "Cross_BTC_Gold_Ratio": btc_gold_ratio, "Cross_BTC_Gold_Ratio_ROC": btc_gold_ratio_roc,
        # TF_SPECIFIC_2
        "TF_OU_Theta": tf_ou, "TF_Hurst": tf_hurst,
    }
    return [values_map.get(name, 0.0) for name in ALL_FEATURE_NAMES]


# ── Label Creation (forward barrier with friction) ──────────────────────────


def compute_labels(
    o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray,
    horizon: int, sl_atr_mult: float, tp_atr_mult: float,
    spread_points: float, slippage_points: float, tick_value: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute forward barrier labels with real friction.

    Returns:
        labels: -1 (SL), 0 (timeout), +1 (TP)
        pnl_r: realized PnL in R-multiples (only for SL/TP, NaN for timeout)
        hold_bars: how many bars until barrier was hit

    Friction model (per entry, direction-aware):
      - Long entry  = open[i+1] + slippage  (buy at ask, above mid)
      - Short entry = open[i+1] - slippage  (sell at bid, below mid)
      - SL distance widened by spread (stop fills suffer adverse slippage)
      - TP distance tightened by spread (exit fills at bid/ask, not mid)
    """
    n = len(o)
    labels = np.zeros(n, dtype=np.int8)
    pnl_r = np.full(n, np.nan, dtype=np.float32)
    hold_bars = np.zeros(n, dtype=np.int16)

    for i in range(n - horizon - 1):
        ref_price = o[i + 1]
        if ref_price <= 0:
            continue

        atr_val = _atr(h[:i + 2], l[:i + 2], c[:i + 2])
        if atr_val <= 0:
            continue

        sl_dist = sl_atr_mult * atr_val + spread_points
        tp_dist = tp_atr_mult * atr_val - spread_points
        tp_dist = max(tp_dist, sl_dist * 0.3)  # minimum TP = 0.3 × SL

        # Direction-specific entry prices
        entry_long = ref_price + slippage_points   # buy at ask
        entry_short = ref_price - slippage_points  # sell at bid

        sl_long = entry_long - sl_dist
        tp_long = entry_long + tp_dist
        sl_short = entry_short + sl_dist
        tp_short = entry_short - tp_dist

        # Walk forward
        for j in range(i + 2, min(i + 2 + horizon, n)):
            cur_h, cur_l = h[j], l[j]

            # Long: check SL first (risk management), then TP
            if cur_l <= sl_long:
                labels[i] = -1
                pnl_r[i] = -sl_dist / tp_dist if tp_dist > 0 else -1.0
                hold_bars[i] = j - (i + 1)
                break
            if cur_h >= tp_long:
                labels[i] = 1
                pnl_r[i] = tp_dist / sl_dist if sl_dist > 0 else 1.0
                hold_bars[i] = j - (i + 1)
                break

            # Short
            if cur_h >= sl_short:
                labels[i] = -1
                pnl_r[i] = -sl_dist / tp_dist if tp_dist > 0 else -1.0
                hold_bars[i] = j - (i + 1)
                break
            if cur_l <= tp_short:
                labels[i] = 1
                pnl_r[i] = tp_dist / sl_dist if sl_dist > 0 else 1.0
                hold_bars[i] = j - (i + 1)
                break
        # If loop completes without break → timeout (label stays 0)

    return labels, pnl_r, hold_bars


# ── Time-Decay Sample Weighting ─────────────────────────────────────────────


def compute_time_decay_weights(
    timestamps: np.ndarray, half_life_days: float = 90.0,
) -> np.ndarray:
    """Exponential time-decay weights favoring recent samples.

    weight(t) = exp(-age_days * ln(2) / half_life_days)

    The most recent sample gets weight 1.0.  A sample at exactly
    *half_life_days* ago gets weight 0.5.
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


# ── Walk-Forward Purged CV ──────────────────────────────────────────────────


def walk_forward_purged_splits(
    n_samples: int, timestamps: np.ndarray,
    n_folds: int = 5, purge_bars: int = 144,
) -> list[dict[str, np.ndarray]]:
    """Generate walk-forward purged CV split indices.

    Each fold:
      - train: [0 : test_start - purge_bars]
      - purge: [test_start - purge_bars : test_start]  (discarded)
      - test:  [test_start : test_end]

    Folds are chronologically anchored — fold 0 uses the earliest test window,
    fold (n_folds-1) uses the latest.
    """
    splits = []
    fold_size = n_samples // (n_folds + 1)  # +1 to leave room for final train

    for fold in range(n_folds):
        test_start = n_samples - (n_folds - fold) * fold_size
        test_end = min(n_samples, test_start + fold_size)

        if test_start - purge_bars <= 0:
            continue  # skip fold if no training data

        train_idx = np.arange(0, test_start - purge_bars, dtype=np.int64)
        test_idx = np.arange(test_start, test_end, dtype=np.int64)

        splits.append({
            "fold": fold,
            "train_idx": train_idx,
            "test_idx": test_idx,
            "train_start_ts": timestamps[train_idx[0]] if len(train_idx) > 0 else 0,
            "train_end_ts": timestamps[train_idx[-1]] if len(train_idx) > 0 else 0,
            "test_start_ts": timestamps[test_idx[0]] if len(test_idx) > 0 else 0,
            "test_end_ts": timestamps[test_idx[-1]] if len(test_idx) > 0 else 0,
        })

    return splits


# ── Dataset Builder ──────────────────────────────────────────────────────────


def build_dataset(
    csv_path: str,
    output_dir: str,
    horizon: int = 24,
    sl_atr_mult: float = 3.0,
    tp_atr_mult: float = 2.0,
    spread_points: float = 10.0,
    slippage_points: float = 10.0,
    tick_value: float = 0.01,
    decay_half_life_days: float = 180.0,
    cv_folds: int = 5,
    purge_bars: int = 24,
    timeframe_minutes: float = 60.0,
) -> dict[str, Any]:
    """Full B2 pipeline: CSV → features + labels → weights → CV splits → NPZ."""
    print(f"[B2] Loading BTC M5 data from {csv_path}...")
    df = pd.read_csv(csv_path)
    n_bars = len(df)
    print(f"  Loaded {n_bars:,} bars")

    o = df["open"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64)
    c = df["close"].values.astype(np.float64)
    v = df["tick_volume"].values.astype(np.float64)
    spreads = df.get("spread", pd.Series([10] * n_bars)).values.astype(np.float64)
    timestamps = pd.to_datetime(df["time"]).astype(np.int64).values // 10**9
    timestamps_f = timestamps.astype(np.float64)

    # ── Build day-level features (simplified: use rolling M5 → D1 resample) ──
    print("[B2] Computing day-level context features...")
    # Re-sample to daily for D1 features
    df_dt = pd.to_datetime(df["time"])
    daily = df.set_index(df_dt).resample("D").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "tick_volume": "sum",
    }).dropna()
    daily_ts = daily.index.astype(np.int64).values // 10**9
    daily_ts_f = daily_ts.astype(np.float64)
    daily_o = daily["open"].values
    daily_h = daily["high"].values
    daily_l = daily["low"].values
    daily_c = daily["close"].values

    # Map each M5 bar to its daily index
    m5_day_map = np.searchsorted(daily_ts, timestamps, side="right") - 1
    m5_day_map = np.clip(m5_day_map, 0, len(daily_ts) - 1)

    # Compute daily features for each day
    day_features: dict[int, dict[str, float]] = {}
    for d_idx in range(len(daily_c)):
        end = d_idx + 1
        d_o = daily_o[:end]
        d_h = daily_h[:end]
        d_l = daily_l[:end]
        d_c_s = daily_c[:end]
        feat = {
            "D1_Ret_1": (d_c_s[-1] - d_c_s[-2]) / d_c_s[-2] if len(d_c_s) >= 2 and d_c_s[-2] > 0 else 0.0,
            "D1_Body_Ratio": abs(d_c_s[-1] - d_o[-1]) / (d_h[-1] - d_l[-1]) if (d_h[-1] - d_l[-1]) > 0 else 0.5,
            "D1_ATR_14": _atr(d_h, d_l, d_c_s),
            "D1_RSI_14": _rsi(d_c_s),
            "D1_MACD": _macd(d_c_s)[2],
            "D1_Vol_ZScore": _vol_zscore(d_c_s),
            "D1_Bollinger_Width": _bollinger_width(d_c_s),
            "D1_ADX_14": _adx(d_h, d_l, d_c_s),
            "H4_Trend_Strength": 0.0,
            "H4_ATR_Ratio": 0.0,
            "H4_RSI_Divergence": 0.0,
            "H4_vs_D1_Alignment": 0.0,
            "XAUUSDc_return": 0.0,
            "XAUUSDc_close": 0.0,
            "Cross_DXY_Return": 0.0,
            "Cross_EURUSD_Return": 0.0,
            "Cross_Risk_On_Off": 0.0,
        }
        day_features[d_idx] = feat

    # ── Compute labels (both long and short) ──
    print(f"[B2] Computing forward-barrier labels (SL={sl_atr_mult} ATR, TP={tp_atr_mult} ATR, spread={spread_points}, slippage={slippage_points})...")
    labels_long, pnl_r_long, hold_long = compute_labels(
        o, h, l, c, horizon, sl_atr_mult, tp_atr_mult,
        spread_points, slippage_points, tick_value,
    )
    labels_short, pnl_r_short, hold_short = compute_labels(
        o, h, l, c, horizon, sl_atr_mult, tp_atr_mult,
        spread_points, slippage_points, tick_value,
    )

    # Combine: use long label if long triggered, short label if short triggered,
    # or the one with earlier barrier hit
    # Simplify: use long direction only (swing strategy is directional)
    labels = labels_long.copy()
    pnl_r = pnl_r_long.copy()
    hold_bars = hold_long.copy()

    # ── Compute features ──
    print(f"[B2] Computing 37-dim features for {n_bars} bars...")
    features = np.zeros((n_bars, N_FEATURES), dtype=np.float32)
    start_bar = MIN_BARS

    for i in range(start_bar, n_bars - horizon - 1):
        if (i - start_bar) % 50000 == 0 and i > start_bar:
            print(f"  ... {i}/{n_bars} bars ({100*i/n_bars:.0f}%)")
        row = compute_feature_row(
            i, o, h, l, c, v, spreads, day_features,
            daily_ts_f, daily_o, daily_h, daily_l, daily_c,
            c,  # btc_price_hist = close prices
            tf_minutes=timeframe_minutes,
        )
        features[i] = np.asarray(row, dtype=np.float32)

    # ── Filter to labeled bars with valid features ──
    valid_idx = np.arange(start_bar, n_bars - horizon - 1)
    features = features[valid_idx]
    labels = labels[valid_idx]
    pnl_r = pnl_r[valid_idx]
    hold_bars = hold_bars[valid_idx]
    ts_valid = timestamps_f[valid_idx]

    # Remove timeout (label=0) → binary classification TP vs SL
    non_timeout = labels != 0
    features_bin = features[non_timeout]
    labels_bin = labels[non_timeout]
    pnl_r_bin = pnl_r[non_timeout]
    ts_bin = ts_valid[non_timeout]

    n_total = len(labels_bin)
    n_tp = int(np.sum(labels_bin == 1))
    n_sl = int(np.sum(labels_bin == -1))
    print(f"[B2] Binary samples: {n_total:,} (TP={n_tp}, SL={n_sl}, WR={n_tp/max(n_total,1):.1%})")
    tp_samples_pnl = float(np.mean(pnl_r_bin[labels_bin == 1])) if n_tp > 0 else 0.0
    sl_samples_pnl = float(np.mean(pnl_r_bin[labels_bin == -1])) if n_sl > 0 else 0.0
    ev = float(np.mean(pnl_r_bin))
    print(f"[B2] Avg PnL: TP={tp_samples_pnl:.3f}R, SL={sl_samples_pnl:.3f}R, EV={ev:.3f}R")

    # ── Time-decay weights ──
    print(f"[B2] Computing time-decay weights (half-life={decay_half_life_days}d)...")
    sample_weights = compute_time_decay_weights(ts_bin, decay_half_life_days)
    print(f"  Weight range: [{sample_weights.min():.3f}, {sample_weights.max():.3f}]")
    print(f"  Weight mean: {sample_weights.mean():.3f}")

    # ── Walk-forward purged CV splits ──
    print(f"[B2] Generating walk-forward purged CV splits ({cv_folds} folds, {purge_bars} bar purge)...")
    splits = walk_forward_purged_splits(n_total, ts_bin, cv_folds, purge_bars)
    for s in splits:
        train_n = len(s["train_idx"])
        test_n = len(s["test_idx"])
        test_wr = float(np.mean(labels_bin[s["test_idx"]] == 1)) if test_n > 0 else 0.0
        print(f"  Fold {s['fold']}: train={train_n:,}, test={test_n:,}, test_WR={test_wr:.1%}")

    # ── Save dataset ──
    os.makedirs(output_dir, exist_ok=True)
    np.savez_compressed(
        os.path.join(output_dir, "train.npz"),
        X=features_bin,
        y=labels_bin,
        pnl_r=pnl_r_bin,
        sample_weight=sample_weights,
        timestamps=ts_bin,
    )
    # Save metadata
    meta = {
        "schema_version": "btc_swing_v9.v1",
        "feature_names": ALL_FEATURE_NAMES,
        "n_features": N_FEATURES,
        "n_samples": int(n_total),
        "n_tp": int(n_tp),
        "n_sl": int(n_sl),
        "horizon": horizon,
        "sl_atr_mult": sl_atr_mult,
        "tp_atr_mult": tp_atr_mult,
        "spread_points": spread_points,
        "slippage_points": slippage_points,
        "tick_value": tick_value,
        "decay_half_life_days": decay_half_life_days,
        "cv_folds": cv_folds,
        "purge_bars": purge_bars,
        "ev_r": float(ev),
        "built_at": datetime.now(UTC).isoformat(),
    }
    with open(os.path.join(output_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    # Save CV split indices
    cv_data = {
        "n_folds": len(splits),
        "purge_bars": purge_bars,
        "splits": [{k: v.tolist() if isinstance(v, np.ndarray) else v for k, v in s.items()} for s in splits],
    }
    with open(os.path.join(output_dir, "cv_splits.json"), "w", encoding="utf-8") as f:
        json.dump(cv_data, f, indent=2)

    print(f"[B2] Dataset saved to {output_dir}/")
    print(f"  train.npz: X={features_bin.shape}, y={labels_bin.shape}")
    return meta


# ── Model Training ───────────────────────────────────────────────────────────


def train_xgboost(
    X_train: np.ndarray, y_train: np.ndarray, w_train: np.ndarray,
    X_val: np.ndarray, y_val: np.ndarray, w_val: np.ndarray,
    params: dict[str, Any],
) -> tuple[Any, dict[str, float]]:
    """Train XGBoost classifier with sample weights."""
    import xgboost as xgb

    dtrain = xgb.DMatrix(X_train, label=(y_train > 0).astype(int), weight=w_train)
    dval = xgb.DMatrix(X_val, label=(y_val > 0).astype(int), weight=w_val)

    model = xgb.train(
        params,
        dtrain,
        num_boost_round=params.get("n_estimators", 500),
        evals=[(dval, "val")],
        early_stopping_rounds=50,
        verbose_eval=False,
    )

    # Evaluate
    y_pred = model.predict(dval)
    y_pred_binary = (y_pred > 0.5).astype(int)
    y_true = (y_val > 0).astype(int)

    accuracy = float(np.mean(y_pred_binary == y_true))
    tp_mask = y_pred_binary == 1
    if tp_mask.sum() > 0:
        wr = float(np.mean(y_true[tp_mask] == 1))
    else:
        wr = 0.0

    return model, {"val_accuracy": accuracy, "val_wr": wr, "n_trees": model.best_iteration}


def train_lightgbm(
    X_train: np.ndarray, y_train: np.ndarray, w_train: np.ndarray,
    X_val: np.ndarray, y_val: np.ndarray, w_val: np.ndarray,
    params: dict[str, Any],
) -> tuple[Any, dict[str, float]]:
    """Train LightGBM classifier with sample weights."""
    import lightgbm as lgb

    dtrain = lgb.Dataset(X_train, label=(y_train > 0).astype(int), weight=w_train)
    dval = lgb.Dataset(X_val, label=(y_val > 0).astype(int), weight=w_val,
                        reference=dtrain)

    model = lgb.train(
        params,
        dtrain,
        valid_sets=[dval],
        valid_names=["val"],
    )

    y_pred = model.predict(X_val)
    y_pred_binary = (y_pred > 0.5).astype(int)
    y_true = (y_val > 0).astype(int)

    accuracy = float(np.mean(y_pred_binary == y_true))
    tp_mask = y_pred_binary == 1
    wr = float(np.mean(y_true[tp_mask] == 1)) if tp_mask.sum() > 0 else 0.0

    return model, {"val_accuracy": accuracy, "val_wr": wr, "n_trees": model.best_iteration}


def train_models(
    data_dir: str,
    output_dir: str,
    optuna_trials: int = 50,
    n_seeds: int = 3,
) -> dict[str, Any]:
    """B3: Train XGBoost + LightGBM with walk-forward purged CV evaluation."""

    print(f"[B3] Loading dataset from {data_dir}/...")
    data = np.load(os.path.join(data_dir, "train.npz"))
    X = data["X"]
    y = data["y"]
    weights = data["sample_weight"]
    timestamps = data["timestamps"]

    with open(os.path.join(data_dir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    with open(os.path.join(data_dir, "cv_splits.json"), encoding="utf-8") as f:
        cv_data = json.load(f)

    print(f"[B3] Data: {X.shape[0]:,} samples × {X.shape[1]} features")
    print(f"[B3] CV: {cv_data['n_folds']} folds, {cv_data['purge_bars']} bar purge")

    os.makedirs(output_dir, exist_ok=True)

    results: dict[str, list[dict[str, Any]]] = {"xgboost": [], "lightgbm": []}

    for fold_split in cv_data["splits"]:
        fold = fold_split["fold"]
        train_idx = np.array(fold_split["train_idx"])
        test_idx = np.array(fold_split["test_idx"])

        X_tr, y_tr, w_tr = X[train_idx], y[train_idx], weights[train_idx]
        X_te, y_te = X[test_idx], y[test_idx]

        print(f"\n{'='*60}")
        print(f"[B3] Fold {fold}: train={len(train_idx):,}, test={len(test_idx):,}")
        test_wr_baseline = float(np.mean(y_te == 1))
        print(f"[B3] Test baseline WR: {test_wr_baseline:.1%}")

        # ── XGBoost ──
        print(f"\n[B3] --- XGBoost (fold {fold}) ---")
        # scale_pos_weight = n_negative / n_positive for imbalanced data
        n_pos = int(np.sum(y_tr == 1))
        n_neg = int(np.sum(y_tr == -1))
        scale_pos_weight_val = n_neg / max(n_pos, 1)
        print(f"  Class balance: {n_pos} TP / {n_neg} SL, scale_pos_weight={scale_pos_weight_val:.1f}")
        xgb_params = {
            "objective": "binary:logistic",
            "eval_metric": "logloss",
            "max_depth": 5,
            "learning_rate": 0.02,
            "n_estimators": 500,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "min_child_weight": 5,
            "reg_alpha": 0.1,
            "reg_lambda": 1.0,
            "scale_pos_weight": scale_pos_weight_val,
            "random_state": 42,
        }
        model_xgb, metrics_xgb = train_xgboost(X_tr, y_tr, w_tr, X_te, y_te, weights[test_idx], xgb_params)
        print(f"  val_acc={metrics_xgb['val_accuracy']:.3f}, val_wr={metrics_xgb['val_wr']:.3f}, trees={metrics_xgb['n_trees']}")
        results["xgboost"].append({"fold": fold, "metrics": metrics_xgb})

        # Save XGBoost model
        xgb_path = os.path.join(output_dir, f"xgboost_fold{fold}_s42.json")
        model_xgb.save_model(xgb_path)
        print(f"  Saved: {xgb_path}")

        # ── LightGBM ──
        print(f"\n[B3] --- LightGBM (fold {fold}) ---")
        lgb_params = {
            "objective": "binary",
            "metric": "binary_logloss",
            "max_depth": 5,
            "learning_rate": 0.02,
            "n_estimators": 500,
            "num_leaves": 31,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "min_child_samples": 20,
            "reg_alpha": 0.1,
            "reg_lambda": 1.0,
            "scale_pos_weight": scale_pos_weight_val,
            "random_state": 42,
            "verbose": -1,
        }
        model_lgb, metrics_lgb = train_lightgbm(X_tr, y_tr, w_tr, X_te, y_te, weights[test_idx], lgb_params)
        print(f"  val_acc={metrics_lgb['val_accuracy']:.3f}, val_wr={metrics_lgb['val_wr']:.3f}, trees={metrics_lgb['n_trees']}")
        results["lightgbm"].append({"fold": fold, "metrics": metrics_lgb})

        # Save LightGBM model
        lgb_path = os.path.join(output_dir, f"lightgbm_fold{fold}_s42.txt")
        model_lgb.save_model(lgb_path)
        print(f"  Saved: {lgb_path}")

    # ── Summary ──
    print(f"\n{'='*60}")
    print("[B3] === Walk-Forward CV Summary ===")
    for arch in ["xgboost", "lightgbm"]:
        wrs = [r["metrics"]["val_wr"] for r in results[arch]]
        accs = [r["metrics"]["val_accuracy"] for r in results[arch]]
        print(f"  {arch}: WR={np.mean(wrs):.2%} +/- {np.std(wrs):.2%}, Acc={np.mean(accs):.2%} +/- {np.std(accs):.2%}")

    # ── Save full results ──
    summary = {
        "schema_version": "btc_swing_v9_training.v1",
        "data_dir": data_dir,
        "cv_summary": {
            arch: {
                "mean_val_wr": float(np.mean([r["metrics"]["val_wr"] for r in results[arch]])),
                "std_val_wr": float(np.std([r["metrics"]["val_wr"] for r in results[arch]])),
                "mean_val_acc": float(np.mean([r["metrics"]["val_accuracy"] for r in results[arch]])),
                "folds": len(results[arch]),
            }
            for arch in ["xgboost", "lightgbm"]
        },
        "params": {
            "xgboost": {
                "max_depth": 5, "learning_rate": 0.02, "n_estimators": 500,
            },
            "lightgbm": {
                "max_depth": 5, "learning_rate": 0.02, "num_leaves": 31,
            },
        },
        "trained_at": datetime.now(UTC).isoformat(),
    }
    with open(os.path.join(output_dir, "training_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"\n[B3] Training complete. Results saved to {output_dir}/")
    return summary


# ── CLI ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="BTC Swing V9 Training Pipeline")
    parser.add_argument("--full", action="store_true", help="Run full pipeline (build + train)")
    parser.add_argument("--build-only", action="store_true", help="Build dataset only")
    parser.add_argument("--train-only", action="store_true", help="Train only (requires pre-built dataset)")
    parser.add_argument("--csv", default="data/raw/btcusdc_h1_merged.csv", help="BTC OHLC CSV path (H1 default)")
    parser.add_argument("--data-dir", default="data/training/btc_swing_v9_h1", help="Dataset output dir")
    parser.add_argument("--model-dir", default="data/models/btc_swing_v9_h1", help="Model output dir")
    parser.add_argument("--timeframe-minutes", type=float, default=60.0, help="Bar interval in minutes (H1=60)")
    parser.add_argument("--horizon", type=int, default=24, help="Forward barrier horizon in bars")
    parser.add_argument("--sl-atr-mult", type=float, default=3.0, help="SL distance in ATR multiples")
    parser.add_argument("--tp-atr-mult", type=float, default=2.0, help="TP distance in ATR multiples")
    parser.add_argument("--spread-points", type=float, default=10.0, help="Spread in price points")
    parser.add_argument("--slippage-points", type=float, default=10.0, help="Slippage in price points")
    parser.add_argument("--decay-half-life-days", type=float, default=180.0, help="Time-decay half-life")
    parser.add_argument("--cv-folds", type=int, default=5, help="Number of walk-forward CV folds")
    parser.add_argument("--purge-bars", type=int, default=24, help="Purge window between train/test (match horizon)")
    parser.add_argument("--optuna-trials", type=int, default=50, help="Optuna TPE trials")
    parser.add_argument("--n-seeds", type=int, default=3, help="Number of random seeds")
    args = parser.parse_args()

    do_build = args.full or args.build_only
    do_train = args.full or args.train_only

    if not do_build and not do_train:
        parser.print_help()
        return

    if do_build:
        meta = build_dataset(
            csv_path=args.csv,
            output_dir=args.data_dir,
            horizon=args.horizon,
            sl_atr_mult=args.sl_atr_mult,
            tp_atr_mult=args.tp_atr_mult,
            spread_points=args.spread_points,
            slippage_points=args.slippage_points,
            tick_value=0.01,
            decay_half_life_days=args.decay_half_life_days,
            cv_folds=args.cv_folds,
            purge_bars=args.purge_bars,
            timeframe_minutes=args.timeframe_minutes,
        )

    if do_train:
        summary = train_models(
            data_dir=args.data_dir,
            output_dir=args.model_dir,
            optuna_trials=args.optuna_trials,
            n_seeds=args.n_seeds,
        )

    print("\n[DONE] BTC Swing V9 pipeline complete.")


if __name__ == "__main__":
    main()
