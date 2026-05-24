"""Order state machine and fill simulator tests."""

import pytest

from core.execution.fill_simulator import FillSimulationConfig, FillSimulator
from core.execution.gateway_contracts import Fill, OrderRequest
from core.execution.order_state_machine import OrderStateMachine


def _request(order_id="ord1", side="buy", quantity=10.0, order_type="market", limit_price=None):
    return OrderRequest(
        order_id=order_id,
        correlation_id="corr1",
        symbol="XAUUSD",
        side=side,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
    )


def _fill(quantity=10.0, price=2000.0):
    return Fill(fill_id="fill1", order_id="ord1", quantity=quantity, price=price)


class TestOrderStateMachine:
    def test_happy_path_fill(self):
        sm = OrderStateMachine()
        state = sm.create(_request(), "PAPER")
        assert state.status == "created"
        sm.acknowledge(state)
        sm.accept(state)
        sm.apply_fill(state, _fill())
        assert state.status == "filled"
        assert state.average_price == 2000.0
        assert state.remaining_quantity == 0.0

    def test_partial_then_full_fill_average_price(self):
        sm = OrderStateMachine()
        state = sm.create(_request(quantity=10), "PAPER")
        sm.acknowledge(state)
        sm.accept(state)
        sm.apply_fill(state, _fill(quantity=4, price=2000))
        assert state.status == "partial"
        sm.apply_fill(state, _fill(quantity=6, price=2010))
        assert state.status == "filled"
        assert state.average_price == 2006.0

    def test_invalid_transition_rejected(self):
        sm = OrderStateMachine()
        state = sm.create(_request(), "PAPER")
        with pytest.raises(ValueError):
            sm.accept(state)

    def test_terminal_cancel_noop(self):
        sm = OrderStateMachine()
        state = sm.create(_request(), "PAPER")
        sm.acknowledge(state)
        sm.accept(state)
        sm.apply_fill(state, _fill())
        assert sm.cancel(state).status == "filled"

    def test_reject_sets_reason(self):
        sm = OrderStateMachine()
        state = sm.create(_request(), "PAPER")
        sm.acknowledge(state)
        sm.reject(state, "risk_block")
        assert state.status == "rejected"
        assert state.rejection_reason == "risk_block"

    def test_overfill_rejected(self):
        sm = OrderStateMachine()
        state = sm.create(_request(quantity=1), "PAPER")
        sm.acknowledge(state)
        sm.accept(state)
        with pytest.raises(ValueError):
            sm.apply_fill(state, _fill(quantity=2))


class TestFillSimulator:
    def test_market_buy_uses_ask(self):
        sm = OrderStateMachine()
        request = _request(side="buy")
        state = sm.create(request, "PAPER")
        sm.acknowledge(state)
        sm.accept(state)
        fill = FillSimulator().simulate(request, state, {"bid": 1999.0, "ask": 2000.0})
        assert fill.price == 2000.0
        assert fill.quantity == 10.0

    def test_market_sell_uses_bid(self):
        sm = OrderStateMachine()
        request = _request(side="sell")
        state = sm.create(request, "PAPER")
        sm.acknowledge(state)
        sm.accept(state)
        fill = FillSimulator().simulate(request, state, {"bid": 1999.0, "ask": 2000.0})
        assert fill.price == 1999.0

    def test_limit_order_not_executable_returns_none(self):
        sm = OrderStateMachine()
        request = _request(order_type="limit", limit_price=1999.0)
        state = sm.create(request, "PAPER")
        sm.acknowledge(state)
        sm.accept(state)
        assert FillSimulator().simulate(request, state, {"bid": 1998.0, "ask": 2000.0}) is None

    def test_partial_fill_by_ratio(self):
        sm = OrderStateMachine()
        request = _request(quantity=10)
        state = sm.create(request, "PAPER")
        sm.acknowledge(state)
        sm.accept(state)
        fill = FillSimulator(FillSimulationConfig(max_fill_ratio=0.25)).simulate(
            request, state, {"price": 2000.0}
        )
        assert fill.quantity == 2.5

    def test_partial_fill_by_available_quantity(self):
        sm = OrderStateMachine()
        request = _request(quantity=10)
        state = sm.create(request, "PAPER")
        sm.acknowledge(state)
        sm.accept(state)
        fill = FillSimulator().simulate(request, state, {"price": 2000.0, "available_quantity": 3})
        assert fill.quantity == 3.0

    def test_slippage_buy_and_sell(self):
        sm = OrderStateMachine()
        buy = _request(side="buy")
        buy_state = sm.create(buy, "PAPER")
        sm.acknowledge(buy_state)
        sm.accept(buy_state)
        sell = _request(order_id="ord2", side="sell")
        sell_state = sm.create(sell, "PAPER")
        sm.acknowledge(sell_state)
        sm.accept(sell_state)
        simulator = FillSimulator(FillSimulationConfig(slippage_bps=10))
        assert simulator.simulate(buy, buy_state, {"price": 100.0}).price == 100.1
        assert simulator.simulate(sell, sell_state, {"price": 100.0}).price == 99.9

    def test_min_liquidity_quantity_blocks_small_fill(self):
        sm = OrderStateMachine()
        request = _request(quantity=10)
        state = sm.create(request, "PAPER")
        sm.acknowledge(state)
        sm.accept(state)
        simulator = FillSimulator(
            FillSimulationConfig(max_fill_ratio=0.1, min_liquidity_quantity=2)
        )
        assert simulator.simulate(request, state, {"price": 2000.0}) is None

    def test_config_validation(self):
        with pytest.raises(ValueError):
            FillSimulationConfig(max_fill_ratio=0)
        with pytest.raises(ValueError):
            FillSimulationConfig(slippage_bps=-1)
        with pytest.raises(ValueError):
            FillSimulationConfig(min_liquidity_quantity=0)
