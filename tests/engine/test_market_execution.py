from datetime import datetime

from core.market.position_tracker import PositionTracker, MarketContextProvider
from core.execution.execution_manager import ExecutionManager
from core.observability.metric_names import execution_event_metric
from core.observability.metrics_collector import MetricsCollector


class TestPositionTracker:
    def test_open_and_close(self):
        pt = PositionTracker()
        pt.open_position(position_id="p1", symbol="XAUUSD", side="long", quantity=1.0, entry_price=2000.0)
        assert len(pt.list_open()) == 1
        closed = pt.close_position("p1", exit_price=2050.0)
        assert closed["realized_pnl"] == 50.0
        assert len(pt.list_open()) == 0
        assert len(pt.list_closed()) == 1

    def test_short_pnl(self):
        pt = PositionTracker()
        pt.open_position(position_id="p1", symbol="XAUUSD", side="short", quantity=2.0, entry_price=2000.0)
        closed = pt.close_position("p1", exit_price=1980.0)
        assert closed["realized_pnl"] == 40.0

    def test_risk_context(self):
        pt = PositionTracker()
        pt.open_position(position_id="p1", symbol="XAUUSD", side="long", quantity=1.0, entry_price=2000.0)
        pt.open_position(position_id="p2", symbol="XAUUSD", side="long", quantity=0.5, entry_price=2010.0)
        pt.open_position(position_id="p3", symbol="EURUSD", side="short", quantity=100.0, entry_price=1.08)
        ctx = pt.get_risk_context()
        assert ctx["open_position_count"] == 3
        assert ctx["positions_per_symbol"]["XAUUSD"] == 2
        assert ctx["positions_per_symbol"]["EURUSD"] == 1
        assert ctx["current_notional_exposure"] > 0

    def test_close_unknown_returns_none(self):
        pt = PositionTracker()
        assert pt.close_position("unknown", 100) is None


class TestMarketContextProvider:
    def test_update_and_get(self):
        mc = MarketContextProvider()
        mc.update("XAUUSD", bid=2000.0, ask=2001.0)
        ctx = mc.get_context("XAUUSD")
        assert ctx["available"] is True
        assert ctx["mid"] == 2000.5
        assert ctx["spread"] == 1.0

    def test_price_move(self):
        mc = MarketContextProvider()
        mc.update("XAUUSD", bid=2000.0, ask=2001.0)
        mc.update("XAUUSD", bid=2010.0, ask=2011.0)
        ctx = mc.get_context("XAUUSD")
        assert ctx["price_move_pct"] > 0

    def test_unknown_symbol(self):
        mc = MarketContextProvider()
        ctx = mc.get_context("UNKNOWN")
        assert ctx["available"] is False

    def test_get_all(self):
        mc = MarketContextProvider()
        mc.update("A", bid=1.0, ask=1.1)
        mc.update("B", bid=2.0, ask=2.1)
        assert len(mc.get_all()) == 2


class TestExecutionManager:
    def test_register_and_process_fill(self):
        em = ExecutionManager()
        em.register_order(message_id="m1", correlation_id="c1",
                          symbol="XAUUSD", side="long", quantity=1.0)
        r = em.process_venue_event(message_id="m1", event_type="ack")
        assert r["new_status"] == "sent"

        r = em.process_venue_event(message_id="m1", event_type="filled",
                                    filled_quantity=1.0, price=2000.0, venue="venue_a")
        assert r["new_status"] == "filled"
        assert r["order"]["filled_quantity"] == 1.0
        assert r["order"]["average_price"] == 2000.0

    def test_partial_fills(self):
        em = ExecutionManager()
        em.register_order(message_id="m1", correlation_id="c1",
                          symbol="XAUUSD", side="long", quantity=3.0)
        em.process_venue_event(message_id="m1", event_type="partially_filled",
                               filled_quantity=1.0, price=2000.0)
        em.process_venue_event(message_id="m1", event_type="partially_filled",
                               filled_quantity=1.0, price=2010.0)
        order = em.get_order("m1")
        assert order["filled_quantity"] == 2.0
        assert order["average_price"] == 2005.0
        assert order["status"] == "partial"

    def test_rejected(self):
        em = ExecutionManager()
        em.register_order(message_id="m1", correlation_id="c1",
                          symbol="XAUUSD", side="long", quantity=1.0)
        r = em.process_venue_event(message_id="m1", event_type="rejected")
        assert r["new_status"] == "rejected"

    def test_unknown_order(self):
        em = ExecutionManager()
        r = em.process_venue_event(message_id="unknown", event_type="ack")
        assert r["status"] == "unknown_order"

    def test_list_by_status(self):
        em = ExecutionManager()
        em.register_order(message_id="m1", correlation_id="c1",
                          symbol="XAUUSD", side="long", quantity=1.0)
        em.register_order(message_id="m2", correlation_id="c2",
                          symbol="EURUSD", side="short", quantity=2.0)
        em.process_venue_event(message_id="m1", event_type="filled",
                               filled_quantity=1.0, price=2000.0)
        assert len(em.list_orders("filled")) == 1
        assert len(em.list_orders("pending")) == 1
        assert len(em.list_orders()) == 2

    def test_with_position_tracker(self):
        pt = PositionTracker()
        em = ExecutionManager(position_tracker=pt)
        em.register_order(message_id="m1", correlation_id="c1",
                          symbol="XAUUSD", side="long", quantity=1.0)
        em.process_venue_event(message_id="m1", event_type="filled",
                               filled_quantity=1.0, price=2000.0)
        assert len(pt.list_open()) == 1
        assert pt.list_open()[0]["symbol"] == "XAUUSD"

    def test_with_metrics(self):
        m = MetricsCollector()
        em = ExecutionManager(metrics=m)
        em.register_order(message_id="m1", correlation_id="c1",
                          symbol="XAUUSD", side="long", quantity=1.0)
        em.process_venue_event(message_id="m1", event_type="ack")
        em.process_venue_event(message_id="m1", event_type="filled",
                               filled_quantity=1.0, price=2000.0)
        assert m.get_counter(execution_event_metric("ack")) == 1
        assert m.get_counter(execution_event_metric("filled")) == 1
