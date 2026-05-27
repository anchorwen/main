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

from collections import deque
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from core.execution.mt5_worker import MT5Worker

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
                    raw[sym] = np.array([0.0], dtype=np.float64)
            except Exception:
                raw[sym] = np.array([0.0], dtype=np.float64)
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
                cross_returns[name] = [0.0] * usable
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
                ret.append(float((curr - prev) / prev * 100.0) if prev else 0.0)
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
        except Exception:
            return None

    def _fetch_tf_rates(self, count: int, tf_str: str):
        """Fetch rates directly at a given timeframe (not resampled)."""
        try:
            tf = _mt5_timeframe(tf_str)
            if tf is None:
                return None
            return self._copy_rates(self._symbol, tf, 0, count)
        except Exception:
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

        # OHLC-derived
        close = bar["close"]
        row[0] = float((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
        row[1] = float((bar["high"] - bar["low"]) / close) if close else 0.0
        row[2] = float(close / bar["open"]) if bar["open"] else 1.0

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
        result["tick_return"] = (
            float((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
        )
        result["hl_ratio"] = float((high - low) / close) if close else 0.0
        result["co_ratio"] = float(close / open_v) if open_v else 1.0

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
        except Exception:
            ticks = None

        if ticks is None or len(ticks) < 2:
            result.update({"avg_spread": 0.0, "OIM": 0.0, "tick_velocity": 0.0})
            return

        bids = np.array([float(t[2]) for t in ticks], dtype=np.float64)
        asks = np.array([float(t[1]) for t in ticks], dtype=np.float64)
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
        # Computed from tick volume + flags (bid/ask direction).
        # Per-bar OFI raw → rolling z-score over 100-bar buffer.
        # NOT part of ML feature schema — consumed only by strategy_line toxicity gate.
        # Fail-open: if OFI computation fails for any reason, OFI=0.0 (gate skipped).
        try:
            _vols = np.array([float(t[4]) if len(t) > 4 else 1.0 for t in ticks], dtype=np.float64)
            # COPY_TICKS_ALL returns 8-field ticks: (time, bid, ask, last, volume, time_msc, flags, volume_real)
            # flags is at index 6; index 5 is time_msc (~1.78e12 → overflows np.int32).
            _flgs = np.array([int(t[6]) if len(t) > 6 else 4 for t in ticks], dtype=np.int32)
            _bid_mask = (_flgs == 1) | (_flgs == 4)
            _ask_mask = _flgs == 2
            _bid_vol = float(np.sum(_vols[_bid_mask]))
            _ask_vol = float(np.sum(_vols[_ask_mask]))
            _total_vol = _bid_vol + _ask_vol
            _ofi_raw = (_bid_vol - _ask_vol) / _total_vol if _total_vol > 0 else 0.0
            self._ofi_buffer.append(_ofi_raw)
            _ofi_mean = float(np.mean(self._ofi_buffer)) if self._ofi_buffer else 0.0
            _ofi_std = float(np.std(self._ofi_buffer)) if len(self._ofi_buffer) > 1 else 1e-8
            result["OFI"] = float((_ofi_raw - _ofi_mean) / _ofi_std) if _ofi_std > 1e-8 else 0.0
        except Exception:
            result["OFI"] = 0.0

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
            except Exception:
                result[name] = 0.0
                continue
            if rates is not None and len(rates) >= 2:
                c0 = float(rates[-2][4])
                c1 = float(rates[-1][4])
                result[name] = float((c1 - c0) / c0 * 100.0) if c0 else 0.0
            else:
                result[name] = 0.0

    def _compute_cross_sequence(self, n_bars: int, ratio: int) -> dict[str, list[float]]:
        """Compute per-bar cross-asset returns for n_bars."""
        cross: dict[str, list[float]] = {}
        m5_needed = n_bars * ratio + 2
        for sym, name in zip(CROSS_SYMBOLS, CROSS_FEATURE_NAMES, strict=False):
            try:
                rates = self._copy_rates(sym, MT5_TIMEFRAME_M5, 0, m5_needed)
            except Exception:
                cross[name] = [0.0] * n_bars
                continue
            if rates is None or len(rates) < ratio + 1:
                cross[name] = [0.0] * n_bars
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
                    returns.append(float((curr - prev) / prev * 100.0) if prev else 0.0)
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
