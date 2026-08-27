"""Microstructure 9-feature live computer with multi-timeframe + sequence support.

Computes the 9 microstructure features from MT5 OHLC bars, tick data,
and cross-asset returns.  Supports per-bar rolling-window sequences
(32 bars × 9 features) and multi-timeframe aggregation from M5 bars.

Features computed per bar:
  Price micro-movement:  tick_return, hl_ratio, co_ratio
  Market micro-structure: avg_spread, OIM, tick_velocity
  Cross-asset context:    XAGUSDc_return, EURUSDc_return, USDJPYc_return
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from core.execution.mt5_worker import MT5Worker

# ── Division guard (FIX-20260614-015) ────────────────────────────────────
# Python's `if value else default` catches 0.0 but NOT NaN or Inf.
# NaN is truthy → `if float('nan')` evaluates to True → passes through
# to arithmetic → contaminates all downstream features → silent brain poisoning.
#
# _safe_div() replaces ALL implicit truthiness guards with explicit
# math.isfinite() + non-zero checks, blocking NaN/Inf at the source.


def _safe_div(numerator: float, denominator: float, fallback: float = 0.0) -> float:
    """Divide numerator by denominator, returning fallback for unsafe inputs.

    An input is unsafe if it is:
      - NaN (truthy in Python — passes `if x` check)
      - Inf or -Inf

    A denominator is also unsafe if it is exactly 0.0.

    This prevents silent NaN/Inf propagation into the 9-dim feature vector
    that feeds brain inference.  Both numerator AND denominator are checked
    because NaN can enter via either path (e.g. NaN price in OHLC bar).
    """
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator == 0.0:
        return fallback
    return numerator / denominator


# ── DQAF-20260827-002 (Phase 2): Shared OHLC-derived microstructure triplet ──
def pure_ohlc_micro(
    open_: float,
    high: float,
    low: float,
    close: float,
    prev_close: float,
) -> tuple[float, float, float]:
    """Compute the three OHLC-derived micro features — the SINGLE definition.

    This is the one canonical口径 for ``tick_return`` / ``hl_ratio`` /
    ``co_ratio`` shared by BOTH live inference and historical replay:

      * live ``MicrostructureFeatureComputer._bar_to_features`` delegates here
        (behaviour unchanged — same formulas, same \_safe_div guards).
      * training ``core/training/feature_replay.py`` imports this so the
        train/serve slot semantics are mathematically identical (the Phase 2
        goal: kill the Train/Serve Skew ghost at the component definition).

    CANONICAL口径 (FIX-20260827-001): per-bar returns are RAW FRACTIONS,
    never ×100.  Order matches the micro_9 slot order ``[0,1,2]``:

      - tick_return = (close - prev_close) / prev_close
      - hl_ratio    = (high - low) / close
      - co_ratio    = close / open
    """
    tick_return = _safe_div(close - prev_close, prev_close, 0.0)
    hl_ratio = _safe_div(high - low, close, 0.0)
    co_ratio = _safe_div(close, open_, 1.0)
    return tick_return, hl_ratio, co_ratio


# ── FIX-20260827-001: Canonical return口径 (single source of truth) ───────
# Per-bar returns (tick_return, XAG/EUR/USDJPY cross returns) are computed as
# RAW FRACTIONS — (curr - prev) / prev — WITHOUT the ×100 percent scaling.
# This matches the training builder (build_btc_expected_r_dataset.py:399
# tick_return `(c - o) / o`) and the sibling raw ratios hl_ratio / co_ratio.
# A bare `* 100.0` was previously applied here, inflating tick_return 100×
# (911× in the train/live covariate-shift audit) and diverging every downstream
# model.  Absent/invalid data is returned as NaN (not a forged 0.0) so the
# consumer (V9MicroComputer.last_micro_ok / _sanitize_41 NaN-log) can see it.
CROSS_SYMBOLS = ["XAGUSDc", "EURUSDc", "USDJPYc"]
CROSS_FEATURE_NAMES = ["XAGUSDc_return", "EURUSDc_return", "USDJPYc_return"]
FEATURE_NAMES = [
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

MIN_M5_BARS = 4
MIN_TICKS = 10
DEFAULT_SEQ_LEN = 32  # sequence length for Transformer / XGBoost 288-dim

# ── MT5 timeframe constants (hardcoded — no thread-affinity requirement) ──
MT5_TIMEFRAME_M5 = 5
MT5_TIMEFRAME_M15 = 15
MT5_TIMEFRAME_M30 = 30
MT5_TIMEFRAME_H1 = 16385
MT5_TIMEFRAME_H4 = 16388
MT5_COPY_TICKS_ALL = -1  # MetaTrader5 COPY_TICKS_ALL flag

# M5 bars per higher-TF bar
TF_BAR_RATIO = {"M5": 1, "M15": 3, "H1": 12, "H4": 48}


def _mt5_timeframe(tf_str: str):
    """Map timeframe string to MT5 constant (hardcoded — no import needed)."""
    return {
        "M5": MT5_TIMEFRAME_M5,
        "M15": MT5_TIMEFRAME_M15,
        "H1": MT5_TIMEFRAME_H1,
        "H4": MT5_TIMEFRAME_H4,
    }.get(tf_str, MT5_TIMEFRAME_M5)


class MicrostructureFeatureComputer:
    """Compute microstructure features from MT5 data.

    Usage::

        import MetaTrader5 as mt5
        mt5.initialize()

        computer = MicrostructureFeatureComputer(mt5, "XAUUSDc")
        features_1bar = computer.compute_all()        # backward-compat: single bar
        seq = computer.compute_sequence(n_bars=32)     # (32, 9) array for models
        seq_m15 = computer.compute_sequence(n_bars=32, timeframe="M15")
    """

    def __init__(self, mt5_module, symbol: str, mt5_worker: MT5Worker | None = None):
        self._mt5 = mt5_module
        self._symbol = symbol
        self._worker = mt5_worker
        self._ofi_buffer: deque[float] = deque(maxlen=100)  # 100 bars × 5min ≈ 8.3h OFI context
        # ── FIX-20260718-004: Microstructure Gate rolling buffers ──────────
        # Stateful deques — each live cycle computes current bar's value from
        # the existing MT5 tick snapshot and pushes it.  No historical tick
        # queries (I/O death trap avoidance).  Cold start: len < 20 → zscore=0.0.
        self._arrival_buffer: deque[float] = deque(maxlen=100)  # tick arrival rate per bar
        self._spread_buffer: deque[float] = deque(maxlen=100)  # avg spread per bar

    # ── Backward-compatible single-bar API ──────────────────────────────

    def compute_all(self, *, reference_time: datetime | None = None) -> dict[str, float]:
        """Compute 9 features from the latest M5 bar + tick snapshot.

        Backward-compatible with existing code that expects a dict.

        Args:
            reference_time: Optional datetime for backtest/historical mode.
                When None (default), uses datetime.now(UTC) for live trading.
        """
        result: dict[str, float] = {}
        rates = self._fetch_m5_rates(2)
        if rates is None or len(rates) < 2:
            self._fill_ohlc_defaults(result)
        else:
            self._compute_ohlc_features_from_row(rates[-1], rates[-2][4], result)

        self._compute_tick_features(result, reference_time=reference_time)
        self._compute_cross_features(result)
        return result

    # ── Multi-bar sequence API ──────────────────────────────────────────

    def compute_sequence(
        self,
        n_bars: int = DEFAULT_SEQ_LEN,
        timeframe: str = "M5",
        *,
        reference_time: datetime | None = None,
    ) -> np.ndarray:
        """Compute per-bar 9-feature sequence for model inference.

        Args:
            n_bars: Number of bars in the sequence (default 32).
            timeframe: "M5" | "M15" | "H1" | "H4" (M15/H1/H4 are M5-resampled).
            reference_time: Optional datetime for backtest mode.
                When None, uses datetime.now(UTC) for live trading.

        Returns:
            (n_bars, 9) float32 array in chronological order (oldest→newest).
        """
        ratio = TF_BAR_RATIO.get(timeframe, 1)
        m5_needed = n_bars * ratio + 2  # +2 for return computation + safety
        rates = self._fetch_m5_rates(m5_needed)
        if rates is None or len(rates) < ratio + 2:
            return np.zeros((n_bars, 9), dtype=np.float32)

        closes = np.array([float(r[4]) for r in rates], dtype=np.float64)
        opens = np.array([float(r[1]) for r in rates], dtype=np.float64)
        highs = np.array([float(r[2]) for r in rates], dtype=np.float64)
        lows = np.array([float(r[3]) for r in rates], dtype=np.float64)

        # Resample to target timeframe
        if ratio > 1:
            target_bars = len(closes) // ratio
            t_closes, t_opens, t_highs, t_lows = [], [], [], []
            for i in range(target_bars):
                start = i * ratio
                end = start + ratio
                t_opens.append(opens[start])
                t_highs.append(float(np.max(highs[start:end])))
                t_lows.append(float(np.min(lows[start:end])))
                t_closes.append(closes[end - 1])
            closes = np.array(t_closes, dtype=np.float64)
            opens = np.array(t_opens, dtype=np.float64)
            highs = np.array(t_highs, dtype=np.float64)
            lows = np.array(t_lows, dtype=np.float64)

        requested_bars = n_bars
        available = len(closes) - 1  # need prev_close for each bar
        usable = min(requested_bars, max(1, available))

        # Tick features (single snapshot, replicated across bars)
        tick_features = self._compute_tick_features_dict(reference_time=reference_time)

        # Cross-asset per-bar returns
        cross_returns = self._compute_cross_sequence(usable, ratio)

        # Build (n_bars, 9) array — pad leading rows with zeros when data is short
        seq = np.zeros((requested_bars, 9), dtype=np.float32)
        pad = requested_bars - usable  # leading empty rows
        for i in range(usable):
            bar_idx = -(usable - i)
            prev_close = closes[bar_idx - 1] if bar_idx > -len(closes) else closes[bar_idx]
            bar = {
                "open": float(opens[bar_idx]),
                "high": float(highs[bar_idx]),
                "low": float(lows[bar_idx]),
                "close": float(closes[bar_idx]),
            }
            row = self._bar_to_features(bar, float(prev_close), tick_features, cross_returns, i)
            seq[pad + i] = row

        return seq

    # ── Batch multi-TF sequence API ─────────────────────────────────────

    def compute_all_sequences(
        self, n_bars: int = DEFAULT_SEQ_LEN, *, reference_time: datetime | None = None
    ) -> dict[str, np.ndarray]:
        """Compute (n_bars, 9) sequences for M5, M15, H1, H4 in one shot.

        Fetches M5 bars once (max needed = n_bars*48 for H4), resamples
        per timeframe, and shares tick/cross-asset snapshots across TFs.

        Args:
            reference_time: Optional datetime for backtest mode.
                When None, uses datetime.now(UTC) for live trading.
        """
        max_ratio = max(TF_BAR_RATIO.values())  # 48 for H4
        m5_needed = n_bars * max_ratio + 2
        rates = self._fetch_m5_rates(m5_needed)
        if rates is None or len(rates) < 2:
            return {tf: np.zeros((n_bars, 9), dtype=np.float32) for tf in TF_BAR_RATIO}

        closes = np.array([float(r[4]) for r in rates], dtype=np.float64)
        opens = np.array([float(r[1]) for r in rates], dtype=np.float64)
        highs = np.array([float(r[2]) for r in rates], dtype=np.float64)
        lows = np.array([float(r[3]) for r in rates], dtype=np.float64)

        tick_features = self._compute_tick_features_dict(reference_time=reference_time)
        cross_raw = self._compute_cross_raw(n_bars, max_ratio)

        result: dict[str, np.ndarray] = {}
        for tf, ratio in TF_BAR_RATIO.items():
            result[tf] = self._resample_and_build_sequence(
                closes,
                opens,
                highs,
                lows,
                n_bars,
                ratio,
                tick_features,
                cross_raw,
            )

        return result

    def _compute_cross_raw(self, n_bars: int, max_ratio: int) -> dict[str, np.ndarray]:
        """Fetch cross-asset closes once, return raw arrays for resampling."""
        m5_needed = n_bars * max_ratio + 2
        raw: dict[str, np.ndarray] = {}
        for sym in CROSS_SYMBOLS:
            try:
                rates = self._copy_rates(sym, MT5_TIMEFRAME_M5, 0, m5_needed)
                if rates is not None and len(rates) >= 2:
                    raw[sym] = np.array([float(r[4]) for r in rates], dtype=np.float64)
                else:
                    raw[sym] = np.array(
                        [float("nan")], dtype=np.float64
                    )  # FIX-20260827-001: absent → NaN
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                raw[sym] = np.array(
                    [float("nan")], dtype=np.float64
                )  # FIX-20260827-001: absent → NaN
        return raw

    def _resample_and_build_sequence(
        self,
        closes: np.ndarray,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        n_bars: int,
        ratio: int,
        tick_features: dict[str, float],
        cross_raw: dict[str, np.ndarray],
    ) -> np.ndarray:
        """Resample M5 OHLC to target TF and build (n_bars, 9) sequence.
        Pads leading rows with zeros when not enough source bars are available."""
        # Resample OHLC
        r_closes, r_opens, r_highs, r_lows = self._resample_ohlc(closes, opens, highs, lows, ratio)
        available = len(r_closes) - 1  # need prev_close
        usable = min(n_bars, max(1, available))

        # Resample cross-asset closes and compute per-bar returns
        cross_returns: dict[str, list[float]] = {}
        for _i, (sym, name) in enumerate(zip(CROSS_SYMBOLS, CROSS_FEATURE_NAMES, strict=False)):
            raw = cross_raw.get(sym)
            if raw is None or len(raw) < ratio + 1:
                cross_returns[name] = [float("nan")] * usable  # FIX-20260827-001: absent → NaN
                continue
            c_resampled = self._resample_closes(raw, ratio)
            cross_returns[name] = self._compute_returns(c_resampled, usable)

        seq = np.zeros((n_bars, 9), dtype=np.float32)
        pad = n_bars - usable
        for i in range(usable):
            idx = -(usable - i)
            prev = float(r_closes[idx - 1]) if idx > -len(r_closes) else float(r_closes[idx])
            bar = {
                "open": float(r_opens[idx]),
                "high": float(r_highs[idx]),
                "low": float(r_lows[idx]),
                "close": float(r_closes[idx]),
            }
            seq[pad + i] = self._bar_to_features(bar, prev, tick_features, cross_returns, i)
        return seq

    @staticmethod
    def _resample_ohlc(closes, opens, highs, lows, ratio):
        if ratio <= 1:
            return closes, opens, highs, lows
        target = len(closes) // ratio
        t_c, t_o, t_h, t_l = [], [], [], []
        for i in range(target):
            s, e = i * ratio, (i + 1) * ratio
            t_o.append(opens[s])
            t_h.append(float(np.max(highs[s:e])))
            t_l.append(float(np.min(lows[s:e])))
            t_c.append(closes[e - 1])
        return (
            np.array(t_c, dtype=np.float64),
            np.array(t_o, dtype=np.float64),
            np.array(t_h, dtype=np.float64),
            np.array(t_l, dtype=np.float64),
        )

    @staticmethod
    def _resample_closes(closes, ratio):
        if ratio <= 1:
            return closes
        target = len(closes) // ratio
        return np.array([closes[(i + 1) * ratio - 1] for i in range(target)], dtype=np.float64)

    @staticmethod
    def _compute_returns(closes, n_bars):
        ret = []
        for i in range(n_bars):
            idx = -(n_bars - i)
            if idx > -len(closes):
                prev = closes[idx - 1]
                curr = closes[idx]
                ret.append(_safe_div(curr - prev, prev, 0.0))  # FIX-20260827-001: raw fraction
            else:
                ret.append(0.0)
        return ret

    # ── Private helpers ─────────────────────────────────────────────────

    def _copy_rates(self, symbol: str, timeframe: int, start_pos: int, count: int):
        """Fetch rates — prefers worker when available, falls back to raw MT5 module."""
        if self._worker is not None:
            return self._worker.copy_rates_from_pos(symbol, timeframe, start_pos, count)
        return self._mt5.copy_rates_from_pos(symbol, timeframe, start_pos, count)

    def _fetch_m5_rates(self, count: int):
        try:
            return self._copy_rates(self._symbol, MT5_TIMEFRAME_M5, 0, count)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return None

    def _fetch_tf_rates(self, count: int, tf_str: str):
        """Fetch rates directly at a given timeframe (not resampled)."""
        try:
            tf = _mt5_timeframe(tf_str)
            if tf is None:
                return None
            return self._copy_rates(self._symbol, tf, 0, count)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return None

    def _bar_to_features(
        self,
        bar: dict[str, float],
        prev_close: float,
        tick_features: dict[str, float],
        cross_returns: dict[str, list[float]],
        bar_idx: int,
    ) -> np.ndarray:
        """Compute 9 features for a single bar."""
        row = np.zeros(9, dtype=np.float32)

        # OHLC-derived.  FIX-20260827-001: per-bar returns are RAW FRACTIONS
        # (no ×100) — the canonical口径 matching training + hl_ratio/co_ratio.
        # DQAF-20260827-002: delegates to the SHARED pure function so historical
        # replay (feature_replay.py) and live inference are bit-identical.
        close = bar["close"]
        row[0], row[1], row[2] = pure_ohlc_micro(
            bar["open"], bar["high"], bar["low"], close, prev_close
        )

        # Tick-derived (snapshot, same for all bars)
        row[3] = tick_features.get("avg_spread", 0.0)
        row[4] = tick_features.get("OIM", 0.0)
        row[5] = tick_features.get("tick_velocity", 0.0)

        # Cross-asset
        row[6] = cross_returns.get("XAGUSDc_return", [0.0])[bar_idx]
        row[7] = cross_returns.get("EURUSDc_return", [0.0])[bar_idx]
        row[8] = cross_returns.get("USDJPYc_return", [0.0])[bar_idx]

        return row

    def _compute_ohlc_features_from_row(
        self, bar_row, prev_close: float, result: dict[str, float]
    ) -> None:
        close = float(bar_row[4])
        open_v = float(bar_row[1])
        high = float(bar_row[2])
        low = float(bar_row[3])
        # DQAF-20260827-002: shared pure function — same单帧口径 as _bar_to_features.
        result["tick_return"], result["hl_ratio"], result["co_ratio"] = pure_ohlc_micro(
            open_v, high, low, close, prev_close
        )

    @staticmethod
    def _fill_ohlc_defaults(result: dict) -> None:
        result.update({"tick_return": 0.0, "hl_ratio": 0.0, "co_ratio": 0.0})

    def _compute_tick_features(
        self, result: dict[str, float], *, reference_time: datetime | None = None
    ) -> None:
        # Use reference_time when provided (backtest/historical), fall back to
        # datetime.now(UTC) in live mode.  Always timezone-aware to prevent
        # naive/aware mixing (护栏 3: spatio-temporal consistency).
        if reference_time is None:
            _now = datetime.now(UTC)
        elif reference_time.tzinfo is None:
            _now = reference_time.replace(tzinfo=UTC)
        else:
            _now = reference_time
        try:
            from_date = _now - timedelta(minutes=5)
            if self._worker is not None:
                ticks = self._worker.copy_ticks_from(
                    self._symbol, from_date.timestamp(), 5000, MT5_COPY_TICKS_ALL
                )
            else:
                ticks = self._mt5.copy_ticks_from(self._symbol, from_date, 5000, MT5_COPY_TICKS_ALL)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            ticks = None
        if ticks is None or len(ticks) < 2:
            result.update({"avg_spread": 0.0, "OIM": 0.0, "tick_velocity": 0.0})
            return

        # FIX-20260827-001: bid/ask index swap.  COPY_TICKS_ALL returns
        # (time, bid, ask, last, ...) per the contract below (line ~486) →
        # index 1 = BID, index 2 = ASK.  Pre-fix code read bids=t[2](ASK) and
        # asks=t[1](BID), so `spreads = asks - bids` computed bid - ask →
        # avg_spread NEGATIVE (-3939), poisoning every consumer (BTC + XAU,
        # shared computer).  Fixed to read the tuple correctly.
        bids = np.array([float(t[1]) for t in ticks], dtype=np.float64)  # index1 = bid
        asks = np.array([float(t[2]) for t in ticks], dtype=np.float64)  # index2 = ask
        spreads = asks - bids
        mid = (asks + bids) / 2.0
        mid_mean = float(np.mean(mid))
        result["avg_spread"] = float(np.mean(spreads)) if mid_mean else float(np.mean(spreads))

        price_deltas = np.diff(np.array([float(t[3]) for t in ticks], dtype=np.float64))
        up_ticks: int = np.sum(price_deltas > 0)
        down_ticks: int = np.sum(price_deltas < 0)
        total_directional = up_ticks + down_ticks
        result["OIM"] = (
            float((up_ticks - down_ticks) / total_directional) if total_directional > 0 else 0.0
        )

        t0 = float(ticks[0][0])
        t1 = float(ticks[-1][0])
        duration = t1 - t0
        result["tick_velocity"] = float(len(ticks) / duration) if duration > 0 else 0.0

        # ── OFI (Order Flow Imbalance) for toxicity gate ──
        # DQAF-20260707-004: Fixed tick flag classification.
        # MT5 COPY_TICKS_ALL flags (verified 2026-07-07):
        #   TICK_FLAG_BID=2 (0x02), ASK=4 (0x04), LAST=8 (0x08),
        #   VOLUME=16 (0x10), BUY=32 (0x20), SELL=64 (0x40)
        #
        # Pre-FIX bug: used exact equality checks with wrong values
        #   (_flgs==1 never-matched, _flgs==4=ASK→"bid", _flgs==2=BID→"ask").
        #   BUY/SELL deal flags (32/64) were completely ignored.
        #
        # Post-FIX: Primary = BUY/SELL deal flags (trade direction),
        #   Fallback = BID/ASK price-change flags (no deal on this tick).
        #   volume_real (index 7) used for real-volume OFI when available.
        # Per-bar OFI raw → rolling z-score over 100-bar buffer.
        # NOT part of ML feature schema — consumed only by strategy_line toxicity gate.
        # Fail-open: if OFI computation fails for any reason, OFI=0.0 (gate skipped).
        try:
            _vols = np.array([float(t[4]) if len(t) > 4 else 1.0 for t in ticks], dtype=np.float64)
            # COPY_TICKS_ALL returns 8-field ticks: (time, bid, ask, last, volume, time_msc, flags, volume_real)
            # flags is at index 6; index 5 is time_msc (~1.78e12 → overflows np.int32).
            _flgs = np.array([int(t[6]) if len(t) > 6 else 4 for t in ticks], dtype=np.int32)
            # volume_real at index 7 (actual traded volume, may be zero for non-deal ticks)
            _vols_real = np.array(
                [float(t[7]) if len(t) > 7 else 0.0 for t in ticks], dtype=np.float64
            )

            # ── DQAF-20260707-004: Primary — BUY/SELL deal flags (bitwise) ──
            _TICK_FLAG_BUY = 32  # 0x20
            _TICK_FLAG_SELL = 64  # 0x40
            _TICK_FLAG_BID = 2  # 0x02 (fallback)
            _TICK_FLAG_ASK = 4  # 0x04 (fallback)

            _buy_deal_mask = (_flgs & _TICK_FLAG_BUY) != 0
            _sell_deal_mask = (_flgs & _TICK_FLAG_SELL) != 0

            # ── Fallback: ticks without deal flags use BID/ASK heuristic ──
            _no_deal = ~(_buy_deal_mask | _sell_deal_mask)
            _bid_fallback = _no_deal & ((_flgs & _TICK_FLAG_BID) != 0)
            _ask_fallback = _no_deal & ((_flgs & _TICK_FLAG_ASK) != 0)

            # ── Combine: deal flags (primary) + BID/ASK fallback ──
            _buy_mask = _buy_deal_mask | _bid_fallback
            _sell_mask = _sell_deal_mask | _ask_fallback

            _bid_vol = float(np.sum(_vols[_buy_mask]))
            _ask_vol = float(np.sum(_vols[_sell_mask]))
            _bid_vol_real = float(np.sum(_vols_real[_buy_mask]))
            _ask_vol_real = float(np.sum(_vols_real[_sell_mask]))

            # ── Tick-volume OFI (existing metric) ──
            _total_vol = _bid_vol + _ask_vol
            _ofi_raw = (_bid_vol - _ask_vol) / _total_vol if _total_vol > 0 else 0.0

            # ── DQAF-20260707-004: Real-volume OFI (more accurate) ──
            _total_vol_real = _bid_vol_real + _ask_vol_real
            _ofi_real_raw = (
                (_bid_vol_real - _ask_vol_real) / _total_vol_real
                if _total_vol_real > 0
                else _ofi_raw  # fall back to tick-volume OFI
            )

            self._ofi_buffer.append(_ofi_raw)
            _ofi_mean = float(np.mean(self._ofi_buffer)) if self._ofi_buffer else 0.0
            _ofi_std = float(np.std(self._ofi_buffer)) if len(self._ofi_buffer) > 1 else 1e-8
            result["OFI"] = float((_ofi_raw - _ofi_mean) / _ofi_std) if _ofi_std > 1e-8 else 0.0
            # DQAF-20260707-004: Real-volume OFI z-score for enhanced toxicity detection
            result["OFI_Real"] = (
                float((_ofi_real_raw - _ofi_mean) / _ofi_std) if _ofi_std > 1e-8 else 0.0
            )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            result["OFI"] = 0.0
            result["OFI_Real"] = 0.0

        # ── FIX-20260718-004: Microstructure Gate features ──────────────
        # Computed from the SAME tick snapshot (zero additional MT5 calls).
        # All use stateful rolling buffers for baseline — cold start returns
        # neutral values (zscore=0.0, toxicity=1.0, pressure=0.5).
        # These are NOT part of the ML feature vector — consumed only by
        # MicrostructureGate via micro_feature_dict.
        try:  # BLE001:FOG — fail_open_guard: any failure → neutral gate pass
            # 1. arrival_rate_5s — ticks per second in current 5-min window
            _arrival_rate = float(len(ticks) / duration) if duration > 0 else 0.0
            self._arrival_buffer.append(_arrival_rate)
            result["arrival_rate_5s"] = _arrival_rate

            # 2. quote_intensity_zscore — rolling z-score of arrival rate
            if len(self._arrival_buffer) >= 20:
                _arr_mu = statistics.mean(self._arrival_buffer)
                _arr_std = (
                    statistics.stdev(self._arrival_buffer)
                    if len(self._arrival_buffer) > 1
                    else 1e-8
                )
                result["quote_intensity_zscore"] = (
                    float((_arrival_rate - _arr_mu) / _arr_std) if _arr_std > 1e-8 else 0.0
                )
            else:
                result["quote_intensity_zscore"] = 0.0  # cold start: safe pass

            # 3. buy_pressure_20 — fraction of BUY ticks among last 20
            #    Uses instantaneous last-20 snapshot (not rolling buffer).
            #    BUY=32 (0x20), SELL=64 (0x40) — leveraging existing bitmask.
            _TICK_FLAG_BUY = 32
            _TICK_FLAG_SELL = 64
            _recent = ticks[-20:] if len(ticks) >= 20 else ticks
            _buy_count = sum(1 for t in _recent if len(t) > 6 and (int(t[6]) & _TICK_FLAG_BUY))
            _sell_count = sum(1 for t in _recent if len(t) > 6 and (int(t[6]) & _TICK_FLAG_SELL))
            _total_dir = _buy_count + _sell_count
            result["buy_pressure_20"] = float(_buy_count / _total_dir) if _total_dir > 0 else 0.5

            # 4. spread_toxicity — ratio of current spread to rolling median
            _curr_spread = float(result.get("avg_spread", 0.0))
            self._spread_buffer.append(_curr_spread)
            if len(self._spread_buffer) >= 20:
                _sorted = sorted(self._spread_buffer)
                _mid = len(_sorted) // 2
                _median = _sorted[_mid]
                result["spread_toxicity"] = float(_curr_spread / _median) if _median > 1e-8 else 1.0
            else:
                result["spread_toxicity"] = 1.0  # cold start: neutral
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            result["arrival_rate_5s"] = 0.0
            result["quote_intensity_zscore"] = 0.0
            result["buy_pressure_20"] = 0.5
            result["spread_toxicity"] = 1.0

    def _compute_tick_features_dict(
        self, *, reference_time: datetime | None = None
    ) -> dict[str, float]:
        result: dict[str, float] = {}
        self._compute_tick_features(result, reference_time=reference_time)
        return result

    def _compute_cross_features(self, result: dict[str, float]) -> None:
        for sym, name in zip(CROSS_SYMBOLS, CROSS_FEATURE_NAMES, strict=False):
            try:
                rates = self._copy_rates(sym, MT5_TIMEFRAME_M5, 0, 2)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                result[name] = float("nan")  # FIX-20260827-001: absent → NaN (not forged 0.0)
                continue
            if rates is not None and len(rates) >= 2:
                c0 = float(rates[-2][4])
                c1 = float(rates[-1][4])
                result[name] = _safe_div(c1 - c0, c0, 0.0)  # FIX-20260827-001: raw fraction
            else:
                result[name] = float("nan")  # FIX-20260827-001: absent → NaN

    def _compute_cross_sequence(self, n_bars: int, ratio: int) -> dict[str, list[float]]:
        """Compute per-bar cross-asset returns for n_bars."""
        cross: dict[str, list[float]] = {}
        m5_needed = n_bars * ratio + 2
        for sym, name in zip(CROSS_SYMBOLS, CROSS_FEATURE_NAMES, strict=False):
            try:
                rates = self._copy_rates(sym, MT5_TIMEFRAME_M5, 0, m5_needed)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                cross[name] = [float("nan")] * n_bars  # FIX-20260827-001: absent → NaN
                continue
            if rates is None or len(rates) < ratio + 1:
                cross[name] = [float("nan")] * n_bars  # FIX-20260827-001: absent → NaN
                continue
            closes = np.array([float(r[4]) for r in rates], dtype=np.float64)
            if ratio > 1:
                target = len(closes) // ratio
                resampled = np.array(
                    [closes[(i + 1) * ratio - 1] for i in range(target)], dtype=np.float64
                )
                closes = resampled
            returns = []
            for i in range(n_bars):
                idx = -(n_bars - i)
                if idx > -len(closes):
                    prev = closes[idx - 1]
                    curr = closes[idx]
                    returns.append(
                        _safe_div(curr - prev, prev, 0.0)
                    )  # FIX-20260827-001: raw fraction
                else:
                    returns.append(0.0)
            cross[name] = returns
        return cross


# ── Standalone smoke-test ──
if __name__ == "__main__":
    import json
    import sys

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print(json.dumps({"error": "MetaTrader5 not installed"}))
        sys.exit(2)

    symbol = sys.argv[1] if len(sys.argv) > 1 else "XAUUSDc"
    terminal_path = sys.argv[2] if len(sys.argv) > 2 else None

    if terminal_path:
        if not mt5.initialize(path=terminal_path):
            print(json.dumps({"error": "mt5_initialize_failed"}))
            sys.exit(2)
    else:
        if not mt5.initialize():
            print(json.dumps({"error": "mt5_initialize_failed"}))
            sys.exit(2)

    computer = MicrostructureFeatureComputer(mt5, symbol)
    features = computer.compute_all()
    seq = computer.compute_sequence(n_bars=32, timeframe="M5")

    mt5.shutdown()

    print(
        json.dumps(
            {
                "symbol": symbol,
                "single_bar_features": len(features),
                "sequence_shape": list(seq.shape),
                "timestamp_utc": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            },
            indent=2,
        )
    )
