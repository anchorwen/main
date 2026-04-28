from datetime import datetime, timedelta
import json

from core.contracts.domain.replay_execution_record import ReplayExecutionRecord
from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import CommunicationMessageType, CommunicationPriority, DispatchStatus, ReplayGateDecision
from core.ledger.governance_sources import REPLAY_GOVERNANCE_PROJECTION_SOURCE_REPLAY_RECORD_EXECUTION
from core.ledger.services.communication_inspection_service import CommunicationInspectionService
from core.ledger.services.communication_record_reader import CommunicationRecordReader
from core.ledger.services.communication_record_writer import CommunicationRecordWriter
from core.ledger.services.communication_replay_executor import CommunicationReplayExecutor
from core.ledger.services.communication_replay_gate import CommunicationReplayGate
from core.ledger.services.communication_replay_service import CommunicationReplayService
from core.ledger.services.replay_execution_writer import ReplayExecutionWriter
from core.ledger.stream_names import LEDGER_STREAM_REPLAYS, stream_jsonl_filename
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.protocol.services.communication_dispatcher import CommunicationDispatcher
from core.protocol.services.file_queue_receipt_reader import FileQueueReceiptReader
from core.protocol.services.stub_communication_adapter import StubCommunicationAdapter
from core.protocol.schema_versions import SCHEMA_COMMUNICATION_ENVELOPE, SCHEMA_DISPATCH_RESULT
from tests.engine.shadow_testkit import (
    assert_runtime_summary_matches_governance_contract,
    build_runtime_summary_from_execution_result,
)

STALE_SUMMARY_SOURCE = "stale_summary_source"


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


def build_result(message_id: str, *, status=DispatchStatus.PROTOCOL_VALIDATED, attempts=None, fallback_adapter_name=None, recorded_at=None):
    return DispatchResult(
        schema_version=SCHEMA_DISPATCH_RESULT,
        dispatch_id=f"dispatch_{message_id}",
        message_id=message_id,
        status=status,
        recorded_at=recorded_at or datetime(2026, 4, 24, 12, 0, 1),
        target="exec_bridge",
        adapter_name="stub_adapter",
        fallback_adapter_name=fallback_adapter_name,
        attempts=attempts or [{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        degrade_reason="primary down" if status == DispatchStatus.DEGRADED else None,
        failure_reason="hard failure" if status == DispatchStatus.FAILED else None,
    )


def write_receipt(receipt_dir, *, date_key: str = "2026-04-24", message_id: str, ack_status: str = "acknowledged", received_at: str = "2026-04-24T12:00:03"):
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


def build_correlation_executor_fixture(tmp_path, *, correlation_id: str, message_specs: list[dict]):
    receipt_dir = tmp_path / "receipts"
    writer, replay_service, _, executor = build_services(tmp_path, receipt_dir=receipt_dir)
    envelopes_by_message_id = {}

    for spec in message_specs:
        envelope = build_envelope(spec["message_id"], correlation_id)
        envelopes_by_message_id[spec["message_id"]] = envelope
        writer.write_record(
            envelope,
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

    replay_plan = replay_service.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id=correlation_id,
    )
    result = executor.execute_correlation_replay(
        replay_plan,
        envelopes_by_message_id=envelopes_by_message_id,
    )
    return replay_plan, result, executor


def build_services(tmp_path, receipt_dir=None):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    replay_writer = ReplayExecutionWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir)) if receipt_dir else None
    inspection = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)
    replay_service = CommunicationReplayService(inspection_service=inspection)
    replay_gate = CommunicationReplayGate()
    dispatcher = CommunicationDispatcher(
        adapter=StubCommunicationAdapter(),
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    executor = CommunicationReplayExecutor(
        replay_gate=replay_gate,
        dispatcher=dispatcher,
        replay_execution_writer=replay_writer,
    )
    return writer, replay_service, replay_gate, executor


def test_replay_execution_record_supported_governance_summary_sources():
    assert ReplayExecutionRecord.is_supported_governance_summary_source(
        ReplayExecutionRecord.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS
    )
    assert ReplayExecutionRecord.is_supported_governance_summary_source(
        ReplayExecutionRecord.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE
    )
    assert ReplayExecutionRecord.is_supported_governance_summary_source(
        ReplayExecutionRecord.REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED
    )
    assert not ReplayExecutionRecord.is_supported_governance_summary_source(None)
    assert not ReplayExecutionRecord.is_supported_governance_summary_source(STALE_SUMMARY_SOURCE)


def test_replay_executor_executes_allowed_message_plan(tmp_path):
    writer, replay_service, _, executor = build_services(tmp_path)
    envelope = build_envelope("message_001", "corr_001")
    writer.write_record(envelope, build_result("message_001"))

    replay_plan = replay_service.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_001",
    )
    result = executor.execute_message_replay(replay_plan, envelope)

    assert result["status"] == "executed"
    assert result["gate_decision"]["decision"] == ReplayGateDecision.ALLOW
    assert result["dispatch_result"].status == DispatchStatus.PROTOCOL_VALIDATED
    assert result["replay_trace"]["execution_state"] == "dispatched"
    assert result["replay_record"].scope == "message"
    assert result["replay_record"].gate_decision["governance_summary"] == result["gate_decision"]["governance_summary"]
    assert result["replay_record"].execution["governance_posture"] == "auto_replay"
    assert result["replay_record"].execution["governance_decision"] == ReplayGateDecision.ALLOW
    assert result["replay_record"].execution == ReplayExecutionRecord._build_execution_projection(
        result,
        result["replay_trace"],
        result["gate_decision"]["governance_summary"],
    )
    assert result["replay_record"].execution["execution_mode"] == "full"
    assert result["replay_record"].extensions["governance_summary"] == result["gate_decision"]["governance_summary"]
    assert result["replay_record"].results["dispatch_result"]["message_id"] == "message_001"
    assert result["replay_ledger_path"].name == stream_jsonl_filename("exec_bridge", LEDGER_STREAM_REPLAYS)


