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
MIN_BARS = (
    max(
        ATR_PERIOD,
        RSI_PERIOD,
        MACD_SLOW + MACD_SIGNAL,
        VOL_ZS_LOOKBACK,
        OU_LOOKBACK,
        HURST_MAX_LAG,
        ADX_PERIOD,
        BB_PERIOD,
    )
    + 2
)

# ── BTC 37-dim feature schema (FIX-20260604-081) ────────────────────────────

BTC_MACRO_24 = [
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
]

BTC_MICRO_9 = [
    "tick_return",
    "hl_ratio",
    "co_ratio",
    "avg_spread",
    "OIM",
    "tick_velocity",
    "AUDJPYc_return",
    "EURUSDc_return",
    "USDJPYc_return",
]

BTC_CROSS_2 = ["Cross_BTC_Gold_Ratio", "Cross_BTC_Gold_Ratio_ROC"]

TF_SPECIFIC_2 = ["TF_OU_Theta", "TF_Hurst"]
# FIX-20260614-B3-feat: Second-order regime features.
# Raw OU/Hurst are too slow for per-bar tree splits → derivatives capture
# the REGIME TRANSITION, not the absolute level.
REGIME_DERIVED_4 = [
    "TF_delta_OU",  # OU acceleration: OU(t) - OU(t-1)
    "TF_delta_Hurst",  # Hurst velocity: Hurst(t) - Hurst(t-1)
    "TF_OU_x_Hurst",  # Combined signal: high OU + low Hurst = mean-reversion
    "TF_OU_div_ADX",  # Mean-reversion strength relative to trend
]

# FIX-20260625-137: Schema canonical order (Order B) — aligned with live inference
# augmenter output.  Previously BTC_CROSS_2 was before TF_SPECIFIC_2 (Order A),
# causing 4-feature swap in slots 33-36 between training and live dispatch.
ALL_FEATURE_NAMES = BTC_MACRO_24 + BTC_MICRO_9 + TF_SPECIFIC_2 + REGIME_DERIVED_4 + BTC_CROSS_2
N_FEATURES = len(ALL_FEATURE_NAMES)  # 37 → 41


# ── Feature Computers ─────────────────────────────────────────────────────────


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
    prev_c = c[-(period + 1) : -1]
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
    returns = np.diff(c[-(lookback + 1) :]) / c[-(lookback + 1) : -1]
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


def compute_feature_row(
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
    btc_price_hist: np.ndarray,
    tf_minutes: float = 5.0,
    prev_ou: float | None = None,
    prev_hurst: float | None = None,
    aligned_cross_m5: dict[str, np.ndarray] | None = None,
    m5_day_map: np.ndarray | None = None,
) -> tuple[list[float], float, float]:
    """Compute 41-dim BTC feature vector at bar *idx*.

    Returns (row, ou_val, hurst_val) — ou_val/hurst_val are returned so
    the caller can track them as prev_ou/prev_hurst for the next bar.
    """
    end = idx + 1
    price_slice = c[:end]
    o_slice, h_slice, l_slice, c_slice = o[:end], h[:end], l[:end], c[:end]

    # ── D1 features (FIX-20260705-022: use m5_day_map for correct day alignment) ──
    bar_ts = daily_ts[idx] if idx < len(daily_ts) else 0
    bar_date = (
        datetime.fromtimestamp(float(bar_ts), tz=UTC).strftime("%Y-%m-%d") if bar_ts > 0 else ""
    )
    if m5_day_map is not None and idx < len(m5_day_map):
        day_idx = int(m5_day_map[idx])
    else:
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
    atr_5d = (
        _atr(h_slice, l_slice, c_slice)
        if len(c_slice) < 288 * 5
        else _atr(h_slice[-288 * 5 :], l_slice[-288 * 5 :], c_slice[-288 * 5 :])
    )
    vol_regime = atr_val / atr_5d if atr_5d > 0 else 1.0
    mom_5d = (
        (c[idx] - c[max(0, idx - 288 * 5)]) / c[max(0, idx - 288 * 5)] if len(c) > 288 * 5 else 0.0
    )
    mom_20d = (
        (c[idx] - c[max(0, idx - 288 * 20)]) / c[max(0, idx - 288 * 20)]
        if len(c) > 288 * 20
        else 0.0
    )

    # ── Micro features (M5 bar-level) ──
    prev_c_val = c[idx - 1] if idx > 0 else c[idx]
    tick_ret = (c[idx] - o[idx]) / o[idx] if o[idx] > 0 else 0.0
    body_ratio = abs(c[idx] - o[idx]) / (h[idx] - l[idx]) if (h[idx] - l[idx]) > 0 else 0.5
    hl_ratio_val = (h[idx] - l[idx]) / prev_c_val if prev_c_val > 0 else 0.0
    co_ratio_val = abs(c[idx] - o[idx]) / (h[idx] - l[idx]) if (h[idx] - l[idx]) > 0 else 0.0
    avg_spread_val = float(spreads[idx]) if idx < len(spreads) else 10.0
    oim = (c[idx] - o[idx]) / (h[idx] - l[idx]) if (h[idx] - l[idx]) > 0 else 0.0
    tick_vel = (
        float(v[idx]) / (float(np.mean(v[max(0, idx - 20) : end])) + 1e-8) if idx > 20 else 1.0
    )

    # Cross-market micro returns (FIX-20260705-022: from aligned M5 data)
    audjpy_ret = 0.0
    eur_ret = 0.0
    usdjpy_ret = 0.0
    if aligned_cross_m5 is not None:
        _ac = aligned_cross_m5
        if idx > 0:
            _prev_i = idx - 1
            if "AUDJPYc" in _ac and _prev_i < len(_ac["AUDJPYc"]) and _ac["AUDJPYc"][_prev_i] > 0:
                audjpy_ret = (_ac["AUDJPYc"][idx] - _ac["AUDJPYc"][_prev_i]) / _ac["AUDJPYc"][
                    _prev_i
                ]
            if "EURUSDc" in _ac and _prev_i < len(_ac["EURUSDc"]) and _ac["EURUSDc"][_prev_i] > 0:
                eur_ret = (_ac["EURUSDc"][idx] - _ac["EURUSDc"][_prev_i]) / _ac["EURUSDc"][_prev_i]
            if "USDJPYc" in _ac and _prev_i < len(_ac["USDJPYc"]) and _ac["USDJPYc"][_prev_i] > 0:
                usdjpy_ret = (_ac["USDJPYc"][idx] - _ac["USDJPYc"][_prev_i]) / _ac["USDJPYc"][
                    _prev_i
                ]

    # ── BTC-specific cross features (FIX-20260705-022: real XAU M5 for ratio) ──
    btc_gold_ratio = 0.0
    btc_gold_ratio_roc = 0.0
    # Use real XAU M5 close if available, fall back to D1 XAU close
    _xau_close = 0.0
    if aligned_cross_m5 is not None and "XAUUSDc" in aligned_cross_m5:
        _xau_close = (
            float(aligned_cross_m5["XAUUSDc"][idx])
            if idx < len(aligned_cross_m5["XAUUSDc"])
            else 0.0
        )
    if _xau_close <= 0:
        _xau_close = float(d_feat.get("XAUUSDc_close", 0.0))
    if len(btc_price_hist) > 0 and btc_price_hist[idx] > 0 and _xau_close > 0:
        btc_gold_ratio = btc_price_hist[idx] / _xau_close
        if idx >= 288:
            _prev_xau = 0.0
            if aligned_cross_m5 is not None and "XAUUSDc" in aligned_cross_m5:
                _prev_xau = (
                    float(aligned_cross_m5["XAUUSDc"][idx - 288])
                    if (idx - 288) < len(aligned_cross_m5["XAUUSDc"])
                    else 0.0
                )
            if _prev_xau <= 0:
                _prev_xau = float(d_feat.get("XAUUSDc_close", 0.0))
            prev_ratio = btc_price_hist[idx - 288] / max(_prev_xau, 1.0)
            btc_gold_ratio_roc = (
                (btc_gold_ratio - prev_ratio) / prev_ratio if prev_ratio > 0 else 0.0
            )

    # ── TF-specific (timeframe-aware dt for OU_Theta) ──
    tf_ou = _ou_theta(price_slice)  # FIX-B3: production parity, dt=1 implicit
    tf_hurst = _hurst(price_slice)

    # ── FIX-20260614-B3-feat: Second-order regime derivatives ──
    # Raw OU/Hurst are too slow for per-bar tree splits.
    # Derivatives capture the REGIME TRANSITION — what changed since last bar?
    delta_ou = tf_ou - prev_ou if prev_ou is not None else 0.0
    delta_hurst = tf_hurst - prev_hurst if prev_hurst is not None else 0.0
    ou_x_hurst = tf_ou * (1.0 - tf_hurst)  # high OU + low Hurst = strong mean-reversion
    # ADX from D1 features (slot 7) — guard against zero
    adx_val = d1_adx if d1_adx > 1.0 else 1.0
    ou_div_adx = tf_ou / adx_val

    # ── Assemble in schema order ──
    values_map: dict[str, float] = {
        # BTC_MACRO_24
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
        "XAUUSDc_return": xau_return,
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
        # BTC_MICRO_9
        "tick_return": tick_ret,
        "hl_ratio": hl_ratio_val,
        "co_ratio": co_ratio_val,
        "avg_spread": avg_spread_val,
        "OIM": oim,
        "tick_velocity": tick_vel,
        "AUDJPYc_return": audjpy_ret,
        "EURUSDc_return": eur_ret,
        "USDJPYc_return": usdjpy_ret,
        # BTC_CROSS_2
        "Cross_BTC_Gold_Ratio": btc_gold_ratio,
        "Cross_BTC_Gold_Ratio_ROC": btc_gold_ratio_roc,
        # TF_SPECIFIC_2
        "TF_OU_Theta": tf_ou,
        "TF_Hurst": tf_hurst,
        # REGIME_DERIVED_4
        "TF_delta_OU": delta_ou,
        "TF_delta_Hurst": delta_hurst,
        "TF_OU_x_Hurst": ou_x_hurst,
        "TF_OU_div_ADX": ou_div_adx,
    }
    return [values_map.get(name, 0.0) for name in ALL_FEATURE_NAMES], tf_ou, tf_hurst


