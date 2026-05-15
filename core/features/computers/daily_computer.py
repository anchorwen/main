"""Daily Swing feature computer — offline, CSV-based, vectorized.

Computes 24 features from D1 OHLC data with optional H4 and cross-asset integration.
All technical features are pre-computed as full arrays (vectorized), eliminating the
O(N²) per-row slicing pattern.  H4 alignment uses timestamp-based merge_asof to
prevent look-ahead bias.  Temporal features use sin/cos cyclical encoding.

Usage::

    from core.features.computers.daily_computer import DailyFeatureComputer
    comp = DailyFeatureComputer(
        d1_csv="data/raw/xauusdc_d1_merged.csv",
        h4_csv="data/raw/xauusdc_h4_merged.csv",
        cross_assets={
            "XAGUSDc": "data/raw/xagusdc_d1_merged.csv",
            "EURUSDc": "data/raw/eurusdc_d1_merged.csv",
        },
    )
    features, timestamps = comp.compute_all()
    # features.shape == (N, 24)
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import numpy as np

from core.features.schemas.daily_swing_schema import DAILY_SWING_24_FEATURES

# ── Lookback constants ──
ATR_PERIOD = 14
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
VOL_ZS_LOOKBACK = 20
ADX_PERIOD = 14
BB_PERIOD = 20
MOM_5D = 5
MOM_20D = 20
MIN_LOOKBACK = (
    max(
        ATR_PERIOD,
        RSI_PERIOD,
        MACD_SLOW + MACD_SIGNAL,
        VOL_ZS_LOOKBACK,
        ADX_PERIOD,
        BB_PERIOD,
        MOM_20D,
    )
    + 2
)


# ═══════════════════════════════════════════════════════════════════════════════
# CSV loader
# ═══════════════════════════════════════════════════════════════════════════════


def _load_ohlc_csv(
    csv_path: str | Path,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Load OHLC CSV, return (opens, highs, lows, closes, volumes, timestamps)."""
    with open(csv_path, encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"No header in {csv_path}")
        col_map: dict[str, str] = {}
        for key in reader.fieldnames:
            kl = key.strip().lower()
            if kl in ("time", "datetime", "date", "timestamp"):
                col_map["time"] = key
            elif kl in ("open", "o"):
                col_map["open"] = key
            elif kl in ("high", "h"):
                col_map["high"] = key
            elif kl in ("low", "l"):
                col_map["low"] = key
            elif kl in ("close", "c"):
                col_map["close"] = key
            elif kl == "tick_volume":
                col_map["tick_volume"] = key

        rows_o: list[float] = []
        rows_h: list[float] = []
        rows_l: list[float] = []
        rows_c: list[float] = []
        rows_v: list[float] = []
        timestamps: list[str] = []
        for row in reader:
            try:
                timestamps.append(str(row[col_map["time"]]))
                rows_o.append(float(row[col_map["open"]]))
                rows_h.append(float(row[col_map["high"]]))
                rows_l.append(float(row[col_map["low"]]))
                rows_c.append(float(row[col_map["close"]]))
                rows_v.append(float(row.get(col_map.get("tick_volume", ""), 0) or 0))
            except (ValueError, KeyError):
                continue

    return (
        np.array(rows_o, dtype=np.float64),
        np.array(rows_h, dtype=np.float64),
        np.array(rows_l, dtype=np.float64),
        np.array(rows_c, dtype=np.float64),
        np.array(rows_v, dtype=np.float64),
        timestamps,
    )


def _parse_timestamps(ts_list: list[str]) -> list[datetime]:
    """Parse a list of timestamp strings to datetime objects."""
    dts: list[datetime] = []
    for ts in ts_list:
        ts_s = ts.strip()
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S",
            "%Y.%m.%d",
            "%Y.%m.%d %H:%M:%S",
            "%d/%m/%Y",
        ):
            try:
                dts.append(datetime.strptime(ts_s[:19] if len(ts_s) >= 19 else ts_s, fmt))
                break
            except ValueError:
                continue
        else:
            dts.append(datetime(2000, 1, 1))  # fallback
    return dts


