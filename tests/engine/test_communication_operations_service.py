import json
from datetime import datetime, timedelta

from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import (
    CommunicationMessageType,
    CommunicationPriority,
    DispatchStatus,
    ReplayGateDecision,
)
from core.contracts.schema_versions import SCHEMA_REPLAY_EXECUTION_RECORD
from core.ledger.services.communication_inspection_service import CommunicationInspectionService
from core.ledger.services.communication_operations_service import CommunicationOperationsService
from core.ledger.services.communication_record_reader import CommunicationRecordReader
from core.ledger.services.communication_record_writer import CommunicationRecordWriter
from core.ledger.services.communication_replay_executor import CommunicationReplayExecutor
from core.ledger.services.communication_replay_gate import CommunicationReplayGate
from core.ledger.services.communication_replay_service import CommunicationReplayService
from core.ledger.services.replay_execution_reader import ReplayExecutionReader
from core.ledger.services.replay_execution_writer import ReplayExecutionWriter
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.ledger.stream_names import LEDGER_STREAM_REPLAYS, stream_jsonl_filename
from core.protocol.schema_versions import SCHEMA_COMMUNICATION_ENVELOPE, SCHEMA_DISPATCH_RESULT
from core.protocol.services.communication_dispatcher import CommunicationDispatcher
from core.protocol.services.file_queue_receipt_reader import FileQueueReceiptReader
from core.protocol.services.stub_communication_adapter import StubCommunicationAdapter


def assert_operations_view_stable_contract(
    view: dict,
    *,
    operations_summary: dict,
    summary_source: str | None = None,
    execution_projection_source: str | None = None,
) -> None:
    assert view["operations_summary"] == operations_summary
    assert view["operations_posture"] == operations_summary.get("posture")
    assert view["posture_sources"] == {
        "operations_posture_source": operations_summary.get("posture_source"),
    }
    if summary_source is None and execution_projection_source is None:
        assert "governance_sources" not in view or view["governance_sources"] is None
    else:
        assert view["governance_sources"] == {
            "summary_source": summary_source,
            "execution_projection_source": execution_projection_source,
        }


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
):
    return DispatchResult(
        schema_version=SCHEMA_DISPATCH_RESULT,
        dispatch_id=f"dispatch_{message_id}",
        message_id=message_id,
        status=status,
        recorded_at=datetime(2026, 4, 24, 12, 0, 1),
        target="exec_bridge",
        adapter_name="stub_adapter",
        fallback_adapter_name=fallback_adapter_name,
        attempts=attempts
        or [{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        degrade_reason="primary down" if status == DispatchStatus.DEGRADED else None,
        failure_reason="hard failure" if status == DispatchStatus.FAILED else None,
    )


def build_operations_summary(
    *,
    posture: str,
    posture_source: str | None,
    governance_decision=ReplayGateDecision.ALLOW,
    governance_posture: str = "auto_replay",
    recommended_strategy: str = "direct_replay_candidate",
    target_issue_codes: list[str] | None = None,
    review_issue_codes: list[str] | None = None,
    governance_tags: list[str] | None = None,
    governance_summary_source: str | None = None,
    execution_projection_source: str | None = None,
) -> dict:
    return {
        "posture": posture,
        "posture_source": posture_source,
        "governance_decision": governance_decision,
        "governance_posture": governance_posture,
        "recommended_strategy": recommended_strategy,
        "target_issue_codes": target_issue_codes or [],
        "review_issue_codes": review_issue_codes or [],
        "governance_tags": governance_tags or [],
        "governance_summary_source": governance_summary_source,
        "execution_projection_source": execution_projection_source,
        "reconciliation_status": None,
    }


def build_replay_operations_summary(
    *,
    posture: str,
    governance_decision=ReplayGateDecision.ALLOW,
    governance_posture: str = "auto_replay",
    recommended_strategy: str = "direct_replay_candidate",
    target_issue_codes: list[str] | None = None,
    review_issue_codes: list[str] | None = None,
    governance_tags: list[str] | None = None,
    governance_summary_source: str | None = None,
    execution_projection_source: str | None = None,
    execution_mode: str,
    executed_message_ids: list[str] | None = None,
    skipped_message_ids: list[str] | None = None,
    blocked_message_ids: list[str] | None = None,
    skip_reasons: dict | None = None,
    block_reasons: dict | None = None,
) -> dict:
    return {
        **build_operations_summary(
            posture=posture,
            posture_source="governance_summary.posture",
            governance_decision=governance_decision,
            governance_posture=governance_posture,
            recommended_strategy=recommended_strategy,
            target_issue_codes=target_issue_codes,
            review_issue_codes=review_issue_codes,
            governance_tags=governance_tags,
            governance_summary_source=governance_summary_source,
            execution_projection_source=execution_projection_source,
        ),
        "execution_mode": execution_mode,
        "executed_message_ids": executed_message_ids or [],
        "skipped_message_ids": skipped_message_ids or [],
        "blocked_message_ids": blocked_message_ids or [],
        "skip_reasons": skip_reasons or {},
        "block_reasons": block_reasons or {},
    }


def sample_governance_summary(
    *,
    decision=ReplayGateDecision.ALLOW,
    posture: str = "auto_replay",
    recommended_strategy: str = "direct_replay_candidate",
    target_issue_codes: list[str] | None = None,
    review_issue_codes: list[str] | None = None,
    governance_tags: list[str] | None = None,
) -> dict:
    return {
        "decision": decision,
        "posture": posture,
        "recommended_strategy": recommended_strategy,
        "target_issue_codes": target_issue_codes or [],
        "review_issue_codes": review_issue_codes or [],
        "governance_tags": governance_tags or [],
    }


def build_governance_sources(
    *, summary_source: str | None, execution_projection_source: str | None
) -> dict:
    return {
        "summary_source": summary_source,
        "execution_projection_source": execution_projection_source,
    }


def build_services(tmp_path, receipt_dir=None):
    store = JsonlLedgerStore(str(tmp_path))
    communication_writer = CommunicationRecordWriter(ledger_store=store)
    replay_writer = ReplayExecutionWriter(ledger_store=store)
    communication_reader = CommunicationRecordReader(base_dir=str(tmp_path))
    replay_reader = ReplayExecutionReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir)) if receipt_dir else None
    inspection = CommunicationInspectionService(
        record_reader=communication_reader, receipt_reader=receipt_reader
    )
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
    operations = CommunicationOperationsService(
        communication_reader=communication_reader,
        inspection_service=inspection,
        replay_service=replay_service,
        replay_gate=replay_gate,
        replay_reader=replay_reader,
        receipt_reader=receipt_reader,
    )
    return communication_writer, replay_service, executor, operations


