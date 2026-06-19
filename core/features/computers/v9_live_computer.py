"""V9 Institutional 40-feature live computer.

Computes all 40 features from MT5 multi-timeframe OHLC data in one shot.
Designed to replace the price-diff-only signal in live_intent_loop.py with
real feature vectors consumed by V9FeatureAdapter → ONNX inference.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from core.execution.mt5_worker import MT5Worker

# ── MT5 timeframe constants (hardcoded — no thread-affinity requirement) ──
# Logical minute identifiers (used as dict keys and loop values)
MT5_TIMEFRAME_M5 = 5
MT5_TIMEFRAME_M15 = 15
MT5_TIMEFRAME_M30 = 30
MT5_TIMEFRAME_H1 = 60

# Actual MT5 timeframe constants (passed to copy_rates_from_pos)
MT5_TF_MAP = {
    5: 5,  # M5:  PERIOD_M5
    15: 15,  # M15: PERIOD_M15
    30: 30,  # M30: PERIOD_M30 (this is actually PERIOD_M30 = 30 in newer MT5 builds)
    60: 16385,  # H1:  PERIOD_H1
}

TIMEFRAMES = [MT5_TIMEFRAME_M5, MT5_TIMEFRAME_M15, MT5_TIMEFRAME_M30, MT5_TIMEFRAME_H1]
TF_LABELS = {5: "M5", 15: "M15", 30: "M30", 60: "H1"}

# ── Lookback requirements per feature ──
ATR_PERIOD = 14
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
VOL_ZS_LOOKBACK = 20
OU_LOOKBACK = 20
HURST_MAX_LAG = 20

# Bars needed: max(ATR_PERIOD, RSI_PERIOD, MACD_SLOW+SIGNAL,
#            VOL_ZS_LOOKBACK, OU_LOOKBACK, HURST_MAX_LAG) + 1
MIN_BARS = (
    max(
        ATR_PERIOD, RSI_PERIOD, MACD_SLOW + MACD_SIGNAL, VOL_ZS_LOOKBACK, OU_LOOKBACK, HURST_MAX_LAG
    )
    + 2
)


def _body_ratio(o: np.ndarray, h: np.ndarray, low: np.ndarray, c: np.ndarray) -> np.ndarray:
    """(close-open)/(high-low), clamped to [-1,1]"""
    denom = h - low
    denom = np.where(denom == 0, 1e-8, denom)
    return np.clip((c - o) / denom, -1.0, 1.0)


def _returns(c: np.ndarray) -> float:
    """Percentage return: (c[-1] - c[-2]) / c[-2] * 100"""
    return (c[-1] - c[-2]) / c[-2] * 100.0 if len(c) >= 2 else 0.0


def _atr(h: np.ndarray, low: np.ndarray, c: np.ndarray, period: int = ATR_PERIOD) -> float:
    """Average True Range over `period` bars. Returns single float."""
    if len(c) < period + 1:
        return 0.0
    prev_c = c[-(period + 1) : -1]
    cur_h = h[-period:]
    cur_l = low[-period:]
    c[-period:]
    tr = np.maximum(cur_h - cur_l, np.maximum(np.abs(cur_h - prev_c), np.abs(cur_l - prev_c)))
    return float(np.mean(tr))


def _rsi(c: np.ndarray, period: int = RSI_PERIOD) -> float:
    """Wilder's RSI. Returns single float."""
    if len(c) < period + 1:
        return 50.0
    deltas = np.diff(c[-(period + 1) :])
    gain = np.mean(np.maximum(deltas, 0))
    loss = np.mean(np.abs(np.minimum(deltas, 0)))
    if loss == 0:
        return 100.0
    rs = gain / loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _macd(c: np.ndarray) -> float:
    """MACD line (12-EMA minus 26-EMA). Returns single float."""
    need = MACD_SLOW + MACD_SIGNAL
    if len(c) < need:
        return 0.0
    ema12 = _ema(c, MACD_FAST)
    ema26 = _ema(c, MACD_SLOW)
    return float(ema12 - ema26)