# ═══════════════════════════════════════════════════════════════════════════════
# Vectorized feature pre-computation helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _rolling_mean(arr: np.ndarray, window: int) -> np.ndarray:
    """Rolling mean — result[i] = mean(arr[i-window+1:i+1]).  First window-1 values are NaN."""
    if len(arr) < window:
        return np.full(len(arr), np.nan)
    kernel = np.ones(window) / window
    result = np.convolve(arr, kernel, mode="full")[: len(arr)]
    result[: window - 1] = np.nan
    return result


def _rolling_std(arr: np.ndarray, window: int) -> np.ndarray:
    """Rolling standard deviation."""
    if len(arr) < window:
        return np.full(len(arr), np.nan)
    mean_arr = _rolling_mean(arr, window)
    # Compute rolling sum of squares
    sq_arr = arr * arr
    sq_mean = _rolling_mean(sq_arr, window)
    var = np.maximum(sq_mean - mean_arr * mean_arr, 0.0)
    return np.sqrt(var)


def _ema_vectorized(arr: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average over full array."""
    if len(arr) == 0:
        return arr.copy()
    alpha = 2.0 / (period + 1.0)
    result = np.empty(len(arr))
    result[0] = arr[0]
    for i in range(1, len(arr)):
        result[i] = alpha * arr[i] + (1.0 - alpha) * result[i - 1]
    return result


def _compute_true_range(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> np.ndarray:
    """Vectorized true range."""
    tr = np.zeros(len(closes))
    tr[1:] = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])),
    )
    tr[0] = highs[0] - lows[0]
    return tr


def _compute_atr_array(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = ATR_PERIOD
) -> np.ndarray:
    """Compute full-length ATR array using Wilder's smoothing."""
    tr = _compute_true_range(highs, lows, closes)
    n = len(tr)
    atr = np.zeros(n)
    if n <= period:
        return atr
    atr[period] = np.mean(tr[1 : period + 1])
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def _compute_rsi_array(closes: np.ndarray, period: int = RSI_PERIOD) -> np.ndarray:
    """Compute full-length RSI array using Wilder's smoothing."""
    n = len(closes)
    rsi = np.full(n, 50.0)
    if n < period + 1:
        return rsi
    deltas = np.diff(closes)
    gains = np.maximum(deltas, 0.0)
    losses = np.abs(np.minimum(deltas, 0.0))

    avg_gain = np.zeros(n)
    avg_loss = np.zeros(n)
    avg_gain[period] = np.mean(gains[:period])
    avg_loss[period] = np.mean(losses[:period])

    for i in range(period + 1, n):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i - 1]) / period

    with np.errstate(invalid="ignore"):
        rs = np.where(avg_loss == 0, 100.0, avg_gain / avg_loss)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    return rsi


def _compute_macd_array(closes: np.ndarray) -> np.ndarray:
    """Compute MACD line (fast EMA - slow EMA) for full array."""
    ema_fast = _ema_vectorized(closes, MACD_FAST)
    ema_slow = _ema_vectorized(closes, MACD_SLOW)
    return ema_fast - ema_slow


def _compute_bollinger_width_array(closes: np.ndarray, period: int = BB_PERIOD) -> np.ndarray:
    """Compute Bollinger Band width for full array."""
    ma = _rolling_mean(closes, period)
    std = _rolling_std(closes, period)
    width = np.zeros(len(closes))
    mask = ma > 0
    width[mask] = 2.0 * std[mask] / ma[mask]
    return width