def test_operations_service_returns_message_view(tmp_path):
    communication_writer, _, _, operations = build_services(tmp_path)
    envelope = build_envelope("message_001", "corr_001")
    communication_writer.write_record(envelope, build_result("message_001"))

    view = operations.get_message_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_001",
    )

    assert view["record"]["message_id"] == "message_001"
    assert view["trace"]["message_id"] == "message_001"
    assert view["trace"]["delivery_state"]["phase"] == "dispatch_recorded"
    assert view["trace"]["delivery_state"]["issue_code"] == "dispatch_pending"
    assert view["trace"]["delivery_state"]["delivery_posture"] == "action_required"
    assert_operations_view_stable_contract(
        view,
        operations_summary=build_operations_summary(
            posture="action_required",
            posture_source="trace.delivery_state.delivery_posture",
            target_issue_codes=["dispatch_pending"],
            governance_tags=["auto_replay_eligible"],
        ),
    )
    assert view["replay_plan"]["recommended_strategy"] == "direct_replay_candidate"
    assert view["replay_gate"]["decision"] == ReplayGateDecision.ALLOW
    assert view["replay_gate"]["governance_summary"] == view["governance_summary"]
    assert view["governance_summary"] == sample_governance_summary(
        target_issue_codes=["dispatch_pending"],
        governance_tags=["auto_replay_eligible"],
    )


def test_operations_service_returns_rejected_message_view_with_review_governance(tmp_path):
    receipt_dir = tmp_path / "receipts"
    communication_writer, _, _, operations = build_services(tmp_path, receipt_dir=receipt_dir)
    envelope = build_envelope("message_rejected", "corr_rejected")
    communication_writer.write_record(
        envelope,
        build_result("message_rejected", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    receipt_path = receipt_dir / "2026-04-24" / "exec_bridge"
    receipt_path.mkdir(parents=True, exist_ok=True)
    (receipt_path / "message_rejected.ack.json").write_text(
        json.dumps(
            {
                "message_id": "message_rejected",
                "ack_status": "rejected",
                "received_at": "2026-04-24T12:00:03",
            }
        ),
        encoding="utf-8",
    )

    view = operations.get_message_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_rejected",
    )

    assert view["trace"]["delivery_state"]["phase"] == "receipt_rejected"
    assert view["operations_posture"] == "action_required"
    assert view["replay_plan"]["recommended_strategy"] == "review_rejected_receipt_before_replay"
    assert view["governance_summary"] == {
        "decision": ReplayGateDecision.REVIEW,
        "posture": "review_required",
        "recommended_strategy": "review_rejected_receipt_before_replay",
        "target_issue_codes": ["receipt_rejected"],
        "review_issue_codes": ["receipt_rejected"],
        "governance_tags": ["requires_governance_review", "receipt_rejected"],
    }
    assert_operations_view_stable_contract(
        view,
        operations_summary=build_operations_summary(
            posture="action_required",
            posture_source="trace.delivery_state.delivery_posture",
            governance_decision=ReplayGateDecision.REVIEW,
            governance_posture="review_required",
            recommended_strategy="review_rejected_receipt_before_replay",
            target_issue_codes=["receipt_rejected"],
            review_issue_codes=["receipt_rejected"],
            governance_tags=["requires_governance_review", "receipt_rejected"],
        ),
    )


def test_operations_service_message_view_prefers_plan_governance_summary_when_gate_omits_it():
    class CommunicationReaderStub:
        def find_by_message_id(self, **kwargs):
            return {"message_id": kwargs["message_id"]}

    class InspectionServiceStub:
        def get_message_trace(self, **kwargs):
            return {
                "message_id": kwargs["message_id"],
                "delivery_state": {"delivery_posture": "action_required"},
            }

    class ReplayServiceStub:
        def build_message_replay_plan(self, **kwargs):
            return {
                "recommended_strategy": "direct_replay_candidate",
                "target_issue_codes": ["dispatch_pending"],
                "review_issue_codes": [],
                "governance_summary": {
                    "decision": None,
                    "posture": "unknown",
                    "recommended_strategy": "direct_replay_candidate",
                    "target_issue_codes": ["dispatch_pending"],
                    "review_issue_codes": [],
                    "governance_tags": [],
                },
            }

    class ReplayGateStub:
        def evaluate_message_plan(self, replay_plan):
            return {
                "decision": ReplayGateDecision.ALLOW,
            }

    operations = CommunicationOperationsService(
        communication_reader=CommunicationReaderStub(),
        inspection_service=InspectionServiceStub(),
        replay_service=ReplayServiceStub(),
        replay_gate=ReplayGateStub(),
    )

    view = operations.get_message_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_plan_fallback",
    )

    assert view["governance_summary"] == {
        "decision": None,
        "posture": "unknown",
        "recommended_strategy": "direct_replay_candidate",
        "target_issue_codes": ["dispatch_pending"],
        "review_issue_codes": [],
        "governance_tags": [],
    }
    assert_operations_view_stable_contract(
        view,
        operations_summary=build_operations_summary(
            posture="action_required",
            posture_source="trace.delivery_state.delivery_posture",
            governance_decision=None,
            governance_posture="unknown",
            recommended_strategy="direct_replay_candidate",
            target_issue_codes=["dispatch_pending"],
            review_issue_codes=[],
            governance_tags=[],
        ),
    )


