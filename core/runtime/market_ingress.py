"""Market data ingress helpers — MT5 data fetching and regime gate feeding.

Extracted from live_cycle.py. These functions delegate to :class:`MT5Worker`
for all MT5 C++ calls — no daemon threads, no direct ``mt5.*`` access.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from core.runtime.fault_handler import FaultLevel, FaultTolerantContext

if TYPE_CHECKING:
    from core.execution.mt5_worker import MT5Worker

# XAUUSDc physical bounds — gold CFD in cents, 3 decimal places.
# Physical extreme: gold has never traded below $250 or above $3,500
# in inflation-adjusted terms; 1000-4000 gives a 10x safety margin.
_GOLD_PRICE_MIN = 1000.0
_GOLD_PRICE_MAX = 4000.0
# BTCUSDc physical bounds — crypto CFD, 2 decimal places.
# BTC has traded between ~$3,000 and ~$110,000 in its history.
# 2000-200000 gives a wide safety margin for the cent account.
_BTC_PRICE_MIN = 2000.0
_BTC_PRICE_MAX = 200000.0
# Default max spread in price units before treating as data error.
_DEFAULT_MAX_SPREAD = 0.50  # XAUUSDc
_BTC_MAX_SPREAD = 2000.0  # BTCUSDc — spread is naturally larger (~$14)
# Max allowed spread in price units before we treat it as a data error.
# For XAUUSDc (cents), 0.50 = 50 cents = 500 points — well above any
# reasonable market spread even during news events.
_DEFAULT_MAX_SPREAD = 0.50

# MT5 timeframe constants — pure integers, no thread-affinity requirement.
MT5_TIMEFRAME_M5 = 5
MT5_TIMEFRAME_H1 = 16385  # 60-minute bars
MT5_TIMEFRAME_H4 = 16388  # 240-minute bars
MT5_TIMEFRAME_D1 = 16408  # daily bars


def _get_current_atr(
    worker: MT5Worker, symbol: str, period: int = 14, count: int = 15, timeout: float = 10.0
) -> float:
    """Compute current M5 ATR(*period*) — rates fetched on worker thread, math is local."""
    import numpy as np

    rates = None
    with FaultTolerantContext(
        level=FaultLevel.CRASH,
        component="MT5_IPC:copy_rates_from_pos:get_current_atr",
    ):
        rates = worker.copy_rates_from_pos(symbol, MT5_TIMEFRAME_M5, 0, count, timeout=timeout)
    if rates is None or len(rates) < period + 1:
        return 0.0
    h = np.array([r["high"] for r in rates], dtype=np.float64)
    low = np.array([r["low"] for r in rates], dtype=np.float64)
    c = np.array([r["close"] for r in rates], dtype=np.float64)
    prev_c = c[-(period + 1) : -1]
    cur_h = h[-period:]
    cur_l = low[-period:]
    tr = np.maximum(cur_h - cur_l, np.maximum(abs(cur_h - prev_c), abs(cur_l - prev_c)))
    return float(np.mean(tr))


def _position_count(worker: MT5Worker, symbol: str, timeout: float = 5.0) -> int:
    """Count open MT5 positions — executed on the worker thread."""
    pos = None
    with FaultTolerantContext(
        level=FaultLevel.CRASH,
        component="MT5_IPC:positions_get:position_count",
    ):
        pos = worker.positions_get(symbol=symbol, timeout=timeout)
    return len(pos) if pos else 0


def _mid_and_prices(
    worker: MT5Worker, symbol: str, timeout: float = 5.0
) -> tuple[float, float, float, float]:
    """Fetch bid/ask/mid + tick timestamp — executed on the worker thread.

    Returns (mid, bid, ask, tick_time_unix) where tick_time_unix is the
    MT5 server timestamp of the tick (Unix seconds).  Callers MUST verify
    data_age = time.time() - tick_time_unix against the staleness threshold
    before using the price for trading decisions.

    FIX-20260607-XXX: Staleness Contract — tick.time is now propagated to
    callers so live_cycle can detect data pipeline freezes (e.g. MT5
    disconnection causing repeated stale ticks).
    """
    tick = None
    with FaultTolerantContext(
        level=FaultLevel.CRASH,
        component="MT5_IPC:symbol_info_tick:mid_and_prices",
    ):
        tick = worker.symbol_info_tick(symbol, timeout=timeout)
        if tick is None:
            worker.reconnect()
            tick = worker.symbol_info_tick(symbol, timeout=timeout)
    if tick is None:
        raise RuntimeError("tick unavailable")
    bid = float(tick.bid)
    ask = float(tick.ask)

    # ── Extract tick timestamp for staleness detection ──
    # MT5 MqlTick.time is Unix seconds (int).  .time_msc is milliseconds.
    # Prefer .time_msc / 1000 for sub-second precision; fall back to .time.
    try:
        tick_time = float(getattr(tick, "time_msc", 0) or 0) / 1000.0
        if tick_time <= 0:
            tick_time = float(getattr(tick, "time", 0) or 0)
    except (TypeError, ValueError):
        tick_time = 0.0

    # ── Physical sanity checks (crash on bad data — crash-only philosophy) ──
    # Defense 1: symbol-aware bounds from ASSET_REGISTRY, not fragile string match
    from core.config.asset_registry import ASSET_REGISTRY

    _asset = ASSET_REGISTRY.get(symbol)
    if _asset is not None:
        _price_min, _price_max = _asset.min_price, _asset.max_price
        # Use spread guard from config if available, else a generous default
        _max_spread = getattr(_asset, "max_spread", _asset.max_price * 0.02)
    elif "BTC" in symbol.upper():
        _price_min, _price_max, _max_spread = _BTC_PRICE_MIN, _BTC_PRICE_MAX, _BTC_MAX_SPREAD
    else:
        _price_min, _price_max, _max_spread = _GOLD_PRICE_MIN, _GOLD_PRICE_MAX, _DEFAULT_MAX_SPREAD

    if not (math.isfinite(bid) and math.isfinite(ask)):
        raise ValueError(f"Price NaN/Inf: bid={bid} ask={ask}")
    if bid <= 0 or ask <= 0:
        raise ValueError(f"Price zero/negative: bid={bid} ask={ask}")
    if bid < _price_min or bid > _price_max:
        raise ValueError(f"Bid out of physical bounds [{_price_min}, {_price_max}]: {bid}")
    if ask < _price_min or ask > _price_max:
        raise ValueError(f"Ask out of physical bounds [{_price_min}, {_price_max}]: {ask}")
    if (ask - bid) > _max_spread:
        raise ValueError(f"Spread explosion: bid={bid} ask={ask} spread={ask - bid:.5f}")

    return (bid + ask) / 2.0, bid, ask, tick_time


def _bootstrap_regime_gate(
    worker: MT5Worker, symbol: str, gate: Any, timeout: float = 10.0
) -> bool:
    """Bootstrap RegimeGate with recent M5 and H1 bars from MT5.

    Called once on first cycle to fill the ADX buffer. Returns True if
    enough bars were loaded for M5 ADX.
    """
    m5_rates = None
    with FaultTolerantContext(
        level=FaultLevel.CRASH, component="MT5_IPC:copy_rates_from_pos:bootstrap_regime_m5"
    ):
        m5_rates = worker.copy_rates_from_pos(symbol, MT5_TIMEFRAME_M5, 0, 50, timeout=timeout)
    if m5_rates is not None and len(m5_rates) >= 15:
        gate.feed_m5_bars_batch(m5_rates)

    h1_rates = None
    with FaultTolerantContext(
        level=FaultLevel.CRASH, component="MT5_IPC:copy_rates_from_pos:bootstrap_regime_h1"
    ):
        h1_rates = worker.copy_rates_from_pos(symbol, MT5_TIMEFRAME_H1, 0, 60, timeout=timeout)
    if h1_rates is not None and len(h1_rates) >= 20:
        gate.feed_h1_bars_batch(h1_rates)

    # ── FIX-20260603-063: hydrate H4 and D1 TrendDetectors ──
    # Previously H4 was built from 48 M5 bars (40h to warm up) and D1 from
    # 6 H4 bars (10 days!).  Loading historical bars directly from MT5
    # gives counter_trend accurate long-term trend from cycle 1.
    h4_rates = None
    with FaultTolerantContext(
        level=FaultLevel.CRASH, component="MT5_IPC:copy_rates_from_pos:bootstrap_regime_h4"
    ):
        h4_rates = worker.copy_rates_from_pos(symbol, MT5_TIMEFRAME_H4, 0, 100, timeout=timeout)
    if h4_rates is not None and len(h4_rates) >= 20:
        gate.feed_h4_bars_batch(h4_rates)

    d1_rates = None
    with FaultTolerantContext(
        level=FaultLevel.CRASH, component="MT5_IPC:copy_rates_from_pos:bootstrap_regime_d1"
    ):
        d1_rates = worker.copy_rates_from_pos(symbol, MT5_TIMEFRAME_D1, 0, 60, timeout=timeout)
    if d1_rates is not None and len(d1_rates) >= 10:
        gate.feed_d1_bars_batch(d1_rates)

    # ── FIX-20260603-063 Step 3: Startup integrity check ──
    # If MT5 returned bars but TrendDetector still isn't ready, something
    # is wrong — refuse to start rather than silently running with
    # unreliable counter_trend.
    import logging as _logging

    _log = _logging.getLogger(__name__)
    if h4_rates is not None and len(h4_rates) >= 20 and not gate.h4_is_ready:
        _log.error("H4 TrendDetector not ready despite %d bars loaded", len(h4_rates))
    if d1_rates is not None and len(d1_rates) >= 10 and not gate._d1.is_ready:
        _log.error("D1 TrendDetector not ready despite %d bars loaded", len(d1_rates))

    return gate.is_ready


def _feed_regime_gate_cycle(
    worker: MT5Worker, symbol: str, gate: Any, timeout: float = 5.0
) -> None:
    """Feed latest M5 and H1 bar to RegimeGate (incremental update).

    Called every cycle. Only the most recent bar is added; duplicates are
    harmless because ADX uses the full buffer.
    """
    m5_bar = None
    with FaultTolerantContext(
        level=FaultLevel.CRASH, component="MT5_IPC:copy_rates_from_pos:feed_regime_m5"
    ):
        m5_bar = worker.copy_rates_from_pos(symbol, MT5_TIMEFRAME_M5, 0, 1, timeout=timeout)
    if m5_bar is not None and len(m5_bar) == 1:
        gate.feed_m5_bar(m5_bar[0]["high"], m5_bar[0]["low"], m5_bar[0]["close"])

    h1_bar = None
    with FaultTolerantContext(
        level=FaultLevel.CRASH, component="MT5_IPC:copy_rates_from_pos:feed_regime_h1"
    ):
        h1_bar = worker.copy_rates_from_pos(symbol, MT5_TIMEFRAME_H1, 0, 1, timeout=timeout)
    if h1_bar is not None and len(h1_bar) == 1:
        gate.feed_h1_bar(h1_bar[0]["high"], h1_bar[0]["low"], h1_bar[0]["close"])