def _ema(data: np.ndarray, period: int) -> float:
    """Exponential moving average. Returns last value."""
    if len(data) < period:
        return float(np.mean(data))
    alpha = 2.0 / (period + 1.0)
    result = np.mean(data[:period])
    for val in data[period:]:
        result = alpha * val + (1 - alpha) * result
    return float(result)


def _vol_zscore(volume: np.ndarray, lookback: int = VOL_ZS_LOOKBACK) -> float:
    """Volume Z-score. Returns single float."""
    if len(volume) < lookback + 1:
        return 0.0
    window = volume[-lookback:]
    mean = np.mean(window)
    std = np.std(window)
    if std == 0:
        return 0.0
    return float((volume[-1] - mean) / std)


def _ou_theta(price: np.ndarray, lookback: int = OU_LOOKBACK) -> float:
    """Ornstein-Uhlenbeck mean-reversion speed (theta) from OLS.

    dS = theta*(mu - S)*dt + sigma*dW
    Discrete: S[t] = alpha + beta*S[t-1] + eps
    theta = -ln(beta) / dt   (dt=1)
    """
    if len(price) < lookback + 1:
        return 0.0
    window = price[-lookback:]
    y = window[1:]
    x = window[:-1]
    x_mean = np.mean(x)
    y_mean = np.mean(y)
    beta_num: float = np.sum((x - x_mean) * (y - y_mean))
    beta_den: float = np.sum((x - x_mean) ** 2)
    if beta_den == 0:
        return 0.0
    beta = beta_num / beta_den
    # Clamp beta to (0, 1) so log is defined
    beta = np.clip(beta, 1e-8, 0.99999999)
    theta = -math.log(beta)
    return float(theta)


def _hurst(price: np.ndarray, max_lag: int = HURST_MAX_LAG) -> float:
    """Hurst exponent via R/S analysis. Returns float in [0, ~1]."""
    if len(price) < max_lag + 1:
        return 0.5
    series = np.asarray(price[-max_lag:], dtype=np.float64)
    mean = np.mean(series)
    deviations = series - mean
    z = np.cumsum(deviations)
    r = float(np.max(z) - np.min(z))
    s = float(np.std(series))
    if s == 0:
        return 0.5
    rs = r / s
    return float(math.log(rs) / math.log(max_lag)) if max_lag > 1 else 0.5


def _macro1_corr(price: np.ndarray, lookback: int = 20) -> float:
    """Macro proxy: auto-correlation at lag 1. Returns float in [-1, 1]."""
    if len(price) < lookback + 1:
        return 0.0
    window = price[-lookback:]
    ret = np.diff(window)
    if len(ret) < 2:
        return 0.0
    return float(np.corrcoef(ret[:-1], ret[1:])[0, 1])


def _price_zscore(price: np.ndarray, lookback: int = 20) -> float:
    """Price z-score: deviation from 20-period MA, normalized by std."""
    if len(price) < lookback:
        return 0.0
    window = price[-lookback:]
    ma = np.mean(window)
    std = np.std(window)
    if std == 0:
        return 0.0
    return float((price[-1] - ma) / std)