def test_operations_service_returns_correlation_view(tmp_path):
    receipt_dir = tmp_path / "receipts"
    communication_writer, _, _, operations = build_services(tmp_path, receipt_dir=receipt_dir)
    communication_writer.write_record(
        build_envelope("message_101", "corr_shared"),
        build_result("message_101", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    communication_writer.write_record(
        build_envelope("message_102", "corr_shared"),
        DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id="dispatch_message_102",
            message_id="message_102",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
            target="exec_bridge",
            adapter_name="stub_adapter",
            attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        ),
    )
    receipt_path = receipt_dir / "2026-04-24" / "exec_bridge"
    receipt_path.mkdir(parents=True, exist_ok=True)
    (receipt_path / "message_101.ack.json").write_text(
        json.dumps(
            {
                "message_id": "message_101",
                "ack_status": "acknowledged",
                "received_at": "2026-04-24T12:00:03",
            }
        ),
        encoding="utf-8",
    )

    view = operations.get_correlation_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_shared",
    )

    assert view["trace"]["correlation_id"] == "corr_shared"
    assert view["trace"]["delivery_summary"]["phase_counts"] == {
        "receipt_acknowledged": 1,
        "receipt_timeout": 1,
    }
    assert view["trace"]["delivery_summary"]["issue_counts"] == {
        "clean": 1,
        "receipt_timeout": 1,
    }
    assert view["trace"]["delivery_summary"]["issue_message_ids"] == {
        "clean": ["message_101"],
        "receipt_timeout": ["message_102"],
    }
    assert view["trace"]["delivery_summary"]["delivery_posture"] == "action_required"
    assert_operations_view_stable_contract(
        view,
        operations_summary=build_operations_summary(
            posture="action_required",
            posture_source="trace.delivery_summary.delivery_posture",
            governance_posture="targeted_replay",
            recommended_strategy="replay_only_timed_out_messages",
            target_issue_codes=["receipt_timeout"],
            governance_tags=["auto_replay_eligible", "timeout_targeted_replay"],
        ),
    )
    assert view["trace"]["delivery_summary"]["acknowledged_message_ids"] == ["message_101"]
    assert view["trace"]["delivery_summary"]["timed_out_message_ids"] == ["message_102"]
    assert view["replay_plan"]["target_message_ids"] == ["message_102"]
    assert view["replay_plan"]["target_issue_codes"] == ["receipt_timeout"]
    assert view["replay_plan"]["review_issue_codes"] == []
    assert view["replay_plan"]["recommended_strategy"] == "replay_only_timed_out_messages"
    assert view["replay_gate"]["decision"] == ReplayGateDecision.ALLOW
    assert view["replay_gate"]["governance_summary"] == view["governance_summary"]
    assert view["governance_summary"] == sample_governance_summary(
        posture="targeted_replay",
        recommended_strategy="replay_only_timed_out_messages",
        target_issue_codes=["receipt_timeout"],
        governance_tags=["auto_replay_eligible", "timeout_targeted_replay"],
    )


def test_operations_service_correlation_view_prefers_trace_posture_over_targeted_governance_posture():
    class InspectionServiceStub:
        def get_correlation_trace(self, **kwargs):
            return {
                "correlation_id": kwargs["correlation_id"],
                "delivery_summary": {
                    "delivery_posture": "observe",
                },
            }

    class ReplayServiceStub:
        def build_correlation_replay_plan(self, **kwargs):
            return {
                "recommended_strategy": "replay_only_timed_out_messages",
                "target_issue_codes": ["receipt_timeout"],
                "review_issue_codes": [],
                "governance_summary": {
                    "decision": None,
                    "posture": "targeted_replay",
                    "recommended_strategy": "replay_only_timed_out_messages",
                    "target_issue_codes": ["receipt_timeout"],
                    "review_issue_codes": [],
                    "governance_tags": ["timeout_targeted_replay"],
                },
            }

    class ReplayGateStub:
        def evaluate_correlation_plan(self, replay_plan):
            return {
                "decision": ReplayGateDecision.ALLOW,
                "governance_summary": {
                    "decision": ReplayGateDecision.ALLOW,
                    "posture": "targeted_replay",
                    "recommended_strategy": "replay_only_timed_out_messages",
                    "target_issue_codes": ["receipt_timeout"],
                    "review_issue_codes": [],
                    "governance_tags": ["auto_replay_eligible", "timeout_targeted_replay"],
                },
            }

    operations = CommunicationOperationsService(
        communication_reader=None,
        inspection_service=InspectionServiceStub(),
        replay_service=ReplayServiceStub(),
        replay_gate=ReplayGateStub(),
    )

    view = operations.get_correlation_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_trace_first",
    )

    assert view["operations_posture"] == "observe"
    assert view["posture_sources"] == {
        "operations_posture_source": "trace.delivery_summary.delivery_posture",
    }
    assert view["governance_summary"]["posture"] == "targeted_replay"
    assert view["operations_summary"]["posture"] == "observe"
    assert view["operations_summary"]["posture_source"] == "trace.delivery_summary.delivery_posture"
    assert view["operations_summary"]["governance_posture"] == "targeted_replay"


