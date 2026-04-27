"""FIX gateway adapter skeleton tests."""
import pytest

from core.execution.fix_contracts import FixExecutionReport, FixSessionConfig
from core.execution.fix_execution_mapper import FixExecutionReportMapper
from core.execution.fix_gateway_adapter import FixGatewayAdapter
from core.execution.fix_message_builder import FixMessageBuilder
from core.execution.gateway_contracts import OrderRequest
from core.execution.order_state_machine import OrderStateMachine


class FakeWriter:
    def __init__(self):
        self.events = []

    def write_from_venue_payload(self, **kwargs):
        self.events.append(kwargs)
        return kwargs, None


def _config():
    return FixSessionConfig(sender_comp_id="SENDER", target_comp_id="TARGET", venue="FIX_TEST")


def _request(order_id="ord1", side="buy", order_type="market", limit_price=None):
    return OrderRequest(
        order_id=order_id,
        correlation_id="corr1",
        symbol="XAUUSD",
        side=side,
        quantity=10,
        order_type=order_type,
        limit_price=limit_price,
    )


class TestFixContracts:
    def test_session_config_validation(self):
        with pytest.raises(ValueError):
            FixSessionConfig(sender_comp_id="", target_comp_id="TARGET")
        with pytest.raises(ValueError):
            FixSessionConfig(sender_comp_id="SENDER", target_comp_id="TARGET", heartbeat_interval=0)

    def test_execution_report_validation(self):
        with pytest.raises(ValueError):
            FixExecutionReport(order_id="ord1", exec_type="Z", ord_status="Z")


class TestFixMessageBuilder:
    def test_new_order_single_market(self):
        builder = FixMessageBuilder(_config())
        message = builder.build_new_order_single(_request())
        tags = message.to_tag_dict()
        assert tags["35"] == "D"
        assert tags["11"] == "ord1"
        assert tags["54"] == "1"
        assert tags["40"] == "1"
        assert tags["49"] == "SENDER"
        assert "35=D" in message.to_readable_string()

    def test_new_order_single_limit(self):
        builder = FixMessageBuilder(_config())
        message = builder.build_new_order_single(_request(order_type="limit", limit_price=1999.5))
        tags = message.to_tag_dict()
        assert tags["40"] == "2"
        assert tags["44"] == 1999.5

    def test_cancel_request(self):
        builder = FixMessageBuilder(_config())
        message = builder.build_cancel_request("ord1", "XAUUSD", "sell")
        tags = message.to_tag_dict()
        assert tags["35"] == "F"
        assert tags["41"] == "ord1"
        assert tags["54"] == "2"


class TestFixExecutionReportMapper:
    def test_from_tag_dict_and_apply_fill(self):
        sm = OrderStateMachine()
        state = sm.create(_request(), "FIX_TEST")
        sm.acknowledge(state)
        sm.accept(state)
        mapper = FixExecutionReportMapper(sm)
        report = mapper.from_tag_dict({"11": "ord1", "150": "2", "39": "2", "32": "10", "31": "2000"})
        mapper.apply(state, report)
        assert state.status == "filled"
        assert state.average_price == 2000.0
        assert mapper.execution_event_type(report) == "filled"

    def test_reject_report(self):
        sm = OrderStateMachine()
        state = sm.create(_request(), "FIX_TEST")
        sm.acknowledge(state)
        mapper = FixExecutionReportMapper(sm)
        report = FixExecutionReport(order_id="ord1", exec_type="8", ord_status="8", text="bad order")
        mapper.apply(state, report)
        assert state.status == "rejected"
        assert state.rejection_reason == "bad order"


class TestFixGatewayAdapter:
    def test_connect_disconnect(self):
        adapter = FixGatewayAdapter(_config())
        assert adapter.is_connected() is False
        assert adapter.connect()["status"] == "connected"
        assert adapter.is_connected() is True
        assert adapter.disconnect()["status"] == "disconnected"

    def test_submit_requires_connection(self):
        adapter = FixGatewayAdapter(_config())
        with pytest.raises(RuntimeError):
            adapter.submit_order(_request())

    def test_submit_order_writes_outbox_and_ack(self):
        writer = FakeWriter()
        adapter = FixGatewayAdapter(_config(), execution_event_writer=writer)
        adapter.connect()
        state = adapter.submit_order(_request())
        assert state.status == "acknowledged"
        assert adapter.outbox()[0]["35"] == "D"
        assert adapter.list_events()[0]["event_type"] == "ack"
        assert writer.events[0]["event_type"] == "ack"

    def test_receive_accept_and_fill_reports(self):
        adapter = FixGatewayAdapter(_config())
        adapter.connect()
        adapter.submit_order(_request())
        accepted = adapter.receive_execution_report({"11": "ord1", "150": "0", "39": "0"})
        assert accepted.status == "accepted"
        filled = adapter.receive_execution_report({"11": "ord1", "150": "2", "39": "2", "32": "10", "31": "2001"})
        assert filled.status == "filled"
        assert filled.average_price == 2001.0
        assert adapter.list_events()[-1]["event_type"] == "filled"

    def test_cancel_builds_cancel_request(self):
        adapter = FixGatewayAdapter(_config())
        adapter.connect()
        adapter.submit_order(_request())
        adapter.receive_execution_report({"11": "ord1", "150": "0", "39": "0"})
        adapter.cancel_order("ord1")
        assert adapter.outbox()[-1]["35"] == "F"
        assert adapter.outbox()[-1]["41"] == "ord1"

    def test_duplicate_and_unknown_orders_rejected(self):
        adapter = FixGatewayAdapter(_config())
        adapter.connect()
        adapter.submit_order(_request())
        with pytest.raises(ValueError):
            adapter.submit_order(_request())
        with pytest.raises(ValueError):
            adapter.cancel_order("missing")
        with pytest.raises(ValueError):
            adapter.receive_execution_report({"11": "missing", "150": "2", "32": "1", "31": "1"})
