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

# FIX-20260604-081: BTC-specific feature lists (37-dim)
BTC_MACRO_FEATURES_24 = [
    "D1_Ret_1", "D1_Body_Ratio", "D1_ATR_14", "D1_RSI_14",
    "D1_MACD", "D1_Vol_ZScore", "D1_Bollinger_Width", "D1_ADX_14",
    "H4_Trend_Strength", "H4_ATR_Ratio", "H4_RSI_Divergence", "H4_vs_D1_Alignment",
    "XAUUSDc_return", "Cross_DXY_Return", "Cross_EURUSD_Return", "Cross_Risk_On_Off",
    "Derived_Weekday_Sin", "Derived_Weekday_Cos", "Derived_Days_To_MonthEnd",
    "Derived_Is_MonthEnd_Week", "Derived_Weekend_Gap", "Derived_Vol_Regime",
    "Derived_Momentum_5D", "Derived_Momentum_20D",
]
BTC_MICRO_FEATURES_9 = [
    "tick_return", "hl_ratio", "co_ratio", "avg_spread",
    "OIM", "tick_velocity", "AUDJPYc_return", "EURUSDc_return", "USDJPYc_return",
]
BTC_MACRO_FEATURES_2 = ["Cross_BTC_Gold_Ratio", "Cross_BTC_Gold_Ratio_ROC"]

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
    # FIX-081: added AUDJPY, XAUUSD, BTC/XAU ratio + ROC
    if cross_data is not None:
        for sym_key, feat_key in [
            ("xag_close", "XAGUSDc_return"),
            ("eur_close", "EURUSDc_return"),
            ("jpy_close", "USDJPYc_return"),
            ("audjpy_close", "AUDJPYc_return"),
            ("xau_close", "XAUUSDc_return"),
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
        # BTC/XAU ratio + ROC (pre-computed in micro_cross)
        for key in ("btc_xau_ratio", "btc_xau_ratio_roc"):
            arr = cross_data.get(key)
            if arr is not None and bar_idx < len(arr):
                result["Cross_BTC_Gold_Ratio" if key == "btc_xau_ratio" else "Cross_BTC_Gold_Ratio_ROC"] = float(arr[bar_idx])
    else:
        for feat_key in ["XAGUSDc_return", "EURUSDc_return", "USDJPYc_return",
                         "AUDJPYc_return", "XAUUSDc_return",
                         "Cross_BTC_Gold_Ratio", "Cross_BTC_Gold_Ratio_ROC"]:
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
    spread_points: float = 30,
    slippage_points: float = 10,
    tick_size: float = 0.01,
    side: str = "long",
) -> np.ndarray:
    """Compute barrier labels: -1=SL, 0=timeout, 1=TP.

    For each bar, look ahead `horizon` bars. If price hits SL first → -1,
    TP first → 1, neither → 0.

    When sl_atr_mult/tp_atr_mult are provided, they override atr_mult
    for asymmetric SL/TP (dual-track label generation).

    Friction modeling (Phase 4 / C4.2):
      Friction makes TP harder (further) and SL easier (closer) because
      the trader enters with a half-spread loss built in.
        LONG:  effective_tp = entry + tp_dist + friction_cost
               effective_sl = entry - max(0, sl_dist - friction_cost)
        SHORT: effective_tp = entry - tp_dist - friction_cost
               effective_sl = entry + max(0, sl_dist - friction_cost)
    """
    _sl_mult = sl_atr_mult if sl_atr_mult is not None else atr_mult
    _tp_mult = tp_atr_mult if tp_atr_mult is not None else atr_mult
    friction_points = spread_points + slippage_points
    friction_cost = friction_points * tick_size
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

        # Apply friction: TP harder (further), SL easier (closer)
        if side == "long":
            effective_tp = entry + tp_dist + friction_cost
            effective_sl = entry - max(0.0, sl_dist - friction_cost)
        else:
            effective_tp = entry - tp_dist - friction_cost
            effective_sl = entry + max(0.0, sl_dist - friction_cost)

        for j in range(1, horizon + 1):
            idx = i + j
            if idx >= n:
                break
            if side == "long":
                if low[idx] <= effective_sl:
                    labels[i] = -1  # SL hit
                    break
                if high[idx] >= effective_tp:
                    labels[i] = 1  # TP hit
                    break
            else:
                if high[idx] >= effective_sl:
                    labels[i] = -1  # SL hit
                    break
                if low[idx] <= effective_tp:
                    labels[i] = 1  # TP hit
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
    cross_raw_dir: Path | None = None,
    symbol: str = "xauusdc",
    sl_atr_mult: float | None = None,
    tp_atr_mult: float | None = None,
    spread_points: float = 30,
    slippage_points: float = 10,
    tick_size: float = 0.01,
) -> dict[str, Any]:
    """Build enhanced swing dataset.

    Args:
        tf: Target timeframe.
        horizon: Label barrier horizon in bars.
        output_dir: Where to save NPZ files.
        val_ratio: Fraction for validation.
        test_ratio: Fraction for test.
        raw_dir: Directory containing raw CSV data.
        symbol: Symbol prefix for CSV files (e.g. "xauusdc", "btcusdc").
            FIX-20260531-024: was hardcoded to xauusdc.
    """
    _sym = symbol.lower()
    _data_dir = raw_dir or Path("data/raw")
    n_m5 = M5_PER_TF.get(tf, 1) if tf not in M5_PER_TF else M5_PER_TF[tf]

    # ── Load target-TF XAUUSD data ──
    tf_csv = _data_dir / f"{symbol}_{tf.lower()}_merged.csv"
    print(f"[swing:{tf}] Loading {tf_csv}...")
    tf_ohlc = load_ohlc_csv(tf_csv)
    print(f"  {tf_ohlc['n_bars']} bars loaded")

    # ── Load M5 XAUUSD for micro features ──
    m5_csv = _data_dir / f"{_sym}_m5_merged.csv"
    m5_ohlc = load_ohlc_csv(m5_csv)
    print(f"  M5: {m5_ohlc['n_bars']} bars loaded")

    # ── Load cross-symbol data (FIX-20260604-081) ──
    # BTC 24/7 trading vs forex/gold 24/5 → weekend bars need ffill.
    # First try --raw-dir, fall back to --cross-raw-dir (global macro data lake).
    # FIX-081: added AUDJPYc (risk appetite) and XAUUSDc (physical gold).
    _cross_fallback_dir = cross_raw_dir or Path("data/raw")
    cross_data: dict[str, tuple[np.ndarray, list[pd.Timestamp]]] = {}
    for sym_name, csv_name in [
        ("silver", "xagusdc_m5_merged.csv"),
        ("eur", "eurusdc_m5_merged.csv"),
        ("dxy", "usdjpyc_m5_merged.csv"),
        ("audjpy", "audjpyc_m5_merged.csv"),      # NEW: risk appetite proxy
        ("xau", "xauusdc_m5_merged.csv"),          # NEW: physical gold for BTC/XAU ratio
    ]:
        sym_path = _data_dir / csv_name
        if not sym_path.exists():
            sym_path = _cross_fallback_dir / csv_name
        if sym_path.exists():
            sym_ohlc = load_ohlc_csv(sym_path)
            cross_data[sym_name] = (sym_ohlc["close"], sym_ohlc["timestamp"])
            print(f"  {sym_name}: {sym_ohlc['n_bars']} bars loaded (from {sym_path})")
        else:
            print(f"  [WARN] {sym_name} ({csv_name}) not found in raw-dir or cross-raw-dir")

    # ── Initialize DailyFeatureComputer for SSOT 24-dim macro features ──
    d1_csv_path = _data_dir / f"{_sym}_d1_merged.csv"
    h4_csv_path = _data_dir / f"{_sym}_h4_merged.csv"
    cross_asset_paths: dict[str, str | Path] = {}
    for name, csv_name in [
        ("XAGUSDc", "xagusdc_d1_merged.csv"),
        ("EURUSDc", "eurusdc_d1_merged.csv"),
        ("AUDJPYc", "audjpyc_d1_merged.csv"),      # NEW
        ("XAUUSDc", "xauusdc_d1_merged.csv"),       # NEW: BTC/XAU ratio source
    ]:
        p = _data_dir / csv_name
        if not p.exists():
            p = _cross_fallback_dir / csv_name
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
        tf_ohlc, atr_mult=1.5, horizon=horizon,
        sl_atr_mult=sl_atr_mult, tp_atr_mult=tp_atr_mult,
        spread_points=spread_points, slippage_points=slippage_points,
        tick_size=tick_size, side="long",
    )

    # ── Build feature matrix ──
    n_bars = tf_ohlc["n_bars"]
    # FIX-20260604-081: BTC uses 37-dim schema
    _is_btc = _sym == "btcusdc"
    _macro_feats = BTC_MACRO_FEATURES_24 if _is_btc else SWING_MACRO_FEATURES
    _micro_feats = BTC_MICRO_FEATURES_9 if _is_btc else MICRO_FEATURES
    _extra_feats = BTC_MACRO_FEATURES_2 if _is_btc else []
    _all_feats = _macro_feats + _micro_feats + TF_SPECIFIC_FEATURES + _extra_feats
    _n_feats = len(_all_feats)
    print(f"  Features: {_n_feats}-dim schema {'(BTC 37)' if _is_btc else '(XAU 35)'}")

    # ── FIX-138: load XAUUSDc D1 close prices for BTC slot [12] override ──
    _xau_d1_closes: list[float] | None = None
    _xau_d1_dates: list | None = None  # for timestamp-based alignment
    if _is_btc:
        _xau_d1_path = _data_dir / "xauusdc_d1_merged.csv"
        if not _xau_d1_path.exists():
            _xau_d1_path = _cross_fallback_dir / "xauusdc_d1_merged.csv"
        if _xau_d1_path.exists():
            _xau_d1_data = load_ohlc_csv(_xau_d1_path)
            _xau_d1_closes = list(_xau_d1_data["close"])
            _xau_d1_dates = list(_xau_d1_data["timestamp"])
            print(f"  XAUUSDc D1: {len(_xau_d1_closes)} bars loaded for slot [12] override")
        else:
            print("  [WARN] XAUUSDc D1 not found — slot [12] will be zero-filled")

    # Need enough history for features
    start_idx = max(100, n_m5 * 2)  # skip first bars for feature stability
    valid_count = 0

    X = np.zeros((n_bars - start_idx, _n_feats), dtype=np.float32)
    y = np.zeros(n_bars - start_idx, dtype=np.int32)
    bar_indices = np.zeros(n_bars - start_idx, dtype=np.int32)

    # Prepare cross-data for micro features
    micro_cross: dict[str, np.ndarray] = {}
    for sym_name, (sym_close, sym_ts) in cross_data.items():
        # Align to M5 timestamps
        if sym_name == "silver":
            key = "xag_close"
        elif sym_name == "eur":
            key = "eur_close"
        elif sym_name == "audjpy":
            key = "audjpy_close"
        elif sym_name == "xau":
            key = "xau_close"
        else:
            key = "jpy_close"
        # Use searchsorted for alignment
        sym_ts_arr = np.array([ts.timestamp() for ts in sym_ts], dtype=np.float64)
        idxs_raw = np.searchsorted(sym_ts_arr, m5_ts_arr, side="right") - 1
        idxs: np.ndarray = np.asarray(idxs_raw, dtype=np.intp)
        valid_mask = (idxs >= 0) & (idxs < len(sym_close))
        aligned = np.full(len(m5_ts_arr), np.nan, dtype=np.float64)
        aligned[valid_mask] = np.asarray(sym_close)[idxs[valid_mask]]
        # P0 Guardrail 1: ffill BEFORE ROC computation.
        # Weekend bars have no cross-pair data — forward-fill from Friday close.
        # NEVER fill zero.  Zeros = fake training signal.
        aligned = pd.Series(aligned).ffill().bfill().to_numpy(dtype=np.float64)
        micro_cross[key] = aligned

    # ── FIX-20260604-081 P0: BTC/XAU ratio + ROC (ffill already done above) ──
    if "xau_close" in micro_cross and len(m5_ohlc["close"]) == len(micro_cross["xau_close"]):
        btc_close = np.asarray(m5_ohlc["close"], dtype=np.float64)
        xau_close = micro_cross["xau_close"]
        btc_xau_ratio = np.full(len(btc_close), np.nan, dtype=np.float64)
        mask = (xau_close > 0) & (btc_close > 0)
        btc_xau_ratio[mask] = btc_close[mask] / xau_close[mask]
        # P0 Guardrail 1 (cont.): ROC on ffill'd ratio sequence
        btc_xau_ratio = pd.Series(btc_xau_ratio).ffill().bfill().to_numpy(dtype=np.float64)
        btc_xau_roc = pd.Series(btc_xau_ratio).pct_change(periods=5).fillna(0.0).to_numpy(dtype=np.float64)
        micro_cross["btc_xau_ratio"] = btc_xau_ratio
        micro_cross["btc_xau_ratio_roc"] = btc_xau_roc

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
                # ── FIX-138: BTC slot [12] is XAUUSDc_return, not Cross_Gold_Silver_Ratio ──
                if _is_btc and _xau_d1_closes is not None and _xau_d1_dates is not None:
                    # ── Align XAU D1 to BTC D1 bar by timestamp ──
                    _btc_dt = daily_computer._d1_datetimes[d1_idx_tracker]
                    _xau_idx = 0
                    for _j in range(len(_xau_d1_dates)):
                        if _xau_d1_dates[_j] <= _btc_dt:
                            _xau_idx = _j
                        else:
                            break
                    if _xau_idx > 0 and _xau_idx < len(_xau_d1_closes):
                        _xau_curr = _xau_d1_closes[_xau_idx]
                        _xau_prev = _xau_d1_closes[_xau_idx - 1]
                        if _xau_prev > 0:
                            macro["XAUUSDc_return"] = (
                                (_xau_curr - _xau_prev) / _xau_prev * 100.0
                            )
                        else:
                            macro["XAUUSDc_return"] = 0.0
                    else:
                        macro["XAUUSDc_return"] = 0.0
                elif _is_btc:
                    macro["XAUUSDc_return"] = 0.0
            else:
                macro = dict.fromkeys(_macro_feats, 0.0)
        else:
            macro = dict.fromkeys(_macro_feats, 0.0)

        # ── Micro features (single M5 snapshot, matches inference) ──
        micro = compute_micro_features_at_bar(m5_ohlc, micro_cross, m5_idx)

        # ── TF-specific features ──
        m5_close_window = m5_ohlc["close"][max(0, m5_idx - OU_LOOKBACK) : m5_idx + 1]
        tf_ou = _ou_theta(m5_close_window, lookback=min(OU_LOOKBACK, len(m5_close_window)))
        tf_hurst = _hurst(m5_close_window, max_lag=min(HURST_MAX_LAG, len(m5_close_window)))

        # ── Assemble feature vector ──
        row_idx = valid_count
        col = 0
        for name in _macro_feats:
            X[row_idx, col] = float(macro.get(name, 0.0))
            col += 1
        for name in _micro_feats:
            X[row_idx, col] = float(micro.get(name, 0.0))
            col += 1
        X[row_idx, col] = tf_ou
        col += 1
        X[row_idx, col] = tf_hurst
        col += 1
        # FIX-081: BTC-specific extra features (BTC/XAU ratio + ROC)
        for name in _extra_feats:
            val = micro.get(name, 0.0) if isinstance(micro, dict) else 0.0
            X[row_idx, col] = float(val)
            col += 1

        # FIX-20260530-074: removed nan_to_num — XGBoost natively handles NaN
        # via its `missing` parameter.  0.0 is a valid value (zero return, zero
        # spread) and converting NaN→0.0 was feeding the model false information.
        # Inf values are already prevented upstream by physical price bounds.

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
        feature_names=np.array(_all_feats, dtype=str),
    )

    # Save metadata
    import json

    meta = {
        "tf": tf,
        "horizon": horizon,
        "n_features": _n_feats,
        "feature_names": list(_all_feats),
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
        "--tf", default="M30", choices=["M5", "M15", "M30", "H1", "H4"], help="Target timeframe (default: M30)"
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
        "--cross-raw-dir",
        default="data/raw",
        help="Fallback directory for cross-asset CSV data (XAG, EUR, JPY). "
        "Used when --raw-dir lacks these files (e.g. BTC-only directory). "
        "Default: data/raw (global macro data lake).",
    )
    parser.add_argument("--symbol", default="xauusdc", help="Symbol prefix for CSV files (e.g. xauusdc, btcusdc)")
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
    parser.add_argument(
        "--spread-points",
        type=float,
        default=30,
        help="Spread in MT5 points for friction modeling (default: 30)",
    )
    parser.add_argument(
        "--slippage-points",
        type=float,
        default=10,
        help="Slippage in MT5 points for friction modeling (default: 10)",
    )
    parser.add_argument(
        "--tick-size",
        type=float,
        default=0.01,
        help="MT5 tick size for cost calculation (default: 0.01)",
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
        cross_raw_dir=Path(args.cross_raw_dir),
        symbol=args.symbol,
        spread_points=args.spread_points,
        slippage_points=args.slippage_points,
        tick_size=args.tick_size,
    )


if __name__ == "__main__":
    main()
