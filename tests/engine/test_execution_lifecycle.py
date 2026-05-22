from datetime import UTC, datetime

import pytest

from core.contracts.domain.execution_event import ExecutionEvent
from core.contracts.ids import new_execution_event_id
from core.ledger.schema_versions import SCHEMA_EXECUTION_EVENT
from core.ledger.services.execution_event_reader import ExecutionEventReader
from core.ledger.services.execution_event_writer import ExecutionEventWriter
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore


def make_event(
    message_id, correlation_id, event_type, filled_qty=0, event_time=None, venue="test_venue"
):
    event_time = event_time or datetime(2026, 4, 24, 12, 0, 5)
    qty = {"filled": filled_qty} if filled_qty else {}
    return ExecutionEvent(
        schema_version=SCHEMA_EXECUTION_EVENT,
        event_id=new_execution_event_id(),
        message_id=message_id,
        correlation_id=correlation_id,
        event_type=event_type,
        event_time=event_time,
        recorded_at=event_time,
        venue=venue,
        quantity=qty,
    )


class TestExecutionEventDomain:
    def test_valid_event_types(self):
        for et in [
            "ack",
            "rejected",
            "accepted",
            "partially_filled",
            "filled",
            "cancelled",
            "amended",
            "expired",
        ]:
            event = make_event("m1", "c1", et)
            assert event.event_type == et

    def test_invalid_event_type_raises(self):
        with pytest.raises(ValueError, match="event_type must be one of"):
            make_event("m1", "c1", "unknown_type")

    def test_missing_event_id_raises(self):
        with pytest.raises(ValueError, match="event_id is required"):
            ExecutionEvent(
                schema_version="v1",
                event_id="",
                message_id="m1",
                correlation_id="c1",
                event_type="ack",
                event_time=datetime.now(UTC).replace(tzinfo=None),
                recorded_at=datetime.now(UTC).replace(tzinfo=None),
                venue="v",
            )

    def test_is_terminal(self):
        assert make_event("m1", "c1", "filled").is_terminal is True
        assert make_event("m1", "c1", "rejected").is_terminal is True
        assert make_event("m1", "c1", "cancelled").is_terminal is True
        assert make_event("m1", "c1", "expired").is_terminal is True
        assert make_event("m1", "c1", "ack").is_terminal is False
        assert make_event("m1", "c1", "accepted").is_terminal is False

    def test_is_fill(self):
        assert make_event("m1", "c1", "filled").is_fill is True
        assert make_event("m1", "c1", "partially_filled").is_fill is True
        assert make_event("m1", "c1", "ack").is_fill is False


class TestExecutionEventWriterReader:
    def test_write_and_read_events(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        writer = ExecutionEventWriter(store)
        writer.write_event(make_event("m1", "c1", "ack"))
        writer.write_event(make_event("m1", "c1", "filled", filled_qty=100))

        reader = ExecutionEventReader(str(tmp_path))
        events = reader.list_events(date_key="2026-04-24", correlation_id="c1")
        assert len(events) == 2
        assert events[0]["event_type"] == "ack"
        assert events[1]["event_type"] == "filled"

    def test_find_by_message_id(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        writer = ExecutionEventWriter(store)
        writer.write_event(make_event("m1", "c1", "ack"))
        writer.write_event(make_event("m2", "c1", "ack"))
        writer.write_event(make_event("m1", "c1", "filled", filled_qty=100))

        reader = ExecutionEventReader(str(tmp_path))
        events = reader.find_by_message_id(
            date_key="2026-04-24", correlation_id="c1", message_id="m1"
        )
        assert len(events) == 2

    def test_find_terminal_event(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        writer = ExecutionEventWriter(store)
        writer.write_event(make_event("m1", "c1", "ack"))
        writer.write_event(make_event("m1", "c1", "accepted"))
        writer.write_event(make_event("m1", "c1", "filled", filled_qty=100))

        reader = ExecutionEventReader(str(tmp_path))
        terminal = reader.find_terminal_event(
            date_key="2026-04-24", correlation_id="c1", message_id="m1"
        )
        assert terminal is not None
        assert terminal["event_type"] == "filled"

    def test_find_terminal_returns_none_when_no_terminal(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        writer = ExecutionEventWriter(store)
        writer.write_event(make_event("m1", "c1", "ack"))

        reader = ExecutionEventReader(str(tmp_path))
        assert (
            reader.find_terminal_event(date_key="2026-04-24", correlation_id="c1", message_id="m1")
            is None
        )

    def test_build_execution_timeline(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        writer = ExecutionEventWriter(store)
        writer.write_event(make_event("m1", "c1", "ack"))
        writer.write_event(make_event("m1", "c1", "accepted"))
        writer.write_event(make_event("m1", "c1", "partially_filled", filled_qty=50))
        writer.write_event(make_event("m1", "c1", "filled", filled_qty=50))

        reader = ExecutionEventReader(str(tmp_path))
        timeline = reader.build_execution_timeline(
            date_key="2026-04-24", correlation_id="c1", message_id="m1"
        )

        assert timeline["event_count"] == 4
        assert timeline["event_types"] == ["ack", "accepted", "partially_filled", "filled"]
        assert timeline["total_filled_quantity"] == 100
        assert timeline["terminal_event_type"] == "filled"
        assert timeline["is_terminal"] is True

    def test_empty_timeline(self, tmp_path):
        reader = ExecutionEventReader(str(tmp_path))
        timeline = reader.build_execution_timeline(
            date_key="2026-04-24", correlation_id="c1", message_id="m1"
        )
        assert timeline["event_count"] == 0
        assert timeline["is_terminal"] is False

    def test_write_from_venue_payload(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        writer = ExecutionEventWriter(store)
        event, path = writer.write_from_venue_payload(
            message_id="m1",
            correlation_id="c1",
            event_type="filled",
            venue="exchange_a",
            event_time=datetime(2026, 4, 24, 12, 5, 0),
            venue_order_id="ord_123",
            quantity={"filled": 100},
        )
        assert event.event_type == "filled"
        assert event.venue_order_id == "ord_123"
        assert path.exists()
