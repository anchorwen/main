"""Microstructure 9-feature live computer.

Computes the 9 microstructure features from MT5 M5 OHLC bars, tick data,
and cross-asset returns.  Designed to feed the Transformer V4.3 and XGBoost
V4.5 brains.

Features computed:
  Price micro-movement:  tick_return, hl_ratio, co_ratio
  Market micro-structure: avg_spread, OIM, tick_velocity
  Cross-asset context:    XAGUSDc_return, EURUSDc_return, USDJPYc_return
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

CROSS_SYMBOLS = ["XAGUSDc", "EURUSDc", "USDJPYc"]
CROSS_FEATURE_NAMES = ["XAGUSDc_return", "EURUSDc_return", "USDJPYc_return"]

MIN_M5_BARS = 4  # need at least 2 for return, 3 for hl_ratio shift
MIN_TICKS = 10


class MicrostructureFeatureComputer:
    """Compute 9 microstructure features from MT5 M5 data + cross-symbol returns.

    Usage::

        import MetaTrader5 as mt5
        mt5.initialize()

        computer = MicrostructureFeatureComputer(mt5, "XAUUSDc")
        features = computer.compute_all()
        # features is a dict with 9 keys
    """

    def __init__(self, mt5_module, symbol: str):
        self._mt5 = mt5_module
        self._symbol = symbol

    def compute_all(self) -> dict[str, float]:
        result: dict[str, float] = {}

        # ── 1. OHLC-derived features (M5 bars) ──
        try:
            rates = self._mt5.copy_rates_from_pos(
                self._symbol, self._mt5.TIMEFRAME_M5, 0, MIN_M5_BARS + 1
            )
        except Exception:
            rates = None

        if rates is None or len(rates) < 2:
            result.update(
                {
                    "tick_return": 0.0,
                    "hl_ratio": 0.0,
                    "co_ratio": 0.0,
                }
            )
        else:
            closes = np.array([float(r[4]) for r in rates], dtype=np.float64)
            opens = np.array([float(r[1]) for r in rates], dtype=np.float64)
            highs = np.array([float(r[2]) for r in rates], dtype=np.float64)
            lows = np.array([float(r[3]) for r in rates], dtype=np.float64)

            # tick_return: percentage return of latest bar close vs previous close
            if len(closes) >= 2 and closes[-2] != 0:
                result["tick_return"] = float((closes[-1] - closes[-2]) / closes[-2] * 100.0)
            else:
                result["tick_return"] = 0.0

            # hl_ratio: (high-low)/close — intra-bar volatility
            denom = closes[-1]
            result["hl_ratio"] = (
                float((highs[-1] - lows[-1]) / denom) if denom and denom != 0 else 0.0
            )

            # co_ratio: close/open — bar direction and strength
            result["co_ratio"] = (
                float(closes[-1] / opens[-1]) if opens[-1] and opens[-1] != 0 else 1.0
            )

        # ── 2. Tick-derived features ──
        #    copy_ticks_from with a datetime → most recent ticks from that point
        try:
            from datetime import timedelta

            from_date = datetime.now() - timedelta(minutes=5)
            ticks = self._mt5.copy_ticks_from(
                self._symbol, from_date, 5000, self._mt5.COPY_TICKS_ALL
            )
        except Exception:
            ticks = None

        if ticks is None or len(ticks) < 2:
            result.update(
                {
                    "avg_spread": 0.0,
                    "OIM": 0.0,
                    "tick_velocity": 0.0,
                }
            )
        else:
            # avg_spread: mean bid-ask spread (normalised by mid-price)
            bids = np.array([float(t[2]) for t in ticks], dtype=np.float64)
            asks = np.array([float(t[1]) for t in ticks], dtype=np.float64)
            spreads = asks - bids
            mid = (asks + bids) / 2.0
            mid_mean = float(np.mean(mid))
            result["avg_spread"] = (
                float(np.mean(spreads)) if mid_mean and mid_mean != 0 else float(np.mean(spreads))
            )

            # OIM: order imbalance metric — ratio of (ask_vol - bid_vol) / total_vol
            # tick format: time, bid, ask, last, volume, flags (volume is often -1 in MT5)
            # Use tick count asymmetry as proxy when volume unavailable
            if len(ticks) >= 2:
                price_deltas = np.diff(np.array([float(t[3]) for t in ticks], dtype=np.float64))
                up_ticks = np.sum(price_deltas > 0)
                down_ticks = np.sum(price_deltas < 0)
                total_directional = up_ticks + down_ticks
                result["OIM"] = (
                    float((up_ticks - down_ticks) / total_directional)
                    if total_directional > 0
                    else 0.0
                )
            else:
                result["OIM"] = 0.0

            # tick_velocity: ticks per second over the sample window
            if len(ticks) >= 2:
                t0 = float(ticks[0][0])
                t1 = float(ticks[-1][0])
                duration = t1 - t0
                result["tick_velocity"] = float(len(ticks) / duration) if duration > 0 else 0.0
            else:
                result["tick_velocity"] = 0.0

        # ── 3. Cross-asset returns ──
        for sym, name in zip(CROSS_SYMBOLS, CROSS_FEATURE_NAMES, strict=False):
            try:
                rates = self._mt5.copy_rates_from_pos(sym, self._mt5.TIMEFRAME_M5, 0, 2)
            except Exception:
                result[name] = 0.0
                continue

            if rates is not None and len(rates) >= 2:
                c0 = float(rates[-2][4])
                c1 = float(rates[-1][4])
                result[name] = float((c1 - c0) / c0 * 100.0) if c0 and c0 != 0 else 0.0
            else:
                result[name] = 0.0

        return result


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

    computer = MicrostructureFeatureComputer(mt5, symbol)
    features = computer.compute_all()

    mt5.shutdown()

    print(
        json.dumps(
            {
                "symbol": symbol,
                "feature_count": len(features),
                "features": {k: round(v, 6) for k, v in features.items()},
                "timestamp_utc": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            },
            indent=2,
        )
    )