def test_operations_service_returns_terminal_mixed_correlation_view(tmp_path):
    receipt_dir = tmp_path / "receipts"
    communication_writer, _, _, operations = build_services(tmp_path, receipt_dir=receipt_dir)
    receipt_path = receipt_dir / "2026-04-24" / "exec_bridge"
    receipt_path.mkdir(parents=True, exist_ok=True)

    communication_writer.write_record(
        build_envelope("message_accepted", "corr_terminal_mix"),
        DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id="dispatch_message_accepted",
            message_id="message_accepted",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 1),
            target="exec_bridge",
            adapter_name="stub_adapter",
            attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        ),
    )
    communication_writer.write_record(
        build_envelope("message_timeout", "corr_terminal_mix"),
        DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id="dispatch_message_timeout",
            message_id="message_timeout",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
            target="exec_bridge",
            adapter_name="stub_adapter",
            attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        ),
    )
    communication_writer.write_record(
        build_envelope("message_acked", "corr_terminal_mix"),
        DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id="dispatch_message_acked",
            message_id="message_acked",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 1),
            target="exec_bridge",
            adapter_name="stub_adapter",
            attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        ),
    )
    (receipt_path / "message_accepted.ack.json").write_text(
        json.dumps(
            {
                "message_id": "message_accepted",
                "ack_status": "accepted",
                "received_at": "2026-04-24T12:00:03",
            }
        ),
        encoding="utf-8",
    )
    (receipt_path / "message_acked.ack.json").write_text(
        json.dumps(
            {
                "message_id": "message_acked",
                "ack_status": "acknowledged",
                "received_at": "2026-04-24T12:00:03",
            }
        ),
        encoding="utf-8",
    )

    view = operations.get_correlation_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_terminal_mix",
    )

    assert view["trace"]["delivery_summary"]["issue_counts"] == {
        "receipt_accepted": 1,
        "receipt_timeout": 1,
        "clean": 1,
    }
    assert view["trace"]["delivery_summary"]["issue_message_ids"] == {
        "receipt_accepted": ["message_accepted"],
        "receipt_timeout": ["message_timeout"],
        "clean": ["message_acked"],
    }
    assert view["replay_plan"]["recommended_strategy"] == "do_not_replay_terminal_receipts"
    assert view["replay_plan"]["target_issue_codes"] == ["receipt_timeout"]
    assert view["replay_plan"]["review_issue_codes"] == []
    assert view["replay_plan"]["target_message_ids"] == ["message_timeout"]
    assert view["replay_gate"]["decision"] == ReplayGateDecision.DENY
    assert view["governance_summary"] == {
        "decision": ReplayGateDecision.DENY,
        "posture": "blocked",
        "recommended_strategy": "do_not_replay_terminal_receipts",
        "target_issue_codes": ["receipt_timeout"],
        "review_issue_codes": [],
        "governance_tags": ["replay_not_required", "terminal_receipt"],
    }
    assert_operations_view_stable_contract(
        view,
        operations_summary=build_operations_summary(
            posture="action_required",
            posture_source="trace.delivery_summary.delivery_posture",
            governance_decision=ReplayGateDecision.DENY,
            governance_posture="blocked",
            recommended_strategy="do_not_replay_terminal_receipts",
            target_issue_codes=["receipt_timeout"],
            review_issue_codes=[],
            governance_tags=["replay_not_required", "terminal_receipt"],
        ),
    )


def test_operations_service_replay_view_prefers_governance_posture_over_execution_projection(
    tmp_path,
):
    _, _, _, operations = build_services(tmp_path)
    replay_id = "replay_projection_mismatch"
    replay_path = (
        tmp_path / "2026-04-24" / stream_jsonl_filename("exec_bridge", LEDGER_STREAM_REPLAYS)
    )
    replay_path.parent.mkdir(parents=True, exist_ok=True)
    replay_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_REPLAY_EXECUTION_RECORD,
                "replay_id": replay_id,
                "scope": "message",
                "source_message_id": "message_projection_mismatch",
                "source_correlation_id": "corr_projection_mismatch",
                "executed_at": "2026-04-24T12:00:02",
                "gate_decision": {
                    "decision": ReplayGateDecision.ALLOW,
                    "reasons": ["clean_replay_candidate"],
                    "governance_tags": ["auto_replay_eligible"],
                    "governance_summary": {
                        "decision": ReplayGateDecision.ALLOW,
                        "posture": "targeted_replay",
                        "recommended_strategy": "replay_only_timed_out_messages",
                        "target_issue_codes": ["receipt_timeout"],
                        "review_issue_codes": [],
                        "governance_tags": ["timeout_targeted_replay"],
                    },
                },
                "execution": {
                    "status": "executed",
                    "execution_state": "dispatched",
                    "governance_decision": ReplayGateDecision.ALLOW,
                    "governance_posture": "auto_replay",
                },
                "results": {
                    "dispatch_result": None,
                    "results": [],
                },
                "trace": {
                    "scope": "message",
                    "message_id": "message_projection_mismatch",
                    "correlation_id": "corr_projection_mismatch",
                    "execution_state": "dispatched",
                },
                "plan": {
                    "recommended_strategy": "replay_only_timed_out_messages",
                    "target_issue_codes": ["receipt_timeout"],
                    "review_issue_codes": [],
                },
                "extensions": {
                    "governance_summary": {
                        "decision": ReplayGateDecision.ALLOW,
                        "posture": "targeted_replay",
                        "recommended_strategy": "replay_only_timed_out_messages",
                        "target_issue_codes": ["receipt_timeout"],
                        "review_issue_codes": [],
                        "governance_tags": ["timeout_targeted_replay"],
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    view = operations.get_replay_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        replay_id=replay_id,
    )

    assert view is not None
    assert view["governance_summary"]["posture"] == "targeted_replay"
    assert view["execution_governance_projection"] == {
        "decision": ReplayGateDecision.ALLOW,
        "posture": "auto_replay",
    }
    assert view["operations_posture"] == "targeted_replay"
    assert view["posture_sources"] == {
        "operations_posture_source": "governance_summary.posture",
    }
    assert view["operations_summary"]["posture"] == "targeted_replay"
    assert view["operations_summary"]["posture_source"] == "governance_summary.posture"
    assert view["operations_summary"]["governance_posture"] == "targeted_replay"
    assert (
        view["operations_summary"]["execution_projection_source"]
        == CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION
    )

    communication_writer, replay_service, executor, operations = build_services(tmp_path)
    envelope = build_envelope("message_blocked", "corr_blocked")
    communication_writer.write_record(
        envelope,
        build_result(
            "message_blocked",
            status=DispatchStatus.FAILED,
            attempts=[
                {"adapter_name": "stub_adapter", "status": "failed", "reason": "hard failure"}
            ],
        ),
    )

    replay_plan = replay_service.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_blocked",
    )
    execution_result = executor.execute_message_replay(replay_plan, envelope)
    replay_id = execution_result["replay_record"].replay_id

    view = operations.get_replay_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        replay_id=replay_id,
    )

    assert view is not None
    assert view["replay_record"]["replay_id"] == replay_id
    assert view["replay_status"] == "blocked"
    assert view["gate_decision"]["decision"] == ReplayGateDecision.REVIEW
    assert view["execution_governance_projection"] == {
        "decision": ReplayGateDecision.REVIEW,
        "posture": "review_required",
    }
    assert view["governance_summary"] == sample_governance_summary(
        decision=ReplayGateDecision.REVIEW,
        posture="review_required",
        recommended_strategy="replay_with_governance_review",
        target_issue_codes=["dispatch_pending"],
        review_issue_codes=["attempt_history_requires_review"],
        governance_tags=["requires_manual_review", "failed_history"],
    )
    assert_operations_view_stable_contract(
        view,
        operations_summary=build_replay_operations_summary(
            posture="review_required",
            governance_decision=ReplayGateDecision.REVIEW,
            governance_posture="review_required",
            recommended_strategy="replay_with_governance_review",
            target_issue_codes=["dispatch_pending"],
            review_issue_codes=["attempt_history_requires_review"],
            governance_tags=["requires_manual_review", "failed_history"],
            governance_summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
            execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
            execution_mode="blocked",
            blocked_message_ids=["message_blocked"],
            block_reasons={"block_review_required": ["message_blocked"]},
        ),
        summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
        execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
    )
    assert view["execution_summary"] == {
        "targeted_message_ids": ["message_blocked"],
        "executed_message_ids": [],
        "skipped_message_ids": [],
        "blocked_message_ids": ["message_blocked"],
        "skip_reasons": {},
        "block_reasons": {"block_review_required": ["message_blocked"]},
        "execution_mode": "blocked",
    }


