"""MT5 implementation of :class:`BrokerAdapter`.

Wraps the MetaTrader5 Python API behind the broker-agnostic protocol so
every consumer (live cycle, order dispatch, risk evaluation) talks to
a ``BrokerAdapter`` instead of the raw MT5 library.
"""

from __future__ import annotations

from typing import Any


class MT5BrokerAdapter:
    """BrokerAdapter backed by a long-lived MetaTrader5 connection.

    The MT5 terminal must already be initialized (``mt5.initialize()``)
    before constructing this adapter.  The caller owns the MT5 lifecycle;
    this adapter does NOT call ``mt5.initialize()`` or ``mt5.shutdown()``.

    Usage::

        import MetaTrader5 as mt5
        mt5.initialize(path="C:\\...")
        broker = MT5BrokerAdapter(mt5)
        mid, bid, ask = broker.fetch_prices("XAUUSDc")
    """

    broker_name = "mt5"

    def __init__(self, mt5_module: Any) -> None:
        self._mt5 = mt5_module

    # ── Required by BrokerAdapter ──

    def fetch_prices(self, symbol: str, timeout: float = 5.0) -> tuple[float, float, float]:
        """Fetch bid/ask/mid with thread timeout to prevent MT5 hang."""
        import threading

        result: list[Any] = [None]
        exc_info: list[Any] = [None]

        def _target() -> None:
            try:
                tick = self._mt5.symbol_info_tick(symbol)
                if tick is None:
                    try:
                        self._mt5.initialize()
                        tick = self._mt5.symbol_info_tick(symbol)
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
            raise RuntimeError(f"tick unavailable for {symbol}")
        bid = float(tick.bid)
        ask = float(tick.ask)
        mid = (bid + ask) / 2.0
        return mid, bid, ask

    def fetch_current_atr(self, symbol: str, period: int = 14, timeout: float = 5.0) -> float:
        """Compute current M5 ATR(14) with thread timeout to prevent MT5 hang."""
        import threading

        import numpy as np

        result: list[Any] = [None]
        exc_info: list[Any] = [None]

        def _target() -> None:
            try:
                rates = self._mt5.copy_rates_from_pos(symbol, self._mt5.TIMEFRAME_M5, 0, period + 1)
                result[0] = rates
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
        tr = np.maximum(
            cur_h - cur_l,
            np.maximum(abs(cur_h - prev_c), abs(cur_l - prev_c)),
        )
        return float(np.mean(tr))

    def count_positions(self, symbol: str, timeout: float = 5.0) -> int:
        """Count open MT5 positions with thread timeout to prevent hanging."""
        import threading

        result: list[int | None] = [None]
        exc_info: list[Any] = [None]

        def _target() -> None:
            try:
                pos = self._mt5.positions_get(symbol=symbol)
                result[0] = len(pos) if pos else 0
            except Exception as e:
                exc_info[0] = e

        t = threading.Thread(target=_target, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            return -1  # timed out → signal caller to treat as unavailable
        if exc_info[0] is not None:
            return -1
        return result[0] if result[0] is not None else -1

    def get_position_tickets(self, symbol: str) -> list[int]:
        pos = self._mt5.positions_get(symbol=symbol)
        return [p.ticket for p in pos] if pos else []

    def get_account_drawdown_pct(self) -> float:
        try:
            acc = self._mt5.account_info()
            if acc is None:
                return 0.0
            equity = float(getattr(acc, "equity", 0))
            balance = float(getattr(acc, "balance", 0))
            if balance <= 0:
                return 0.0
            return round(max(0.0, (balance - equity) / balance) * 100, 2)
        except Exception:
            return 0.0

    def close_position(self, ticket: int, slippage: int = 200) -> tuple[bool, str]:
        """L2 forced liquidation: close a position by ticket directly via MT5.

        Returns (success, message).  Bypasses the bridge — use only when the
        normal dispatch path has timed out or exhausted retries.
        """
        import threading

        result: list[Any] = [None]
        exc_info: list[Any] = [None]

        def _target() -> None:
            try:
                request = {
                    "action": self._mt5.TRADE_ACTION_DEAL,
                    "position": ticket,
                    "slippage": slippage,
                }
                resp = self._mt5.order_send(request)
                if resp is None:
                    result[0] = (False, f"order_send returned None (ticket={ticket})")
                elif resp.retcode == self._mt5.TRADE_RETCODE_DONE:
                    result[0] = (True, f"L2 close ok ticket={ticket} vol={resp.volume}")
                else:
                    result[0] = (
                        False,
                        f"L2 close failed ticket={ticket} retcode={resp.retcode} "
                        f"comment={getattr(resp, 'comment', '')}",
                    )
            except Exception as e:
                exc_info[0] = e

        t = threading.Thread(target=_target, daemon=True)
        t.start()
        t.join(timeout=10.0)
        if t.is_alive():
            return False, f"L2 close timed out (ticket={ticket})"
        if exc_info[0] is not None:
            return False, f"L2 close exception: {exc_info[0]}"
        return result[0] if result[0] is not None else (False, "L2 close: no result")

    def get_open_positions_detail(self, symbol: str) -> list[dict[str, Any]]:
        positions = self._mt5.positions_get(symbol=symbol) or []
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

    # ── Future extension points ──

    def connect(self) -> bool:
        return True  # MT5 is already initialized by caller

    def disconnect(self) -> None:
        pass  # MT5 lifecycle is owned by caller

    def is_connected(self) -> bool:
        return True