# ── Label Creation (forward barrier with friction) ──────────────────────────


def compute_labels(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    horizon: int,
    sl_atr_mult: float,
    tp_atr_mult: float,
    spread_points: float,
    slippage_points: float,
    tick_value: float,
    side: str | None = None,
    spreads: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute forward barrier labels with real friction.

    Args:
        side: 'long' = only simulate LONG entry; 'short' = only simulate SHORT;
              None (default) = merged both-directions check (backward compatible).
        spreads: Per-bar spread in PRICE UNITS (e.g. dollars for BTC).
              When provided, replaces the constant `spread_points` with real
              per-bar spread — captures volatility-driven spread widening.
              When None (default), uses `spread_points` constant (legacy).

    Returns:
        labels: -1 (SL), 0 (timeout), +1 (TP)
        pnl_r: realized PnL in R-multiples (only for SL/TP, NaN for timeout)
        hold_bars: how many bars until barrier was hit

    Friction model (per entry, direction-aware):
      - Long entry  = open[i+1] + slippage  (buy at ask, above mid)
      - Short entry = open[i+1] - slippage  (sell at bid, below mid)
      - SL distance widened by spread (stop fills suffer adverse slippage)
      - TP distance tightened by spread (exit fills at bid/ask, not mid)

    DQAF-20260703-062 (L3 fix): When side='long' or side='short', only the
    specified direction's barriers are checked.  This enables independent
    directional outcome tracking for proper 3-class label construction where
    the label encodes "which direction was profitable" rather than "which
    barrier was hit first."

    FIX-20260706-024 (L2 fix): `spreads` parameter replaces constant spread
    with per-bar real spread from CSV data. Old constant `spread_points=10`
    underestimated actual BTC spread by ~44% ($10 vs $18 median, $160 at P99).
    Per-bar spread captures volatility-regime-dependent friction.
    """
    n = len(o)
    labels = np.zeros(n, dtype=np.int8)
    pnl_r = np.full(n, np.nan, dtype=np.float32)
    hold_bars = np.zeros(n, dtype=np.int16)

    check_long = side is None or side == "long"
    check_short = side is None or side == "short"

    for i in range(n - horizon - 1):
        ref_price = o[i + 1]
        if ref_price <= 0:
            continue

        atr_val = _atr(h[: i + 2], l[: i + 2], c[: i + 2])
        if atr_val <= 0:
            continue

        _spread = float(spreads[i]) if spreads is not None and i < len(spreads) else spread_points
        sl_dist = sl_atr_mult * atr_val + _spread
        tp_dist = tp_atr_mult * atr_val - _spread
        tp_dist = max(tp_dist, sl_dist * 0.3)  # minimum TP = 0.3 × SL

        # Direction-specific entry prices
        entry_long = ref_price + slippage_points  # buy at ask
        entry_short = ref_price - slippage_points  # sell at bid

        sl_long = entry_long - sl_dist
        tp_long = entry_long + tp_dist
        sl_short = entry_short + sl_dist
        tp_short = entry_short - tp_dist

        # Walk forward
        for j in range(i + 2, min(i + 2 + horizon, n)):
            cur_h, cur_l = h[j], l[j]

            # Long: check SL first (risk management), then TP
            if check_long:
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
            if check_short:
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
    timestamps: np.ndarray,
    half_life_days: float = 90.0,
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
    n_samples: int,
    timestamps: np.ndarray,
    n_folds: int = 5,
    purge_bars: int = 144,
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

        splits.append(
            {
                "fold": fold,
                "train_idx": train_idx,
                "test_idx": test_idx,
                "train_start_ts": timestamps[train_idx[0]] if len(train_idx) > 0 else 0,
                "train_end_ts": timestamps[train_idx[-1]] if len(train_idx) > 0 else 0,
                "test_start_ts": timestamps[test_idx[0]] if len(test_idx) > 0 else 0,
                "test_end_ts": timestamps[test_idx[-1]] if len(test_idx) > 0 else 0,
            }
        )

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
    multi_class: bool = False,
    min_adx: float = 0.0,
    exclude_weekends: bool = False,
    atr_pctile_low: float = 0.0,
    atr_pctile_high: float = 0.0,
    neutral_max_pct: float | None = None,
) -> dict[str, Any]:
    """Full B2 pipeline: CSV → features + labels → weights → CV splits → NPZ.

    When multi_class=True: trains 3-class (SHORT/NEUTRAL/LONG) instead of binary.

    Curation filters (FIX-20260705-021):
        min_adx: Exclude bars where D1_ADX < min_adx (0=disabled, recommended: 20)
        exclude_weekends: Exclude Fri 22:00 – Sun 22:00 UTC bars
        atr_pctile_low: Exclude bars below this ATR percentile (0=disabled, recommended: 5)
        atr_pctile_high: Exclude bars above this ATR percentile (0=disabled, recommended: 95)
    """
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
    # FIX-20260706-024: Convert MT5 raw spread (points) to price units (dollars).
    # BTCUSD has Digits=2, so 1 MT5 point = 0.01; divide by 100 for $.
    # Typical: 1800 points → $18.00.  Range: 1008–16000 → $10.08–$160.00.
    spreads_real = spreads / 100.0  # MT5 points → dollars for barrier labels
    timestamps = pd.to_datetime(df["time"]).astype(np.int64).values // 10**9
    timestamps_f = timestamps.astype(np.float64)

    # ── Load cross-asset data for feature alignment (FIX-20260705-022) ─────────
    _raw_dir = Path("data/raw")
    _cross_ts_map: dict[str, tuple[np.ndarray, np.ndarray]] = {}  # symbol → (closes, timestamps)
    _xau_d1_closes: np.ndarray | None = None
    _xau_d1_ts: np.ndarray | None = None
    _eur_d1_closes: np.ndarray | None = None
    _eur_d1_ts: np.ndarray | None = None
    _h4_closes: np.ndarray | None = None
    _h4_ts: np.ndarray | None = None

    # M5 cross-asset for micro returns (AUDJPY, EURUSD, USDJPY)
    for _sym, _csv in [
        ("AUDJPYc", "audjpyc_m5_merged.csv"),
        ("EURUSDc", "eurusdc_m5_merged.csv"),
        ("USDJPYc", "usdjpyc_m5_merged.csv"),
        ("XAUUSDc", "xauusdc_m5_merged.csv"),
    ]:
        _p = _raw_dir / _csv
        if _p.exists():
            _cdf = pd.read_csv(_p)
            _cross_ts_map[_sym] = (
                _cdf["close"].values.astype(np.float64),
                pd.to_datetime(_cdf["time"], format="mixed").astype(np.int64).values // 10**9,
            )
            print(f"  Cross M5 {_sym}: {len(_cdf):,} bars loaded")
        else:
            print(f"  [WARN] Cross M5 {_sym} ({_csv}) not found")

    # D1 cross-asset for daily features
    _xau_d1_path = _raw_dir / "xauusdc_d1_merged.csv"
    if _xau_d1_path.exists():
        _xdf = pd.read_csv(_xau_d1_path)
        _xau_d1_closes = _xdf["close"].values.astype(np.float64)
        _xau_d1_ts = pd.to_datetime(_xdf["time"], format="mixed").astype(np.int64).values // 10**9
        print(f"  XAU D1: {len(_xdf):,} bars loaded")

    _eur_d1_path = _raw_dir / "eurusdc_d1_merged.csv"
    if _eur_d1_path.exists():
        _edf = pd.read_csv(_eur_d1_path)
        _eur_d1_closes = _edf["close"].values.astype(np.float64)
        _eur_d1_ts = pd.to_datetime(_edf["time"], format="mixed").astype(np.int64).values // 10**9
        print(f"  EUR D1: {len(_edf):,} bars loaded")

    # H4 BTC for intermediate-term macro features
    _h4_path = _raw_dir / "btcusdc_h4_merged.csv"
    if _h4_path.exists():
        _h4df = pd.read_csv(_h4_path)
        _h4_closes = _h4df["close"].values.astype(np.float64)
        _h4_ts = pd.to_datetime(_h4df["time"], format="mixed").astype(np.int64).values // 10**9
        print(f"  H4 BTC: {len(_h4df):,} bars loaded")

    # Pre-align M5 cross-asset closes to BTC M5 timestamps
    _aligned_cross: dict[str, np.ndarray] = {}
    for _sym, (_closes, _ts) in _cross_ts_map.items():
        _idx = np.searchsorted(_ts, timestamps, side="right") - 1
        _idx = np.clip(_idx, 0, len(_closes) - 1)
        _aligned_cross[_sym] = _closes[_idx]
        print(f"  Aligned {_sym} M5 closes to {n_bars:,} BTC bars")

    # ── Build day-level features (simplified: use rolling M5 → D1 resample) ──
    print("[B2] Computing day-level context features...")
    # Re-sample to daily for D1 features
    df_dt = pd.to_datetime(df["time"])
    daily = (
        df.set_index(df_dt)
        .resample("D")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "tick_volume": "sum",
            }
        )
        .dropna()
    )
    daily_ts = daily.index.astype(np.int64).values // 10**9
    daily_ts_f = daily_ts.astype(np.float64)
    daily_o = daily["open"].values
    daily_h = daily["high"].values
    daily_l = daily["low"].values
    daily_c = daily["close"].values

    # Map each M5 bar to its daily index
    m5_day_map = np.searchsorted(daily_ts, timestamps, side="right") - 1
    m5_day_map = np.clip(m5_day_map, 0, len(daily_ts) - 1)

    # ── Pre-compute daily H4 and cross-asset features (FIX-20260705-022) ─────
    # H4 alignment: map each D1 day to its most recent H4 bar
    _h4_day_map: np.ndarray | None = None
    _h4_atr_arr: list[float] = []
    _h4_rsi_arr: list[float] = []
    _h4_mom_24: list[float] = []
    if _h4_closes is not None and _h4_ts is not None:
        _h4_day_map = np.asarray(np.searchsorted(_h4_ts, daily_ts, side="right") - 1, dtype=np.intp)
        _h4_day_map = np.clip(_h4_day_map, 0, len(_h4_closes) - 1)
        # Pre-compute H4 ATR, RSI, 24-bar momentum
        _h4_atr_period = 14
        for _i in range(len(_h4_closes)):
            _end = _i + 1
            _h4_atr_arr.append(
                _atr(_h4_closes[:_end], _h4_closes[:_end], _h4_closes[:_end])
                if _i >= _h4_atr_period
                else 0.0
            )
            _h4_rsi_arr.append(_rsi(_h4_closes[:_end]) if _i >= 14 else 50.0)
            _h4_mom_24.append(
                (_h4_closes[_i] - _h4_closes[_i - 24]) / _h4_closes[_i - 24] * 100
                if _i >= 24 and _h4_closes[_i - 24] > 0
                else 0.0
            )
        print(f"  H4 features pre-computed: {len(_h4_atr_arr)} bars")

    # XAU D1 alignment
    _xau_d1_day_map: np.ndarray | None = None
    if _xau_d1_closes is not None and _xau_d1_ts is not None:
        _xau_d1_day_map = np.searchsorted(_xau_d1_ts, daily_ts, side="right") - 1  # type: ignore[assignment]
        _xau_d1_day_map = np.clip(_xau_d1_day_map, 0, len(_xau_d1_closes) - 1)

    # EUR D1 alignment
    _eur_d1_day_map: np.ndarray | None = None
    if _eur_d1_closes is not None and _eur_d1_ts is not None:
        _eur_d1_day_map = np.searchsorted(_eur_d1_ts, daily_ts, side="right") - 1  # type: ignore[assignment]
        _eur_d1_day_map = np.clip(_eur_d1_day_map, 0, len(_eur_d1_closes) - 1)

    # Compute daily features for each day
    day_features: dict[int, dict[str, float]] = {}
    for d_idx in range(len(daily_c)):
        end = d_idx + 1
        d_o = daily_o[:end]
        d_h = daily_h[:end]
        d_l = daily_l[:end]
        d_c_s = daily_c[:end]
        feat = {
            "D1_Ret_1": (d_c_s[-1] - d_c_s[-2]) / d_c_s[-2]
            if len(d_c_s) >= 2 and d_c_s[-2] > 0
            else 0.0,
            "D1_Body_Ratio": abs(d_c_s[-1] - d_o[-1]) / (d_h[-1] - d_l[-1])
            if (d_h[-1] - d_l[-1]) > 0
            else 0.5,
            "D1_ATR_14": _atr(d_h, d_l, d_c_s),
            "D1_RSI_14": _rsi(d_c_s),
            "D1_MACD": _macd(d_c_s)[2],
            "D1_Vol_ZScore": _vol_zscore(d_c_s),
            "D1_Bollinger_Width": _bollinger_width(d_c_s),
            "D1_ADX_14": _adx(d_h, d_l, d_c_s),
        }
        # ── H4 features (FIX-20260705-022) ──────────────────────────────────
        if _h4_day_map is not None and d_idx < len(_h4_day_map):
            _h4i = int(_h4_day_map[d_idx])
            if _h4i >= 20:
                _h4_trend = _h4_mom_24[_h4i] if _h4i < len(_h4_mom_24) else 0.0
                _h4_atr_v = _h4_atr_arr[_h4i] if _h4i < len(_h4_atr_arr) else 0.0
                _d1_atr_v = feat.get("D1_ATR_14", 0.0)
                _h4_atr_ratio = _h4_atr_v / _d1_atr_v if _d1_atr_v > 0 else 0.0
                _h4_rsi_v = _h4_rsi_arr[_h4i] if _h4i < len(_h4_rsi_arr) else 50.0
                _h4_rsi_div = feat.get("D1_RSI_14", 50.0) - _h4_rsi_v
                # H4 vs D1 alignment
                if _h4i >= 6 and d_idx >= 1 and _h4_closes is not None and _h4_closes[_h4i - 6] > 0:
                    _h4_ret = (_h4_closes[_h4i] - _h4_closes[_h4i - 6]) / _h4_closes[_h4i - 6]
                    _d1_ret_v = feat.get("D1_Ret_1", 0.0)  # already in return units
                    _h4_align = (
                        1.0
                        if _h4_ret * _d1_ret_v > 0
                        else (-1.0 if _h4_ret * _d1_ret_v < 0 else 0.0)
                    )
                else:
                    _h4_align = 0.0
                feat["H4_Trend_Strength"] = round(_h4_trend, 6)
                feat["H4_ATR_Ratio"] = round(_h4_atr_ratio, 6)
                feat["H4_RSI_Divergence"] = round(_h4_rsi_div, 6)
                feat["H4_vs_D1_Alignment"] = round(_h4_align, 6)
        # ── Cross-asset D1 features (FIX-20260705-022) ──────────────────────
        # XAUUSDc return
        if _xau_d1_day_map is not None and d_idx < len(_xau_d1_day_map):
            _xau_i = int(_xau_d1_day_map[d_idx])
            if _xau_i >= 1 and _xau_d1_closes is not None and _xau_d1_closes[_xau_i - 1] > 0:
                _xau_ret = (
                    (_xau_d1_closes[_xau_i] - _xau_d1_closes[_xau_i - 1])
                    / _xau_d1_closes[_xau_i - 1]
                    * 100
                )
                feat["XAUUSDc_return"] = round(_xau_ret, 6)
                feat["XAUUSDc_close"] = round(float(_xau_d1_closes[_xau_i]), 4)
        # Cross DXY / EURUSD
        if _eur_d1_day_map is not None and d_idx < len(_eur_d1_day_map):
            _eur_i = int(_eur_d1_day_map[d_idx])
            if _eur_i >= 1 and _eur_d1_closes is not None and _eur_d1_closes[_eur_i - 1] > 0:
                _eur_ret = (
                    (_eur_d1_closes[_eur_i] - _eur_d1_closes[_eur_i - 1])
                    / _eur_d1_closes[_eur_i - 1]
                    * 100
                )
                feat["Cross_DXY_Return"] = round(-_eur_ret, 6)  # DXY ≈ -EURUSD
                feat["Cross_EURUSD_Return"] = round(_eur_ret, 6)
        # Cross Risk-On/Off: using XAU 5d momentum as risk proxy
        if _xau_d1_day_map is not None and d_idx < len(_xau_d1_day_map):
            _xau_i2 = int(_xau_d1_day_map[d_idx])
            if _xau_i2 >= 5 and _xau_d1_closes is not None and _xau_d1_closes[_xau_i2 - 5] > 0:
                _xau_5d_mom = (
                    (_xau_d1_closes[_xau_i2] - _xau_d1_closes[_xau_i2 - 5])
                    / _xau_d1_closes[_xau_i2 - 5]
                    * 100
                )
                # Normalize: gold 5d momentum vs BTC 5d momentum (risk appetite spread)
                _btc_5d_mom = feat.get("Derived_Momentum_5D", 0.0)
                feat["Cross_Risk_On_Off"] = round(_xau_5d_mom - _btc_5d_mom, 6)
        day_features[d_idx] = feat

    # ── Compute labels ──
    # DQAF-20260703-062 (L3 fix): When multi_class, compute LONG and SHORT
    # outcomes INDEPENDENTLY then merge into proper directional labels.
    # Old behavior merged both directions in one pass (LONG-first priority)
    # → label encoded "barrier hit" not "profitable direction."
    # New behavior: label = which direction (if any) would have hit TP.
    print(
        f"[B2] Computing forward-barrier labels (SL={sl_atr_mult} ATR, TP={tp_atr_mult} ATR, spread=per-bar (median={np.median(spreads_real):.1f}), slippage={slippage_points})..."
    )
    if multi_class:
        # Independent directional outcome tracking
        labels_long, pnl_r_long, hold_long = compute_labels(
            o,
            h,
            l,
            c,
            horizon,
            sl_atr_mult,
            tp_atr_mult,
            spread_points,
            slippage_points,
            tick_value,
            side="long",
            spreads=spreads_real,
        )
        labels_short, pnl_r_short, hold_short = compute_labels(
            o,
            h,
            l,
            c,
            horizon,
            sl_atr_mult,
            tp_atr_mult,
            spread_points,
            slippage_points,
            tick_value,
            side="short",
            spreads=spreads_real,
        )
    else:
        # Backward-compatible merged path (binary training)
        labels_long, pnl_r_long, hold_long = compute_labels(
            o,
            h,
            l,
            c,
            horizon,
            sl_atr_mult,
            tp_atr_mult,
            spread_points,
            slippage_points,
            tick_value,
            side=None,
            spreads=spreads_real,
        )
        labels_short, pnl_r_short, hold_short = (
            labels_long.copy(),
            pnl_r_long.copy(),
            hold_long.copy(),
        )

    # Legacy: use labels_long as the base; multiclass path overrides below
    labels = labels_long.copy()
    pnl_r = pnl_r_long.copy()
    hold_bars = hold_long.copy()

    # ── Compute features ──
    print(f"[B2] Computing {N_FEATURES}-dim features for {n_bars} bars...")
    features = np.zeros((n_bars, N_FEATURES), dtype=np.float32)
    start_bar = MIN_BARS
    prev_ou: float | None = None
    prev_hurst: float | None = None

    for i in range(start_bar, n_bars - horizon - 1):
        if (i - start_bar) % 50000 == 0 and i > start_bar:
            print(f"  ... {i}/{n_bars} bars ({100*i/n_bars:.0f}%)")
        row, tf_ou, tf_hurst = compute_feature_row(
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
            c,
            tf_minutes=timeframe_minutes,
            prev_ou=prev_ou,
            prev_hurst=prev_hurst,
            aligned_cross_m5=_aligned_cross if _aligned_cross else None,
            m5_day_map=m5_day_map,
        )
        features[i] = np.asarray(row, dtype=np.float32)
        prev_ou = tf_ou
        prev_hurst = tf_hurst

    # ── Sample Curation (FIX-20260705-021) ──────────────────────────────────
    curation_mask = np.ones(n_bars, dtype=bool)
    curation_reasons: list[str] = []

    # ADX filter: exclude bars where D1 ADX is below threshold
    if min_adx > 0:
        d1_adx_per_bar = np.array(
            [day_features[d_idx].get("D1_ADX_14", 0.0) for d_idx in m5_day_map]
        )
        adx_mask = d1_adx_per_bar >= min_adx
        n_excluded_adx = int((~adx_mask).sum())
        curation_mask &= adx_mask
        curation_reasons.append(f"ADX<{min_adx}: -{n_excluded_adx:,}")
        print(
            f"[B2] Curation ADX filter (min={min_adx}): "
            f"excluded {n_excluded_adx:,}/{n_bars:,} bars "
            f"({100*n_excluded_adx/n_bars:.1f}%)"
        )

    # Weekend filter: exclude Fri 22:00 – Sun 22:00 UTC
    if exclude_weekends:
        # Convert unix timestamps to UTC weekday/hour
        utc_dt = pd.to_datetime(timestamps, unit="s", utc=True)
        weekday = utc_dt.dayofweek  # Mon=0, Sun=6
        hour = utc_dt.hour
        # Weekend window: Fri 22:00+ (day=4, h>=22), Sat all day (day=5), Sun before 22:00 (day=6, h<22)
        is_weekend = (
            ((weekday == 4) & (hour >= 22)) | (weekday == 5) | ((weekday == 6) & (hour < 22))
        )
        n_excluded_weekend = int(is_weekend.sum())
        curation_mask &= ~is_weekend
        curation_reasons.append(f"weekend: -{n_excluded_weekend:,}")
        print(
            f"[B2] Curation weekend filter: "
            f"excluded {n_excluded_weekend:,}/{n_bars:,} bars "
            f"({100*n_excluded_weekend/n_bars:.1f}%)"
        )

    # ATR percentile filter: exclude extreme ATR bars
    if atr_pctile_low > 0 or atr_pctile_high > 0:
        d1_atr_per_bar = np.array(
            [day_features[d_idx].get("D1_ATR_14", 0.0) for d_idx in m5_day_map]
        )
        atr_mask = np.ones(n_bars, dtype=bool)
        if atr_pctile_low > 0:
            atr_lo = np.percentile(d1_atr_per_bar, atr_pctile_low)
            atr_mask &= d1_atr_per_bar >= atr_lo
            n_excluded_lo = int((d1_atr_per_bar < atr_lo).sum())
            curation_reasons.append(f"ATR<p{atr_pctile_low:.0f}: -{n_excluded_lo:,}")
            print(
                f"[B2] Curation ATR low filter (p{atr_pctile_low:.0f}={atr_lo:.1f}): "
                f"excluded {n_excluded_lo:,} bars"
            )
        if atr_pctile_high > 0:
            atr_hi = np.percentile(d1_atr_per_bar, atr_pctile_high)
            atr_mask &= d1_atr_per_bar <= atr_hi
            n_excluded_hi = int((d1_atr_per_bar > atr_hi).sum())
            curation_reasons.append(f"ATR>p{atr_pctile_high:.0f}: -{n_excluded_hi:,}")
            print(
                f"[B2] Curation ATR high filter (p{atr_pctile_high:.0f}={atr_hi:.1f}): "
                f"excluded {n_excluded_hi:,} bars"
            )
        curation_mask &= atr_mask

    n_curated = int(curation_mask.sum())
    n_excluded_total = n_bars - n_curated
    if curation_reasons:
        print(
            f"[B2] Curation summary: {n_curated:,}/{n_bars:,} bars kept "
            f"(-{n_excluded_total:,}, -{100*n_excluded_total/max(n_bars,1):.1f}%) "
            f"[{', '.join(curation_reasons)}]"
        )

    # ── Filter to labeled bars with valid features ──
    valid_idx = np.arange(start_bar, n_bars - horizon - 1)
    # Intersect valid_idx with curation_mask
    valid_idx = valid_idx[curation_mask[valid_idx]]
    print(
        f"[B2] After curation: {len(valid_idx):,} labeled bars "
        f"(from {n_bars - start_bar - horizon - 1:,} pre-curation)"
    )
    features = features[valid_idx]
    labels = labels[valid_idx]
    pnl_r = pnl_r[valid_idx]
    hold_bars = hold_bars[valid_idx]
    ts_valid = timestamps_f[valid_idx]

    # ── Multi-class vs Binary path ──
    if multi_class:
        # ── DQAF-20260703-062 (L3): Directional profitability labels ──
        # Build labels from independent LONG/SHORT outcomes:
        #   Class 2 (LONG):  LONG TP hit AND SHORT did NOT hit TP,
        #                    or both hit TP but LONG hit first
        #   Class 0 (SHORT): SHORT TP hit AND LONG did NOT hit TP,
        #                    or both hit TP but SHORT hit first
        #   Class 1 (NEUTRAL): neither hit TP (timeout both directions)
        n = len(labels)
        labels_directional = np.zeros(n, dtype=np.int8)
        pnl_r_directional = np.zeros(n, dtype=np.float32)
        hold_directional = np.zeros(n, dtype=np.int16)

        for i in range(n):
            long_win = labels_long[i] == 1
            short_win = labels_short[i] == 1
            long_lost = labels_long[i] == -1
            short_lost = labels_short[i] == -1

            if long_win and not short_win:
                # Only LONG was profitable
                labels_directional[i] = 1
                pnl_r_directional[i] = pnl_r_long[i]
                hold_directional[i] = hold_long[i]
            elif short_win and not long_win:
                # Only SHORT was profitable
                labels_directional[i] = -1
                pnl_r_directional[i] = pnl_r_short[i]
                hold_directional[i] = hold_short[i]
            elif long_win and short_win:
                # Both profitable — pick the faster one
                if hold_long[i] <= hold_short[i]:
                    labels_directional[i] = 1
                    pnl_r_directional[i] = pnl_r_long[i]
                    hold_directional[i] = hold_long[i]
                else:
                    labels_directional[i] = -1
                    pnl_r_directional[i] = pnl_r_short[i]
                    hold_directional[i] = hold_short[i]
            else:
                # Neither profitable → NEUTRAL (label stays 0)
                pnl_r_directional[i] = 0.0

        labels = labels_directional
        pnl_r = np.where(labels_directional != 0, pnl_r_directional, np.nan)
        hold_bars = hold_directional

        # ── NEUTRAL downsampling (IC Mandate: prevent model collapse) ──
        # When neutral_max_pct is set, randomly downsample NEUTRAL-labeled
        # bars so they don't exceed the target fraction of total samples.
        # This prevents the multi:softprob model from collapsing to
        # "always predict NEUTRAL" when training data is dominated by
        # timeout bars (common in higher timeframes like H4).
        if neutral_max_pct is not None and neutral_max_pct < 1.0:
            neutral_mask = labels == 0
            non_neutral_mask = ~neutral_mask
            n_neutral = int(neutral_mask.sum())
            n_non_neutral = int(non_neutral_mask.sum())

            if n_neutral > 0 and n_non_neutral > 0:
                # Target: neutral / (neutral + non_neutral) ≤ neutral_max_pct
                max_neutral = int(neutral_max_pct * n_non_neutral / (1.0 - neutral_max_pct))

                if n_neutral > max_neutral:
                    rng = np.random.RandomState(42)
                    neutral_idx = np.where(neutral_mask)[0]
                    keep_n = max(
                        max_neutral, n_non_neutral // 2
                    )  # floor: keep at least half of non-neutral count
                    keep_neutral_idx = rng.choice(neutral_idx, size=keep_n, replace=False)
                    keep_mask = np.zeros(len(labels), dtype=bool)
                    keep_mask[keep_neutral_idx] = True
                    keep_mask[non_neutral_mask] = True
                    keep_mask_sorted = np.sort(np.where(keep_mask)[0])

                    labels = labels[keep_mask_sorted]
                    pnl_r = pnl_r[keep_mask_sorted]
                    hold_bars = hold_bars[keep_mask_sorted]
                    ts_valid = ts_valid[keep_mask_sorted]
                    features = features[keep_mask_sorted]
                    print(
                        f"[B2] NEUTRAL downsampling: {n_neutral:,} → {keep_n:,} "
                        f"({100*keep_n/(keep_n+n_non_neutral):.0f}% of {keep_n+n_non_neutral:,} total, "
                        f"target ≤{neutral_max_pct:.0%})"
                    )

        labels_out = labels + 1  # [-1,0,1] → [0,1,2] for multi:softprob
        pnl_r_out = np.nan_to_num(pnl_r, nan=0.0)
        ts_out = ts_valid
        features_out = features

        # ── Directional diagnostics (DQAF-20260703-062) ──
        n_short = int(np.sum(labels == -1))
        n_neutral = int(np.sum(labels == 0))
        n_long = int(np.sum(labels == 1))
        n_total = len(labels_out)
        n_non_neutral = n_short + n_long
        long_short_ratio = n_long / max(n_short, 1)
        print(
            f"[B2] 3-class directional labels: {n_total:,} samples "
            f"(LONG={n_long}, NEUTRAL={n_neutral}, SHORT={n_short})"
        )
        print(
            f"[B2] LONG:SHORT ratio = {long_short_ratio:.2f} "
            f"({'BALANCED' if 0.85 <= long_short_ratio <= 1.15 else 'IMBALANCED'})"
        )
        # Also show old-style distribution for comparison
        old_short = int(np.sum(labels_long == -1) + np.sum(labels_short == -1))
        old_long = int(np.sum(labels_long == 1) + np.sum(labels_short == 1))
        print(
            f"[B2] Reference (old merged): LONG signals={int(np.sum(labels_long == 1))}, "
            f"SHORT signals={int(np.sum(labels_long == -1))}, "
            f"NEUTRAL={int(np.sum(labels_long == 0))}"
        )
        ev = float(np.mean(pnl_r_out))

        # ── Time-decay weights ──
        print(f"[B2] Computing time-decay weights (half-life={decay_half_life_days}d)...")
        sample_weights = compute_time_decay_weights(ts_out, decay_half_life_days)

        # ── Class-balanced weighting (IC Mandate #1) ──
        from sklearn.utils.class_weight import compute_sample_weight as csw

        class_bal_weights = csw("balanced", labels_out)
        sample_weights = sample_weights * class_bal_weights
        print(
            f"  Class-balance multipliers: "
            f"SHORT={class_bal_weights[labels_out==0].mean():.2f}, "
            f"NEUTRAL={class_bal_weights[labels_out==1].mean():.2f}, "
            f"LONG={class_bal_weights[labels_out==2].mean():.2f}"
        )
        print(f"  Combined weight range: [{sample_weights.min():.3f}, {sample_weights.max():.3f}]")
        print(f"  Combined weight mean: {sample_weights.mean():.3f}")
    else:
        # Remove timeout (label=0) → binary classification TP vs SL
        non_timeout = labels != 0
        features_out = features[non_timeout]
        labels_out = labels[non_timeout]
        pnl_r_out = pnl_r[non_timeout]
        ts_out = ts_valid[non_timeout]

        n_total = len(labels_out)
        n_tp = int(np.sum(labels_out == 1))
        n_sl = int(np.sum(labels_out == -1))
        print(
            f"[B2] Binary samples: {n_total:,} (TP={n_tp}, SL={n_sl}, WR={n_tp/max(n_total,1):.1%})"
        )
        tp_samples_pnl = float(np.mean(pnl_r_out[labels_out == 1])) if n_tp > 0 else 0.0
        sl_samples_pnl = float(np.mean(pnl_r_out[labels_out == -1])) if n_sl > 0 else 0.0
        ev = float(np.mean(pnl_r_out))
        print(f"[B2] Avg PnL: TP={tp_samples_pnl:.3f}R, SL={sl_samples_pnl:.3f}R, EV={ev:.3f}R")

        # ── Time-decay weights ──
        print(f"[B2] Computing time-decay weights (half-life={decay_half_life_days}d)...")
        sample_weights = compute_time_decay_weights(ts_out, decay_half_life_days)
        print(f"  Weight range: [{sample_weights.min():.3f}, {sample_weights.max():.3f}]")
        print(f"  Weight mean: {sample_weights.mean():.3f}")

    # ── Walk-forward purged CV splits ──
    print(
        f"[B2] Generating walk-forward purged CV splits ({cv_folds} folds, {purge_bars} bar purge)..."
    )
    splits = walk_forward_purged_splits(n_total, ts_out, cv_folds, purge_bars)
    for s in splits:
        train_n = len(s["train_idx"])
        test_n = len(s["test_idx"])
        if multi_class:
            test_wr = float(np.mean(labels_out[s["test_idx"]] == 2)) if test_n > 0 else 0.0
            test_sr = float(np.mean(labels_out[s["test_idx"]] == 0)) if test_n > 0 else 0.0
            print(
                f"  Fold {s['fold']}: train={train_n:,}, test={test_n:,}, "
                f"test_WR(LONG)={test_wr:.1%}, test_SR(SHORT)={test_sr:.1%}"
            )
        else:
            test_wr = float(np.mean(labels_out[s["test_idx"]] == 1)) if test_n > 0 else 0.0
            print(f"  Fold {s['fold']}: train={train_n:,}, test={test_n:,}, test_WR={test_wr:.1%}")

    # ── Save dataset ──
    os.makedirs(output_dir, exist_ok=True)
    np.savez_compressed(
        os.path.join(output_dir, "train.npz"),
        X=features_out,
        y=labels_out,
        pnl_r=pnl_r_out,
        sample_weight=sample_weights,
        timestamps=ts_out,
    )
    # Save metadata
    meta: dict[str, Any] = {
        "schema_version": "btc_swing_v9.v1",
        "feature_names": ALL_FEATURE_NAMES,
        "n_features": N_FEATURES,
        "n_samples": int(n_total),
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
        "objective": "multi:softprob" if multi_class else "binary:logistic",
        "num_class": 3 if multi_class else 1,
        "built_at": datetime.now(UTC).isoformat(),
    }
    if multi_class:
        meta["n_short"] = int(np.sum(labels_out == 0))
        meta["n_neutral"] = int(np.sum(labels_out == 1))
        meta["n_long"] = int(np.sum(labels_out == 2))
    else:
        meta["n_tp"] = int(np.sum(labels_out == 1))
        meta["n_sl"] = int(np.sum(labels_out == -1))
    with open(os.path.join(output_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    # Save CV split indices
    cv_data = {
        "n_folds": len(splits),
        "purge_bars": purge_bars,
        "splits": [
            {k: v.tolist() if isinstance(v, np.ndarray) else v for k, v in s.items()}
            for s in splits
        ],
    }
    with open(os.path.join(output_dir, "cv_splits.json"), "w", encoding="utf-8") as f:
        json.dump(cv_data, f, indent=2)

    print(f"[B2] Dataset saved to {output_dir}/")
    print(f"  train.npz: X={features_out.shape}, y={labels_out.shape}")
    return meta


# ── Model Training ───────────────────────────────────────────────────────────


def train_xgboost(
    X_train: np.ndarray,
    y_train: np.ndarray,
    w_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    w_val: np.ndarray,
    params: dict[str, Any],
    multi_class: bool = False,
) -> tuple[Any, dict[str, float]]:
    """Train XGBoost classifier with sample weights.

    When multi_class=True: y values are class labels 0/1/2 (SHORT/NEUTRAL/LONG).
    Otherwise: y values are -1 (SL) / +1 (TP), binarized to 0/1.
    """
    import xgboost as xgb

    if multi_class:
        dtrain = xgb.DMatrix(X_train, label=y_train.astype(int), weight=w_train)
        dval = xgb.DMatrix(X_val, label=y_val.astype(int), weight=w_val)
    else:
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
    if multi_class:
        y_probs = model.predict(dval)  # shape (n, 3)
        y_pred = y_probs.argmax(axis=1).astype(int)
        accuracy = float(np.mean(y_pred == y_val.astype(int)))
        # Directional WR: (class 2 = LONG) vs (class 0 = SHORT), ignoring neutral
        directional_mask = y_val != 1  # exclude neutral
        if directional_mask.sum() > 0:
            long_pred_mask = y_pred == 2
            wr = (
                float(np.mean(y_val[long_pred_mask & directional_mask] == 2))
                if long_pred_mask.sum() > 0
                else 0.0
            )
        else:
            wr = 0.0
    else:
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
    X_train: np.ndarray,
    y_train: np.ndarray,
    w_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    w_val: np.ndarray,
    params: dict[str, Any],
    multi_class: bool = False,
) -> tuple[Any, dict[str, float]]:
    """Train LightGBM classifier with sample weights.

    When multi_class=True: y values are class labels 0/1/2 (SHORT/NEUTRAL/LONG).
    Otherwise: y values are -1 (SL) / +1 (TP), binarized to 0/1.
    """
    import lightgbm as lgb

    if multi_class:
        dtrain = lgb.Dataset(X_train, label=y_train.astype(int), weight=w_train)
        dval = lgb.Dataset(X_val, label=y_val.astype(int), weight=w_val, reference=dtrain)
    else:
        dtrain = lgb.Dataset(X_train, label=(y_train > 0).astype(int), weight=w_train)
        dval = lgb.Dataset(X_val, label=(y_val > 0).astype(int), weight=w_val, reference=dtrain)

    model = lgb.train(
        params,
        dtrain,
        valid_sets=[dval],
        valid_names=["val"],
    )

    if multi_class:
        y_probs = model.predict(X_val)  # shape (n, 3) for multiclass
        y_pred = y_probs.argmax(axis=1).astype(int)
        accuracy = float(np.mean(y_pred == y_val.astype(int)))
        directional_mask = y_val != 1
        if directional_mask.sum() > 0:
            long_pred_mask = y_pred == 2
            wr = (
                float(np.mean(y_val[long_pred_mask & directional_mask] == 2))
                if long_pred_mask.sum() > 0
                else 0.0
            )
        else:
            wr = 0.0
    else:
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
    multi_class: bool | None = None,
) -> dict[str, Any]:
    """B3: Train XGBoost + LightGBM with walk-forward purged CV evaluation.

    When multi_class=True: 3-class training (SHORT/NEUTRAL/LONG).
    When None (default): auto-detect from meta.json.
    """

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

    # Auto-detect multi_class from meta if not explicitly set
    if multi_class is None:
        multi_class = meta.get("num_class", 1) >= 3

    print(f"[B3] Data: {X.shape[0]:,} samples × {X.shape[1]} features")
    print(f"[B3] Mode: {'3-class (SHORT/NEUTRAL/LONG)' if multi_class else 'binary (TP/SL)'}")
    print(f"[B3] CV: {cv_data['n_folds']} folds, {cv_data['purge_bars']} bar purge")

    os.makedirs(output_dir, exist_ok=True)

    results: dict[str, list[dict[str, Any]]] = {"xgboost": [], "lightgbm": []}
    # Accumulate across folds for direction diversity check
    all_y_te: list[np.ndarray] = []
    all_y_probs_xgb: list[np.ndarray] = []

    for fold_split in cv_data["splits"]:
        fold = fold_split["fold"]
        train_idx = np.array(fold_split["train_idx"])
        test_idx = np.array(fold_split["test_idx"])

        X_tr, y_tr, w_tr = X[train_idx], y[train_idx], weights[train_idx]
        X_te, y_te = X[test_idx], y[test_idx]

        print(f"\n{'='*60}")
        print(f"[B3] Fold {fold}: train={len(train_idx):,}, test={len(test_idx):,}")
        if multi_class:
            n_short_te = int(np.sum(y_te == 0))
            n_neutral_te = int(np.sum(y_te == 1))
            n_long_te = int(np.sum(y_te == 2))
            print(f"[B3] Test dist: SHORT={n_short_te}, NEUTRAL={n_neutral_te}, LONG={n_long_te}")
        else:
            test_wr_baseline = float(np.mean(y_te == 1))
            print(f"[B3] Test baseline WR: {test_wr_baseline:.1%}")

        # ── XGBoost ──
        print(f"\n[B3] --- XGBoost (fold {fold}) ---")
        if multi_class:
            xgb_params = {
                "objective": "multi:softprob",
                "num_class": 3,
                "eval_metric": "mlogloss",
                "max_depth": 5,
                "learning_rate": 0.02,
                "n_estimators": 500,
                "subsample": 0.8,
                "colsample_bytree": 0.8,
                "min_child_weight": 5,
                "reg_alpha": 0.1,
                "reg_lambda": 1.0,
                "random_state": 42,
            }
        else:
            n_pos = int(np.sum(y_tr == 1))
            n_neg = int(np.sum(y_tr == -1))
            scale_pos_weight_val = n_neg / max(n_pos, 1)
            print(
                f"  Class balance: {n_pos} TP / {n_neg} SL, scale_pos_weight={scale_pos_weight_val:.1f}"
            )
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
        model_xgb, metrics_xgb = train_xgboost(
            X_tr,
            y_tr,
            w_tr,
            X_te,
            y_te,
            weights[test_idx],
            xgb_params,
            multi_class=multi_class,
        )
        print(
            f"  val_acc={metrics_xgb['val_accuracy']:.3f}, val_wr={metrics_xgb['val_wr']:.3f}, trees={metrics_xgb['n_trees']}"
        )
        results["xgboost"].append({"fold": fold, "metrics": metrics_xgb})

        # Save XGBoost model
        xgb_path = os.path.join(output_dir, f"xgboost_fold{fold}_s42.json")
        model_xgb.save_model(xgb_path)
        print(f"  Saved: {xgb_path}")

        # ── LightGBM ──
        print(f"\n[B3] --- LightGBM (fold {fold}) ---")
        if multi_class:
            lgb_params = {
                "objective": "multiclass",
                "num_class": 3,
                "metric": "multi_logloss",
                "max_depth": 5,
                "learning_rate": 0.02,
                "n_estimators": 500,
                "num_leaves": 31,
                "subsample": 0.8,
                "colsample_bytree": 0.8,
                "min_child_samples": 20,
                "reg_alpha": 0.1,
                "reg_lambda": 1.0,
                "random_state": 42,
                "verbose": -1,
            }
        else:
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
        model_lgb, metrics_lgb = train_lightgbm(
            X_tr,
            y_tr,
            w_tr,
            X_te,
            y_te,
            weights[test_idx],
            lgb_params,
            multi_class=multi_class,
        )
        print(
            f"  val_acc={metrics_lgb['val_accuracy']:.3f}, val_wr={metrics_lgb['val_wr']:.3f}, trees={metrics_lgb['n_trees']}"
        )
        results["lightgbm"].append({"fold": fold, "metrics": metrics_lgb})

        # Save LightGBM model
        lgb_path = os.path.join(output_dir, f"lightgbm_fold{fold}_s42.txt")
        model_lgb.save_model(lgb_path)
        print(f"  Saved: {lgb_path}")

        # Accumulate for direction diversity check (multi-class only)
        if multi_class:
            all_y_te.append(y_te)
            # Reload XGBoost to get prediction probs
            import xgboost as xgb

            dmat_te = xgb.DMatrix(X_te)
            all_y_probs_xgb.append(model_xgb.predict(dmat_te))

    # ── Summary ──
    print(f"\n{'='*60}")
    print("[B3] === Walk-Forward CV Summary ===")
    for arch in ["xgboost", "lightgbm"]:
        wrs = [r["metrics"]["val_wr"] for r in results[arch]]
        accs = [r["metrics"]["val_accuracy"] for r in results[arch]]
        print(
            f"  {arch}: WR={np.mean(wrs):.2%} +/- {np.std(wrs):.2%}, Acc={np.mean(accs):.2%} +/- {np.std(accs):.2%}"
        )

    # ── Save full results ──
    summary: dict[str, Any] = {
        "schema_version": "btc_swing_v9_training.v1",
        "data_dir": data_dir,
        "objective": "multi:softprob" if multi_class else "binary:logistic",
        "num_class": 3 if multi_class else 1,
        "cv_summary": {
            arch: {
                "mean_val_wr": float(np.mean([r["metrics"]["val_wr"] for r in results[arch]])),
                "std_val_wr": float(np.std([r["metrics"]["val_wr"] for r in results[arch]])),
                "mean_val_acc": float(
                    np.mean([r["metrics"]["val_accuracy"] for r in results[arch]])
                ),
                "folds": len(results[arch]),
            }
            for arch in ["xgboost", "lightgbm"]
        },
        "params": {
            "xgboost": {
                "max_depth": 5,
                "learning_rate": 0.02,
                "n_estimators": 500,
            },
            "lightgbm": {
                "max_depth": 5,
                "learning_rate": 0.02,
                "num_leaves": 31,
            },
        },
        "trained_at": datetime.now(UTC).isoformat(),
    }
    with open(os.path.join(output_dir, "training_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    # ── Direction Diversity Gate (multi-class only) ──
    if multi_class and all_y_probs_xgb:
        print(f"\n{'='*60}")
        print("[B3] === Direction Diversity Gate ===")
        y_all = np.concatenate(all_y_te)
        probs_all = np.concatenate(all_y_probs_xgb)  # shape (n, 3): [SHORT, NEUTRAL, LONG]

        # 4a: Direction distribution
        raw_scores = probs_all[:, 2] - probs_all[:, 0]  # LONG - SHORT
        n_long = int(np.sum(raw_scores > 0.1))
        n_short = int(np.sum(raw_scores < -0.1))
        n_neutral = int(np.sum((raw_scores >= -0.1) & (raw_scores <= 0.1)))
        print(f"[B3] Direction diversity: LONG={n_long}, NEUTRAL={n_neutral}, SHORT={n_short}")
        short_ratio = n_short / len(raw_scores)
        gate_warnings: list[str] = []
        if n_short == 0:
            gate_warnings.append("ZERO SHORT predictions — model may be degenerate!")

        # 4b: Wasserstein distance with conditional slicing (IC Mandate #3)
        try:
            from scipy.stats import wasserstein_distance

            p_neutral = probs_all[:, 1]
            p_long = probs_all[:, 2]
            p_short = probs_all[:, 0]

            # Conditional slice: exclude dormant samples (P_neutral > 0.60)
            active_mask = p_neutral < 0.60
            n_active = int(np.sum(active_mask))

            if n_active > 50:
                w_dist = wasserstein_distance(p_long[active_mask], p_short[active_mask])
                pool_label = "active"
            else:
                w_dist = wasserstein_distance(p_long, p_short)
                pool_label = "full (fallback — <50 active samples)"

            max_long_prob = float(np.max(p_long))
            max_short_prob = float(np.max(p_short))

            print(
                f"[B3] Wasserstein({pool_label})={w_dist:.4f}, "
                f"active={n_active}/{len(probs_all)}, "
                f"max_LONG={max_long_prob:.3f}, max_SHORT={max_short_prob:.3f}"
            )

            if w_dist < 0.05:
                gate_warnings.append(
                    f"Wasserstein distance {w_dist:.4f} < 0.05 — "
                    "LONG/SHORT distributions may be indistinguishable!"
                )
            if max_long_prob < 0.25:
                gate_warnings.append(
                    f"Max P(LONG)={max_long_prob:.3f} < 0.25 — "
                    "directional confidence too weak on LONG side!"
                )
            if max_short_prob < 0.25:
                gate_warnings.append(
                    f"Max P(SHORT)={max_short_prob:.3f} < 0.25 — "
                    "directional confidence too weak on SHORT side!"
                )

            diversity = {
                "n_long": n_long,
                "n_short": n_short,
                "n_neutral": n_neutral,
                "short_ratio": float(short_ratio),
                "wasserstein_long_short": float(w_dist),
                "wasserstein_pool": pool_label,
                "n_active_samples": n_active,
                "max_long_prob": max_long_prob,
                "max_short_prob": max_short_prob,
                "warnings": gate_warnings,
            }
        except ImportError:
            print("[B3] (scipy not available — skipping Wasserstein check)")
            diversity = {
                "n_long": n_long,
                "n_short": n_short,
                "n_neutral": n_neutral,
                "short_ratio": float(short_ratio),
                "warnings": gate_warnings,
            }

        summary["direction_diversity"] = diversity

        for w in gate_warnings:
            print(f"[B3] WARNING: {w}")
        if not gate_warnings:
            print("[B3] Direction diversity gate PASSED")

    print(f"\n[B3] Training complete. Results saved to {output_dir}/")
    return summary


# ── CLI ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="BTC Swing V9 Training Pipeline")
    parser.add_argument("--full", action="store_true", help="Run full pipeline (build + train)")
    parser.add_argument("--build-only", action="store_true", help="Build dataset only")
    parser.add_argument(
        "--train-only", action="store_true", help="Train only (requires pre-built dataset)"
    )
    parser.add_argument(
        "--csv", default="data/raw/btcusdc_h1_merged.csv", help="BTC OHLC CSV path (H1 default)"
    )
    parser.add_argument(
        "--data-dir", default="data/training/btc_swing_v9_h1", help="Dataset output dir"
    )
    parser.add_argument(
        "--model-dir", default="data/models/btc_swing_v9_h1", help="Model output dir"
    )
    parser.add_argument(
        "--timeframe-minutes", type=float, default=60.0, help="Bar interval in minutes (H1=60)"
    )
    parser.add_argument("--horizon", type=int, default=24, help="Forward barrier horizon in bars")
    parser.add_argument(
        "--sl-atr-mult", type=float, default=3.0, help="SL distance in ATR multiples"
    )
    parser.add_argument(
        "--tp-atr-mult", type=float, default=2.0, help="TP distance in ATR multiples"
    )
    parser.add_argument("--spread-points", type=float, default=10.0, help="Spread in price points")
    parser.add_argument(
        "--slippage-points", type=float, default=10.0, help="Slippage in price points"
    )
    parser.add_argument(
        "--decay-half-life-days", type=float, default=180.0, help="Time-decay half-life"
    )
    parser.add_argument("--cv-folds", type=int, default=5, help="Number of walk-forward CV folds")
    parser.add_argument(
        "--purge-bars", type=int, default=24, help="Purge window between train/test (match horizon)"
    )
    parser.add_argument("--optuna-trials", type=int, default=50, help="Optuna TPE trials")
    parser.add_argument("--n-seeds", type=int, default=3, help="Number of random seeds")
    parser.add_argument(
        "--multi-class",
        action="store_true",
        help="Train 3-class model (SHORT/NEUTRAL/LONG) instead of binary (TP/SL only)",
    )
    # ── Sample Curation (FIX-20260705-021) ──────────────────────────────────
    parser.add_argument(
        "--min-adx",
        type=float,
        default=0.0,
        help="Minimum D1 ADX for bar curation (0=disabled). Recommended: 20 for trend filter.",
    )
    parser.add_argument(
        "--exclude-weekends",
        action="store_true",
        help="Exclude weekend bars (Fri 22:00 – Sun 22:00 UTC) from training",
    )
    parser.add_argument(
        "--atr-pctile-low",
        type=float,
        default=0.0,
        help="Exclude bars below this ATR percentile (0=disabled). Recommended: 5 for dead-zone removal.",
    )
    parser.add_argument(
        "--atr-pctile-high",
        type=float,
        default=0.0,
        help="Exclude bars above this ATR percentile (0=disabled). Recommended: 95 for crash-spike removal.",
    )
    parser.add_argument(
        "--neutral-max-pct",
        type=float,
        default=None,
        help="Maximum fraction of NEUTRAL labels in training data (None=no downsampling). "
        "Recommended: 0.40 for high-timeframe models where timeout bars dominate. "
        "Prevents model collapse to 'always predict NEUTRAL'.",
    )
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
            multi_class=args.multi_class,
            min_adx=args.min_adx,
            exclude_weekends=args.exclude_weekends,
            atr_pctile_low=args.atr_pctile_low,
            atr_pctile_high=args.atr_pctile_high,
            neutral_max_pct=args.neutral_max_pct,
        )

    if do_train:
        summary = train_models(
            data_dir=args.data_dir,
            output_dir=args.model_dir,
            optuna_trials=args.optuna_trials,
            n_seeds=args.n_seeds,
            multi_class=args.multi_class,
        )

    print("\n[DONE] BTC Swing V9 pipeline complete.")


if __name__ == "__main__":
    main()
