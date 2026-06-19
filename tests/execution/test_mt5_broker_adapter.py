"""Tests for core.execution.mt5_broker_adapter — MT5 broker adapter.

FIX-20260619-037: Tier 1 zero-coverage breakout #8.
Covers MT5BrokerAdapter with mocked MT5Worker.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.execution.mt5_broker_adapter import MT5BrokerAdapter


class TestMT5BrokerAdapter:
    def _make_adapter(self) -> MT5BrokerAdapter:
        return MT5BrokerAdapter(MagicMock())

    def test_fetch_prices_returns_mid_bid_ask(self) -> None:
        adapter = self._make_adapter()
        tick = SimpleNamespace(bid=4699.5, ask=4700.5)
        adapter._worker.symbol_info_tick.return_value = tick

        mid, bid, ask = adapter.fetch_prices("XAUUSDc")
        assert bid == 4699.5
        assert ask == 4700.5
        assert mid == pytest.approx(4700.0)

    def test_fetch_prices_raises_when_tick_none(self) -> None:
        adapter = self._make_adapter()
        adapter._worker.symbol_info_tick.return_value = None

        with pytest.raises(RuntimeError, match="tick unavailable"):
            adapter.fetch_prices("XAUUSDc")

    def test_get_account_equity(self) -> None:
        adapter = self._make_adapter()
        adapter._worker.account_info.return_value = SimpleNamespace(equity=9500.0)

        eq = adapter.get_account_equity()
        assert eq == 9500.0

    def test_get_account_equity_returns_none_on_error(self) -> None:
        adapter = self._make_adapter()
        adapter._worker.account_info.side_effect = RuntimeError("mt5 crash")

        eq = adapter.get_account_equity()
        assert eq is None

    def test_count_positions(self) -> None:
        adapter = self._make_adapter()
        adapter._worker.positions_get.return_value = [MagicMock(), MagicMock()]

        assert adapter.count_positions("XAUUSDc") == 2

    def test_count_positions_zero_when_none(self) -> None:
        adapter = self._make_adapter()
        adapter._worker.positions_get.return_value = None

        assert adapter.count_positions("XAUUSDc") == 0

    def test_get_position_tickets(self) -> None:
        adapter = self._make_adapter()
        p1 = SimpleNamespace(ticket=1001)
        p2 = SimpleNamespace(ticket=1002)
        adapter._worker.positions_get.return_value = [p1, p2]

        tickets = adapter.get_position_tickets("XAUUSDc")
        assert tickets == [1001, 1002]

    def test_get_account_drawdown_pct(self) -> None:
        adapter = self._make_adapter()
        adapter._worker.account_info.return_value = SimpleNamespace(equity=9000.0, balance=10000.0)

        dd = adapter.get_account_drawdown_pct()
        assert dd == pytest.approx(10.0)

    def test_get_account_drawdown_zero_when_no_balance(self) -> None:
        adapter = self._make_adapter()
        adapter._worker.account_info.return_value = SimpleNamespace(equity=9000.0, balance=0.0)

        assert adapter.get_account_drawdown_pct() == 0.0

    def test_close_position_success(self) -> None:
        adapter = self._make_adapter()
        adapter._worker.order_send.return_value = SimpleNamespace(retcode=10009, volume=0.1)

        ok, msg = adapter.close_position(1001)
        assert ok is True

    def test_close_position_failure(self) -> None:
        adapter = self._make_adapter()
        adapter._worker.order_send.return_value = SimpleNamespace(retcode=99999)

        ok, msg = adapter.close_position(1001)
        assert ok is False

    def test_close_position_none_response(self) -> None:
        adapter = self._make_adapter()
        adapter._worker.order_send.return_value = None

        ok, msg = adapter.close_position(1001)
        assert ok is False