def test_replay_executor_blocks_message_plan_under_review(tmp_path):
    writer, replay_service, _, executor = build_services(tmp_path)
    envelope = build_envelope("message_002", "corr_002")
    writer.write_record(
        envelope,
        build_result(
            "message_002",
            status=DispatchStatus.DEGRADED,
            attempts=[
                {"adapter_name": "exec_adapter", "status": "failed", "reason": "primary down"},
                {"adapter_name": "backup_adapter", "status": "degraded", "reason": "fallback_success"},
            ],
            fallback_adapter_name="backup_adapter",
        ),
    )

    replay_plan = replay_service.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_002",
    )
    result = executor.execute_message_replay(replay_plan, envelope)

    assert result["status"] == "blocked"
    assert result["gate_decision"]["decision"] == ReplayGateDecision.REVIEW
    assert result["dispatch_result"] is None
    assert result["blocked_messages"] == [{"message_id": "message_002", "reason": executor.BLOCK_REASON_REVIEW_REQUIRED}]
    assert result["skip_reasons"] == {}
    assert result["block_reasons"] == {
        executor.BLOCK_REASON_REVIEW_REQUIRED: ["message_002"],
    }
    assert result["replay_trace"]["execution_state"] == "not_executed"
    assert result["replay_record"].execution["status"] == "blocked"
    assert result["replay_record"].execution["execution_mode"] == "blocked"
    assert result["replay_record"].execution["governance_posture"] == "review_required"
    assert result["replay_record"].blocked_messages == result["blocked_messages"]
    assert result["replay_record"].extensions["governance_summary"] == result["gate_decision"]["governance_summary"]


