"""Market data ingress helpers — MT5 data fetching and regime gate feeding.

Extracted from live_cycle.py. These functions delegate to :class:`MT5Worker`
for all MT5 C++ calls — no daemon threads, no direct ``mt5.*`` access.
"""

from __future__ import annotations

import contextlib
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
MT5_TIMEFRAME_M15 = 15  # 15-minute bars
MT5_TIMEFRAME_M30 = 30  # 30-minute bars (MT5 build 1320+ native support)
MT5_TIMEFRAME_H1 = 16385  # 60-minute bars
MT5_TIMEFRAME_H4 = 16388  # 240-minute bars
MT5_TIMEFRAME_D1 = 16408  # daily bars


# ── TF string → MT5 constant mapping ──
_TF_STR_TO_MT5: dict[str, int] = {
    "M5": MT5_TIMEFRAME_M5,
    "M15": MT5_TIMEFRAME_M15,
    "M30": MT5_TIMEFRAME_M30,
    "H1": MT5_TIMEFRAME_H1,
    "H4": MT5_TIMEFRAME_H4,
    "D1": MT5_TIMEFRAME_D1,
}


def _compute_atr_from_rates(rates: list[dict[str, float]] | None, period: int = 14) -> float:
    """Compute ATR from MT5 rates array — pure math, no I/O.

    #10 hot-path: 调用侧 (FaultTolerantContext) rates 为 Any|None, 实现已含
    ``rates is None`` 短路返回 0.0; 签名补齐 None 声明消除红线类型债 (TECH_DEBT-008).
    """
    import numpy as np

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


def _get_current_atr(
    worker: MT5Worker, symbol: str, period: int = 14, count: int = 15, timeout: float = 10.0
) -> float:
    """Compute current M5 ATR(*period*) — rates fetched on worker thread, math is local."""
    rates = None
    with FaultTolerantContext(
        level=FaultLevel.CRASH,
        component="MT5_IPC:copy_rates_from_pos:get_current_atr",
    ):
        rates = worker.copy_rates_from_pos(symbol, MT5_TIMEFRAME_M5, 0, count, timeout=timeout)
    return _compute_atr_from_rates(rates, period)


def get_multi_timeframe_atr(
    worker: MT5Worker,
    symbol: str,
    timeframes: set[str],
    period: int = 14,
    count: int = 15,
    timeout: float = 10.0,
) -> dict[str, float]:
    """Fetch ATR for multiple timeframes using real MT5 bars from each TF.

    FIX-20260706-027 (L3): Per-strategy timeframe ATR injection.
    Previous architecture hardcoded M5 ATR for ALL strategies regardless of
    their own timeframe — btc_swing_h1 (H1) got M5 ATR, m30_swing (M30) got
    M5 ATR, etc.  This caused:
      - SL/TP barriers at serving time 2.5–7× tighter than training labels
      - btc_swing (M5 SHORT) + btc_swing_h1 (H1 LONG) producing mirror SL/TP
        barriers ($4 apart — essentially identical levels swapped).

    Each timeframe fetches its own bars via MT5 and computes the ATR from
    those bars — no √t estimation, no statistical approximation.

    Args:
        worker: MT5Worker for IPC calls.
        symbol: Trading instrument (e.g. "XAUUSDc").
        timeframes: Set of timeframe strings (e.g. {"M5", "M30", "H1"}).
        period: ATR lookback period (default 14).
        count: Number of bars to fetch (default 15 — period+1 minimum).
        timeout: MT5 IPC timeout per TF.

    Returns:
        Dict mapping timeframe string → ATR value.  M5 is always present
        (computed first as the canonical fallback).  TFs that fail to fetch
        fall back to the M5 value with a logged warning.
    """
    import logging as _logging

    _log = _logging.getLogger(__name__)
    result: dict[str, float] = {}

    # M5 always computed first — canonical fallback
    m5_atr = _get_current_atr(worker, symbol, period=period, count=count, timeout=timeout)
    result["M5"] = m5_atr

    for tf in sorted(timeframes):
        if tf == "M5":
            continue  # already computed

        mt5_tf = _TF_STR_TO_MT5.get(tf)
        if mt5_tf is None:
            _log.warning("get_multi_timeframe_atr: unknown timeframe %s, falling back to M5", tf)
            result[tf] = m5_atr
            continue

        rates = None
        with FaultTolerantContext(
            level=FaultLevel.DEGRADE,
            component=f"MT5_IPC:copy_rates_from_pos:get_atr_{tf}",
        ):
            rates = worker.copy_rates_from_pos(symbol, mt5_tf, 0, count, timeout=timeout)

        tf_atr = _compute_atr_from_rates(rates, period)
        if tf_atr <= 0:
            _log.warning(
                "get_multi_timeframe_atr: %s ATR fetch returned 0, falling back to M5 (%.4f)",
                tf,
                m5_atr,
            )
            result[tf] = m5_atr
        else:
            result[tf] = tf_atr

    return result


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

    FIX-20260613-048: Staleness Contract — tick.time is now propagated to
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


def build_tf_atr_map(
    mt5_worker: MT5Worker,
    symbol: str,
    strategies: dict[str, Any],
    base_tf_atr_map: dict[str, float],
) -> dict[str, float]:
    """Collect unique timeframes from strategies and fetch per-TF ATR.

    FIX-20260706-027 (L3): Extracted from live_cycle.py to preserve monolith
    decoupling — live_cycle calls this single function instead of inlining
    ~25 lines of TF collection + multi-TF fetch logic.

    Non-M5 strategies (M15/M30/H1/H4) were previously using M5 ATR for
    SL/TP → systematically tight barriers (2.5–7× too tight relative to
    training labels).  This function fetches each TF's ATR from real MT5
    bars — no √t estimation.

    Args:
        mt5_worker: MT5Worker for IPC calls.
        symbol: Trading instrument.
        strategies: Dict of strategy_name → strategy_object from
                    _build_strategy_lines().
        base_tf_atr_map: Pre-populated map (must contain "M5" key).

    Returns:
        Updated dict with per-TF ATR values.  Strategies whose TF fetch
        fails fall back to M5 ATR.
    """
    import logging as _logging

    _log = _logging.getLogger(__name__)

    if base_tf_atr_map.get("M5", 0.0) <= 0:
        return base_tf_atr_map

    _active_tfs: set[str] = set()
    for _strat in strategies.values():
        _tf = getattr(_strat.config, "timeframe", "M5")
        if _tf and _tf != "M5":
            _active_tfs.add(_tf)

    if not _active_tfs:
        return base_tf_atr_map

    try:
        _mtf = get_multi_timeframe_atr(mt5_worker, symbol, _active_tfs)
        base_tf_atr_map.update(_mtf)
        with contextlib.suppress(RuntimeError, ValueError, KeyError, TypeError, OSError):
            _log.info(
                "Multi-TF ATR map: %s",
                {k: round(v, 4) for k, v in base_tf_atr_map.items()},
            )
    except (RuntimeError, ValueError, TypeError, OSError) as _mtf_exc:
        with contextlib.suppress(RuntimeError, ValueError, KeyError, TypeError, OSError):
            _log.warning(
                "Multi-TF ATR fetch failed (%s) — all strategies fall back to M5 ATR",
                _mtf_exc,
            )

    return base_tf_atr_map
