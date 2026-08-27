"""BTC historical feature replay — the SINGLE training-side entry into the shared assembly.

Phase 1 / M1 (FIX-20260803-XXX, BTC 机构级训练管线重建 — 战役一):
    Historical replay and live inference flow through the SAME pure assembly
    (``core.features.computers.btc_feature_augmenter.assemble_41_series``).
    This module produces the per-bar COMPONENTS (daily_24 / micro_9 / tf /
    cross-asset) from a historical aligned OHLC frame, then feeds them to the
    shared pure assembly.

Component layout MUST match the live augmenter's inputs (Schema Order B):
    - daily_24:  daily_swing_24 layout (H4_* slots zero-filled for replay)
    - micro_9:   microstructure_9 layout
    - tf_ou / tf_hurst: TF-specific OU theta + Hurst exponent
    - xau_return / audjpy_return / btc_xau_ratio / btc_xau_ratio_roc

Bit-identical guarantee (test_feature_bit_identical.py):
    ``assemble_41_series(components)`` == calling ``augment()`` N times in a row
    for the same components.  The upstream COMPONENT computation is a documented
    replay provider — it follows the training-side vectorized OHLC indicator
    implementations (same source lineage as build_btc_expected_r_dataset.py).
    The ASSEMBLY (slot mapping / regime-delta / ordering — the historically
    recurring failure class) is the SHARED pure code.

Iron Law #11: any statistical claim about the replay output must come from a
script (build_btc_dataset_from_ssot.py), never from eyeballing NPZ files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from core.features.computers.btc_feature_augmenter import assemble_41_series
from core.features.computers.microstructure_computer import (
    MicrostructureFeatureComputer,
    pure_ohlc_micro,
)
from core.features.schemas.registry import get_schema_feature_names

_log = logging.getLogger(__name__)

# ── Indicator constants (shared lineage with build_btc_expected_r_dataset.py) ──
_ATR_PERIOD = 14
_RSI_PERIOD = 14
_MACD_SLOW = 26
_MACD_SIGNAL = 9
_VOL_ZS_LOOKBACK = 20
_BB_PERIOD = 20
_ADX_PERIOD = 14
_OU_LOOKBACK = 20
_HURST_MAX_LAG = 20
_MIN_WARMUP = (
    max(
        _ATR_PERIOD,
        _RSI_PERIOD,
        _MACD_SLOW + _MACD_SIGNAL,
        _VOL_ZS_LOOKBACK,
        _BB_PERIOD,
        _ADX_PERIOD,
        _OU_LOOKBACK,
        _HURST_MAX_LAG,
    )
    + 5
)

# Public alias for dataset builders / quality gates
MIN_WARMUP = _MIN_WARMUP


@dataclass
class ReplayComponents:
    """Per-bar components consumed by the shared 41-dim assembly.

    All arrays are aligned row-for-row (index = bar order).
    """

    daily: np.ndarray = field(default_factory=lambda: np.zeros((0, 24)))
    micro: np.ndarray = field(default_factory=lambda: np.zeros((0, 9)))
    tf_ou: np.ndarray = field(default_factory=lambda: np.zeros(0))
    tf_hurst: np.ndarray = field(default_factory=lambda: np.zeros(0))
    xau_return: np.ndarray = field(default_factory=lambda: np.zeros(0))
    audjpy_return: np.ndarray = field(default_factory=lambda: np.zeros(0))
    btc_xau_ratio: np.ndarray = field(default_factory=lambda: np.zeros(0))
    btc_xau_ratio_roc: np.ndarray = field(default_factory=lambda: np.zeros(0))
    # DQAF-20260827-002 (Phase 2): the RAW OHLC/spread used to build features,
    # resampled to the target TF when tf_minutes > 5.  Exposed so dataset
    # builders compute LABELS on the SAME resampled bars as the features —
    # closing the M15-vs-M5 train/serve skew at the slice level (A3).
    o: np.ndarray = field(default_factory=lambda: np.zeros(0))
    h: np.ndarray = field(default_factory=lambda: np.zeros(0))
    l: np.ndarray = field(default_factory=lambda: np.zeros(0))
    c: np.ndarray = field(default_factory=lambda: np.zeros(0))
    spreads: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @property
    def n_bars(self) -> int:
        return len(self.daily)

    def micro_zeros_frac(self) -> float:
        """Fraction of micro rows that are entirely zero (unavailable history).

        Documented in NPZ metadata (Iron Law #11: statistical claims from
        computation, not eyeballing).  Rows with all-zero micro features mean
        tick-level history was unavailable for that bar — a soft-quality signal,
        NOT a correctness failure (live also zero-fills when micro is absent).
        """
        if self.n_bars == 0:
            return 0.0
        all_zero = ~np.any(self.micro != 0.0, axis=1)
        return float(all_zero.sum() / self.n_bars)


# ═══════════════════════════════════════════════════════════════════════════
# Replay provider — historical OHLC → per-bar components
# ═══════════════════════════════════════════════════════════════════════════


# ── DQAF-20260827-002 (A3): slice-aligned cross-asset resample helper ────────
def _extract_times(df: pd.DataFrame) -> np.ndarray:
    """Return the bar datetime grid as a numpy ``datetime64[ns]`` array.

    The aligned CSVs set ``time`` as the index; synthetic frames (tests) carry a
    ``time`` column.  Callers downstream (calendar helpers) rebuild from this grid
    so calendar features stay row-aligned after TF resampling.
    """
    if "time" in df.columns:
        return pd.to_datetime(df["time"]).values
    return pd.to_datetime(df.index).values


def _resample_last(arr: np.ndarray, ratio: int, n: int) -> np.ndarray:
    """Take the last M5 value within each target-TF bar (cross-asset return proxy).

    Used only when tf_minutes > 5 (M15/H1/H4 slices).  Mirrors the live
    ``_resample_closes`` *last-close* semantics for the already-aligned cross
    return series, so slot [12]/[30]/[31]/[32] align to the target bar rather
    than stranding M5 granularity inside an M15 slice.  ``ratio == 1`` is a no-op.
    """
    if ratio <= 1 or len(arr) == 0:
        return arr
    out = np.zeros(n, dtype=np.float64)
    for k in range(n):
        s, e = k * ratio, (k + 1) * ratio
        out[k] = arr[e - 1] if e > 0 else arr[s]
    return out


def _ou_theta(price: np.ndarray, max_n: int = 200) -> float:
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


def _hurst(price: np.ndarray, max_lag: int = 20, max_n: int = 500) -> float:
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


def compute_replay_components(
    df: pd.DataFrame,
    tf_minutes: float = 5.0,
) -> ReplayComponents:
    """Compute per-bar replay components from a historical aligned OHLC frame.

    Args:
        df: Aligned multi-TF frame with at least ``open/high/low/close`` and
            optional cross-asset columns (``XAUUSDc_return``, ``EURUSDc_return``,
            ``AUDJPYc_return``, ``USDJPYc_return``, ``XAUUSDc_close``).
            Cross-asset columns missing or empty → zero-fill (graceful
            degradation, mirrors live augmenter with unavailable sources).
        tf_minutes: Bar timeframe in minutes (5=M5, 15=M15, 30=M30, 60=H1).
            When ``tf_minutes > 5`` the M5 bars are FIRST resampled to the
            target TF via the live ``MicrostructureFeatureComputer._resample_ohlc``
            (first-open / max-high / min-low / last-close) so the slice is
            mathematically identical to what the live ``_mtf_price_service``
            reconstructs (DQAF-20260827-002 A3).  ``tf_minutes == 5`` is a no-op.

    Returns:
        ReplayComponents with row-aligned arrays.  ``o/h/l/c/spreads`` are the
        resampled bars the features were computed on — dataset builders use them
        to compute LABELS so features and labels share the same slice.
    """
    n_raw = len(df)
    times = _extract_times(df)  # DQAF-20260827-002: datetime grid (M5 source)
    o = df["open"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64)
    c = df["close"].values.astype(np.float64)
    v = df.get("tick_volume", pd.Series(np.zeros(n_raw))).values.astype(np.float64)
    spreads_raw = df.get("spread", pd.Series([200] * n_raw)).values.astype(np.float64)
    # MT5 raw points → price dollars (same lineage as train_btc_swing_v9.py:693)
    spreads_full = np.nan_to_num(spreads_raw) / 100.0

    # ── DQAF-20260827-002 (A3): Timeframe alignment to live _resample_ohlc ──
    # The live MT5 computer NEVER reads a higher-TF bar directly — it resamples
    # M5 bars via first-open / max-high / min-low / last-close
    # (MicrostructureFeatureComputer._resample_ohlc) and computes features on THOSE
    # bars.  If historical replay instead runs features on raw M5 bars while only
    # scaling bars_per_day, the M15 slices diverge from what _mtf_price_service
    # reconstructs.  Here the same resample is applied FIRST so the training slice
    # is physically identical to the live one.  ratio==1 (M5) is a no-op.
    ratio = max(1, int(round(tf_minutes / 5.0)))
    if ratio > 1:
        c, o, h, l = MicrostructureFeatureComputer._resample_ohlc(c, o, h, l, ratio)
        n = len(c)
        v_agg = np.zeros(n)
        v_agg[:] = [float(v[k * ratio : (k + 1) * ratio].sum()) for k in range(n)]
        v = v_agg
        spreads = np.array(
            [spreads_full[min((k + 1) * ratio - 1, n_raw - 1)] for k in range(n)],
            dtype=np.float64,
        )
        # DQAF-20260827-002 (A3): calendar features must sit on the RESAMPLED bar
        # grid, so rebuild the datetime axis to the target (each bar ends at the
        # last M5 timestamp in its window).  Without this, the calendar helpers
        # would return raw-M5-length arrays and fail to broadcast into ``daily``.
        times = np.array(
            [times[min((k + 1) * ratio - 1, n_raw - 1)] for k in range(n)],
            dtype="datetime64[ns]",
        )
    else:
        n = n_raw
        spreads = spreads_full

    bars_per_day = max(1, int(24 * 60 / tf_minutes))
    lookback_5d = bars_per_day * 5
    lookback_20d = bars_per_day * 20

    # ── Cross-asset series (zero-fill when column absent / empty) ──
    def _col(name: str, default: float = 0.0) -> np.ndarray:
        if name not in df.columns:
            return np.full(n, default, dtype=np.float64)
        vals = df[name].values.astype(np.float64)
        vals = np.nan_to_num(vals, nan=default, posinf=default, neginf=default)
        return vals

    xau_return = _col("XAUUSDc_return")
    audjpy_return = _col("AUDJPYc_return")
    eur_return = _col("EURUSDc_return")
    usdjpy_return = _col("USDJPYc_return")
    # default=0.0 → missing XAU close yields ratio 0.0 (matches LIVE graceful
    # degradation in _compute_btc_xau_ratio; the old training code's ones-fill
    # produced fake ratio ≈ BTC price for ~31% of bars — corrected for SSOT).
    xau_close = _col("XAUUSDc_close", default=0.0)

    # ── DQAF-20260827-002 (A3): resample cross-asset series to target TF ──
    # Live _resample_and_build_sequence resamples each cross close via
    # _resample_closes (last-close) and computes the per-bar return from the
    # resampled closes.  Here the aligned return series (already per-M5-bar)
    # take the last M5 value within each target bar; the XAU close that feeds
    # the BTC/XAU ratio is resampled with the live _resample_closes semantics
    # so slots [39-40] align to the target TF.  ratio==1 (M5) is a no-op.
    if ratio > 1:
        xau_return = _resample_last(xau_return, ratio, n)
        audjpy_return = _resample_last(audjpy_return, ratio, n)
        eur_return = _resample_last(eur_return, ratio, n)
        usdjpy_return = _resample_last(usdjpy_return, ratio, n)
        xau_close = MicrostructureFeatureComputer._resample_closes(xau_close, ratio)

    # ── daily_swing_24 layout (24) ──
    daily = np.zeros((n, 24), dtype=np.float64)

    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr_series = pd.Series(tr).rolling(_ATR_PERIOD, min_periods=1).mean().values

    daily[:, 0] = (c - np.roll(c, bars_per_day)) / np.maximum(np.roll(c, bars_per_day), 1e-9)
    daily[:, 0][:bars_per_day] = 0.0  # no 1-day lookback at warmup
    daily[:, 1] = np.abs(c - o) / np.maximum(h - l, 1e-9)
    daily[:, 2] = atr_series
    daily[:, 3] = _rsi_series(c)
    daily[:, 4] = _macd_line(c)
    daily[:, 5] = _vol_zscore_series(c)
    daily[:, 6] = _bollinger_width_series(c)
    daily[:, 7] = _adx_series(h, l, c)
    # [8-11] H4_* placeholders — not available in aligned M5 replay → 0
    # [12]  Cross_Gold_Silver_Ratio — replaced by live XAUUSDc_return; 0 here
    daily[:, 13] = -eur_return  # Cross_DXY_Return ≈ -EUR (DXY not on retail MT5)
    daily[:, 14] = eur_return  # Cross_EURUSD_Return
    daily[:, 15] = _cross_risk_on_off(c, xau_close, lookback_5d)  # Cross_Risk_On_Off
    daily[:, 16], daily[:, 17] = _weekday_sin_cos(times)
    daily[:, 18], daily[:, 19] = _month_end_features(times)
    daily[:, 20] = _weekend_gap(times)
    atr_5d = pd.Series(tr).rolling(lookback_5d, min_periods=1).mean().values
    daily[:, 21] = np.divide(atr_series, atr_5d, out=np.ones(n), where=atr_5d > 0)
    daily[:, 22] = _momentum(c, lookback_5d)
    daily[:, 23] = _momentum(c, lookback_20d)

    # ── microstructure_9 layout (9) ──
    # DQAF-20260827-002 (A2): the three OHLC-derived features (tick_return /
    # hl_ratio / co_ratio) come from the SHARED live pure function, so they are
    # bit-identical to MicrostructureFeatureComputer._bar_to_features.  This is
    # the Train/Serve Skew fix — the training formulas that used (c-o)/o,
    # (h-l)/prev_c and |c-o|/(h-l) are REPLACED by the live (c-prev)/prev,
    # (h-l)/close and close/open definitions.
    #
    # avg_spread / OIM / tick_velocity are NOT recoverable from OHLC history
    # (they need live tick snapshots).  Replay uses the bar's spread column and
    # OHLC / volume proxies, documented as soft approximations — NOT asserted
    # bit-identical in the regression长城.
    micro = np.zeros((n, 9), dtype=np.float64)
    for j in range(n):
        prev_c_j = float(c[j - 1]) if j > 0 else float(c[j])
        micro[j, 0], micro[j, 1], micro[j, 2] = pure_ohlc_micro(
            float(o[j]), float(h[j]), float(l[j]), float(c[j]), prev_c_j
        )
    micro[:, 3] = spreads  # avg_spread (spread column — soft proxy)
    micro[:, 4] = (c - o) / np.maximum(h - l, 1e-9)  # OIM (OHLC proxy — soft)
    vol_mean_20 = pd.Series(v).rolling(20, min_periods=1).mean().values
    micro[:, 5] = v / np.maximum(vol_mean_20, 1e-8)  # tick_velocity (volume proxy — soft)
    micro[:, 6] = audjpy_return  # AUDJPYc_return (dropped by live assembly slot[30])
    micro[:, 7] = eur_return  # EURUSDc_return
    micro[:, 8] = usdjpy_return  # USDJPYc_return

    # ── TF-specific (OU theta + Hurst) — every 50th bar, forward-fill ──
    ou_vals = np.zeros(n)
    hurst_vals = np.zeros(n)
    for i in range(0, n, 50):
        end = max(i + 1, 20)
        price_slice = c[max(0, i - 200) : end]
        ou_vals[i] = _ou_theta(price_slice)
        hurst_vals[i] = _hurst(price_slice)
    for i in range(1, n):
        if ou_vals[i] == 0.0:
            ou_vals[i] = ou_vals[i - 1]
        if hurst_vals[i] == 0.0:
            hurst_vals[i] = hurst_vals[i - 1]

    # ── BTC/Gold ratio + ROC ──
    with np.errstate(divide="ignore", invalid="ignore"):
        btc_gold_ratio = np.where(xau_close > 0, c / xau_close, 0.0)
    ratio_roc = np.zeros(n)
    valid_ratio = btc_gold_ratio[:-1] > 0
    ratio_roc[1:][valid_ratio] = (
        btc_gold_ratio[1:][valid_ratio] - btc_gold_ratio[:-1][valid_ratio]
    ) / btc_gold_ratio[:-1][valid_ratio]

    return ReplayComponents(
        daily=daily,
        micro=micro,
        tf_ou=ou_vals,
        tf_hurst=hurst_vals,
        xau_return=xau_return,
        audjpy_return=audjpy_return,
        btc_xau_ratio=btc_gold_ratio,
        btc_xau_ratio_roc=ratio_roc,
        o=o,
        h=h,
        l=l,
        c=c,
        spreads=spreads,
    )


# ── Indicator series helpers (vectorized, aligned to training lineage) ──────


def _rsi_series(close: np.ndarray, period: int = 14) -> np.ndarray:
    delta = np.diff(close, prepend=close[0])
    gain = np.maximum(delta, 0)
    loss = np.maximum(-delta, 0)
    avg_gain = pd.Series(gain).rolling(period, min_periods=1).mean().values
    avg_loss = pd.Series(loss).rolling(period, min_periods=1).mean().values
    rs = np.divide(avg_gain, avg_loss, out=np.ones_like(avg_gain) * 100, where=avg_loss > 0)
    return 100.0 - 100.0 / (1.0 + rs)


def _macd_line(close: np.ndarray) -> np.ndarray:
    ema12 = pd.Series(close).ewm(span=12, adjust=False).mean().values
    ema26 = pd.Series(close).ewm(span=26, adjust=False).mean().values
    return ema12 - ema26


def _vol_zscore_series(close: np.ndarray, period: int = 20) -> np.ndarray:
    returns = np.diff(np.log(np.maximum(close, 1e-12)), prepend=0)
    ret_mean = pd.Series(returns).rolling(period, min_periods=1).mean().values
    ret_std = pd.Series(returns).rolling(period, min_periods=1).std().fillna(1e-8).values
    return (returns - ret_mean) / np.maximum(ret_std, 1e-8)


def _bollinger_width_series(close: np.ndarray, period: int = 20) -> np.ndarray:
    bb_ma = pd.Series(close).rolling(period, min_periods=1).mean().values
    bb_std = pd.Series(close).rolling(period, min_periods=1).std().fillna(0).values
    return np.divide(2 * bb_std, bb_ma, out=np.zeros_like(bb_ma), where=bb_ma > 0)


def _adx_series(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    plus_dm = np.where((h - prev_c > prev_c - l) & (h - prev_c > 0), h - prev_c, 0.0)
    minus_dm = np.where((prev_c - l > h - prev_c) & (prev_c - l > 0), prev_c - l, 0.0)
    tr_smooth = pd.Series(tr).rolling(period, min_periods=1).sum().values
    plus_smooth = pd.Series(plus_dm).rolling(period, min_periods=1).sum().values
    minus_smooth = pd.Series(minus_dm).rolling(period, min_periods=1).sum().values
    plus_di = 100.0 * plus_smooth / np.maximum(tr_smooth, 1e-8)
    minus_di = 100.0 * minus_smooth / np.maximum(tr_smooth, 1e-8)
    di_sum = plus_di + minus_di
    return np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / di_sum, 25.0)


def _cross_risk_on_off(
    c: np.ndarray,
    xau_close: np.ndarray,
    lookback_5d: int,
) -> np.ndarray:
    c_5d_ago = np.roll(c, lookback_5d)
    c_5d_ago[:lookback_5d] = c[0]
    xau_5d_ago = np.roll(xau_close, lookback_5d)
    xau_5d_ago[:lookback_5d] = xau_close[0]
    xau_5d_mom = np.divide(
        xau_close - xau_5d_ago,
        np.maximum(xau_5d_ago, 1e-9),
        out=np.zeros(len(c)),
        where=xau_5d_ago > 0,
    )
    btc_5d_mom = np.divide(
        c - c_5d_ago,
        np.maximum(c_5d_ago, 1e-9),
        out=np.zeros(len(c)),
        where=c_5d_ago > 0,
    )
    return xau_5d_mom - btc_5d_mom


def _momentum(c: np.ndarray, lookback: int) -> np.ndarray:
    c_ago = np.roll(c, lookback)
    c_ago[:lookback] = c[0]
    return np.divide(
        c - c_ago,
        np.maximum(c_ago, 1e-9),
        out=np.zeros(len(c)),
        where=c_ago > 0,
    )


def _weekday_sin_cos(times: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n = len(times)
    if n == 0:
        return np.zeros(0), np.zeros(0)
    try:
        ts = pd.to_datetime(times)
        weekdays = np.array([t.weekday() for t in ts])
    except (ValueError, TypeError):
        return np.zeros(n), np.zeros(n)
    return np.sin(2 * np.pi * weekdays / 7.0), np.cos(2 * np.pi * weekdays / 7.0)


def _month_end_features(times: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n = len(times)
    if n == 0:
        return np.zeros(0), np.zeros(0)
    try:
        ts = pd.to_datetime(times)
        month_ends = np.array([t.days_in_month - t.day for t in ts])
    except (ValueError, TypeError, AttributeError):
        return np.zeros(n), np.zeros(n)
    days_to_month_end = month_ends.astype(np.float64) / 31.0
    is_month_end_week = (month_ends <= 5).astype(np.float64)
    return days_to_month_end, is_month_end_week


def _weekend_gap(times: np.ndarray) -> np.ndarray:
    n = len(times)
    if n == 0:
        return np.zeros(0)
    try:
        ts = pd.to_datetime(times)
        weekdays = np.array([t.weekday() for t in ts])
    except (ValueError, TypeError):
        return np.zeros(n)
    return np.where(weekdays >= 4, 1.0, 0.0)


# ═══════════════════════════════════════════════════════════════════════════
# End-to-end replay → shared assembly
# ═══════════════════════════════════════════════════════════════════════════


def replay_features_41(components: ReplayComponents) -> np.ndarray:
    """Assemble (n, 41) matrix via the SHARED pure assembly (SSOT path).

    This is the identical code path live inference uses — bit-identical output
    given identical components (test_feature_bit_identical.py).
    """
    return assemble_41_series(
        components.daily,
        components.micro,
        xau_return_series=components.xau_return,
        audjpy_return_series=components.audjpy_return,
        btc_xau_ratio_series=components.btc_xau_ratio,
        btc_xau_ratio_roc_series=components.btc_xau_ratio_roc,
        tf_ou_series=components.tf_ou,
        tf_hurst_series=components.tf_hurst,
    )


def extract_schema_subset(
    x41: np.ndarray,
    schema_name: str,
) -> np.ndarray:
    """Extract a subset schema from the canonical 41-dim matrix by NAME.

    Schemas whose feature names are a strict subset/permutation of the 41-dim
    canonical names are extracted by name (never by hardcoded index) — this is
    the DQAF-20260801-006 position-independent contract.  Returns *x41*
    unchanged when the schema is the canonical 41-dim or has no name list.
    """
    canonical_names = get_schema_feature_names("btc_macro_enhanced_41_v2")
    want_names = get_schema_feature_names(schema_name)
    if not want_names or not canonical_names:
        return x41
    if set(want_names) == set(canonical_names) and len(want_names) == len(canonical_names):
        return x41
    index_map = [canonical_names.index(name) for name in want_names]
    return x41[:, index_map]


def replay_features(
    df: pd.DataFrame,
    tf_minutes: float = 5.0,
    schema_name: str = "btc_macro_enhanced_41_v2",
) -> tuple[np.ndarray, dict]:
    """End-to-end historical replay: aligned OHLC → schema feature matrix.

    Returns:
        (X, meta) where X is (n, dim) and meta carries provenance:
            schema_id, feature_names, n_features, micro_zeros_frac,
            min_warmup, n_bars, created_at.
    """
    components = compute_replay_components(df, tf_minutes=tf_minutes)
    x41 = replay_features_41(components)
    x_out = extract_schema_subset(x41, schema_name)

    meta = {
        "schema_id": schema_name,
        "feature_names": list(get_schema_feature_names(schema_name)),
        "n_features": x_out.shape[1],
        "n_bars": components.n_bars,
        "micro_zeros_frac": round(components.micro_zeros_frac(), 6),
        "min_warmup": _MIN_WARMUP,
        "assembly": "shared_pure_assemble_41_series",
        "created_at": datetime.now(UTC).isoformat(),
    }
    return x_out, meta