def test_operations_service_replay_view_keeps_summary_sources_aligned_with_stable_operations_summary(
    tmp_path,
):
    receipt_dir = tmp_path / "receipts"
    communication_writer, replay_service, executor, operations = build_services(
        tmp_path, receipt_dir=receipt_dir
    )
    envelope_1 = build_envelope("message_exec_1", "corr_exec")
    envelope_2 = build_envelope("message_exec_2", "corr_exec")
    communication_writer.write_record(
        envelope_1, build_result("message_exec_1", status=DispatchStatus.TRANSPORT_DELIVERED)
    )
    communication_writer.write_record(
        envelope_2,
        DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id="dispatch_message_exec_2",
            message_id="message_exec_2",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
            target="exec_bridge",
            adapter_name="stub_adapter",
            attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        ),
    )
    receipt_path = receipt_dir / "2026-04-24" / "exec_bridge"
    receipt_path.mkdir(parents=True, exist_ok=True)
    (receipt_path / "message_exec_1.ack.json").write_text(
        json.dumps(
            {
                "message_id": "message_exec_1",
                "ack_status": "acknowledged",
                "received_at": "2026-04-24T12:00:03",
            }
        ),
        encoding="utf-8",
    )

    replay_plan = replay_service.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_exec",
    )
    execution_result = executor.execute_correlation_replay(
        replay_plan,
        envelopes_by_message_id={
            "message_exec_1": envelope_1,
            "message_exec_2": envelope_2,
        },
    )
    replay_id = execution_result["replay_record"].replay_id

    view = operations.get_replay_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        replay_id=replay_id,
    )

    assert view is not None
    assert view["execution_summary"] == {
        "targeted_message_ids": ["message_exec_2"],
        "executed_message_ids": ["message_exec_2"],
        "skipped_message_ids": ["message_exec_1"],
        "blocked_message_ids": [],
        "skip_reasons": {"skip_acknowledged_message": ["message_exec_1"]},
        "block_reasons": {},
        "execution_mode": "targeted",
    }
    assert view["operations_summary"]["execution_mode"] == "targeted"
    assert view["operations_summary"]["executed_message_ids"] == ["message_exec_2"]
    assert view["operations_summary"]["skipped_message_ids"] == ["message_exec_1"]
    assert view["operations_summary"]["blocked_message_ids"] == []
    assert view["operations_summary"]["skip_reasons"] == {
        "skip_acknowledged_message": ["message_exec_1"]
    }
    assert view["operations_summary"]["block_reasons"] == {}