def test_replay_executor_blocks_message_plan_under_review_for_rejected_receipt(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay_service, _, executor = build_services(tmp_path, receipt_dir=receipt_dir)
    envelope = build_envelope("message_rejected", "corr_rejected")
    writer.write_record(
        envelope,
        build_result("message_rejected", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_rejected", ack_status="rejected")

    replay_plan = replay_service.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_rejected",
    )
    result = executor.execute_message_replay(replay_plan, envelope)

    assert result["status"] == "blocked"
    assert result["gate_decision"]["decision"] == ReplayGateDecision.REVIEW
    assert result["blocked_messages"] == [{"message_id": "message_rejected", "reason": executor.BLOCK_REASON_REJECTED_RECEIPT}]


def test_replay_executor_blocks_message_plan_under_review_for_cancelled_receipt(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay_service, _, executor = build_services(tmp_path, receipt_dir=receipt_dir)
    envelope = build_envelope("message_cancelled", "corr_cancelled")
    writer.write_record(
        envelope,
        build_result("message_cancelled", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_cancelled", ack_status="cancelled")

    replay_plan = replay_service.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_cancelled",
    )
    result = executor.execute_message_replay(replay_plan, envelope)

    assert result["status"] == "blocked"
    assert result["gate_decision"]["decision"] == ReplayGateDecision.REVIEW
    assert result["blocked_messages"] == [{"message_id": "message_cancelled", "reason": executor.BLOCK_REASON_CANCELLED_RECEIPT}]


def test_replay_executor_denies_missing_message_plan_with_empty_block_entries(tmp_path):
    _, _, _, executor = build_services(tmp_path)
    envelope = build_envelope("message_missing", "corr_missing")

    result = executor.execute_message_replay(None, envelope)

    assert result["status"] == "blocked"
    assert result["gate_decision"]["decision"] == ReplayGateDecision.DENY
    assert result["gate_decision"]["reasons"] == ["missing_replay_plan"]
    assert result["blocked_messages"] == []
    assert result["dispatch_result"] is None
    assert result["replay_trace"] == {
        "scope": "message",
        "message_id": None,
        "correlation_id": None,
        "execution_state": "not_executed",
    }
    assert result["replay_record"].scope == "message"
    assert result["replay_record"].source_message_id is None
    assert result["replay_record"].source_correlation_id is None
    assert result["replay_record"].execution["status"] == "blocked"
    assert result["replay_record"].execution["execution_mode"] == "blocked"
    assert result["replay_record"].execution["governance_posture"] == "blocked"
    assert result["replay_record"].execution["governance_decision"] == ReplayGateDecision.DENY
    assert result["replay_record"].blocked_messages == []
    assert result["replay_record"].extensions["governance_summary"] == result["gate_decision"]["governance_summary"]


def test_replay_executor_executes_allowed_correlation_plan(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay_service, _, executor = build_services(tmp_path, receipt_dir=receipt_dir)
    envelope_1 = build_envelope("message_101", "corr_shared")
    envelope_2 = build_envelope("message_102", "corr_shared")
    writer.write_record(
        envelope_1,
        build_result(
            "message_101",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
        ),
    )
    writer.write_record(
        envelope_2,
        build_result("message_102", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_101")

    replay_plan = replay_service.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_shared",
    )

    assert replay_plan["target_message_ids"] == ["message_101", "message_102"]
    assert replay_plan["recommended_strategy"] == "replay_correlation_direct"

    result = executor.execute_correlation_replay(
        replay_plan,
        envelopes_by_message_id={
            "message_101": envelope_1,
            "message_102": envelope_2,
        },
    )

    assert result["status"] == "executed"
    assert result["gate_decision"]["decision"] == ReplayGateDecision.ALLOW
    assert len(result["results"]) == 2
    assert [item["message_id"] for item in result["results"]] == ["message_101", "message_102"]
    assert result["skipped_messages"] == []
    assert result["replay_trace"]["message_count"] == 2
    assert result["replay_trace"]["target_message_ids"] == ["message_101", "message_102"]
    assert result["replay_trace"]["skipped_message_ids"] == []
    assert result["replay_record"].scope == "correlation"
    assert result["replay_record"].execution["governance_posture"] == "auto_replay"
    assert result["replay_record"].execution["execution_mode"] == "full"
    assert result["replay_record"].skipped_messages == []
    assert result["replay_record"].blocked_messages == []
    assert result["replay_record"].extensions["governance_summary"] == result["gate_decision"]["governance_summary"]


def test_replay_executor_executes_only_timed_out_messages_when_next_day_receipt_is_found(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay_service, _, executor = build_services(tmp_path, receipt_dir=receipt_dir)
    envelope_1 = build_envelope("message_301", "corr_timeout")
    envelope_2 = build_envelope("message_302", "corr_timeout")
    writer.write_record(
        envelope_1,
        build_result(
            "message_301",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
        ),
    )
    writer.write_record(
        envelope_2,
        build_result(
            "message_302",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 2),
        ),
    )
    write_receipt(receipt_dir, date_key="2026-04-25", message_id="message_302")

    replay_plan = replay_service.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_timeout",
    )

    assert replay_plan["message_ids"] == ["message_301", "message_302"]
    assert replay_plan["delivery_summary"]["timed_out_message_ids"] == ["message_301"]
    assert replay_plan["delivery_summary"]["acknowledged_message_ids"] == ["message_302"]
    assert replay_plan["target_message_ids"] == ["message_301"]
    assert replay_plan["recommended_strategy"] == "replay_only_timed_out_messages"

    result = executor.execute_correlation_replay(
        replay_plan,
        envelopes_by_message_id={
            "message_301": envelope_1,
            "message_302": envelope_2,
        },
    )

    assert result["status"] == "executed"
    assert result["gate_decision"]["decision"] == ReplayGateDecision.ALLOW
    assert result["gate_decision"]["reasons"] == ["targeted_timeout_replay_candidate"]
    assert result["gate_decision"]["governance_summary"]["posture"] == "targeted_replay"
    assert result["gate_decision"]["governance_summary"]["recommended_strategy"] == "replay_only_timed_out_messages"
    assert len(result["results"]) == 1
    assert [item["message_id"] for item in result["results"]] == ["message_301"]
    assert result["skipped_messages"] == [
        {"message_id": "message_302", "reason": executor.SKIP_REASON_ACKNOWLEDGED}
    ]
    assert result["skip_reasons"] == {
        executor.SKIP_REASON_ACKNOWLEDGED: ["message_302"],
    }
    assert result["block_reasons"] == {}
    assert result["replay_trace"]["message_count"] == 1
    assert result["replay_trace"]["target_message_ids"] == ["message_301"]
    assert result["replay_trace"]["skipped_message_ids"] == ["message_302"]
    assert result["replay_record"].scope == "correlation"
    assert result["replay_record"].execution["governance_posture"] == "targeted_replay"
    assert result["replay_record"].execution["governance_decision"] == ReplayGateDecision.ALLOW
    assert result["replay_record"].execution["execution_mode"] == "targeted"
    assert result["replay_record"].results["results"][0]["message_id"] == "message_301"
    assert result["replay_record"].results["results"][0]["dispatch_result"]["message_id"] == "message_301"
    assert result["replay_record"].skipped_messages == result["skipped_messages"]
    assert result["replay_record"].blocked_messages == []
    assert result["replay_record"].extensions["governance_summary"] == result["gate_decision"]["governance_summary"]


def test_replay_executor_blocks_terminal_message_receipt_with_terminal_block_reason(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay_service, _, executor = build_services(tmp_path, receipt_dir=receipt_dir)
    envelope = build_envelope("message_filled", "corr_filled")
    writer.write_record(
        envelope,
        build_result("message_filled", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_filled", ack_status="filled")

    replay_plan = replay_service.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_filled",
    )
    result = executor.execute_message_replay(replay_plan, envelope)

    assert result["status"] == "blocked"
    assert result["gate_decision"]["decision"] == ReplayGateDecision.DENY
    assert result["blocked_messages"] == [{"message_id": "message_filled", "reason": executor.BLOCK_REASON_TERMINAL_RECEIPT}]
    assert result["replay_record"].execution["governance_posture"] == "blocked"
    assert result["replay_record"].execution["execution_mode"] == "blocked"
    assert result["replay_record"].blocked_messages == result["blocked_messages"]


def test_replay_executor_runtime_summary_projection_blocks_terminal_message_receipt(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay_service, _, executor = build_services(tmp_path, receipt_dir=receipt_dir)
    envelope = build_envelope("message_filled", "corr_filled")
    writer.write_record(
        envelope,
        build_result("message_filled", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_filled", ack_status="filled")

    replay_plan = replay_service.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_filled",
    )
    result = executor.execute_message_replay(replay_plan, envelope)

    summary = build_runtime_summary_from_execution_result(result)

    assert_runtime_summary_matches_governance_contract(
        summary,
        posture="blocked",
        governance_decision=ReplayGateDecision.DENY,
        governance_posture="blocked",
        recommended_strategy="do_not_replay_terminal_receipt",
        target_issue_codes=["receipt_filled"],
        review_issue_codes=[],
        governance_tags=["replay_not_required", "terminal_receipt"],
        execution_projection_source=REPLAY_GOVERNANCE_PROJECTION_SOURCE_REPLAY_RECORD_EXECUTION,
        execution_mode="blocked",
        executed_message_ids=[],
        skipped_message_ids=[],
        blocked_message_ids=["message_filled"],
        skip_reasons={},
        block_reasons={"block_terminal_receipt": ["message_filled"]},
    )



def test_replay_executor_runtime_summary_projection_blocks_terminal_partially_filled_message_receipt(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay_service, _, executor = build_services(tmp_path, receipt_dir=receipt_dir)
    envelope = build_envelope("message_partial", "corr_partial")
    writer.write_record(
        envelope,
        build_result("message_partial", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_partial", ack_status="partially_filled")

    replay_plan = replay_service.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_partial",
    )
    result = executor.execute_message_replay(replay_plan, envelope)

    summary = build_runtime_summary_from_execution_result(result)

    assert result["gate_decision"]["governance_summary"]["posture"] == "blocked"
    assert_runtime_summary_matches_governance_contract(
        summary,
        posture="blocked",
        governance_decision=ReplayGateDecision.DENY,
        governance_posture="blocked",
        recommended_strategy="do_not_replay_terminal_receipt",
        target_issue_codes=["receipt_partially_filled"],
        review_issue_codes=["receipt_partially_filled"],
        governance_tags=["replay_not_required", "terminal_receipt"],
        execution_projection_source=REPLAY_GOVERNANCE_PROJECTION_SOURCE_REPLAY_RECORD_EXECUTION,
        execution_mode="blocked",
        executed_message_ids=[],
        skipped_message_ids=[],
        blocked_message_ids=["message_partial"],
        skip_reasons={},
        block_reasons={"block_terminal_receipt": ["message_partial"]},
    )


def test_replay_executor_runtime_summary_projection_uses_replay_record_execution_for_targeted_mode(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay_service, _, executor = build_services(tmp_path, receipt_dir=receipt_dir)
    envelope_1 = build_envelope("message_301", "corr_timeout")
    envelope_2 = build_envelope("message_302", "corr_timeout")
    writer.write_record(
        envelope_1,
        build_result(
            "message_301",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
        ),
    )
    writer.write_record(
        envelope_2,
        build_result(
            "message_302",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 2),
        ),
    )
    write_receipt(receipt_dir, date_key="2026-04-25", message_id="message_302")

    replay_plan = replay_service.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_timeout",
    )
    result = executor.execute_correlation_replay(
        replay_plan,
        envelopes_by_message_id={
            "message_301": envelope_1,
            "message_302": envelope_2,
        },
    )

    summary = build_runtime_summary_from_execution_result(result)

    assert_runtime_summary_matches_governance_contract(
        summary,
        posture="targeted_replay",
        governance_decision=ReplayGateDecision.ALLOW,
        governance_posture="targeted_replay",
        recommended_strategy="replay_only_timed_out_messages",
        target_issue_codes=["receipt_timeout"],
        review_issue_codes=[],
        governance_tags=["auto_replay_eligible", "timeout_targeted_replay"],
        execution_projection_source=REPLAY_GOVERNANCE_PROJECTION_SOURCE_REPLAY_RECORD_EXECUTION,
        execution_mode="targeted",
        executed_message_ids=["message_301"],
        skipped_message_ids=["message_302"],
        blocked_message_ids=[],
        skip_reasons={"skip_acknowledged_message": ["message_302"]},
        block_reasons={},
    )


def test_replay_executor_runtime_summary_prefers_executor_aggregated_reason_maps():
    execution_result = {
        "gate_decision": {
            "governance_summary": {
                "decision": ReplayGateDecision.REVIEW,
                "posture": "review_required",
                "recommended_strategy": "replay_with_governance_review",
                "target_issue_codes": ["receipt_timeout"],
                "review_issue_codes": ["attempt_history_requires_review"],
                "governance_tags": ["requires_governance_review"],
            }
        },
        "results": [],
        "skipped_messages": [{"message_id": "message_from_entries", "reason": "skip_not_targeted"}],
        "blocked_messages": [{"message_id": "message_from_entries", "reason": "block_review_required"}],
        "skip_reasons": {"skip_from_executor": ["message_from_summary"]},
        "block_reasons": {"block_from_executor": ["message_from_summary"]},
        "replay_trace": {"message_id": "message_trace"},
        "dispatch_result": None,
    }

    summary = build_runtime_summary_from_execution_result(execution_result)

    assert summary["skip_reasons"] == {"skip_from_executor": ["message_from_summary"]}
    assert summary["block_reasons"] == {"block_from_executor": ["message_from_summary"]}


def test_replay_executor_priority_contract_matrix(tmp_path):
    cases = [
        {
            "correlation_id": "corr_exec_timeout_only",
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
            "target_message_ids": ["message_timeout"],
            "status": "executed",
            "decision": ReplayGateDecision.ALLOW,
            "blocked_messages": [],
            "skipped_messages": [
                {"message_id": "message_acked", "reason": CommunicationReplayExecutor.SKIP_REASON_ACKNOWLEDGED},
            ],
            "blocked_message_ids": [],
            "skipped_message_ids": ["message_acked"],
        },
        {
            "correlation_id": "corr_exec_rejected_over_timeout",
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
            "target_message_ids": ["message_rejected"],
            "status": "blocked",
            "decision": ReplayGateDecision.REVIEW,
            "blocked_messages": [
                {"message_id": "message_rejected", "reason": CommunicationReplayExecutor.BLOCK_REASON_REJECTED_RECEIPT},
            ],
            "skipped_messages": [
                {"message_id": "message_timeout", "reason": CommunicationReplayExecutor.SKIP_REASON_NOT_TARGETED},
            ],
            "blocked_message_ids": ["message_rejected"],
            "skipped_message_ids": ["message_timeout"],
        },
        {
            "correlation_id": "corr_exec_cancelled_over_timeout",
            "message_specs": [
                {
                    "message_id": "message_cancelled",
                    "receipt": {"ack_status": "cancelled"},
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
            "recommended_strategy": "review_cancelled_receipts_before_replay",
            "target_issue_codes": ["receipt_cancelled"],
            "target_message_ids": ["message_cancelled"],
            "status": "blocked",
            "decision": ReplayGateDecision.REVIEW,
            "blocked_messages": [
                {"message_id": "message_cancelled", "reason": CommunicationReplayExecutor.BLOCK_REASON_CANCELLED_RECEIPT},
            ],
            "skipped_messages": [
                {"message_id": "message_timeout", "reason": CommunicationReplayExecutor.SKIP_REASON_NOT_TARGETED},
                {"message_id": "message_acked", "reason": CommunicationReplayExecutor.SKIP_REASON_ACKNOWLEDGED},
            ],
            "blocked_message_ids": ["message_cancelled"],
            "skipped_message_ids": ["message_timeout", "message_acked"],
        },
        {
            "correlation_id": "corr_exec_terminal_accepted_over_timeout",
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
            "target_message_ids": ["message_timeout"],
            "terminal_message_ids": ["message_accepted"],
            "status": "blocked",
            "decision": ReplayGateDecision.DENY,
            "blocked_messages": [
                {"message_id": "message_timeout", "reason": CommunicationReplayExecutor.BLOCK_REASON_TERMINAL_RECEIPT},
            ],
            "skipped_messages": [
                {"message_id": "message_accepted", "reason": CommunicationReplayExecutor.SKIP_REASON_NOT_TARGETED},
                {"message_id": "message_acked", "reason": CommunicationReplayExecutor.SKIP_REASON_ACKNOWLEDGED},
            ],
            "blocked_message_ids": ["message_timeout"],
            "skipped_message_ids": ["message_accepted", "message_acked"],
        },
        {
            "correlation_id": "corr_exec_terminal_partial_over_timeout",
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
            "target_message_ids": ["message_timeout"],
            "terminal_message_ids": ["message_partial"],
            "status": "blocked",
            "decision": ReplayGateDecision.DENY,
            "blocked_messages": [
                {"message_id": "message_timeout", "reason": CommunicationReplayExecutor.BLOCK_REASON_TERMINAL_RECEIPT},
            ],
            "skipped_messages": [
                {"message_id": "message_partial", "reason": CommunicationReplayExecutor.SKIP_REASON_NOT_TARGETED},
            ],
            "blocked_message_ids": ["message_timeout"],
            "skipped_message_ids": ["message_partial"],
        },
        {
            "correlation_id": "corr_exec_terminal_filled_over_timeout",
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
            "target_message_ids": ["message_timeout"],
            "terminal_message_ids": ["message_filled"],
            "status": "blocked",
            "decision": ReplayGateDecision.DENY,
            "blocked_messages": [
                {"message_id": "message_timeout", "reason": CommunicationReplayExecutor.BLOCK_REASON_TERMINAL_RECEIPT},
            ],
            "skipped_messages": [
                {"message_id": "message_filled", "reason": CommunicationReplayExecutor.SKIP_REASON_NOT_TARGETED},
            ],
            "blocked_message_ids": ["message_timeout"],
            "skipped_message_ids": ["message_filled"],
        },
        {
            "correlation_id": "corr_exec_stale_over_rejected_timeout",
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
            "target_message_ids": ["message_stale"],
            "status": "blocked",
            "decision": ReplayGateDecision.REVIEW,
            "blocked_messages": [
                {"message_id": "message_stale", "reason": CommunicationReplayExecutor.BLOCK_REASON_STALE_RECEIPT},
            ],
            "skipped_messages": [
                {"message_id": "message_rejected", "reason": CommunicationReplayExecutor.SKIP_REASON_NOT_TARGETED},
                {"message_id": "message_timeout", "reason": CommunicationReplayExecutor.SKIP_REASON_NOT_TARGETED},
            ],
            "blocked_message_ids": ["message_stale"],
            "skipped_message_ids": ["message_rejected", "message_timeout"],
        },
    ]

    for case in cases:
        plan, result, _ = build_correlation_executor_fixture(
            tmp_path,
            correlation_id=case["correlation_id"],
            message_specs=case["message_specs"],
        )

        assert plan["recommended_strategy"] == case["recommended_strategy"]
        assert plan["target_issue_codes"] == case["target_issue_codes"]
        assert plan["target_message_ids"] == case["target_message_ids"]
        if "terminal_message_ids" in case:
            issue_message_ids = plan["delivery_summary"]["issue_message_ids"]
            assert sorted(
                issue_message_ids.get("receipt_accepted", [])
                + issue_message_ids.get("receipt_partially_filled", [])
                + issue_message_ids.get("receipt_filled", [])
            ) == case["terminal_message_ids"]
        assert result["status"] == case["status"]
        assert result["gate_decision"]["decision"] == case["decision"]
        assert result["blocked_messages"] == case["blocked_messages"]
        assert result["skipped_messages"] == case["skipped_messages"]
        assert result["replay_trace"]["blocked_message_ids"] == case["blocked_message_ids"]
        assert result["replay_trace"]["skipped_message_ids"] == case["skipped_message_ids"]



def test_replay_executor_blocks_terminal_correlation_receipts_with_terminal_block_reason(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay_service, _, executor = build_services(tmp_path, receipt_dir=receipt_dir)
    envelope = build_envelope("message_terminal", "corr_terminal")
    writer.write_record(
        envelope,
        build_result("message_terminal", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_terminal", ack_status="accepted")

    replay_plan = replay_service.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_terminal",
    )
    result = executor.execute_correlation_replay(
        replay_plan,
        envelopes_by_message_id={
            "message_terminal": envelope,
        },
    )

    assert result["status"] == "blocked"
    assert result["gate_decision"]["decision"] == ReplayGateDecision.DENY
    assert result["blocked_messages"] == [{"message_id": "message_terminal", "reason": executor.BLOCK_REASON_TERMINAL_RECEIPT}]
    assert result["replay_record"].execution["governance_posture"] == "blocked"
    assert result["replay_record"].execution["execution_mode"] == "blocked"
    assert result["replay_record"].blocked_messages == result["blocked_messages"]


def test_replay_executor_blocks_correlation_plan_under_review(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay_service, _, executor = build_services(tmp_path, receipt_dir=receipt_dir)
    envelope_1 = build_envelope("message_201", "corr_review")
    writer.write_record(
        envelope_1,
        build_result(
            "message_201",
            status=DispatchStatus.DEGRADED,
            attempts=[
                {"adapter_name": "exec_adapter", "status": "failed", "reason": "primary down"},
                {"adapter_name": "backup_adapter", "status": "degraded", "reason": "fallback_success"},
            ],
        ),
    )

    replay_plan = replay_service.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_review",
    )

    assert replay_plan["recommended_strategy"] == "replay_correlation_with_sequenced_review"
    assert replay_plan["review_issue_codes"] == ["attempt_history_requires_review"]

    result = executor.execute_correlation_replay(
        replay_plan,
        envelopes_by_message_id={
            "message_201": envelope_1,
        },
    )

    assert result["status"] == "blocked"
    assert result["gate_decision"]["decision"] == ReplayGateDecision.REVIEW
    assert result["results"] == []
    assert result["blocked_messages"] == [{"message_id": "message_201", "reason": executor.BLOCK_REASON_REVIEW_REQUIRED}]
    assert result["replay_trace"]["execution_state"] == "not_executed"
    assert result["replay_trace"]["blocked_message_ids"] == ["message_201"]
    assert result["replay_record"].source_correlation_id == "corr_review"
    assert result["replay_record"].execution["governance_posture"] == "review_required"
    assert result["replay_record"].execution["execution_mode"] == "blocked"
    assert result["replay_record"].blocked_messages == result["blocked_messages"]
    assert result["replay_record"].extensions["governance_summary"] == result["gate_decision"]["governance_summary"]


def test_replay_executor_blocks_correlation_plan_with_mixed_receipt_states(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay_service, _, executor = build_services(tmp_path, receipt_dir=receipt_dir)
    envelope_rejected = build_envelope("message_rejected", "corr_mixed")
    envelope_timeout = build_envelope("message_timeout", "corr_mixed")
    envelope_acked = build_envelope("message_acked", "corr_mixed")

    writer.write_record(
        envelope_rejected,
        build_result("message_rejected", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    writer.write_record(
        envelope_timeout,
        build_result(
            "message_timeout",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
        ),
    )
    writer.write_record(
        envelope_acked,
        build_result("message_acked", status=DispatchStatus.TRANSPORT_DELIVERED),
    )

    write_receipt(receipt_dir, message_id="message_rejected", ack_status="rejected")
    write_receipt(receipt_dir, message_id="message_acked", ack_status="acknowledged")

    replay_plan = replay_service.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_mixed",
    )
    result = executor.execute_correlation_replay(
        replay_plan,
        envelopes_by_message_id={
            "message_rejected": envelope_rejected,
            "message_timeout": envelope_timeout,
            "message_acked": envelope_acked,
        },
    )

    assert replay_plan["recommended_strategy"] == "review_rejected_receipts_before_replay"
    assert replay_plan["target_message_ids"] == ["message_rejected"]
    assert replay_plan["target_issue_codes"] == ["receipt_rejected"]
    assert result["status"] == "blocked"
    assert result["gate_decision"]["decision"] == ReplayGateDecision.REVIEW
    assert result["blocked_messages"] == [{"message_id": "message_rejected", "reason": executor.BLOCK_REASON_REJECTED_RECEIPT}]
    assert result["skipped_messages"] == [
        {"message_id": "message_timeout", "reason": executor.SKIP_REASON_NOT_TARGETED},
        {"message_id": "message_acked", "reason": executor.SKIP_REASON_ACKNOWLEDGED},
    ]
    assert result["skip_reasons"] == {
        executor.SKIP_REASON_NOT_TARGETED: ["message_timeout"],
        executor.SKIP_REASON_ACKNOWLEDGED: ["message_acked"],
    }
    assert result["block_reasons"] == {
        executor.BLOCK_REASON_REJECTED_RECEIPT: ["message_rejected"],
    }
    assert result["replay_trace"]["blocked_message_ids"] == ["message_rejected"]
    assert result["replay_trace"]["skipped_message_ids"] == ["message_timeout", "message_acked"]



def test_replay_executor_blocks_correlation_plan_with_stale_priority_over_rejected_and_timeout(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay_service, _, executor = build_services(tmp_path, receipt_dir=receipt_dir)
    envelope_stale = build_envelope("message_stale", "corr_priority")
    envelope_rejected = build_envelope("message_rejected", "corr_priority")
    envelope_timeout = build_envelope("message_timeout", "corr_priority")

    writer.write_record(
        envelope_stale,
        build_result("message_stale", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    writer.write_record(
        envelope_rejected,
        build_result("message_rejected", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    writer.write_record(
        envelope_timeout,
        build_result(
            "message_timeout",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
        ),
    )

    write_receipt(receipt_dir, date_key="2026-04-25", message_id="message_stale", received_at="2026-04-24T12:00:11")
    write_receipt(receipt_dir, message_id="message_rejected", ack_status="rejected")

    replay_plan = replay_service.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_priority",
    )
    result = executor.execute_correlation_replay(
        replay_plan,
        envelopes_by_message_id={
            "message_stale": envelope_stale,
            "message_rejected": envelope_rejected,
            "message_timeout": envelope_timeout,
        },
    )

    assert replay_plan["recommended_strategy"] == "review_stale_receipts_before_replay"
    assert replay_plan["target_message_ids"] == ["message_stale"]
    assert replay_plan["target_issue_codes"] == ["stale_receipt"]
    assert result["status"] == "blocked"
    assert result["gate_decision"]["decision"] == ReplayGateDecision.REVIEW
    assert result["blocked_messages"] == [{"message_id": "message_stale", "reason": executor.BLOCK_REASON_STALE_RECEIPT}]
    assert result["skipped_messages"] == [
        {"message_id": "message_rejected", "reason": executor.SKIP_REASON_NOT_TARGETED},
        {"message_id": "message_timeout", "reason": executor.SKIP_REASON_NOT_TARGETED},
    ]
    assert result["replay_trace"]["blocked_message_ids"] == ["message_stale"]
    assert result["replay_trace"]["skipped_message_ids"] == ["message_rejected", "message_timeout"]



def test_replay_executor_blocks_correlation_plan_with_cancelled_priority_over_timeout(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay_service, _, executor = build_services(tmp_path, receipt_dir=receipt_dir)
    envelope_cancelled = build_envelope("message_cancelled", "corr_cancel_mix")
    envelope_timeout = build_envelope("message_timeout", "corr_cancel_mix")
    envelope_acked = build_envelope("message_acked", "corr_cancel_mix")

    writer.write_record(
        envelope_cancelled,
        build_result("message_cancelled", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    writer.write_record(
        envelope_timeout,
        build_result(
            "message_timeout",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
        ),
    )
    writer.write_record(
        envelope_acked,
        build_result("message_acked", status=DispatchStatus.TRANSPORT_DELIVERED),
    )

    write_receipt(receipt_dir, message_id="message_cancelled", ack_status="cancelled")
    write_receipt(receipt_dir, message_id="message_acked", ack_status="acknowledged")

    replay_plan = replay_service.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_cancel_mix",
    )
    result = executor.execute_correlation_replay(
        replay_plan,
        envelopes_by_message_id={
            "message_cancelled": envelope_cancelled,
            "message_timeout": envelope_timeout,
            "message_acked": envelope_acked,
        },
    )

    assert replay_plan["recommended_strategy"] == "review_cancelled_receipts_before_replay"
    assert replay_plan["target_message_ids"] == ["message_cancelled"]
    assert replay_plan["target_issue_codes"] == ["receipt_cancelled"]
    assert result["status"] == "blocked"
    assert result["gate_decision"]["decision"] == ReplayGateDecision.REVIEW
    assert result["blocked_messages"] == [{"message_id": "message_cancelled", "reason": executor.BLOCK_REASON_CANCELLED_RECEIPT}]
    assert result["skipped_messages"] == [
        {"message_id": "message_timeout", "reason": executor.SKIP_REASON_NOT_TARGETED},
        {"message_id": "message_acked", "reason": executor.SKIP_REASON_ACKNOWLEDGED},
    ]
    assert result["replay_trace"]["blocked_message_ids"] == ["message_cancelled"]
    assert result["replay_trace"]["skipped_message_ids"] == ["message_timeout", "message_acked"]
