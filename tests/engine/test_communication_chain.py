from datetime import datetime, timedelta
from typing import Any, cast

from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.communication_record import CommunicationRecord
from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import CommunicationMessageType, CommunicationPriority, DispatchStatus
from core.contracts.schema_versions import SCHEMA_COMMUNICATION_RECORD
from core.ledger.services.communication_record_writer import CommunicationRecordWriter
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.ledger.stream_names import LEDGER_STREAM_COMMUNICATIONS, stream_jsonl_filename
from core.protocol.schema_versions import SCHEMA_COMMUNICATION_ENVELOPE, SCHEMA_DISPATCH_RESULT


def test_communication_envelope_validates_required_fields():
    envelope = CommunicationEnvelope(
        schema_version=SCHEMA_COMMUNICATION_ENVELOPE,
        message_id="message_001",
        correlation_id="corr_001",
        causation_id="intent_001",
        event_time=datetime(2026, 4, 24, 12, 0, 0),
        producer="decision_engine",
        target="exec_bridge",
        message_type=CommunicationMessageType.DECISION_INTENT,
        priority=CommunicationPriority.HIGH,
        payload={"intent_id": "intent_001"},
        deadline_at=datetime(2026, 4, 24, 12, 0, 5),
        idempotency_key="idem_001",
    )

    assert envelope.message_id == "message_001"
    assert envelope.target == "exec_bridge"


def test_dispatch_result_requires_failure_reason_for_failed_status():
    try:
        DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id="dispatch_001",
            message_id="message_001",
            status=DispatchStatus.FAILED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 1),
            target="exec_bridge",
            adapter_name="stub_adapter",
        )
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "failure_reason" in str(exc)


def test_communication_record_from_dispatch_maps_envelope_and_result():
    envelope = CommunicationEnvelope(
        schema_version=SCHEMA_COMMUNICATION_ENVELOPE,
        message_id="message_001",
        correlation_id="corr_001",
        causation_id="intent_001",
        event_time=datetime(2026, 4, 24, 12, 0, 0),
        producer="decision_engine",
        target="exec_bridge",
        message_type=CommunicationMessageType.DECISION_INTENT,
        priority=CommunicationPriority.NORMAL,
        payload={"intent_id": "intent_001"},
        trace={"hop": 1},
        extensions={"x": "y"},
    )
    result = DispatchResult(
        schema_version=SCHEMA_DISPATCH_RESULT,
        dispatch_id="dispatch_001",
        message_id="message_001",
        status=DispatchStatus.PROTOCOL_VALIDATED,
        recorded_at=datetime(2026, 4, 24, 12, 0, 1),
        target="exec_bridge",
        adapter_name="stub_adapter",
        ack_id="ack_001",
        protocol_metadata={"validated": True},
        trace={"adapter": "stub"},
        extensions={"dispatch_ext": True},
    )

    record = CommunicationRecord.from_dispatch(
        record_id="communication_record_001",
        envelope=envelope,
        dispatch_result=result,
    )

    assert record.message_id == "message_001"
    assert record.channel["producer"] == "decision_engine"
    assert record.dispatch["status"] == DispatchStatus.PROTOCOL_VALIDATED
    assert record.trace["dispatch_trace"]["adapter"] == "stub"


def test_communication_record_writer_persists_jsonl(tmp_path):
    ledger_store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=ledger_store)

    envelope = CommunicationEnvelope(
        schema_version=SCHEMA_COMMUNICATION_ENVELOPE,
        message_id="message_001",
        correlation_id="corr_001",
        causation_id=None,
        event_time=datetime(2026, 4, 24, 12, 0, 0),
        producer="decision_engine",
        target="exec_bridge",
        message_type=CommunicationMessageType.EXECUTION_DISPATCH,
        priority=CommunicationPriority.CRITICAL,
        payload={"order_id": "order_001"},
        deadline_at=datetime(2026, 4, 24, 12, 0, 0) + timedelta(seconds=3),
    )
    result = DispatchResult(
        schema_version=SCHEMA_DISPATCH_RESULT,
        dispatch_id="dispatch_001",
        message_id="message_001",
        status=DispatchStatus.TRANSPORT_DELIVERED,
        recorded_at=datetime(2026, 4, 24, 12, 0, 1),
        target="exec_bridge",
        adapter_name="stub_adapter",
        transport_metadata={"latency_ms": 12},
    )

    record, ledger_path = writer.write_record(envelope, result)
    ledger_path = cast(Any, ledger_path)  # TECH_DEBT-009: write_record 返回 object, 运行时恒为 Path

    assert record.correlation_id == "corr_001"
    assert ledger_path.exists()
    assert ledger_path.name == stream_jsonl_filename("exec_bridge", LEDGER_STREAM_COMMUNICATIONS)
    contents = ledger_path.read_text(encoding="utf-8")
    assert SCHEMA_COMMUNICATION_RECORD in contents
    assert "message_001" in contents