def test_operations_service_replay_view_prefers_extensions_governance_summary_over_gate(
    tmp_path,
):
    _, _, _, operations = build_services(tmp_path)
    replay_id = "replay_summary_sources"
    replay_path = (
        tmp_path / "2026-04-24" / stream_jsonl_filename("exec_bridge", LEDGER_STREAM_REPLAYS)
    )
    replay_path.parent.mkdir(parents=True, exist_ok=True)
    replay_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_REPLAY_EXECUTION_RECORD,
                "replay_id": replay_id,
                "scope": "message",
                "source_message_id": "message_summary",
                "source_correlation_id": "corr_summary",
                "executed_at": "2026-04-24T12:00:02",
                "gate_decision": {
                    "decision": ReplayGateDecision.ALLOW,
                    "reasons": ["clean_replay_candidate"],
                    "governance_tags": ["auto_replay_eligible"],
                    "governance_summary": {
                        "decision": ReplayGateDecision.ALLOW,
                        "posture": "auto_replay",
                        "recommended_strategy": "direct_replay_candidate",
                        "target_issue_codes": ["dispatch_pending"],
                        "review_issue_codes": [],
                        "governance_tags": ["stale_gate_tag"],
                    },
                },
                "execution": {
                    "status": "executed",
                    "execution_state": "dispatched",
                    "governance_decision": ReplayGateDecision.ALLOW,
                    "governance_posture": "auto_replay",
                },
                "results": {
                    "dispatch_result": None,
                    "results": [],
                },
                "trace": {
                    "scope": "message",
                    "message_id": "message_summary",
                    "correlation_id": "corr_summary",
                    "execution_state": "dispatched",
                },
                "plan": {
                    "recommended_strategy": "direct_replay_candidate",
                    "target_issue_codes": ["dispatch_pending"],
                    "review_issue_codes": [],
                },
                "extensions": {
                    "governance_summary": {
                        "decision": ReplayGateDecision.ALLOW,
                        "posture": "auto_replay",
                        "recommended_strategy": "direct_replay_candidate",
                        "target_issue_codes": ["dispatch_pending"],
                        "review_issue_codes": [],
                        "governance_tags": ["extension_preferred"],
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    view = operations.get_replay_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        replay_id=replay_id,
    )

    assert view is not None
    assert view["governance_summary"] == view["replay_record"]["extensions"]["governance_summary"]
    assert view["governance_sources"] == build_governance_sources(
        summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
        execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
    )
    assert (
        view["operations_summary"]["governance_summary_source"]
        == view["governance_sources"]["summary_source"]
    )
    assert (
        view["operations_summary"]["execution_projection_source"]
        == view["governance_sources"]["execution_projection_source"]
    )
    assert view["operations_summary"]["governance_tags"] == ["extension_preferred"]


def test_operations_service_prefers_gate_governance_summary_when_extensions_missing(tmp_path):
    _, _, _, operations = build_services(tmp_path)
    replay_id = "replay_gate_fallback"
    replay_path = (
        tmp_path / "2026-04-24" / stream_jsonl_filename("exec_bridge", LEDGER_STREAM_REPLAYS)
    )
    replay_path.parent.mkdir(parents=True, exist_ok=True)
    replay_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_REPLAY_EXECUTION_RECORD,
                "replay_id": replay_id,
                "scope": "message",
                "source_message_id": "message_gate",
                "source_correlation_id": "corr_gate",
                "executed_at": "2026-04-24T12:00:02",
                "gate_decision": {
                    "decision": ReplayGateDecision.ALLOW,
                    "reasons": ["clean_replay_candidate"],
                    "governance_tags": ["auto_replay_eligible"],
                    "governance_summary": {
                        "decision": ReplayGateDecision.ALLOW,
                        "posture": "auto_replay",
                        "recommended_strategy": "direct_replay_candidate",
                        "target_issue_codes": ["dispatch_pending"],
                        "review_issue_codes": [],
                        "governance_tags": ["auto_replay_eligible"],
                    },
                },
                "execution": {
                    "status": "executed",
                    "execution_state": "dispatched",
                },
                "results": {
                    "dispatch_result": None,
                    "results": [],
                },
                "trace": {
                    "scope": "message",
                    "message_id": "message_gate",
                    "correlation_id": "corr_gate",
                    "execution_state": "dispatched",
                },
                "extensions": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    view = operations.get_replay_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        replay_id=replay_id,
    )

    assert view is not None
    assert view["governance_summary"] == sample_governance_summary(
        target_issue_codes=["dispatch_pending"],
        governance_tags=["auto_replay_eligible"],
    )
    assert view["governance_sources"] == {
        "summary_source": CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        "execution_projection_source": None,
    }


def test_operations_service_derives_governance_summary_for_legacy_replay_record(tmp_path):
    _, _, _, operations = build_services(tmp_path)
    replay_id = "replay_legacy_fallback"
    replay_path = (
        tmp_path / "2026-04-24" / stream_jsonl_filename("exec_bridge", LEDGER_STREAM_REPLAYS)
    )
    replay_path.parent.mkdir(parents=True, exist_ok=True)
    replay_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_REPLAY_EXECUTION_RECORD,
                "replay_id": replay_id,
                "scope": "message",
                "source_message_id": "message_legacy",
                "source_correlation_id": "corr_legacy",
                "executed_at": "2026-04-24T12:00:02",
                "gate_decision": {
                    "decision": ReplayGateDecision.ALLOW,
                    "reasons": ["clean_replay_candidate"],
                    "governance_tags": ["auto_replay_eligible"],
                },
                "execution": {
                    "status": "executed",
                    "execution_state": "dispatched",
                },
                "results": {
                    "dispatch_result": None,
                    "results": [],
                },
                "trace": {
                    "scope": "message",
                    "message_id": "message_legacy",
                    "correlation_id": "corr_legacy",
                    "execution_state": "dispatched",
                },
                "plan": {
                    "recommended_strategy": "direct_replay_candidate",
                    "target_issue_codes": ["dispatch_pending"],
                    "review_issue_codes": [],
                },
                "extensions": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    view = operations.get_replay_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        replay_id=replay_id,
    )

    assert view is not None
    assert view["governance_summary"] == sample_governance_summary(
        target_issue_codes=["dispatch_pending"],
        governance_tags=["auto_replay_eligible"],
    )
    assert view["governance_sources"] == {
        "summary_source": CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED,
        "execution_projection_source": None,
    }


