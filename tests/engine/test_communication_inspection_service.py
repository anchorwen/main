from datetime import datetime, timedelta
import json

from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import CommunicationMessageType, CommunicationPriority, DispatchStatus
from core.ledger.services.communication_inspection_service import CommunicationInspectionService
from core.ledger.services.communication_record_reader import CommunicationRecordReader
from core.ledger.services.communication_record_writer import CommunicationRecordWriter
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.protocol.services.file_queue_receipt_reader import FileQueueReceiptReader
from core.protocol.schema_versions import SCHEMA_COMMUNICATION_ENVELOPE, SCHEMA_DISPATCH_RESULT


def build_envelope(message_id: str, correlation_id: str, target: str = "exec_bridge"):
    return CommunicationEnvelope(
        schema_version=SCHEMA_COMMUNICATION_ENVELOPE,
        message_id=message_id,
        correlation_id=correlation_id,
        causation_id=None,
        event_time=datetime(2026, 4, 24, 12, 0, 0),
        producer="decision_engine",
        target=target,
        message_type=CommunicationMessageType.DECISION_INTENT,
        priority=CommunicationPriority.NORMAL,
        payload={"intent_id": message_id},
        deadline_at=datetime(2026, 4, 24, 12, 0, 0) + timedelta(seconds=5),
    )