def _compute_adx_array(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = ADX_PERIOD
) -> np.ndarray:
    """Compute ADX array using Wilder's smoothing."""
    n = len(closes)
    adx = np.full(n, 20.0)
    if n < period * 2:
        return adx

    up_move = np.diff(highs)
    down_move = -np.diff(lows)
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr_arr = _compute_true_range(highs, lows, closes)[1:]

    atr_arr = np.zeros(len(tr_arr))
    if len(tr_arr) >= period:
        atr_arr[period - 1] = np.mean(tr_arr[:period])
        for i in range(period, len(atr_arr)):
            atr_arr[i] = (atr_arr[i - 1] * (period - 1) + tr_arr[i]) / period

    smoothed_plus = np.zeros(len(plus_dm))
    smoothed_minus = np.zeros(len(minus_dm))
    if len(plus_dm) >= period:
        smoothed_plus[period - 1] = np.mean(plus_dm[:period])
        smoothed_minus[period - 1] = np.mean(minus_dm[:period])
        for i in range(period, len(smoothed_plus)):
            smoothed_plus[i] = (smoothed_plus[i - 1] * (period - 1) + plus_dm[i]) / period
            smoothed_minus[i] = (smoothed_minus[i - 1] * (period - 1) + minus_dm[i]) / period

    atr_safe = np.where(atr_arr == 0, 1e-8, atr_arr)
    di_plus = 100.0 * smoothed_plus / atr_safe
    di_minus = 100.0 * smoothed_minus / atr_safe
    dx = (
        100.0
        * np.abs(di_plus - di_minus)
        / np.where(di_plus + di_minus == 0, 1e-8, di_plus + di_minus)
    )

    adx_raw = np.zeros(len(dx))
    if len(dx) >= period:
        adx_raw[period - 1] = np.mean(dx[:period])
        for i in range(period, len(dx)):
            adx_raw[i] = (adx_raw[i - 1] * (period - 1) + dx[i]) / period

    adx[1:] = adx_raw  # offset by 1 because dm is diff-based
    return adx


