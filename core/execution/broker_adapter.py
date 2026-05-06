"""Broker adapter protocol — the single swap point for MT5 → FIX / cloud deployment.

Implement this Protocol to add a new broker without changing the live trading
pipeline.  The current production implementation is :class:`MT5BrokerAdapter`.
"""

from __future__ import annotations

from typing import Any, Protocol


class BrokerAdapter(Protocol):
    """Interface for broker-specific data operations.

    This is the **only** interface you need to implement when swapping
    MT5 for FIX, Interactive Brokers, or any other execution venue.
    The live cycle, order dispatch, and risk evaluation all consume
    this protocol — never a concrete broker type directly.

    Required attributes / methods:
        broker_name: str
        fetch_prices(symbol) -> tuple[mid, bid, ask]
        fetch_current_atr(symbol, period=14) -> float
        count_positions(symbol) -> int
    """

    broker_name: str

    def fetch_prices(self, symbol: str) -> tuple[float, float, float]:
        """Return (mid, bid, ask) for *symbol*.  Raises RuntimeError on failure."""
        ...

    def fetch_current_atr(self, symbol: str, period: int = 14) -> float:
        """Return current ATR(14) for *symbol*.  Returns 0.0 on failure."""
        ...

    def count_positions(self, symbol: str) -> int:
        """Return the number of currently open positions for *symbol*."""
        ...

    def get_position_tickets(self, symbol: str) -> list[int]:
        """Return list of open position ticket IDs for *symbol*."""
        ...

    def get_account_drawdown_pct(self) -> float:
        """Return current drawdown as a percentage (0.0–100.0).  Returns 0.0 on failure."""
        ...

    def get_open_positions_detail(self, symbol: str) -> list[dict[str, Any]]:
        """Return open position details for risk context.

        Each dict should contain: symbol, volume, price_open, ticket.
        """
        ...

    # ── Future extension points (not yet called by any production path) ──

    def connect(self) -> bool:
        """Establish broker connection.  Return True on success."""
        ...

    def disconnect(self) -> None:
        """Tear down broker connection."""
        ...

    def is_connected(self) -> bool:
        """Return whether the broker connection is alive."""
        ...