def build_result(message_id: str, *, status=DispatchStatus.PROTOCOL_VALIDATED, attempts=None, fallback_adapter_name=None):
    return DispatchResult(
        schema_version=SCHEMA_DISPATCH_RESULT,
        dispatch_id=f"dispatch_{message_id}",
        message_id=message_id,
        status=status,
        recorded_at=datetime(2026, 4, 24, 12, 0, 1),
        target="exec_bridge",
        adapter_name="stub_adapter",
        fallback_adapter_name=fallback_adapter_name,
        attempts=attempts or [{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        degrade_reason="primary down" if status == DispatchStatus.DEGRADED else None,
    )


def write_receipt(receipt_dir, *, message_id: str, ack_status: str = "acknowledged", received_at: str = "2026-04-24T12:00:03"):
    receipt_path = receipt_dir / "2026-04-24" / "exec_bridge"
    receipt_path.mkdir(parents=True, exist_ok=True)
    target_file = receipt_path / f"{message_id}.ack.json"
    target_file.write_text(
        json.dumps(
            {
                "message_id": message_id,
                "ack_status": ack_status,
                "received_at": received_at,
            }
        ),
        encoding="utf-8",
    )
    return target_file


def test_communication_inspection_service_returns_message_trace(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    service = CommunicationInspectionService(record_reader=reader)

    writer.write_record(build_envelope("message_001", "corr_001"), build_result("message_001"))

    trace = service.get_message_trace(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_001",
    )

    assert trace is not None
    assert trace["message_id"] == "message_001"
    assert trace["correlation_id"] == "corr_001"
    assert trace["attempt_summary"]["attempt_count"] == 1
    assert trace["attempt_summary"]["adapter_sequence"] == ["stub_adapter"]
    assert trace["delivery_state"]["phase"] == "dispatch_recorded"
    assert trace["delivery_state"]["issue_code"] == "dispatch_pending"
    assert trace["delivery_state"]["receipt_present"] is False
    assert trace["delivery_state"]["deadline_missed"] is False


def test_communication_inspection_service_returns_correlation_trace(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    service = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)

    writer.write_record(build_envelope("message_001", "corr_shared"), build_result("message_001", status=DispatchStatus.TRANSPORT_DELIVERED))
    writer.write_record(build_envelope("message_002", "corr_shared"), build_result("message_002", status=DispatchStatus.TRANSPORT_DELIVERED))
    writer.write_record(
        build_envelope("message_003", "corr_shared"),
        DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id="dispatch_message_003",
            message_id="message_003",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 10),
            target="exec_bridge",
            adapter_name="stub_adapter",
            attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        ),
    )
    write_receipt(receipt_dir, message_id="message_001", ack_status="acknowledged")
    write_receipt(receipt_dir, message_id="message_002", ack_status="acknowledged", received_at="2026-04-24T12:00:06")

    trace = service.get_correlation_trace(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_shared",
    )

    assert trace["correlation_id"] == "corr_shared"
    assert trace["message_count"] == 3
    assert trace["message_ids"] == ["message_001", "message_002", "message_003"]
    assert trace["final_statuses"] == [
        DispatchStatus.TRANSPORT_DELIVERED,
        DispatchStatus.TRANSPORT_DELIVERED,
        DispatchStatus.TRANSPORT_DELIVERED,
    ]
    assert trace["delivery_summary"]["phase_counts"] == {
        "receipt_acknowledged": 1,
        "stale_receipt": 1,
        "receipt_timeout": 1,
    }
    assert trace["delivery_summary"]["issue_counts"] == {
        "clean": 1,
        "stale_receipt": 1,
        "receipt_timeout": 1,
    }
    assert trace["delivery_summary"]["issue_message_ids"] == {
        "clean": ["message_001"],
        "stale_receipt": ["message_002"],
        "receipt_timeout": ["message_003"],
    }
    assert trace["delivery_summary"]["acknowledged_message_ids"] == ["message_001"]
    assert trace["delivery_summary"]["stale_receipt_message_ids"] == ["message_002"]
    assert trace["delivery_summary"]["timed_out_message_ids"] == ["message_003"]
    assert trace["delivery_summary"]["waiting_message_ids"] == []
    assert [item["message_id"] for item in trace["message_traces"]] == ["message_001", "message_002", "message_003"]


def test_communication_inspection_service_summarizes_attempts(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    service = CommunicationInspectionService(record_reader=reader)

    writer.write_record(build_envelope("message_003", "corr_003"), build_result("message_003", status=DispatchStatus.DEGRADED, attempts=[
        {"adapter_name": "exec_adapter", "status": "failed", "reason": "primary down"},
        {"adapter_name": "backup_adapter", "status": "degraded", "reason": "fallback_success"},
    ], fallback_adapter_name="backup_adapter"))

    record = reader.find_by_message_id(date_key="2026-04-24", target="exec_bridge", message_id="message_003")
    summary = service.summarize_attempts(record)

    assert summary["attempt_count"] == 2
    assert summary["failed_count"] == 1
    assert summary["degraded_count"] == 1
    assert summary["succeeded_count"] == 0
    assert summary["adapter_sequence"] == ["exec_adapter", "backup_adapter"]


def test_communication_inspection_service_returns_none_for_missing_message(tmp_path):
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    service = CommunicationInspectionService(record_reader=reader)

    assert service.get_message_trace(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="missing_message",
    ) is None


def test_communication_inspection_service_includes_receipt_aware_delivery_state(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    service = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)

    writer.write_record(
        build_envelope("message_ack", "corr_ack"),
        build_result("message_ack", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_ack", ack_status="acknowledged")

    trace = service.get_message_trace(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_ack",
    )

    assert trace is not None
    assert trace["receipt"] is not None
    assert trace["receipt"]["ack_status"] == "acknowledged"
    assert trace["delivery_state"]["phase"] == "receipt_acknowledged"
    assert trace["delivery_state"]["dispatch_status"] == DispatchStatus.TRANSPORT_DELIVERED
    assert trace["delivery_state"]["issue_code"] == "clean"
    assert trace["delivery_state"]["receipt_present"] is True
    assert trace["delivery_state"]["receipt_status"] == "acknowledged"
    assert trace["delivery_state"]["receipt_is_stale"] is False


def test_communication_inspection_service_marks_waiting_receipt_before_deadline(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    service = CommunicationInspectionService(record_reader=reader)

    writer.write_record(
        build_envelope("message_wait", "corr_wait"),
        build_result("message_wait", status=DispatchStatus.TRANSPORT_DELIVERED),
    )

    trace = service.get_message_trace(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_wait",
    )

    assert trace is not None
    assert trace["delivery_state"]["phase"] == "waiting_receipt"
    assert trace["delivery_state"]["issue_code"] == "waiting_receipt"
    assert trace["delivery_state"]["deadline_missed"] is False


def test_communication_inspection_service_marks_receipt_timeout_after_deadline(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    service = CommunicationInspectionService(record_reader=reader)

    writer.write_record(
        build_envelope("message_timeout", "corr_timeout"),
        DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id="dispatch_message_timeout",
            message_id="message_timeout",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 10),
            target="exec_bridge",
            adapter_name="stub_adapter",
            attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        ),
    )

    trace = service.get_message_trace(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_timeout",
    )

    assert trace is not None
    assert trace["delivery_state"]["phase"] == "receipt_timeout"
    assert trace["delivery_state"]["issue_code"] == "receipt_timeout"
    assert trace["delivery_state"]["deadline_missed"] is True




def test_communication_inspection_service_maps_extended_receipt_lifecycle_states(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    service = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)

    cases = [
        ("message_rejected", "rejected", "receipt_rejected", "receipt_rejected", "action_required"),
        ("message_accepted", "accepted", "receipt_accepted", "receipt_accepted", "healthy"),
        ("message_partial", "partially_filled", "receipt_partially_filled", "receipt_partially_filled", "healthy"),
        ("message_filled", "filled", "receipt_filled", "receipt_filled", "healthy"),
        ("message_cancelled", "cancelled", "receipt_cancelled", "receipt_cancelled", "action_required"),
    ]

    for message_id, ack_status, expected_phase, expected_issue_code, expected_posture in cases:
        writer.write_record(
            build_envelope(message_id, f"corr_{message_id}"),
            build_result(message_id, status=DispatchStatus.TRANSPORT_DELIVERED),
        )
        write_receipt(receipt_dir, message_id=message_id, ack_status=ack_status)

        trace = service.get_message_trace(
            date_key="2026-04-24",
            target="exec_bridge",
            message_id=message_id,
        )

        assert trace is not None
        assert trace["delivery_state"]["phase"] == expected_phase
        assert trace["delivery_state"]["issue_code"] == expected_issue_code
        assert trace["delivery_state"]["delivery_posture"] == expected_posture
        assert trace["delivery_state"]["receipt_status"] == ack_status



def test_communication_inspection_service_correlation_trace_summarizes_extended_receipt_lifecycle_states(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    service = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)

    cases = [
        ("message_accepted", "accepted"),
        ("message_filled", "filled"),
        ("message_rejected", "rejected"),
    ]

    for message_id, ack_status in cases:
        writer.write_record(
            build_envelope(message_id, "corr_lifecycle"),
            build_result(message_id, status=DispatchStatus.TRANSPORT_DELIVERED),
        )
        write_receipt(receipt_dir, message_id=message_id, ack_status=ack_status)

    trace = service.get_correlation_trace(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_lifecycle",
    )

    assert trace["delivery_summary"]["phase_counts"] == {
        "receipt_accepted": 1,
        "receipt_filled": 1,
        "receipt_rejected": 1,
    }
    assert trace["delivery_summary"]["issue_counts"] == {
        "receipt_accepted": 1,
        "receipt_filled": 1,
        "receipt_rejected": 1,
    }
    assert trace["delivery_summary"]["issue_message_ids"] == {
        "receipt_accepted": ["message_accepted"],
        "receipt_filled": ["message_filled"],
        "receipt_rejected": ["message_rejected"],
    }
    assert trace["delivery_summary"]["delivery_posture"] == "action_required"

