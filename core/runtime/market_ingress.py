"""Market data ingress helpers — MT5 data fetching and regime gate feeding.

Extracted from live_cycle.py. These functions are pure MT5 adapters with no
dependency on LiveCycleConfig or LiveCycleState.
"""

from __future__ import annotations

from typing import Any


def _get_current_atr(
    mt5: Any, symbol: str, period: int = 14, count: int = 15, timeout: float = 5.0
) -> float:
    """Compute current M5 ATR(14) from MT5 rates with thread timeout."""
    import threading

    import numpy as np

    result: list[Any] = [None]
    exc_info: list[Any] = [None]

    def _target() -> None:
        try:
            result[0] = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, count)
        except Exception as e:
            exc_info[0] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive() or exc_info[0] is not None:
        return 0.0
    rates = result[0]
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


def _position_count(mt5: Any, symbol: str, timeout: float = 5.0) -> int:
    """Count open MT5 positions with a thread timeout to prevent indefinite blocking."""
    import threading

    result: list[int | None] = [None]
    exc_info: list[Any] = [None]

    def _target() -> None:
        try:
            pos = mt5.positions_get(symbol=symbol)
            result[0] = len(pos) if pos else 0
        except Exception as e:
            exc_info[0] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        return 0
    if exc_info[0] is not None:
        raise exc_info[0]
    return result[0] if result[0] is not None else 0


def _mid_and_prices(mt5: Any, symbol: str, timeout: float = 5.0) -> tuple[float, float, float]:
    """Fetch bid/ask/mid with thread timeout to prevent MT5 hang."""
    import threading

    result: list[Any] = [None]
    exc_info: list[Any] = [None]

    def _target() -> None:
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                try:
                    mt5.initialize()
                    tick = mt5.symbol_info_tick(symbol)
                except Exception:
                    pass
            result[0] = tick
        except Exception as e:
            exc_info[0] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise TimeoutError(f"symbol_info_tick timed out after {timeout}s")
    if exc_info[0] is not None:
        raise exc_info[0]
    tick = result[0]
    if tick is None:
        raise RuntimeError("tick unavailable")
    bid = float(tick.bid)
    ask = float(tick.ask)
    return (bid + ask) / 2.0, bid, ask


def _bootstrap_regime_gate(mt5: Any, symbol: str, gate: Any) -> bool:
    """Bootstrap RegimeGate with recent M5 and H1 bars from MT5.

    Called once on first cycle to fill the ADX buffer. Returns True if
    enough bars were loaded for M5 ADX.
    """
    try:
        m5_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 50)
        if m5_rates is not None and len(m5_rates) >= 15:
            gate.feed_m5_bars_batch(m5_rates)

        h1_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 60)
        if h1_rates is not None and len(h1_rates) >= 20:
            gate.feed_h1_bars_batch(h1_rates)

        return gate.is_ready
    except Exception:
        return False


def _feed_regime_gate_cycle(mt5: Any, symbol: str, gate: Any) -> None:
    """Feed latest M5 and H1 bar to RegimeGate (incremental update).

    Called every cycle. Only the most recent bar is added; duplicates are
    harmless because ADX uses the full buffer.
    """
    try:
        m5_bar = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 1)
        if m5_bar is not None and len(m5_bar) == 1:
            gate.feed_m5_bar(m5_bar[0]["high"], m5_bar[0]["low"], m5_bar[0]["close"])

        h1_bar = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 1)
        if h1_bar is not None and len(h1_bar) == 1:
            gate.feed_h1_bar(h1_bar[0]["high"], h1_bar[0]["low"], h1_bar[0]["close"])
    except Exception:
        pass
