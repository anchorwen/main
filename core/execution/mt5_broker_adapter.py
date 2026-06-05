"""MT5 implementation of :class:`BrokerAdapter`.

Wraps :class:`MT5Worker` behind the broker-agnostic protocol so every
consumer (live cycle, order dispatch, risk evaluation) talks to a
``BrokerAdapter`` instead of the raw MT5 library.

All MT5 C++ calls execute on the single dedicated MT5Worker thread.
No daemon threads are spawned and no init/shutdown cycles are performed
by this adapter — the caller (``live_intent_loop.py``) owns the worker
lifecycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.execution.mt5_worker import MT5Worker

# MT5 constants — pure integers, no thread-affinity requirement.
# Hardcoded to avoid requiring MetaTrader5 to be importable.
MT5_TIMEFRAME_M5 = 5
MT5_TRADE_ACTION_DEAL = 1
MT5_TRADE_RETCODE_DONE = 10009


class MT5BrokerAdapter:
    """BrokerAdapter backed by the single-threaded :class:`MT5Worker`.

    Usage::

        worker = MT5Worker()
        worker.start(terminal_path=r"C:\\...")
        broker = MT5BrokerAdapter(worker)
        mid, bid, ask = broker.fetch_prices("XAUUSDc")
    """

    broker_name = "mt5"

    def __init__(self, mt5_worker: MT5Worker) -> None:
        self._worker: MT5Worker = mt5_worker

    # ── Required by BrokerAdapter ──

    def fetch_prices(self, symbol: str, timeout: float = 5.0) -> tuple[float, float, float]:
        """Fetch bid/ask/mid — executed on the MT5 worker thread."""
        tick = self._worker.symbol_info_tick(symbol, timeout=timeout)
        if tick is None:
            raise RuntimeError(f"tick unavailable for {symbol}")
        bid = float(tick.bid)
        ask = float(tick.ask)
        mid = (bid + ask) / 2.0
        return mid, bid, ask

    def get_account_equity(self, timeout: float = 5.0) -> float | None:
        """Return current account equity from the MT5 worker thread.

        Returns None if the worker is unavailable or the query fails.
        Used by live_cycle.py for equity-based risk budgeting.
        """
        try:
            acc = self._worker.account_info(timeout=timeout)
            if acc is not None:
                return float(getattr(acc, "equity", 0))
        except Exception:
            pass
        return None

    def fetch_current_atr(self, symbol: str, period: int = 14, timeout: float = 10.0) -> float:
        """Compute current M5 ATR(*period*) — rates fetched on worker, math is local."""
        import numpy as np

        rates = self._worker.copy_rates_from_pos(
            symbol, MT5_TIMEFRAME_M5, 0, period + 1, timeout=timeout
        )
        if rates is None or len(rates) < period + 1:
            return 0.0
        h = np.array([r["high"] for r in rates], dtype=np.float64)
        low = np.array([r["low"] for r in rates], dtype=np.float64)
        c = np.array([r["close"] for r in rates], dtype=np.float64)
        prev_c = c[-(period + 1) : -1]
        cur_h = h[-period:]
        cur_l = low[-period:]
        tr = np.maximum(
            cur_h - cur_l,
            np.maximum(abs(cur_h - prev_c), abs(cur_l - prev_c)),
        )
        return float(np.mean(tr))

    def count_positions(self, symbol: str, timeout: float = 5.0) -> int:
        """Count open MT5 positions — executed on the worker thread."""
        pos = self._worker.positions_get(symbol=symbol, timeout=timeout)
        return len(pos) if pos else 0

    def get_position_tickets(self, symbol: str, timeout: float = 5.0) -> list[int]:
        """Return open position tickets for *symbol*."""
        pos = self._worker.positions_get(symbol=symbol, timeout=timeout)
        return [p.ticket for p in pos] if pos else []

    def get_account_drawdown_pct(self, timeout: float = 5.0) -> float:
        """Compute drawdown % from account equity/balance."""
        acc = self._worker.account_info(timeout=timeout)
        if acc is None:
            return 0.0
        equity = float(getattr(acc, "equity", 0))
        balance = float(getattr(acc, "balance", 0))
        if balance <= 0:
            return 0.0
        return round(max(0.0, (balance - equity) / balance) * 100, 2)

    def close_position(
        self, ticket: int, slippage: int = 200, timeout: float = 10.0
    ) -> tuple[bool, str]:
        """L2 forced liquidation: close a position by ticket via MT5.

        Returns (success, message).
        """
        request = {
            "action": MT5_TRADE_ACTION_DEAL,
            "position": ticket,
            "slippage": slippage,
        }
        resp = self._worker.order_send(request, timeout=timeout)
        if resp is None:
            return False, f"order_send returned None (ticket={ticket})"
        if resp.retcode == MT5_TRADE_RETCODE_DONE:
            return True, f"L2 close ok ticket={ticket} vol={resp.volume}"
        return (
            False,
            f"L2 close failed ticket={ticket} retcode={resp.retcode} "
            f"comment={getattr(resp, 'comment', '')}",
        )

    def get_open_positions_detail(self, symbol: str, timeout: float = 5.0) -> list[dict[str, Any]]:
        """Return detailed position dicts for *symbol*."""
        positions = self._worker.positions_get(symbol=symbol, timeout=timeout)
        result: list[dict[str, Any]] = []
        for pos in positions:
            result.append(
                {
                    "symbol": getattr(pos, "symbol", symbol),
                    "volume": float(getattr(pos, "volume", 0)),
                    "price_open": float(getattr(pos, "price_open", 0)),
                    "ticket": getattr(pos, "ticket", 0),
                }
            )
        return result

    # ── Connection state (delegated to worker) ──

    def connect(self) -> bool:
        """The worker manages connection — this is a no-op."""
        return True

    def disconnect(self) -> None:
        """The worker manages connection — this is a no-op."""

    def is_connected(self) -> bool:
        return True
