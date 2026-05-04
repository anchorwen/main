"""Paper execution gateway tests."""

import pytest

from core.execution.gateway_contracts import OrderRequest
from core.execution.paper_gateway import PaperExecutionGateway
from core.observability.metric_names import PAPER_EXECUTION_FILL_QUANTITY, PAPER_EXECUTION_FILLED


class FakeWriter:
    def __init__(self):
        self.events = []

    def write_from_venue_payload(self, **kwargs):
        self.events.append(kwargs)
        return kwargs, None


class FakeMetrics:
    def __init__(self):
        self.counters = {}
        self.observations = []

    def inc(self, key, value=1):
        self.counters[key] = self.counters.get(key, 0) + value

    def observe(self, key, value):
        self.observations.append((key, value))


def _request(order_id="ord1", side="buy", quantity=1.0, order_type="market", limit_price=None):
    return OrderRequest(
        order_id=order_id,
        correlation_id="corr1",
        symbol="XAUUSD",
        side=side,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
    )


class TestOrderRequest:
    def test_order_request_validation(self):
        with pytest.raises(ValueError):
            _request(side="hold")
        with pytest.raises(ValueError):
            _request(quantity=0)
        with pytest.raises(ValueError):
            _request(order_type="limit")


class TestPaperExecutionGateway:
    def test_market_buy_fills_at_ask(self):
        gateway = PaperExecutionGateway()
        state = gateway.submit_order(_request(side="buy"), {"bid": 1999.0, "ask": 2000.0})
        assert state.status == "filled"
        assert state.average_price == 2000.0
        assert state.filled_quantity == 1.0
        assert [e["event_type"] for e in gateway.list_events()] == ["ack", "accepted", "filled"]

    def test_market_sell_fills_at_bid(self):
        gateway = PaperExecutionGateway()
        state = gateway.submit_order(_request(side="sell"), {"bid": 1999.0, "ask": 2000.0})
        assert state.status == "filled"
        assert state.average_price == 1999.0

    def test_limit_order_resting_then_mark_to_market(self):
        gateway = PaperExecutionGateway()
        state = gateway.submit_order(
            _request(order_type="limit", limit_price=1999.0), {"bid": 1998.0, "ask": 2000.0}
        )
        assert state.status == "working"
        state = gateway.mark_to_market("ord1", {"bid": 1998.5, "ask": 1999.0})
        assert state.status == "filled"
        assert state.average_price == 1999.0

    def test_cancel_working_order(self):
        gateway = PaperExecutionGateway()
        gateway.submit_order(
            _request(order_type="limit", limit_price=1999.0), {"bid": 1998.0, "ask": 2000.0}
        )
        state = gateway.cancel_order("ord1")
        assert state.status == "cancelled"
        assert state.is_terminal is True
        assert gateway.list_events()[-1]["event_type"] == "cancelled"

    def test_cancel_filled_order_noop(self):
        gateway = PaperExecutionGateway()
        gateway.submit_order(_request(), {"price": 2000.0})
        state = gateway.cancel_order("ord1")
        assert state.status == "filled"

    def test_duplicate_order_rejected(self):
        gateway = PaperExecutionGateway()
        gateway.submit_order(_request(), {"price": 2000.0})
        with pytest.raises(ValueError):
            gateway.submit_order(_request(), {"price": 2000.0})

    def test_unknown_cancel_rejected(self):
        gateway = PaperExecutionGateway()
        with pytest.raises(ValueError):
            gateway.cancel_order("missing")

    def test_writer_receives_execution_events(self):
        writer = FakeWriter()
        gateway = PaperExecutionGateway(execution_event_writer=writer)
        gateway.submit_order(_request(), {"price": 2000.0})
        assert [e["event_type"] for e in writer.events] == ["ack", "accepted", "filled"]
        assert writer.events[-1]["quantity"] == {"filled": 1.0}

    def test_metrics_updated_on_fill(self):
        metrics = FakeMetrics()
        gateway = PaperExecutionGateway(metrics=metrics)
        gateway.submit_order(_request(quantity=2.0), {"price": 2000.0})
        assert metrics.counters[PAPER_EXECUTION_FILLED] == 1
        assert metrics.observations == [(PAPER_EXECUTION_FILL_QUANTITY, 2.0)]

    def test_list_orders_by_status(self):
        gateway = PaperExecutionGateway()
        gateway.submit_order(_request(order_id="ord1"), {"price": 2000.0})
        gateway.submit_order(
            _request(order_id="ord2", order_type="limit", limit_price=1999.0),
            {"bid": 1998.0, "ask": 2000.0},
        )
        assert len(gateway.list_orders()) == 2
        assert len(gateway.list_orders(status="filled")) == 1
        assert len(gateway.list_orders(status="working")) == 1

    def test_order_state_to_dict(self):
        gateway = PaperExecutionGateway()
        state = gateway.submit_order(_request(), {"price": 2000.0})
        payload = state.to_dict()
        assert payload["order_id"] == "ord1"
        assert payload["remaining_quantity"] == 0.0
        assert payload["fills"][0]["price"] == 2000.0