def _compute_momentum_array(closes: np.ndarray, days: int) -> np.ndarray:
    """Rate-of-change over `days` bars, vectorized."""
    n = len(closes)
    result = np.zeros(n)
    if n > days:
        result[days:] = (closes[days:] - closes[:-days]) / closes[:-days] * 100.0
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Timestamp-based H4→D1 alignment (Fix #3)
# ═══════════════════════════════════════════════════════════════════════════════


def _build_h4_alignment(
    d1_timestamps: list[str],
    h4_timestamps: list[str],
) -> list[int]:
    """For each D1 bar, return the H4 index of the LAST H4 bar whose timestamp
    is ≤ the D1 bar's timestamp.  Returns -1 if no H4 bar precedes the D1 bar.

    This is timestamp-based merge_asof — no look-ahead bias.
    """
    d1_dts = _parse_timestamps(d1_timestamps)
    h4_dts = _parse_timestamps(h4_timestamps)

    aligned: list[int] = []
    h4_idx = 0
    n_h4 = len(h4_dts)

    for d1_dt in d1_dts:
        # Advance h4_idx to the last H4 bar ≤ this D1 bar's timestamp
        while h4_idx < n_h4 and h4_dts[h4_idx] <= d1_dt:
            h4_idx += 1
        aligned.append(h4_idx - 1)  # -1 means no H4 data before this D1 bar

    return aligned


# ═══════════════════════════════════════════════════════════════════════════════
# Main computer class
# ═══════════════════════════════════════════════════════════════════════════════


class DailyFeatureComputer:
    """Compute 24 daily swing features from D1 OHLC + optional H4 & cross-asset data.

    All D1 technical features are pre-computed as full-length arrays during __init__.
    H4 alignment is timestamp-based (no index-multiplication look-ahead).
    Temporal features use sin/cos cyclical encoding.
    """

    def __init__(
        self,
        d1_csv: str | Path,
        h4_csv: str | Path | None = None,
        cross_assets: dict[str, str | Path] | None = None,
    ):
        # ── Load D1 data ──
        (self.opens, self.highs, self.lows, self.closes, self._volumes, self.timestamps) = (
            _load_ohlc_csv(d1_csv)
        )
        self._n = len(self.closes)

        # ── Parse D1 timestamps for calendar features ──
        self._d1_datetimes = _parse_timestamps(self.timestamps)

        # ── Pre-compute D1 technical features (Fix #4: vectorized) ──
        self._precompute_d1_technical()

        # ── Pre-compute derived features ──
        self._precompute_derived()

        # ── Load H4 data with timestamp alignment (Fix #3) ──
        self._h4_closes: np.ndarray | None = None
        self._h4_highs: np.ndarray | None = None
        self._h4_lows: np.ndarray | None = None
        self._h4_alignment: list[int] = []
        self._h4_atr_arr: np.ndarray | None = None
        self._h4_rsi_arr: np.ndarray | None = None
        self._h4_momentum_24: np.ndarray | None = None

        if h4_csv is not None and Path(h4_csv).exists():
            h4_o, h4_h, h4_l, h4_c, h4_v, h4_ts = _load_ohlc_csv(h4_csv)
            self._h4_closes = h4_c
            self._h4_highs = h4_h
            self._h4_lows = h4_l
            self._h4_timestamps = h4_ts
            self._h4_alignment = _build_h4_alignment(self.timestamps, h4_ts)
            # Pre-compute H4 rolling features
            if len(h4_c) > ATR_PERIOD:
                self._h4_atr_arr = _compute_atr_array(
                    h4_h, h4_l, h4_c, period=min(ATR_PERIOD, len(h4_c) - 1)
                )
            if len(h4_c) > RSI_PERIOD:
                self._h4_rsi_arr = _compute_rsi_array(h4_c, period=min(RSI_PERIOD, len(h4_c) - 1))
            if len(h4_c) > 24:
                self._h4_momentum_24 = _compute_momentum_array(h4_c, 24)

        # ── Load cross-asset data ──
        self._cross_closes: dict[str, np.ndarray] = {}
        self._cross_timestamps: dict[str, list[str]] = {}
        self._cross_alignment: dict[str, list[int]] = {}
        if cross_assets:
            for name, path in cross_assets.items():
                p = Path(path)
                if p.exists():
                    _, _, _, cc, _, cts = _load_ohlc_csv(p)
                    self._cross_closes[name] = cc
                    self._cross_timestamps[name] = cts
                    self._cross_alignment[name] = _build_h4_alignment(self.timestamps, cts)

    # ── Pre-computation ──

    def _precompute_d1_technical(self) -> None:
        """Pre-compute all 8 D1 technical features as full-length arrays."""
        n = self._n
        o, h, l, c, v = self.opens, self.highs, self.lows, self.closes, self._volumes

        # D1_Ret_1
        self._arr_ret_1 = np.zeros(n)
        self._arr_ret_1[1:] = (c[1:] - c[:-1]) / c[:-1] * 100.0

        # D1_Body_Ratio
        denom = h - l
        self._arr_body_ratio = np.zeros(n)
        mask = denom > 0
        self._arr_body_ratio[mask] = np.clip((c[mask] - o[mask]) / denom[mask], -1.0, 1.0)

        # D1_ATR_14
        self._arr_atr_14 = _compute_atr_array(h, l, c, ATR_PERIOD)

        # D1_RSI_14
        self._arr_rsi_14 = _compute_rsi_array(c, RSI_PERIOD)

        # D1_MACD
        self._arr_macd = _compute_macd_array(c)

        # D1_Vol_ZScore
        self._arr_vol_zscore = np.zeros(n)
        if n > VOL_ZS_LOOKBACK:
            vol_mean = _rolling_mean(v, VOL_ZS_LOOKBACK)
            vol_std = _rolling_std(v, VOL_ZS_LOOKBACK)
            safe_mask = vol_std > 0
            self._arr_vol_zscore[safe_mask] = (v[safe_mask] - vol_mean[safe_mask]) / vol_std[
                safe_mask
            ]

        # D1_Bollinger_Width
        self._arr_bb_width = _compute_bollinger_width_array(c, BB_PERIOD)

        # D1_ADX_14
        self._arr_adx_14 = _compute_adx_array(h, l, c, ADX_PERIOD)

    def _precompute_derived(self) -> None:
        """Pre-compute derived features: momentum, vol regime, month-end."""
        n = self._n
        c = self.closes

        self._arr_mom_5d = _compute_momentum_array(c, MOM_5D)
        self._arr_mom_20d = _compute_momentum_array(c, MOM_20D)

        # Vol Regime: ATR percentile over 63-bar rolling window
        self._arr_vol_regime = np.full(n, 0.5)
        if n > 63:
            for i in range(63, n):
                atr_window = self._arr_atr_14[i - 63 : i + 1]
                current = self._arr_atr_14[i]
                valid = atr_window[~np.isnan(atr_window)]
                if len(valid) > 0 and not np.isnan(current):
                    self._arr_vol_regime[i] = float(np.mean(valid < current))

        # Weekend Gap: close-to-close daily return
        self._arr_weekend_gap = np.zeros(n)
        safe_c = c.copy()
        safe_c[safe_c == 0] = np.nan
        self._arr_weekend_gap[1:] = (c[1:] - c[:-1]) / np.where(c[:-1] != 0, c[:-1], 1.0) * 100.0

        # Month-end trading day features — captures institutional month-end flow effects
        self._arr_days_to_month_end = np.full(n, 0.5)
        self._arr_is_month_end_week = np.zeros(n)
        dts = self._d1_datetimes
        if dts:
            month_buckets: dict[str, list[int]] = {}
            for i, dt in enumerate(dts):
                key = dt.strftime("%Y-%m")
                month_buckets.setdefault(key, []).append(i)
            for indices in month_buckets.values():
                total = len(indices)
                if total < 2:
                    continue
                for pos, idx in enumerate(indices):
                    days_to_end = total - pos - 1
                    self._arr_days_to_month_end[idx] = days_to_end / max(total, 1)
                    self._arr_is_month_end_week[idx] = 1.0 if days_to_end <= 5 else 0.0

    # ── Public API ──

    def compute_all(self) -> tuple[np.ndarray, list[str]]:
        """Compute features for all bars with sufficient lookback.  O(N)."""
        features_list: list[list[float]] = []
        out_timestamps: list[str] = []

        for i in range(MIN_LOOKBACK, self._n):
            feats = self._gather_row(i)
            features_list.append(feats)
            out_timestamps.append(self.timestamps[i])

        return np.array(features_list, dtype=np.float64), out_timestamps

    def compute_at(self, index: int) -> list[float]:
        """Compute a single feature vector at bar index."""
        if index < MIN_LOOKBACK or index >= self._n:
            raise IndexError(f"Bar {index} out of range [{MIN_LOOKBACK}, {self._n})")
        return self._gather_row(index)

    # ── Row gathering (O(1) — just arrays indexing) ──

    def _gather_row(self, i: int) -> list[float]:
        """Gather all 24 features at bar i from pre-computed arrays."""
        feats: list[float] = []

        # ── D1 Technical (8) ──
        feats.append(round(float(self._arr_ret_1[i]), 6))
        feats.append(round(float(self._arr_body_ratio[i]), 6))
        feats.append(round(float(self._arr_atr_14[i]), 6))
        feats.append(round(float(self._arr_rsi_14[i]), 6))
        feats.append(round(float(self._arr_macd[i]), 6))
        feats.append(round(float(self._arr_vol_zscore[i]), 6))
        feats.append(round(float(self._arr_bb_width[i]), 6))
        feats.append(round(float(self._arr_adx_14[i]), 6))

        # ── H4 Macro (4) — timestamp-aligned ──
        feats.extend(self._h4_features(i))

        # ── Cross-asset (4) — timestamp-aligned ──
        feats.extend(self._cross_features(i))

        # ── Derived / Calendar (8) — cyclical encoding ──
        feats.extend(self._derived_features(i))

        return feats

    def _h4_features(self, d1_idx: int) -> list[float]:
        """Compute H4-derived macro features using timestamp-aligned H4 data."""
        if self._h4_closes is None or d1_idx >= len(self._h4_alignment):
            return [0.0, 0.0, 0.0, 0.0]

        h4_idx = self._h4_alignment[d1_idx]
        if h4_idx < 20:
            return [0.0, 0.0, 0.0, 0.0]

        # H4 Trend Strength: 24-bar momentum at aligned H4 index
        if self._h4_momentum_24 is not None and h4_idx < len(self._h4_momentum_24):
            trend = float(self._h4_momentum_24[h4_idx])
        else:
            trend = 0.0

        # H4 ATR Ratio: H4 ATR / D1 ATR
        if self._h4_atr_arr is not None and h4_idx < len(self._h4_atr_arr):
            h4_atr = self._h4_atr_arr[h4_idx]
            d1_atr = self._arr_atr_14[d1_idx]
            atr_ratio = h4_atr / d1_atr if d1_atr > 0 and not np.isnan(h4_atr) else 0.0
        else:
            atr_ratio = 0.0

        # H4 RSI Divergence: D1 RSI - H4 RSI
        if self._h4_rsi_arr is not None and h4_idx < len(self._h4_rsi_arr):
            h4_rsi = float(self._h4_rsi_arr[h4_idx])
            d1_rsi = float(self._arr_rsi_14[d1_idx])
            rsi_div = d1_rsi - h4_rsi if not np.isnan(h4_rsi) else 0.0
        else:
            rsi_div = 0.0

        # H4 vs D1 Alignment: sign agreement of H4 6-bar and D1 1-bar returns
        if h4_idx >= 6 and d1_idx >= 1 and self._h4_closes[h4_idx - 6] > 0:
            h4_ret = (self._h4_closes[h4_idx] - self._h4_closes[h4_idx - 6]) / self._h4_closes[
                h4_idx - 6
            ]
            d1_ret = self._arr_ret_1[d1_idx] / 100.0
            alignment = 1.0 if h4_ret * d1_ret > 0 else (-1.0 if h4_ret * d1_ret < 0 else 0.0)
        else:
            alignment = 0.0

        return [
            round(trend, 6),
            round(atr_ratio, 6),
            round(rsi_div, 6),
            round(alignment, 6),
        ]

    def _cross_features(self, d1_idx: int) -> list[float]:
        """Compute cross-asset features using timestamp-aligned cross data."""
        results: list[float] = []

        # Gold-Silver ratio (XAU / XAG)
        if "XAGUSDc" in self._cross_closes and d1_idx < len(
            self._cross_alignment.get("XAGUSDc", [])
        ):
            xag = self._cross_closes["XAGUSDc"]
            xag_idx = self._cross_alignment["XAGUSDc"][d1_idx]
            if xag_idx >= 0 and xag_idx < len(xag):
                au_price = self.closes[d1_idx]
                ag_price = xag[xag_idx]
                if ag_price > 0 and d1_idx >= 20 and xag_idx >= 20:
                    # Normalize: deviation from 20-bar MA of ratio
                    ratios = []
                    for k in range(20):
                        d1_k = d1_idx - k
                        xag_k = self._cross_alignment["XAGUSDc"][d1_k]
                        if xag_k >= 0 and xag_k < len(xag) and xag[xag_k] > 0:
                            ratios.append(self.closes[d1_k] / xag[xag_k])
                    if ratios:
                        ratio_ma = float(np.mean(ratios))
                        ratio_std = float(np.std(ratios))
                        current_ratio = au_price / ag_price
                        results.append(
                            round((current_ratio - ratio_ma) / ratio_std, 6)
                            if ratio_std > 0
                            else 0.0
                        )
                    else:
                        results.append(0.0)
                else:
                    results.append(0.0)
            else:
                results.append(0.0)
        else:
            results.append(0.0)

        # DXY proxy via EURUSD inverse return, and EURUSD return
        if "EURUSDc" in self._cross_closes and d1_idx < len(
            self._cross_alignment.get("EURUSDc", [])
        ):
            eur = self._cross_closes["EURUSDc"]
            eur_idx = self._cross_alignment["EURUSDc"][d1_idx]
            if eur_idx >= 1 and eur_idx < len(eur) and eur[eur_idx - 1] > 0:
                eur_ret = (eur[eur_idx] - eur[eur_idx - 1]) / eur[eur_idx - 1] * 100.0
                results.append(round(-eur_ret, 6))  # DXY ≈ -EURUSD
                results.append(round(eur_ret, 6))  # EURUSD direct
            else:
                results.extend([0.0, 0.0])
        else:
            results.extend([0.0, 0.0])

        # Risk On/Off: gold 5d momentum proxy
        if d1_idx >= 5:
            gold_ret_5d = float(self._arr_mom_5d[d1_idx])
            if gold_ret_5d < -1.0:
                results.append(1.0)
            elif gold_ret_5d > 1.0:
                results.append(-1.0)
            else:
                results.append(0.0)
        else:
            results.append(0.0)

        return results

    def _derived_features(self, i: int) -> list[float]:
        """Compute derived/calendar features with cyclical weekday + month-end encoding."""
        feats: list[float] = []
        dt = self._d1_datetimes[i] if i < len(self._d1_datetimes) else None

        # Weekday sin/cos (cyclical encoding)
        if dt is not None:
            wd = dt.weekday()  # 0=Mon..6=Sun
            feats.append(round(float(np.sin(2.0 * np.pi * wd / 7.0)), 6))
            feats.append(round(float(np.cos(2.0 * np.pi * wd / 7.0)), 6))
        else:
            feats.extend([0.0, 0.0])

        # Month-end trading day features (institutional flow capture)
        feats.append(round(float(self._arr_days_to_month_end[i]), 6))
        feats.append(round(float(self._arr_is_month_end_week[i]), 4))

        # Weekend Gap
        feats.append(round(float(self._arr_weekend_gap[i]), 6))

        # Vol Regime
        feats.append(round(float(self._arr_vol_regime[i]), 4))

        # Momentum 5D, 20D
        feats.append(round(float(self._arr_mom_5d[i]), 6))
        feats.append(round(float(self._arr_mom_20d[i]), 6))

        return feats


# ── Smoke test ──
if __name__ == "__main__":
    import json
    import sys

    d1_path = sys.argv[1] if len(sys.argv) > 1 else "data/raw/xauusdc_d1_merged.csv"
    h4_path = sys.argv[2] if len(sys.argv) > 2 else "data/raw/xauusdc_h4_merged.csv"

    comp = DailyFeatureComputer(
        d1_csv=d1_path,
        h4_csv=h4_path,
        cross_assets={
            "XAGUSDc": "data/raw/xagusdc_d1_merged.csv",
            "EURUSDc": "data/raw/eurusdc_d1_merged.csv",
        },
    )
    feats, timestamps = comp.compute_all()
    n_features = len(DAILY_SWING_24_FEATURES)
    print(
        json.dumps(
            {
                "schema_version": "daily_swing_24",
                "shape": list(feats.shape),
                "n_features": n_features,
                "feature_names": DAILY_SWING_24_FEATURES,
                "date_range": [timestamps[0], timestamps[-1]] if timestamps else [],
                "sample_row_0": {
                    DAILY_SWING_24_FEATURES[j]: round(float(feats[0, j]), 6)
                    for j in range(min(n_features, feats.shape[1]))
                },
                "nan_count": int(np.sum(np.isnan(feats))),
                "inf_count": int(np.sum(np.isinf(feats))),
            },
            indent=2,
            default=str,
        )
    )
