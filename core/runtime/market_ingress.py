"""Market data ingress helpers — MT5 data fetching and regime gate feeding.

Extracted from live_cycle.py. These functions delegate to :class:`MT5Worker`
for all MT5 C++ calls — no daemon threads, no direct ``mt5.*`` access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.execution.mt5_worker import MT5Worker

# MT5 timeframe constants — pure integers, no thread-affinity requirement.
MT5_TIMEFRAME_M5 = 5
MT5_TIMEFRAME_H1 = 16385  # 60-minute bars


def _get_current_atr(
    worker: MT5Worker, symbol: str, period: int = 14, count: int = 15, timeout: float = 10.0
) -> float:
    """Compute current M5 ATR(*period*) — rates fetched on worker thread, math is local."""
    import numpy as np

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
    pos = worker.positions_get(symbol=symbol, timeout=timeout)
    return len(pos) if pos else 0


def _mid_and_prices(
    worker: MT5Worker, symbol: str, timeout: float = 5.0
) -> tuple[float, float, float]:
    """Fetch bid/ask/mid — executed on the worker thread."""
    tick = worker.symbol_info_tick(symbol, timeout=timeout)
    if tick is None:
        worker.reconnect()
        tick = worker.symbol_info_tick(symbol, timeout=timeout)
    if tick is None:
        raise RuntimeError("tick unavailable")
    bid = float(tick.bid)
    ask = float(tick.ask)
    return (bid + ask) / 2.0, bid, ask


def _bootstrap_regime_gate(
    worker: MT5Worker, symbol: str, gate: Any, timeout: float = 10.0
) -> bool:
    """Bootstrap RegimeGate with recent M5 and H1 bars from MT5.

    Called once on first cycle to fill the ADX buffer. Returns True if
    enough bars were loaded for M5 ADX.
    """
    try:
        m5_rates = worker.copy_rates_from_pos(symbol, MT5_TIMEFRAME_M5, 0, 50, timeout=timeout)
        if m5_rates is not None and len(m5_rates) >= 15:
            gate.feed_m5_bars_batch(m5_rates)

        h1_rates = worker.copy_rates_from_pos(symbol, MT5_TIMEFRAME_H1, 0, 60, timeout=timeout)
        if h1_rates is not None and len(h1_rates) >= 20:
            gate.feed_h1_bars_batch(h1_rates)

        return gate.is_ready
    except Exception:
        return False


def _feed_regime_gate_cycle(
    worker: MT5Worker, symbol: str, gate: Any, timeout: float = 5.0
) -> None:
    """Feed latest M5 and H1 bar to RegimeGate (incremental update).

    Called every cycle. Only the most recent bar is added; duplicates are
    harmless because ADX uses the full buffer.
    """
    try:
        m5_bar = worker.copy_rates_from_pos(symbol, MT5_TIMEFRAME_M5, 0, 1, timeout=timeout)
        if m5_bar is not None and len(m5_bar) == 1:
            gate.feed_m5_bar(m5_bar[0]["high"], m5_bar[0]["low"], m5_bar[0]["close"])

        h1_bar = worker.copy_rates_from_pos(symbol, MT5_TIMEFRAME_H1, 0, 1, timeout=timeout)
        if h1_bar is not None and len(h1_bar) == 1:
            gate.feed_h1_bar(h1_bar[0]["high"], h1_bar[0]["low"], h1_bar[0]["close"])
    except Exception:
        pass