def test_operations_service_message_view_returns_unknown_posture_when_trace_missing():
    class CommunicationReaderStub:
        def find_by_message_id(self, **kwargs):
            return {"message_id": kwargs["message_id"]}

    class InspectionServiceStub:
        def get_message_trace(self, **kwargs):
            return None

    class ReplayServiceStub:
        def build_message_replay_plan(self, **kwargs):
            return {
                "recommended_strategy": "direct_replay_candidate",
                "target_issue_codes": ["dispatch_pending"],
                "review_issue_codes": [],
            }

    class ReplayGateStub:
        def evaluate_message_plan(self, replay_plan):
            return {
                "decision": ReplayGateDecision.ALLOW,
                "governance_summary": None,
            }

    operations = CommunicationOperationsService(
        communication_reader=CommunicationReaderStub(),
        inspection_service=InspectionServiceStub(),
        replay_service=ReplayServiceStub(),
        replay_gate=ReplayGateStub(),
    )

    view = operations.get_message_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_missing_trace",
    )

    assert view["trace"] is None
    assert view["operations_posture"] == "unknown"
    assert view["posture_sources"] == {
        "operations_posture_source": None,
    }
    assert view["operations_summary"] == {
        "posture": "unknown",
        "posture_source": None,
        "governance_decision": ReplayGateDecision.ALLOW,
        "governance_posture": "auto_replay",
        "recommended_strategy": "direct_replay_candidate",
        "target_issue_codes": ["dispatch_pending"],
        "review_issue_codes": [],
        "governance_tags": [],
        "governance_summary_source": None,
        "execution_projection_source": None,
        "reconciliation_status": None,
    }


def test_operations_service_replay_view_returns_unknown_posture_when_governance_summary_posture_is_unrecognized(
    tmp_path,
):
    _, _, _, operations = build_services(tmp_path)
    replay_id = "replay_unknown_posture"
    replay_path = (
        tmp_path / "2026-04-24" / stream_jsonl_filename("exec_bridge", LEDGER_STREAM_REPLAYS)
    )
    replay_path.parent.mkdir(parents=True, exist_ok=True)
    replay_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_REPLAY_EXECUTION_RECORD,
                "replay_id": replay_id,
                "scope": "message",
                "source_message_id": "message_unknown_posture",
                "source_correlation_id": "corr_unknown_posture",
                "executed_at": "2026-04-24T12:00:02",
                "gate_decision": {
                    "decision": ReplayGateDecision.ALLOW,
                    "reasons": ["clean_replay_candidate"],
                    "governance_tags": ["auto_replay_eligible"],
                    "governance_summary": {
                        "decision": ReplayGateDecision.ALLOW,
                        "posture": "healthy",
                        "recommended_strategy": "direct_replay_candidate",
                        "target_issue_codes": ["dispatch_pending"],
                        "review_issue_codes": [],
                        "governance_tags": ["auto_replay_eligible"],
                    },
                },
                "execution": {
                    "status": "executed",
                    "execution_state": "dispatched",
                    "governance_decision": ReplayGateDecision.ALLOW,
                    "governance_posture": "healthy",
                },
                "results": {
                    "dispatch_result": None,
                    "results": [],
                },
                "trace": {
                    "scope": "message",
                    "message_id": "message_unknown_posture",
                    "correlation_id": "corr_unknown_posture",
                    "execution_state": "dispatched",
                },
                "extensions": {
                    "governance_summary": {
                        "decision": ReplayGateDecision.ALLOW,
                        "posture": "healthy",
                        "recommended_strategy": "direct_replay_candidate",
                        "target_issue_codes": ["dispatch_pending"],
                        "review_issue_codes": [],
                        "governance_tags": ["auto_replay_eligible"],
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    view = operations.get_replay_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        replay_id=replay_id,
    )

    assert view is not None
    assert view["operations_posture"] == "unknown"
    assert view["posture_sources"] == {
        "operations_posture_source": None,
    }
    assert view["operations_summary"]["posture"] == "unknown"
    assert view["operations_summary"]["posture_source"] is None
    assert view["governance_summary"]["posture"] == "healthy"


def test_operations_service_correlation_view_returns_unknown_posture_when_trace_missing():
    class InspectionServiceStub:
        def get_correlation_trace(self, **kwargs):
            return None

    class ReplayServiceStub:
        def build_correlation_replay_plan(self, **kwargs):
            return {
                "recommended_strategy": "replay_only_timed_out_messages",
                "target_issue_codes": ["receipt_timeout"],
                "review_issue_codes": [],
            }

    class ReplayGateStub:
        def evaluate_correlation_plan(self, replay_plan):
            return {
                "decision": ReplayGateDecision.ALLOW,
                "governance_summary": None,
                "governance_tags": ["auto_replay_eligible", "timeout_targeted_replay"],
            }

    operations = CommunicationOperationsService(
        communication_reader=None,
        inspection_service=InspectionServiceStub(),
        replay_service=ReplayServiceStub(),
        replay_gate=ReplayGateStub(),
    )

    view = operations.get_correlation_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_missing_trace",
    )

    assert view["trace"] is None
    assert view["operations_posture"] == "unknown"
    assert view["posture_sources"] == {
        "operations_posture_source": None,
    }
    assert view["operations_summary"] == {
        "posture": "unknown",
        "posture_source": None,
        "governance_decision": ReplayGateDecision.ALLOW,
        "governance_posture": "targeted_replay",
        "recommended_strategy": "replay_only_timed_out_messages",
        "target_issue_codes": ["receipt_timeout"],
        "review_issue_codes": [],
        "governance_tags": ["auto_replay_eligible", "timeout_targeted_replay"],
        "governance_summary_source": None,
        "execution_projection_source": None,
        "reconciliation_status": None,
    }


