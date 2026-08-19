import json
from datetime import datetime, timedelta
from typing import Any, cast

from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import CommunicationMessageType, CommunicationPriority, DispatchStatus
from core.ledger.services.communication_inspection_service import CommunicationInspectionService
from core.ledger.services.communication_record_reader import CommunicationRecordReader
from core.ledger.services.communication_record_writer import CommunicationRecordWriter
from core.ledger.services.communication_replay_service import CommunicationReplayService
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.protocol.schema_versions import SCHEMA_COMMUNICATION_ENVELOPE, SCHEMA_DISPATCH_RESULT
from core.protocol.services.file_queue_receipt_reader import FileQueueReceiptReader
from tests.engine.shadow_testkit import (
    assert_runtime_summary_matches_governance_contract,
    build_runtime_summary_from_execution_result,
)


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
        deadline_at=datetime(2026, 4, 24, 12, 0, 0) + timedelta(seconds=10),
    )


def build_result(
    message_id: str,
    *,
    status=DispatchStatus.PROTOCOL_VALIDATED,
    attempts=None,
    fallback_adapter_name=None,
    recorded_at=None,
):
    return DispatchResult(
        schema_version=SCHEMA_DISPATCH_RESULT,
        dispatch_id=f"dispatch_{message_id}",
        message_id=message_id,
        status=status,
        recorded_at=recorded_at or datetime(2026, 4, 24, 12, 0, 1),
        target="exec_bridge",
        adapter_name="stub_adapter",
        fallback_adapter_name=fallback_adapter_name,
        attempts=attempts
        or [{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        degrade_reason="primary down" if status == DispatchStatus.DEGRADED else None,
        failure_reason="hard failure" if status == DispatchStatus.FAILED else None,
    )


def write_receipt(
    receipt_dir,
    *,
    date_key: str = "2026-04-24",
    message_id: str,
    ack_status: str = "acknowledged",
    received_at: str = "2026-04-24T12:00:03",
):
    receipt_path = receipt_dir / date_key / "exec_bridge"
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


def build_correlation_priority_fixture(tmp_path, *, correlation_id: str, message_specs: list[dict]):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    inspection = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    for spec in message_specs:
        writer.write_record(
            build_envelope(spec["message_id"], correlation_id),
            build_result(
                spec["message_id"],
                status=DispatchStatus.TRANSPORT_DELIVERED,
                recorded_at=spec.get("recorded_at", datetime(2026, 4, 24, 12, 0, 1)),
            ),
        )
        receipt = spec.get("receipt")
        if receipt is not None:
            write_receipt(
                receipt_dir,
                date_key=receipt.get("date_key", "2026-04-24"),
                message_id=spec["message_id"],
                ack_status=receipt.get("ack_status", "acknowledged"),
                received_at=receipt.get("received_at", "2026-04-24T12:00:03"),
            )

    return replay.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id=correlation_id,
    )


def test_communication_replay_service_builds_message_replay_plan(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    inspection = CommunicationInspectionService(record_reader=reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(build_envelope("message_001", "corr_001"), build_result("message_001"))

    plan = replay.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_001",
    )

    assert plan is not None
    assert plan["scope"] == "message"
    assert plan["message_id"] == "message_001"
    assert plan["delivery_state"]["phase"] == "dispatch_recorded"
    assert plan["delivery_state"]["delivery_posture"] == "action_required"
    assert plan["issue_code"] == "dispatch_pending"
    assert plan["target_issue_codes"] == ["dispatch_pending"]
    assert plan["review_issue_codes"] == []
    assert plan["recommended_strategy"] == "direct_replay_candidate"
    assert "dispatch.dispatch_id" in plan["non_reusable_fields"]


def test_communication_replay_service_recommends_review_for_degraded_message(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    inspection = CommunicationInspectionService(record_reader=reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(
        build_envelope("message_002", "corr_002"),
        build_result(
            "message_002",
            status=DispatchStatus.DEGRADED,
            attempts=[
                {"adapter_name": "exec_adapter", "status": "failed", "reason": "primary down"},
                {
                    "adapter_name": "backup_adapter",
                    "status": "degraded",
                    "reason": "fallback_success",
                },
            ],
            fallback_adapter_name="backup_adapter",
        ),
    )

    plan = replay.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_002",
    )

    assert plan is not None
    assert plan["recommended_strategy"] == "replay_with_governance_review"
    assert plan["review_issue_codes"] == ["attempt_history_requires_review"]
    assert plan["attempt_summary"]["failed_count"] == 1
    assert plan["attempt_summary"]["degraded_count"] == 1


def test_communication_replay_service_message_plan_exposes_canonical_governance_summary(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    inspection = CommunicationInspectionService(record_reader=reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(build_envelope("message_001", "corr_001"), build_result("message_001"))

    plan = replay.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_001",
    )

    assert plan is not None
    assert plan["governance_summary"] == {
        "decision": None,
        "posture": "unknown",
        "recommended_strategy": "direct_replay_candidate",
        "target_issue_codes": ["dispatch_pending"],
        "review_issue_codes": [],
        "governance_tags": [],
    }


def test_communication_replay_service_builds_correlation_replay_plan(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    inspection = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(
        build_envelope("message_101", "corr_shared"),
        build_result("message_101", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    writer.write_record(
        build_envelope("message_102", "corr_shared"),
        build_result(
            "message_102",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
        ),
    )
    write_receipt(receipt_dir, message_id="message_101")

    plan = replay.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_shared",
    )

    assert plan["scope"] == "correlation"
    assert plan["correlation_id"] == "corr_shared"
    assert plan["message_count"] == 2
    assert plan["message_ids"] == ["message_101", "message_102"]
    assert len(plan["message_plans"]) == 2
    assert plan["issue_counts"] == {"clean": 1, "receipt_timeout": 1}
    assert plan["target_issue_codes"] == ["receipt_timeout"]
    assert plan["review_issue_codes"] == []
    assert plan["delivery_summary"]["timed_out_message_ids"] == ["message_102"]
    assert plan["target_message_ids"] == ["message_102"]
    assert plan["recommended_strategy"] == "replay_only_timed_out_messages"


def test_communication_replay_service_correlation_plan_exposes_canonical_governance_summary(
    tmp_path,
):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    inspection = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(
        build_envelope("message_101", "corr_shared"),
        build_result("message_101", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    writer.write_record(
        build_envelope("message_102", "corr_shared"),
        build_result(
            "message_102",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
        ),
    )
    write_receipt(receipt_dir, message_id="message_101")

    plan = replay.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_shared",
    )

    assert plan["governance_summary"] == {
        "decision": None,
        "posture": "targeted_replay",
        "recommended_strategy": "replay_only_timed_out_messages",
        "target_issue_codes": ["receipt_timeout"],
        "review_issue_codes": [],
        "governance_tags": [],
    }


def test_communication_replay_service_returns_none_for_missing_message(tmp_path):
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    inspection = CommunicationInspectionService(record_reader=reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    assert (
        replay.build_message_replay_plan(
            date_key="2026-04-24",
            target="exec_bridge",
            message_id="missing_message",
        )
        is None
    )


def test_communication_replay_service_supports_next_day_receipt_lookup(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    inspection = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(
        build_envelope("message_stale", "corr_stale"),
        build_result("message_stale", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(
        receipt_dir,
        date_key="2026-04-25",
        message_id="message_stale",
        received_at="2026-04-24T12:00:11",
    )

    plan = replay.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_stale",
    )

    assert plan is not None
    assert plan["delivery_state"]["phase"] == "stale_receipt"
    assert plan["issue_code"] == "stale_receipt"
    assert plan["target_issue_codes"] == ["stale_receipt"]
    assert plan["review_issue_codes"] == ["stale_receipt"]


def test_communication_replay_service_marks_rejected_receipt_for_review(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    inspection = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(
        build_envelope("message_rejected", "corr_rejected"),
        build_result("message_rejected", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_rejected", ack_status="rejected")

    plan = replay.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_rejected",
    )

    assert plan is not None
    assert plan["issue_code"] == "receipt_rejected"
    assert plan["recommended_strategy"] == "review_rejected_receipt_before_replay"
    assert plan["target_issue_codes"] == ["receipt_rejected"]
    assert plan["review_issue_codes"] == ["receipt_rejected"]


def test_communication_replay_service_does_not_replay_terminal_filled_receipt(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    inspection = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(
        build_envelope("message_filled", "corr_filled"),
        build_result("message_filled", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_filled", ack_status="filled")

    plan = replay.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_filled",
    )

    assert plan is not None
    assert plan["issue_code"] == "receipt_filled"
    assert plan["recommended_strategy"] == "do_not_replay_terminal_receipt"
    assert plan["target_issue_codes"] == ["receipt_filled"]
    assert plan["review_issue_codes"] == []
    assert plan["governance_summary"] == {
        "decision": None,
        "posture": "healthy",
        "recommended_strategy": "do_not_replay_terminal_receipt",
        "target_issue_codes": ["receipt_filled"],
        "review_issue_codes": [],
        "governance_tags": [],
    }


def test_communication_replay_service_marks_terminal_partially_filled_receipt_as_healthy_review(
    tmp_path,
):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    inspection = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(
        build_envelope("message_partial", "corr_partial"),
        build_result("message_partial", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_partial", ack_status="partially_filled")

    plan = replay.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_partial",
    )

    assert plan is not None
    assert plan["issue_code"] == "receipt_partially_filled"
    assert plan["recommended_strategy"] == "do_not_replay_terminal_receipt"
    assert plan["target_issue_codes"] == ["receipt_partially_filled"]
    assert plan["review_issue_codes"] == ["receipt_partially_filled"]
    assert plan["governance_summary"] == {
        "decision": None,
        "posture": "healthy",
        "recommended_strategy": "do_not_replay_terminal_receipt",
        "target_issue_codes": ["receipt_partially_filled"],
        "review_issue_codes": ["receipt_partially_filled"],
        "governance_tags": [],
    }


def test_communication_replay_service_does_not_replay_terminal_accepted_receipt(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    inspection = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(
        build_envelope("message_accepted", "corr_accepted"),
        build_result("message_accepted", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_accepted", ack_status="accepted")

    plan = replay.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_accepted",
    )

    assert plan is not None
    assert plan["issue_code"] == "receipt_accepted"
    assert plan["recommended_strategy"] == "do_not_replay_terminal_receipt"
    assert plan["target_issue_codes"] == ["receipt_accepted"]
    assert plan["review_issue_codes"] == []
    assert plan["governance_summary"] == {
        "decision": None,
        "posture": "healthy",
        "recommended_strategy": "do_not_replay_terminal_receipt",
        "target_issue_codes": ["receipt_accepted"],
        "review_issue_codes": [],
        "governance_tags": [],
    }


def test_communication_replay_service_marks_cancelled_receipt_correlation_for_review(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    inspection = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(
        build_envelope("message_cancelled", "corr_cancelled"),
        build_result("message_cancelled", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_cancelled", ack_status="cancelled")

    plan = replay.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_cancelled",
    )

    assert plan["recommended_strategy"] == "review_cancelled_receipts_before_replay"
    assert plan["target_issue_codes"] == ["receipt_cancelled"]
    assert plan["review_issue_codes"] == ["receipt_cancelled"]
    assert plan["target_message_ids"] == ["message_cancelled"]


def test_communication_replay_service_prefers_review_driven_targets_over_timeout_targets(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    inspection = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(
        build_envelope("message_rejected", "corr_mixed"),
        build_result("message_rejected", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    writer.write_record(
        build_envelope("message_timeout", "corr_mixed"),
        build_result(
            "message_timeout",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
        ),
    )
    writer.write_record(
        build_envelope("message_acked", "corr_mixed"),
        build_result("message_acked", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_rejected", ack_status="rejected")
    write_receipt(receipt_dir, message_id="message_acked", ack_status="acknowledged")

    plan = replay.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_mixed",
    )

    assert plan["recommended_strategy"] == "review_rejected_receipts_before_replay"
    assert plan["target_issue_codes"] == ["receipt_rejected"]
    assert plan["review_issue_codes"] == ["receipt_rejected"]
    assert plan["target_message_ids"] == ["message_rejected"]


def test_communication_replay_service_prefers_stale_targets_over_rejected_and_timeout(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    inspection = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(
        build_envelope("message_stale", "corr_priority"),
        build_result("message_stale", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    writer.write_record(
        build_envelope("message_rejected", "corr_priority"),
        build_result("message_rejected", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    writer.write_record(
        build_envelope("message_timeout", "corr_priority"),
        build_result(
            "message_timeout",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
        ),
    )

    write_receipt(
        receipt_dir,
        date_key="2026-04-25",
        message_id="message_stale",
        received_at="2026-04-24T12:00:11",
    )
    write_receipt(receipt_dir, message_id="message_rejected", ack_status="rejected")

    plan = replay.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_priority",
    )

    assert plan["recommended_strategy"] == "review_stale_receipts_before_replay"
    assert plan["target_issue_codes"] == ["stale_receipt"]
    assert plan["review_issue_codes"] == ["stale_receipt", "receipt_rejected"]
    assert plan["target_message_ids"] == ["message_stale"]


def test_communication_replay_service_prefers_cancelled_targets_over_timeout(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    inspection = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(
        build_envelope("message_cancelled", "corr_cancel_mix"),
        build_result("message_cancelled", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    writer.write_record(
        build_envelope("message_timeout", "corr_cancel_mix"),
        build_result(
            "message_timeout",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
        ),
    )
    writer.write_record(
        build_envelope("message_acked", "corr_cancel_mix"),
        build_result("message_acked", status=DispatchStatus.TRANSPORT_DELIVERED),
    )

    write_receipt(receipt_dir, message_id="message_cancelled", ack_status="cancelled")
    write_receipt(receipt_dir, message_id="message_acked", ack_status="acknowledged")

    plan = replay.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_cancel_mix",
    )

    assert plan["recommended_strategy"] == "review_cancelled_receipts_before_replay"
    assert plan["target_issue_codes"] == ["receipt_cancelled"]
    assert plan["review_issue_codes"] == ["receipt_cancelled"]
    assert plan["target_message_ids"] == ["message_cancelled"]


def test_communication_replay_service_blocks_terminal_correlation_receipts(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    inspection = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(
        build_envelope("message_terminal", "corr_terminal"),
        build_result("message_terminal", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_terminal", ack_status="accepted")

    plan = replay.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_terminal",
    )

    assert plan["recommended_strategy"] == "do_not_replay_terminal_receipts"
    assert plan["target_issue_codes"] == ["receipt_accepted"]
    assert plan["review_issue_codes"] == []
    assert plan["governance_summary"] == {
        "decision": None,
        "posture": "healthy",
        "recommended_strategy": "do_not_replay_terminal_receipts",
        "target_issue_codes": ["receipt_accepted"],
        "review_issue_codes": [],
        "governance_tags": [],
    }


def test_communication_replay_service_priority_contract_matrix(tmp_path):
    cases = [
        {
            "correlation_id": "corr_priority_timeout_only",
            "message_specs": [
                {
                    "message_id": "message_timeout",
                    "recorded_at": datetime(2026, 4, 24, 12, 0, 20),
                },
                {
                    "message_id": "message_acked",
                    "receipt": {"ack_status": "acknowledged"},
                },
            ],
            "recommended_strategy": "replay_only_timed_out_messages",
            "target_issue_codes": ["receipt_timeout"],
            "review_issue_codes": [],
            "target_message_ids": ["message_timeout"],
        },
        {
            "correlation_id": "corr_priority_rejected_over_timeout",
            "message_specs": [
                {
                    "message_id": "message_rejected",
                    "receipt": {"ack_status": "rejected"},
                },
                {
                    "message_id": "message_timeout",
                    "recorded_at": datetime(2026, 4, 24, 12, 0, 20),
                },
            ],
            "recommended_strategy": "review_rejected_receipts_before_replay",
            "target_issue_codes": ["receipt_rejected"],
            "review_issue_codes": ["receipt_rejected"],
            "target_message_ids": ["message_rejected"],
        },
        {
            "correlation_id": "corr_priority_cancelled_over_timeout",
            "message_specs": [
                {
                    "message_id": "message_cancelled",
                    "receipt": {"ack_status": "cancelled"},
                },
                {
                    "message_id": "message_timeout",
                    "recorded_at": datetime(2026, 4, 24, 12, 0, 20),
                },
            ],
            "recommended_strategy": "review_cancelled_receipts_before_replay",
            "target_issue_codes": ["receipt_cancelled"],
            "review_issue_codes": ["receipt_cancelled"],
            "target_message_ids": ["message_cancelled"],
        },
        {
            "correlation_id": "corr_priority_terminal_accepted_over_timeout",
            "message_specs": [
                {
                    "message_id": "message_accepted",
                    "receipt": {"ack_status": "accepted"},
                },
                {
                    "message_id": "message_timeout",
                    "recorded_at": datetime(2026, 4, 24, 12, 0, 20),
                },
                {
                    "message_id": "message_acked",
                    "receipt": {"ack_status": "acknowledged"},
                },
            ],
            "recommended_strategy": "do_not_replay_terminal_receipts",
            "target_issue_codes": ["receipt_timeout"],
            "review_issue_codes": [],
            "target_message_ids": ["message_timeout"],
            "terminal_message_ids": ["message_accepted"],
        },
        {
            "correlation_id": "corr_priority_terminal_partial_over_timeout",
            "message_specs": [
                {
                    "message_id": "message_partial",
                    "receipt": {"ack_status": "partially_filled"},
                },
                {
                    "message_id": "message_timeout",
                    "recorded_at": datetime(2026, 4, 24, 12, 0, 20),
                },
            ],
            "recommended_strategy": "do_not_replay_terminal_receipts",
            "target_issue_codes": ["receipt_timeout"],
            "review_issue_codes": ["receipt_partially_filled"],
            "target_message_ids": ["message_timeout"],
            "terminal_message_ids": ["message_partial"],
        },
        {
            "correlation_id": "corr_priority_terminal_filled_over_timeout",
            "message_specs": [
                {
                    "message_id": "message_filled",
                    "receipt": {"ack_status": "filled"},
                },
                {
                    "message_id": "message_timeout",
                    "recorded_at": datetime(2026, 4, 24, 12, 0, 20),
                },
            ],
            "recommended_strategy": "do_not_replay_terminal_receipts",
            "target_issue_codes": ["receipt_timeout"],
            "review_issue_codes": [],
            "target_message_ids": ["message_timeout"],
            "terminal_message_ids": ["message_filled"],
        },
        {
            "correlation_id": "corr_priority_stale_over_rejected_timeout",
            "message_specs": [
                {
                    "message_id": "message_stale",
                    "receipt": {
                        "date_key": "2026-04-25",
                        "received_at": "2026-04-24T12:00:11",
                    },
                },
                {
                    "message_id": "message_rejected",
                    "receipt": {"ack_status": "rejected"},
                },
                {
                    "message_id": "message_timeout",
                    "recorded_at": datetime(2026, 4, 24, 12, 0, 20),
                },
            ],
            "recommended_strategy": "review_stale_receipts_before_replay",
            "target_issue_codes": ["stale_receipt"],
            "review_issue_codes": ["stale_receipt", "receipt_rejected"],
            "target_message_ids": ["message_stale"],
        },
    ]

    for case in cases:
        plan = build_correlation_priority_fixture(
            tmp_path,
            correlation_id=str(case["correlation_id"]),
            message_specs=cast(list[dict[str, Any]], case["message_specs"]),
        )

        assert plan["recommended_strategy"] == case["recommended_strategy"]
        assert plan["target_issue_codes"] == case["target_issue_codes"]
        assert plan["review_issue_codes"] == case["review_issue_codes"]
        assert plan["target_message_ids"] == case["target_message_ids"]
        if "terminal_message_ids" in case:
            issue_message_ids = plan["delivery_summary"]["issue_message_ids"]
            assert (
                sorted(
                    issue_message_ids.get("receipt_accepted", [])
                    + issue_message_ids.get("receipt_partially_filled", [])
                    + issue_message_ids.get("receipt_filled", [])
                )
                == case["terminal_message_ids"]
            )


def test_communication_replay_service_runtime_summary_projection_matches_message_plan_contract(
    tmp_path,
):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    inspection = CommunicationInspectionService(record_reader=reader)
    replay = CommunicationReplayService(inspection_service=inspection)

    writer.write_record(build_envelope("message_001", "corr_001"), build_result("message_001"))

    plan = replay.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_001",
    )
    assert (
        plan is not None
    )  # TECH_DEBT-009: build_message_replay_plan 返回 dict|None, 消息已写入契约下恒非 None

    execution_result = {
        "status": "executed",
        "gate_decision": {
            "governance_summary": {
                "decision": "allow",
                "posture": "auto_replay",
                "recommended_strategy": plan["recommended_strategy"],
                "target_issue_codes": plan["target_issue_codes"],
                "review_issue_codes": plan["review_issue_codes"],
                "tags": ["auto_replay_eligible"],
            },
        },
        "dispatch_result": build_result("message_001"),
        "results": [{"message_id": "message_001", "dispatch_result": build_result("message_001")}],
        "skipped_messages": [],
        "blocked_messages": [],
        "replay_trace": {
            "scope": "message",
            "message_id": plan["message_id"],
            "correlation_id": plan["correlation_id"],
            "execution_state": "dispatched",
        },
    }

    summary = build_runtime_summary_from_execution_result(execution_result)

    assert summary["message_id"] == plan["message_id"]
    assert_runtime_summary_matches_governance_contract(
        summary,
        posture="auto_replay",
        governance_decision="allow",
        governance_posture="auto_replay",
        recommended_strategy=plan["recommended_strategy"],
        target_issue_codes=plan["target_issue_codes"],
        review_issue_codes=plan["review_issue_codes"],
        governance_tags=["auto_replay_eligible"],
        execution_projection_source=None,
        execution_mode=None,
        executed_message_ids=["message_001"],
        skipped_message_ids=[],
        blocked_message_ids=[],
        skip_reasons={},
        block_reasons={},
    )