class V9LiveFeatureComputer:
    """Compute all 40 V9 institutional features from MT5 multi-timeframe candles.

    Usage::

        import MetaTrader5 as mt5
        mt5.initialize()

        computer = V9LiveFeatureComputer(mt5, "XAUUSDc")
        features = computer.compute_all()
        # features is a dict with all 40 keys

        from core.features.adapters.v9_feature_adapter import V9FeatureAdapter
        adapter = V9FeatureAdapter()
        model_input = adapter.build_model_input(features)
        # model_input.shape == (1, 40)
    """

    def __init__(self, mt5_module, symbol: str, mt5_worker: MT5Worker | None = None):
        self._mt5 = mt5_module
        self._symbol = symbol
        self._worker = mt5_worker
        if mt5_worker is not None:
            self._tf_map = dict(MT5_TF_MAP)
        else:
            self._tf_map = {
                MT5_TIMEFRAME_M5: self._mt5.TIMEFRAME_M5,
                MT5_TIMEFRAME_M15: self._mt5.TIMEFRAME_M15,
                MT5_TIMEFRAME_M30: self._mt5.TIMEFRAME_M30,
                MT5_TIMEFRAME_H1: self._mt5.TIMEFRAME_H1,
            }

    def compute_all(self) -> dict[str, float]:
        """Compute all 40 features and return as {name: value} dict."""
        result: dict[str, float] = {}

        for tf_min in TIMEFRAMES:
            label = TF_LABELS[tf_min]
            mt5_tf = self._tf_map[tf_min]
            bars = self._fetch_rates(mt5_tf, MIN_BARS)
            if bars is None or len(bars) < MIN_BARS:
                # Fill with zeros on insufficient data
                self._fill_zeros(result, label)
                continue

            o = np.array([r["open"] for r in bars], dtype=np.float64)
            h = np.array([r["high"] for r in bars], dtype=np.float64)
            low = np.array([r["low"] for r in bars], dtype=np.float64)
            c = np.array([r["close"] for r in bars], dtype=np.float64)
            vol = np.array([r.get("tick_volume", 0) for r in bars], dtype=np.float64)

            result[f"{label}_Ret_1"] = float(_returns(c))
            result[f"{label}_Body_Ratio"] = float(_body_ratio(o, h, low, c)[-1])
            result[f"{label}_ATR_14"] = _atr(h, low, c)
            result[f"{label}_RSI_14"] = _rsi(c)
            result[f"{label}_MACD"] = _macd(c)
            result[f"{label}_Vol_ZScore"] = _vol_zscore(vol)
            result[f"{label}_Macro1_Corr"] = _macro1_corr(c)
            result[f"{label}_Price_ZScore"] = _price_zscore(c)
            result[f"{label}_OU_Theta"] = _ou_theta(c)
            result[f"{label}_Hurst"] = _hurst(c)

        return result

    def _fetch_rates(self, mt5_tf: int, count: int) -> list[dict] | None:
        """Fetch OHLC rates; returns list of dicts or None on failure."""
        try:
            if self._worker is not None:
                rates = self._worker.copy_rates_from_pos(self._symbol, mt5_tf, 0, count)
            else:
                rates = self._mt5.copy_rates_from_pos(self._symbol, mt5_tf, 0, count)
            if rates is None or len(rates) == 0:
                return None
            # Convert numpy structured array to list of dicts
            return [
                {
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "tick_volume": float(r[5]) if len(r) > 5 else 0.0,
                }
                for r in rates
            ]
        except Exception:  # BLE001:REVIEWED
            logging.exception(
                "V9LiveComputer failed fetching rates for symbol=%s timeframe=%s count=%s",
                self._symbol,
                mt5_tf,
                count,
            )
            return None

    def _fill_zeros(self, result: dict[str, float], label: str) -> None:
        """Fill 10 features for a timeframe with zeros."""
        for feat in [
            f"{label}_Ret_1",
            f"{label}_Body_Ratio",
            f"{label}_ATR_14",
            f"{label}_RSI_14",
            f"{label}_MACD",
            f"{label}_Vol_ZScore",
            f"{label}_Macro1_Corr",
            f"{label}_Price_ZScore",
            f"{label}_OU_Theta",
            f"{label}_Hurst",
        ]:
            result[feat] = 0.0


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
            print(json.dumps({"error": "mt5_initialize_failed", "detail": str(mt5.last_error())}))
            sys.exit(2)
    else:
        if not mt5.initialize():
            print(json.dumps({"error": "mt5_initialize_failed", "detail": str(mt5.last_error())}))
            sys.exit(2)

    computer = V9LiveFeatureComputer(mt5, symbol)
    features = computer.compute_all()

    mt5.shutdown()

    print(
        json.dumps(
            {
                "symbol": symbol,
                "feature_count": len(features),
                "sample_features": {k: round(v, 6) for k, v in list(features.items())[:5]},
                "timestamp_utc": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            },
            indent=2,
        )
    )