def test_operations_service_replay_view_keeps_stable_summary_shape_when_execution_projection_missing(
    tmp_path,
):
    _, _, _, operations = build_services(tmp_path)
    replay_id = "replay_missing_projection"
    replay_path = (
        tmp_path / "2026-04-24" / stream_jsonl_filename("exec_bridge", LEDGER_STREAM_REPLAYS)
    )
    replay_path.parent.mkdir(parents=True, exist_ok=True)
    replay_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_REPLAY_EXECUTION_RECORD,
                "replay_id": replay_id,
                "scope": "message",
                "source_message_id": "message_projection",
                "source_correlation_id": "corr_projection",
                "executed_at": "2026-04-24T12:00:02",
                "gate_decision": {
                    "decision": ReplayGateDecision.ALLOW,
                    "reasons": ["clean_replay_candidate"],
                    "governance_tags": ["auto_replay_eligible"],
                },
                "execution": {
                    "status": "executed",
                    "execution_state": "dispatched",
                },
                "results": {
                    "dispatch_result": None,
                    "results": [],
                },
                "trace": {
                    "scope": "message",
                    "message_id": "message_projection",
                    "correlation_id": "corr_projection",
                    "execution_state": "dispatched",
                },
                "plan": {
                    "recommended_strategy": "direct_replay_candidate",
                    "target_issue_codes": ["dispatch_pending"],
                    "review_issue_codes": [],
                },
                "extensions": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    view = operations.get_replay_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        replay_id=replay_id,
    )

    assert view is not None
    assert view["execution_governance_projection"] == {
        "decision": None,
        "posture": None,
    }
    assert view["governance_sources"] == {
        "summary_source": CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED,
        "execution_projection_source": None,
    }
    assert_operations_view_stable_contract(
        view,
        operations_summary=build_replay_operations_summary(
            posture="auto_replay",
            target_issue_codes=["dispatch_pending"],
            governance_tags=["auto_replay_eligible"],
            governance_summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED,
            execution_projection_source=None,
            execution_mode="full",
        ),
        summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED,
        execution_projection_source=None,
    )


def test_operations_service_returns_targeted_replay_view(tmp_path):
    receipt_dir = tmp_path / "receipts"
    communication_writer, replay_service, executor, operations = build_services(
        tmp_path, receipt_dir=receipt_dir
    )
    envelope_1 = build_envelope("message_301", "corr_targeted")
    envelope_2 = build_envelope("message_302", "corr_targeted")
    communication_writer.write_record(
        envelope_1, build_result("message_301", status=DispatchStatus.TRANSPORT_DELIVERED)
    )
    communication_writer.write_record(
        envelope_2,
        DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id="dispatch_message_302",
            message_id="message_302",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
            target="exec_bridge",
            adapter_name="stub_adapter",
            attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        ),
    )
    receipt_path = receipt_dir / "2026-04-24" / "exec_bridge"
    receipt_path.mkdir(parents=True, exist_ok=True)
    (receipt_path / "message_301.ack.json").write_text(
        json.dumps(
            {
                "message_id": "message_301",
                "ack_status": "acknowledged",
                "received_at": "2026-04-24T12:00:03",
            }
        ),
        encoding="utf-8",
    )

    replay_plan = replay_service.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_targeted",
    )
    execution_result = executor.execute_correlation_replay(
        replay_plan,
        envelopes_by_message_id={
            "message_301": envelope_1,
            "message_302": envelope_2,
        },
    )
    replay_id = execution_result["replay_record"].replay_id

    view = operations.get_replay_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        replay_id=replay_id,
    )

    assert view is not None
    assert view["execution_summary"]["targeted_message_ids"] == ["message_302"]
    assert view["execution_summary"]["executed_message_ids"] == ["message_302"]
    assert view["execution_summary"]["skipped_message_ids"] == ["message_301"]
    assert view["execution_summary"]["skip_reasons"] == {
        "skip_acknowledged_message": ["message_301"]
    }
    assert view["execution_summary"]["block_reasons"] == {}
    assert view["execution_summary"]["execution_mode"] == "targeted"
    assert view["governance_summary"] == {
        "decision": ReplayGateDecision.ALLOW,
        "posture": "targeted_replay",
        "recommended_strategy": "replay_only_timed_out_messages",
        "target_issue_codes": ["receipt_timeout"],
        "review_issue_codes": [],
        "governance_tags": ["auto_replay_eligible", "timeout_targeted_replay"],
    }


def test_operations_service_returns_none_for_missing_replay_view(tmp_path):
    _, _, _, operations = build_services(tmp_path)

    view = operations.get_replay_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        replay_id="missing_replay",
    )

    assert view is None


def test_operations_service_returns_receipt_aware_message_view(tmp_path):
    receipt_dir = tmp_path / "receipts"
    communication_writer, _, _, operations = build_services(tmp_path, receipt_dir=receipt_dir)
    envelope = build_envelope("message_ack", "corr_ack")
    communication_writer.write_record(
        envelope,
        build_result("message_ack", status=DispatchStatus.TRANSPORT_DELIVERED),
    )

    receipt_path = receipt_dir / "2026-04-24" / "exec_bridge"
    receipt_path.mkdir(parents=True, exist_ok=True)
    (receipt_path / "message_ack.ack.json").write_text(
        json.dumps(
            {
                "message_id": "message_ack",
                "ack_status": "acknowledged",
                "received_at": "2026-04-24T12:00:03",
            }
        ),
        encoding="utf-8",
    )

    view = operations.get_message_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_ack",
    )

    assert view["receipt"] is not None
    assert view["trace"]["receipt"]["ack_status"] == "acknowledged"
    assert view["trace"]["delivery_state"]["phase"] == "receipt_acknowledged"


def test_operations_service_returns_timeout_message_view(tmp_path):
    communication_writer, _, _, operations = build_services(tmp_path)
    envelope = build_envelope("message_timeout", "corr_timeout")
    communication_writer.write_record(
        envelope,
        DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id="dispatch_message_timeout",
            message_id="message_timeout",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
            target="exec_bridge",
            adapter_name="stub_adapter",
            attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        ),
    )

    view = operations.get_message_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_timeout",
    )

    assert view["trace"]["delivery_state"]["phase"] == "receipt_timeout"
    assert view["trace"]["delivery_state"]["deadline_missed"] is True
