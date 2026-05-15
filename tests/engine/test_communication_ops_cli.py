import json
from datetime import datetime, timedelta
from pathlib import Path

from apps.engine.communication_ops_cli import (
    build_stable_summary_contract,
    extract_stable_summary_fields,
    run_cli,
)
from apps.engine.communication_summary_contract import (
    build_summary_mirror_fields_from_operations_summary,
)
from apps.engine.main_v9_shadow import (
    OutputPlan,
    SessionStreamPlan,
    ShadowSessionManager,
    render_output_content,
    stream_session_sse,
)
from apps.engine.v9_shadow_sse import iter_sse_messages_from_chunks
from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.domain_keys import (
    PAYLOAD_KEY_BLOCK_REASONS,
    PAYLOAD_KEY_BLOCKED_MESSAGE_IDS,
    PAYLOAD_KEY_EXECUTED_MESSAGE_IDS,
    PAYLOAD_KEY_EXECUTION_MODE,
    PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE,
    PAYLOAD_KEY_GOVERNANCE_DECISION,
    PAYLOAD_KEY_GOVERNANCE_POSTURE,
    PAYLOAD_KEY_GOVERNANCE_SOURCES,
    PAYLOAD_KEY_GOVERNANCE_SUMMARY_SOURCE,
    PAYLOAD_KEY_GOVERNANCE_TAGS,
    PAYLOAD_KEY_OPERATIONS_POSTURE,
    PAYLOAD_KEY_OPERATIONS_SUMMARY,
    PAYLOAD_KEY_POSTURE,
    PAYLOAD_KEY_POSTURE_SOURCE,
    PAYLOAD_KEY_POSTURE_SOURCES,
    PAYLOAD_KEY_RECOMMENDED_STRATEGY,
    PAYLOAD_KEY_RECONCILIATION_STATUS,
    PAYLOAD_KEY_REVIEW_ISSUE_CODES,
    PAYLOAD_KEY_SKIP_REASONS,
    PAYLOAD_KEY_SKIPPED_MESSAGE_IDS,
    PAYLOAD_KEY_SUMMARY_SOURCE,
    PAYLOAD_KEY_TARGET_ISSUE_CODES,
)
from core.contracts.enums import CommunicationMessageType, CommunicationPriority, DispatchStatus
from core.contracts.schema_versions import SCHEMA_REPLAY_EXECUTION_RECORD
from core.ledger.services.communication_inspection_service import CommunicationInspectionService
from core.ledger.services.communication_operations_service import CommunicationOperationsService
from core.ledger.services.communication_record_reader import CommunicationRecordReader
from core.ledger.services.communication_record_writer import CommunicationRecordWriter
from core.ledger.services.communication_replay_executor import CommunicationReplayExecutor
from core.ledger.services.communication_replay_gate import CommunicationReplayGate
from core.ledger.services.communication_replay_service import CommunicationReplayService
from core.ledger.services.replay_execution_writer import ReplayExecutionWriter
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.ledger.stream_names import LEDGER_STREAM_REPLAYS, stream_jsonl_filename
from core.protocol.schema_versions import SCHEMA_COMMUNICATION_ENVELOPE, SCHEMA_DISPATCH_RESULT
from core.protocol.services.communication_dispatcher import CommunicationDispatcher
from core.protocol.services.file_queue_receipt_reader import FileQueueReceiptReader
from core.protocol.services.stub_communication_adapter import StubCommunicationAdapter

STALE_SUMMARY_SOURCE = "stale_summary_source"
STALE_LEGACY_SUMMARY_SOURCE = "stale"
STALE_PROJECTION_SOURCE = "stale_projection_source"


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


def build_result(message_id: str):
    return DispatchResult(
        schema_version=SCHEMA_DISPATCH_RESULT,
        dispatch_id=f"dispatch_{message_id}",
        message_id=message_id,
        status=DispatchStatus.PROTOCOL_VALIDATED,
        recorded_at=datetime(2026, 4, 24, 12, 0, 1),
        target="exec_bridge",
        adapter_name="stub_adapter",
        attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
    )


def prepare_targeted_replay(tmp_path):
    base_dir = Path(tmp_path)
    store = JsonlLedgerStore(str(base_dir))
    communication_writer = CommunicationRecordWriter(ledger_store=store)
    replay_writer = ReplayExecutionWriter(ledger_store=store)
    inspection = CommunicationInspectionService(
        record_reader=CommunicationRecordReader(base_dir=str(base_dir))
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

    envelope = build_envelope("message_001", "corr_001")
    communication_writer.write_record(envelope, build_result("message_001"))

    plan = replay_service.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_001",
    )
    replay_result = executor.execute_message_replay(plan, envelope)
    return replay_result["replay_record"].replay_id


def build_operations_summary(
    *,
    posture: str,
    posture_source: str | None,
    governance_decision: str = "allow",
    governance_posture: str = "auto_replay",
    recommended_strategy: str = "direct_replay_candidate",
    target_issue_codes: list[str] | None = None,
    review_issue_codes: list[str] | None = None,
    governance_tags: list[str] | None = None,
    governance_summary_source: str | None = None,
    execution_projection_source: str | None = None,
    execution_mode: str | None = None,
    executed_message_ids: list[str] | None = None,
    skipped_message_ids: list[str] | None = None,
    blocked_message_ids: list[str] | None = None,
    skip_reasons: dict | None = None,
    block_reasons: dict | None = None,
) -> dict:
    summary = {
        PAYLOAD_KEY_POSTURE: posture,
        PAYLOAD_KEY_POSTURE_SOURCE: posture_source,
        PAYLOAD_KEY_GOVERNANCE_DECISION: governance_decision,
        PAYLOAD_KEY_GOVERNANCE_POSTURE: governance_posture,
        PAYLOAD_KEY_RECOMMENDED_STRATEGY: recommended_strategy,
        PAYLOAD_KEY_TARGET_ISSUE_CODES: target_issue_codes or [],
        PAYLOAD_KEY_REVIEW_ISSUE_CODES: review_issue_codes or [],
        PAYLOAD_KEY_GOVERNANCE_TAGS: governance_tags or [],
        PAYLOAD_KEY_GOVERNANCE_SUMMARY_SOURCE: governance_summary_source,
        PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: execution_projection_source,
        PAYLOAD_KEY_RECONCILIATION_STATUS: None,
    }
    if execution_mode is not None:
        summary[PAYLOAD_KEY_EXECUTION_MODE] = execution_mode
        summary[PAYLOAD_KEY_EXECUTED_MESSAGE_IDS] = executed_message_ids or []
        summary[PAYLOAD_KEY_SKIPPED_MESSAGE_IDS] = skipped_message_ids or []
        summary[PAYLOAD_KEY_BLOCKED_MESSAGE_IDS] = blocked_message_ids or []
        summary[PAYLOAD_KEY_SKIP_REASONS] = skip_reasons or {}
        summary[PAYLOAD_KEY_BLOCK_REASONS] = block_reasons or {}
    return summary


def build_stable_governance_sources(
    *,
    summary_source: str | None,
    execution_projection_source: str | None,
) -> dict:
    return {
        PAYLOAD_KEY_SUMMARY_SOURCE: summary_source,
        PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: execution_projection_source,
    }


def build_stub_operations_result(
    *,
    operations_summary: dict,
    operations_posture: str | None = None,
    posture_source: str | None = None,
    governance_sources: dict | None = None,
) -> dict:
    result = {
        PAYLOAD_KEY_OPERATIONS_SUMMARY: operations_summary,
    }
    if operations_posture is not None:
        result[PAYLOAD_KEY_OPERATIONS_POSTURE] = operations_posture  # type: ignore[reportArgumentType]
    if posture_source is not None:
        result[PAYLOAD_KEY_POSTURE_SOURCES] = {
            "operations_posture_source": posture_source,
        }
    if governance_sources is not None:
        result[PAYLOAD_KEY_GOVERNANCE_SOURCES] = governance_sources
    return result


def build_stub_replay_operations_result(
    *, operations_summary: dict, governance_sources: dict | None = None
) -> dict:
    return {
        **build_stub_operations_result(
            operations_summary=operations_summary,
            operations_posture="stale_value",
            posture_source="stale_source",
            governance_sources=governance_sources,
        ),
        "execution_summary": {"ignored": True},
        "replay_record": {"ignored": True},
    }


def assert_stable_summary_mirror_fields(
    payload: dict,
    *,
    operations_summary: dict,
    summary_source: str | None = None,
    execution_projection_source: str | None = None,
) -> None:
    assert payload[PAYLOAD_KEY_OPERATIONS_SUMMARY] == operations_summary
    assert payload[PAYLOAD_KEY_OPERATIONS_POSTURE] == operations_summary.get(PAYLOAD_KEY_POSTURE)
    assert payload[PAYLOAD_KEY_POSTURE_SOURCES] == {
        "operations_posture_source": operations_summary.get(PAYLOAD_KEY_POSTURE_SOURCE),
    }
    if summary_source is None and execution_projection_source is None:
        assert (
            PAYLOAD_KEY_GOVERNANCE_SOURCES not in payload
            or payload[PAYLOAD_KEY_GOVERNANCE_SOURCES] is None
        )
    else:
        assert payload[PAYLOAD_KEY_GOVERNANCE_SOURCES] == {
            PAYLOAD_KEY_SUMMARY_SOURCE: summary_source,
            PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: execution_projection_source,
        }


def assert_execution_mirror_fields(
    payload: dict,
    *,
    execution_mode: str,
    executed_message_ids: list[str],
    skipped_message_ids: list[str],
    blocked_message_ids: list[str],
    skip_reasons: dict,
    block_reasons: dict,
) -> None:
    assert payload[PAYLOAD_KEY_OPERATIONS_SUMMARY][PAYLOAD_KEY_EXECUTION_MODE] == execution_mode
    assert (
        payload[PAYLOAD_KEY_OPERATIONS_SUMMARY][PAYLOAD_KEY_EXECUTED_MESSAGE_IDS]
        == executed_message_ids
    )
    assert (
        payload[PAYLOAD_KEY_OPERATIONS_SUMMARY][PAYLOAD_KEY_SKIPPED_MESSAGE_IDS]
        == skipped_message_ids
    )
    assert (
        payload[PAYLOAD_KEY_OPERATIONS_SUMMARY][PAYLOAD_KEY_BLOCKED_MESSAGE_IDS]
        == blocked_message_ids
    )
    assert payload[PAYLOAD_KEY_OPERATIONS_SUMMARY][PAYLOAD_KEY_SKIP_REASONS] == skip_reasons
    assert payload[PAYLOAD_KEY_OPERATIONS_SUMMARY][PAYLOAD_KEY_BLOCK_REASONS] == block_reasons


def test_extract_stable_summary_fields_returns_none_for_none_input():
    assert extract_stable_summary_fields(None) is None


def test_build_summary_mirror_fields_from_operations_summary_always_projects_posture_mirrors():
    result = {
        "operations_summary": build_operations_summary(
            posture="auto_replay",
            posture_source="governance_summary.posture",
        ),
    }

    assert build_summary_mirror_fields_from_operations_summary(result) == {
        "operations_summary": build_operations_summary(
            posture="auto_replay",
            posture_source="governance_summary.posture",
        ),
        "operations_posture": "auto_replay",
        "posture_sources": {
            "operations_posture_source": "governance_summary.posture",
        },
    }


def test_build_stable_summary_contract_prefers_operations_summary_for_mirrors():
    result = {
        "operations_summary": build_operations_summary(
            posture="auto_replay",
            posture_source="governance_summary.posture",
            governance_summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
            execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
        ),
        "operations_posture": "stale_value",
        "posture_sources": {
            "operations_posture_source": "stale_source",
        },
        "governance_sources": build_stable_governance_sources(
            summary_source=STALE_SUMMARY_SOURCE,
            execution_projection_source=STALE_PROJECTION_SOURCE,
        ),
    }

    stable = build_stable_summary_contract(result)

    assert_stable_summary_mirror_fields(
        stable,  # type: ignore[reportArgumentType]
        operations_summary=build_operations_summary(
            posture="auto_replay",
            posture_source="governance_summary.posture",
            governance_summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
            execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
        ),
        summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
        execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
    )


def test_run_cli_projects_rejected_message_governance_summary(tmp_path):
    receipt_dir = Path(tmp_path) / "receipts"
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    writer.write_record(
        build_envelope("message_rejected", "corr_rejected"),
        DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id="dispatch_message_rejected",
            message_id="message_rejected",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 1),
            target="exec_bridge",
            adapter_name="stub_adapter",
            attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        ),
    )
    target_dir = receipt_dir / "2026-04-24" / "exec_bridge"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "message_rejected.ack.json").write_text(
        json.dumps(
            {
                "message_id": "message_rejected",
                "ack_status": "rejected",
                "received_at": "2026-04-24T12:00:03",
            }
        ),
        encoding="utf-8",
    )

    payload = json.loads(
        run_cli(
            [
                "--base-dir",
                str(tmp_path),
                "--receipt-dir",
                str(receipt_dir),
                "message",
                "--date",
                "2026-04-24",
                "--target",
                "exec_bridge",
                "--message-id",
                "message_rejected",
            ]
        )
    )

    assert payload["replay_plan"]["recommended_strategy"] == "review_rejected_receipt_before_replay"
    assert_stable_summary_mirror_fields(
        payload,
        operations_summary=build_operations_summary(
            posture="action_required",
            posture_source="trace.delivery_state.delivery_posture",
            governance_decision="review",
            governance_posture="review_required",
            recommended_strategy="review_rejected_receipt_before_replay",
            target_issue_codes=["receipt_rejected"],
            review_issue_codes=["receipt_rejected"],
            governance_tags=["requires_governance_review", "receipt_rejected"],
        ),
    )


def test_build_stable_summary_contract_prefers_operations_summary_for_mirrors():
    result = {
        "operations_summary": build_operations_summary(
            posture="auto_replay",
            posture_source="governance_summary.posture",
            governance_summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
            execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
        ),
        "operations_posture": "stale_value",
        "posture_sources": {
            "operations_posture_source": "stale_source",
        },
        "governance_sources": build_stable_governance_sources(
            summary_source=STALE_SUMMARY_SOURCE,
            execution_projection_source=STALE_PROJECTION_SOURCE,
        ),
    }

    stable = build_stable_summary_contract(result)

    assert_stable_summary_mirror_fields(
        stable,  # type: ignore[reportArgumentType]
        operations_summary=build_operations_summary(
            posture="auto_replay",
            posture_source="governance_summary.posture",
            governance_summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
            execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
        ),
        summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
        execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
    )


def test_extract_stable_summary_fields_returns_consistent_contract_slice():
    result = {
        "operations_summary": build_operations_summary(
            posture="unknown",
            posture_source=None,
        ),
        "operations_posture": "unknown",
        "posture_sources": {
            "operations_posture_source": None,
        },
        "governance_sources": build_stable_governance_sources(
            summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
            execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
        ),
        "trace": {"ignored": True},
        "record": {"ignored": True},
    }

    stable = extract_stable_summary_fields(result)

    assert_stable_summary_mirror_fields(
        stable,  # type: ignore[reportArgumentType]
        operations_summary=build_operations_summary(
            posture="unknown",
            posture_source=None,
        ),
        summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
    )


def test_extract_stable_summary_fields_omits_unstable_and_missing_fields():
    result = {
        "operations_summary": build_operations_summary(
            posture="action_required",
            posture_source="trace.delivery_state.delivery_posture",
        ),
        "record": {"message_id": "message_001"},
        "trace": {"ignored": True},
    }

    stable = extract_stable_summary_fields(result)

    assert_stable_summary_mirror_fields(
        stable,  # type: ignore[reportArgumentType]
        operations_summary=build_operations_summary(
            posture="action_required",
            posture_source="trace.delivery_state.delivery_posture",
        ),
    )


def test_cli_message_view_returns_only_stable_summary_slice_when_extracted(tmp_path, monkeypatch):
    expected_result = {
        **build_stub_operations_result(
            operations_summary=build_operations_summary(
                posture="action_required",
                posture_source="trace.delivery_state.delivery_posture",
                governance_summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
                execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
            ),
            operations_posture="stale_value",
            posture_source="stale_source",
            governance_sources=build_stable_governance_sources(
                summary_source=STALE_SUMMARY_SOURCE,
                execution_projection_source=STALE_PROJECTION_SOURCE,
            ),
        ),
        "trace": {"ignored": True},
        "record": {"ignored": True},
    }

    class StubOperationsService:
        def get_message_operations_view(self, **kwargs):
            return expected_result

    monkeypatch.setattr(
        "apps.engine.communication_ops_cli.build_operations_service",
        lambda *args, **kwargs: StubOperationsService(),
    )

    payload = json.loads(
        run_cli(
            [
                "--base-dir",
                str(tmp_path),
                "message",
                "--date",
                "2026-04-24",
                "--target",
                "exec_bridge",
                "--message-id",
                "message_001",
            ]
        )
    )

    stable = extract_stable_summary_fields(payload)

    assert_stable_summary_mirror_fields(
        stable,  # type: ignore[reportArgumentType]
        operations_summary=build_operations_summary(
            posture="action_required",
            posture_source="trace.delivery_state.delivery_posture",
            governance_summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
            execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
        ),
        summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
    )


def test_cli_replay_view_extract_stable_summary_fields_preserves_boundary(tmp_path, monkeypatch):
    expected_result = build_stub_replay_operations_result(
        operations_summary=build_operations_summary(
            posture="auto_replay",
            posture_source="governance_summary.posture",
            governance_summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
            execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
            governance_tags=["auto_replay_eligible"],
            target_issue_codes=["dispatch_pending"],
        ),
        governance_sources=build_stable_governance_sources(
            summary_source=STALE_SUMMARY_SOURCE,
            execution_projection_source=STALE_PROJECTION_SOURCE,
        ),
    )

    class StubOperationsService:
        def get_replay_operations_view(self, **kwargs):
            return expected_result

    monkeypatch.setattr(
        "apps.engine.communication_ops_cli.build_operations_service",
        lambda *args, **kwargs: StubOperationsService(),
    )

    payload = json.loads(
        run_cli(
            [
                "--base-dir",
                str(tmp_path),
                "replay",
                "--date",
                "2026-04-24",
                "--target",
                "exec_bridge",
                "--replay-id",
                "replay_001",
            ]
        )
    )

    stable = extract_stable_summary_fields(payload)

    assert_stable_summary_mirror_fields(
        stable,  # type: ignore[reportArgumentType]
        operations_summary=build_operations_summary(
            posture="auto_replay",
            posture_source="governance_summary.posture",
            governance_summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
            execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
            governance_tags=["auto_replay_eligible"],
            target_issue_codes=["dispatch_pending"],
        ),
        summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
        execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
    )


def test_cli_message_view_prefers_operations_summary_for_posture_fields(tmp_path, monkeypatch):
    expected_result = build_stub_operations_result(
        operations_summary=build_operations_summary(
            posture="action_required",
            posture_source="trace.delivery_state.delivery_posture",
            governance_tags=["auto_replay_eligible"],
            target_issue_codes=["dispatch_pending"],
        ),
        operations_posture="stale_value",
        posture_source="stale_source",
    )

    class StubOperationsService:
        def get_message_operations_view(self, **kwargs):
            return expected_result

    monkeypatch.setattr(
        "apps.engine.communication_ops_cli.build_operations_service",
        lambda *args, **kwargs: StubOperationsService(),
    )

    payload = json.loads(
        run_cli(
            [
                "--base-dir",
                str(tmp_path),
                "message",
                "--date",
                "2026-04-24",
                "--target",
                "exec_bridge",
                "--message-id",
                "message_001",
            ]
        )
    )

    assert payload["operations_posture"] == "action_required"
    assert payload["posture_sources"] == {
        "operations_posture_source": "trace.delivery_state.delivery_posture",
    }


def test_cli_message_view_uses_summary_governance_sources_when_present(tmp_path, monkeypatch):
    expected_result = build_stub_operations_result(
        operations_summary=build_operations_summary(
            posture="action_required",
            posture_source="trace.delivery_state.delivery_posture",
            governance_summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
            execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
        ),
        governance_sources=build_stable_governance_sources(
            summary_source=STALE_LEGACY_SUMMARY_SOURCE,
            execution_projection_source=None,
        ),
    )

    class StubOperationsService:
        def get_message_operations_view(self, **kwargs):
            return expected_result

    monkeypatch.setattr(
        "apps.engine.communication_ops_cli.build_operations_service",
        lambda *args, **kwargs: StubOperationsService(),
    )

    payload = json.loads(
        run_cli(
            [
                "--base-dir",
                str(tmp_path),
                "message",
                "--date",
                "2026-04-24",
                "--target",
                "exec_bridge",
                "--message-id",
                "message_001",
            ]
        )
    )

    assert payload["governance_sources"] == {
        "summary_source": CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        "execution_projection_source": CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
    }


def test_cli_message_view_preserves_stable_governance_sources_when_summary_omits_them(
    tmp_path, monkeypatch
):
    expected_result = build_stub_operations_result(
        operations_summary=build_operations_summary(
            posture="action_required",
            posture_source="trace.delivery_state.delivery_posture",
            governance_tags=["auto_replay_eligible"],
            target_issue_codes=["dispatch_pending"],
        ),
        governance_sources=build_stable_governance_sources(
            summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED,
            execution_projection_source=None,
        ),
    )

    class StubOperationsService:
        def get_message_operations_view(self, **kwargs):
            return expected_result

    monkeypatch.setattr(
        "apps.engine.communication_ops_cli.build_operations_service",
        lambda *args, **kwargs: StubOperationsService(),
    )

    payload = json.loads(
        run_cli(
            [
                "--base-dir",
                str(tmp_path),
                "message",
                "--date",
                "2026-04-24",
                "--target",
                "exec_bridge",
                "--message-id",
                "message_001",
            ]
        )
    )

    assert payload["governance_sources"] == {
        "summary_source": CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED,
        "execution_projection_source": None,
    }


def test_cli_message_view_maps_unknown_posture_from_summary(tmp_path, monkeypatch):
    expected_result = build_stub_operations_result(
        operations_summary=build_operations_summary(
            posture="unknown",
            posture_source=None,
            governance_tags=[],
            target_issue_codes=["dispatch_pending"],
        ),
        operations_posture="stale_value",
        posture_source="stale_source",
    )

    class StubOperationsService:
        def get_message_operations_view(self, **kwargs):
            return expected_result

    monkeypatch.setattr(
        "apps.engine.communication_ops_cli.build_operations_service",
        lambda *args, **kwargs: StubOperationsService(),
    )

    payload = json.loads(
        run_cli(
            [
                "--base-dir",
                str(tmp_path),
                "message",
                "--date",
                "2026-04-24",
                "--target",
                "exec_bridge",
                "--message-id",
                "message_001",
            ]
        )
    )

    assert payload["operations_posture"] == "unknown"
    assert payload["posture_sources"] == {
        "operations_posture_source": None,
    }


def test_cli_message_view(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    writer.write_record(build_envelope("message_001", "corr_001"), build_result("message_001"))

    output = run_cli(
        [
            "--base-dir",
            str(tmp_path),
            "message",
            "--date",
            "2026-04-24",
            "--target",
            "exec_bridge",
            "--message-id",
            "message_001",
        ]
    )
    payload = json.loads(output)

    assert payload["record"]["message_id"] == "message_001"
    assert payload["trace"]["delivery_state"]["phase"] == "dispatch_recorded"
    assert payload["trace"]["delivery_state"]["issue_code"] == "dispatch_pending"
    assert payload["trace"]["delivery_state"]["delivery_posture"] == "action_required"
    assert_stable_summary_mirror_fields(
        payload,
        operations_summary=build_operations_summary(
            posture="action_required",
            posture_source="trace.delivery_state.delivery_posture",
            governance_decision="allow",
            governance_posture="auto_replay",
            recommended_strategy="direct_replay_candidate",
            target_issue_codes=["dispatch_pending"],
            review_issue_codes=[],
            governance_tags=["auto_replay_eligible"],
        ),
    )
    assert payload["trace"]["delivery_state"]["deadline_missed"] is False
    assert payload["replay_gate"]["decision"] == "allow"
    assert payload["replay_gate"]["governance_summary"] == payload["governance_summary"]
    assert payload["governance_summary"] == {
        "decision": "allow",
        "posture": "auto_replay",
        "recommended_strategy": "direct_replay_candidate",
        "target_issue_codes": ["dispatch_pending"],
        "review_issue_codes": [],
        "governance_tags": ["auto_replay_eligible"],
    }


def test_cli_correlation_view_maps_unknown_posture_from_summary(tmp_path, monkeypatch):
    expected_result = build_stub_operations_result(
        operations_summary=build_operations_summary(
            posture="unknown",
            posture_source=None,
            governance_posture="targeted_replay",
            recommended_strategy="replay_only_timed_out_messages",
            target_issue_codes=["receipt_timeout"],
            governance_tags=["auto_replay_eligible", "timeout_targeted_replay"],
        ),
        operations_posture="stale_value",
        posture_source="stale_source",
    )

    class StubOperationsService:
        def get_correlation_operations_view(self, **kwargs):
            return expected_result

    monkeypatch.setattr(
        "apps.engine.communication_ops_cli.build_operations_service",
        lambda *args, **kwargs: StubOperationsService(),
    )

    payload = json.loads(
        run_cli(
            [
                "--base-dir",
                str(tmp_path),
                "correlation",
                "--date",
                "2026-04-24",
                "--target",
                "exec_bridge",
                "--correlation-id",
                "corr_missing_trace",
            ]
        )
    )

    assert payload["operations_posture"] == "unknown"
    assert payload["posture_sources"] == {
        "operations_posture_source": None,
    }


def test_cli_correlation_view(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    receipt_dir = tmp_path / "receipts"
    receipt_path = receipt_dir / "2026-04-24" / "exec_bridge"
    receipt_path.mkdir(parents=True, exist_ok=True)

    writer.write_record(
        build_envelope("message_101", "corr_shared"),
        DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id="dispatch_message_101",
            message_id="message_101",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 1),
            target="exec_bridge",
            adapter_name="stub_adapter",
            attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        ),
    )
    writer.write_record(
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

    output = run_cli(
        [
            "--base-dir",
            str(tmp_path),
            "--receipt-dir",
            str(receipt_dir),
            "correlation",
            "--date",
            "2026-04-24",
            "--target",
            "exec_bridge",
            "--correlation-id",
            "corr_shared",
        ]
    )
    payload = json.loads(output)

    assert payload["trace"]["correlation_id"] == "corr_shared"
    assert payload["trace"]["delivery_summary"]["phase_counts"] == {
        "receipt_acknowledged": 1,
        "receipt_timeout": 1,
    }
    assert payload["trace"]["delivery_summary"]["issue_counts"] == {
        "clean": 1,
        "receipt_timeout": 1,
    }
    assert payload["trace"]["delivery_summary"]["issue_message_ids"] == {
        "clean": ["message_101"],
        "receipt_timeout": ["message_102"],
    }
    assert payload["trace"]["delivery_summary"]["delivery_posture"] == "action_required"
    assert payload["operations_summary"] == build_operations_summary(
        posture="action_required",
        posture_source="trace.delivery_summary.delivery_posture",
        governance_posture="targeted_replay",
        recommended_strategy="replay_only_timed_out_messages",
        target_issue_codes=["receipt_timeout"],
        governance_tags=["auto_replay_eligible", "timeout_targeted_replay"],
    )
    assert payload["operations_posture"] == payload["operations_summary"]["posture"]
    assert payload["posture_sources"] == {
        "operations_posture_source": payload["operations_summary"]["posture_source"],
    }
    assert payload["trace"]["delivery_summary"]["acknowledged_message_ids"] == ["message_101"]
    assert payload["trace"]["delivery_summary"]["timed_out_message_ids"] == ["message_102"]
    assert payload["replay_plan"]["target_message_ids"] == ["message_102"]
    assert payload["replay_plan"]["target_issue_codes"] == ["receipt_timeout"]
    assert payload["replay_plan"]["review_issue_codes"] == []
    assert payload["replay_plan"]["recommended_strategy"] == "replay_only_timed_out_messages"
    assert payload["replay_gate"]["decision"] == "allow"
    assert payload["replay_gate"]["governance_summary"] == payload["governance_summary"]
    assert payload["governance_summary"] == {
        "decision": "allow",
        "posture": "targeted_replay",
        "recommended_strategy": "replay_only_timed_out_messages",
        "target_issue_codes": ["receipt_timeout"],
        "review_issue_codes": [],
        "governance_tags": ["auto_replay_eligible", "timeout_targeted_replay"],
    }


def test_cli_correlation_view_projects_terminal_mixed_contract(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    receipt_dir = tmp_path / "receipts"
    receipt_path = receipt_dir / "2026-04-24" / "exec_bridge"
    receipt_path.mkdir(parents=True, exist_ok=True)

    writer.write_record(
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
    writer.write_record(
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
    writer.write_record(
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

    output = run_cli(
        [
            "--base-dir",
            str(tmp_path),
            "--receipt-dir",
            str(receipt_dir),
            "correlation",
            "--date",
            "2026-04-24",
            "--target",
            "exec_bridge",
            "--correlation-id",
            "corr_terminal_mix",
        ]
    )
    payload = json.loads(output)

    assert payload["trace"]["delivery_summary"]["issue_counts"] == {
        "receipt_accepted": 1,
        "receipt_timeout": 1,
        "clean": 1,
    }
    assert payload["trace"]["delivery_summary"]["issue_message_ids"] == {
        "receipt_accepted": ["message_accepted"],
        "receipt_timeout": ["message_timeout"],
        "clean": ["message_acked"],
    }
    assert payload["replay_plan"]["recommended_strategy"] == "do_not_replay_terminal_receipts"
    assert payload["replay_plan"]["target_issue_codes"] == ["receipt_timeout"]
    assert payload["replay_plan"]["review_issue_codes"] == []
    assert payload["replay_plan"]["target_message_ids"] == ["message_timeout"]
    assert payload["replay_gate"]["decision"] == "deny"
    assert payload["operations_summary"] == build_operations_summary(
        posture="action_required",
        posture_source="trace.delivery_summary.delivery_posture",
        governance_decision="deny",
        governance_posture="blocked",
        recommended_strategy="do_not_replay_terminal_receipts",
        target_issue_codes=["receipt_timeout"],
        review_issue_codes=[],
        governance_tags=["replay_not_required", "terminal_receipt"],
    )
    assert payload["operations_posture"] == payload["operations_summary"]["posture"]
    assert payload["posture_sources"] == {
        "operations_posture_source": payload["operations_summary"]["posture_source"],
    }
    assert payload["governance_summary"] == {
        "decision": "deny",
        "posture": "blocked",
        "recommended_strategy": "do_not_replay_terminal_receipts",
        "target_issue_codes": ["receipt_timeout"],
        "review_issue_codes": [],
        "governance_tags": ["replay_not_required", "terminal_receipt"],
    }


def test_cli_replay_view_existing_end_to_end_sample(tmp_path):
    replay_id = prepare_targeted_replay(tmp_path)

    output = run_cli(
        [
            "--base-dir",
            str(tmp_path),
            "replay",
            "--date",
            "2026-04-24",
            "--target",
            "exec_bridge",
            "--replay-id",
            replay_id,
        ]
    )
    payload = json.loads(output)

    assert payload["replay_record"]["replay_id"] == replay_id
    assert payload["replay_status"] == "executed"
    assert (
        payload["replay_record"]["extensions"]["governance_summary"]
        == payload["governance_summary"]
    )
    assert (
        payload["replay_record"]["gate_decision"]["governance_summary"]
        == payload["governance_summary"]
    )
    assert payload["replay_record"]["execution"]["governance_posture"] == "auto_replay"
    assert payload["execution_governance_projection"] == {
        "decision": "allow",
        "posture": "auto_replay",
    }
    assert payload["operations_summary"] == build_operations_summary(
        posture="auto_replay",
        posture_source="governance_summary.posture",
        governance_summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
        execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
        governance_tags=["auto_replay_eligible"],
        target_issue_codes=["dispatch_pending"],
        execution_mode="full",
        executed_message_ids=["message_001"],
        skipped_message_ids=[],
        blocked_message_ids=[],
        skip_reasons={},
        block_reasons={},
    )
    assert payload["operations_posture"] == payload["operations_summary"]["posture"]
    assert payload["posture_sources"] == {
        "operations_posture_source": payload["operations_summary"]["posture_source"],
    }
    assert payload["governance_sources"] == {
        "summary_source": CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
        "execution_projection_source": CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
    }
    assert payload["execution_summary"]["targeted_message_ids"] == ["message_001"]
    assert payload["execution_summary"]["executed_message_ids"] == ["message_001"]
    assert payload["execution_summary"]["skipped_message_ids"] == []
    assert payload["execution_summary"]["blocked_message_ids"] == []
    assert payload["execution_summary"]["skip_reasons"] == {}
    assert payload["execution_summary"]["block_reasons"] == {}
    assert payload["execution_summary"]["execution_mode"] == "full"
    assert payload["gate_decision"]["governance_summary"] == payload["governance_summary"]
    assert payload["governance_summary"] == {
        "decision": "allow",
        "posture": "auto_replay",
        "recommended_strategy": "direct_replay_candidate",
        "target_issue_codes": ["dispatch_pending"],
        "review_issue_codes": [],
        "governance_tags": ["auto_replay_eligible"],
    }


def test_cli_replay_view_preserves_execution_mirror_fields_from_operations_summary(
    tmp_path, monkeypatch
):
    expected_result = build_stub_replay_operations_result(
        operations_summary=build_operations_summary(
            posture="targeted_replay",
            posture_source="governance_summary.posture",
            governance_summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
            execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
            governance_tags=["auto_replay_eligible", "timeout_targeted_replay"],
            target_issue_codes=["receipt_timeout"],
            execution_mode="targeted",
            executed_message_ids=["message_402"],
            skipped_message_ids=["message_401"],
            blocked_message_ids=[],
            skip_reasons={"skip_acknowledged_message": ["message_401"]},
            block_reasons={},
        ),
    )

    class StubOperationsService:
        def get_replay_operations_view(self, **kwargs):
            return expected_result

    monkeypatch.setattr(
        "apps.engine.communication_ops_cli.build_operations_service",
        lambda *args, **kwargs: StubOperationsService(),
    )

    payload = json.loads(
        run_cli(
            [
                "--base-dir",
                str(tmp_path),
                "replay",
                "--date",
                "2026-04-24",
                "--target",
                "exec_bridge",
                "--replay-id",
                "replay_targeted_001",
            ]
        )
    )

    assert_execution_mirror_fields(
        payload,
        execution_mode="targeted",
        executed_message_ids=["message_402"],
        skipped_message_ids=["message_401"],
        blocked_message_ids=[],
        skip_reasons={"skip_acknowledged_message": ["message_401"]},
        block_reasons={},
    )


def test_cli_replay_view_preserves_blocked_execution_mirror_fields_from_operations_summary(
    tmp_path, monkeypatch
):
    expected_result = build_stub_replay_operations_result(
        operations_summary=build_operations_summary(
            posture="review_required",
            posture_source="governance_summary.posture",
            governance_decision="review",
            governance_posture="review_required",
            governance_summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
            execution_projection_source=None,
            governance_tags=["requires_governance_review", "degraded_or_failed_attempts"],
            review_issue_codes=["attempt_history_requires_review"],
            recommended_strategy="replay_with_governance_review",
            execution_mode="blocked",
            executed_message_ids=[],
            skipped_message_ids=[],
            blocked_message_ids=["message_blocked"],
            skip_reasons={},
            block_reasons={"block_review_required": ["message_blocked"]},
        ),
    )

    class StubOperationsService:
        def get_replay_operations_view(self, **kwargs):
            return expected_result

    monkeypatch.setattr(
        "apps.engine.communication_ops_cli.build_operations_service",
        lambda *args, **kwargs: StubOperationsService(),
    )

    payload = json.loads(
        run_cli(
            [
                "--base-dir",
                str(tmp_path),
                "replay",
                "--date",
                "2026-04-24",
                "--target",
                "exec_bridge",
                "--replay-id",
                "replay_blocked_001",
            ]
        )
    )

    assert_execution_mirror_fields(
        payload,
        execution_mode="blocked",
        executed_message_ids=[],
        skipped_message_ids=[],
        blocked_message_ids=["message_blocked"],
        skip_reasons={},
        block_reasons={"block_review_required": ["message_blocked"]},
    )


def test_cli_replay_view_uses_gate_governance_summary_when_extensions_missing(tmp_path):
    replay_path = (
        tmp_path / "2026-04-24" / stream_jsonl_filename("exec_bridge", LEDGER_STREAM_REPLAYS)
    )
    replay_path.parent.mkdir(parents=True, exist_ok=True)
    replay_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_REPLAY_EXECUTION_RECORD,
                "replay_id": "replay_gate_cli",
                "scope": "message",
                "source_message_id": "message_gate",
                "source_correlation_id": "corr_gate",
                "executed_at": "2026-04-24T12:00:02",
                "gate_decision": {
                    "decision": "allow",
                    "reasons": ["clean_replay_candidate"],
                    "governance_tags": ["auto_replay_eligible"],
                    "governance_summary": {
                        "decision": "allow",
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

    output = run_cli(
        [
            "--base-dir",
            str(tmp_path),
            "replay",
            "--date",
            "2026-04-24",
            "--target",
            "exec_bridge",
            "--replay-id",
            "replay_gate_cli",
        ]
    )
    payload = json.loads(output)

    assert payload["governance_summary"]["decision"] == "allow"
    assert payload["governance_sources"] == {
        "summary_source": CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        "execution_projection_source": None,
    }


def test_cli_replay_view_derives_governance_summary_for_legacy_record(tmp_path):
    replay_path = (
        tmp_path / "2026-04-24" / stream_jsonl_filename("exec_bridge", LEDGER_STREAM_REPLAYS)
    )
    replay_path.parent.mkdir(parents=True, exist_ok=True)
    replay_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_REPLAY_EXECUTION_RECORD,
                "replay_id": "replay_legacy_cli",
                "scope": "message",
                "source_message_id": "message_legacy",
                "source_correlation_id": "corr_legacy",
                "executed_at": "2026-04-24T12:00:02",
                "gate_decision": {
                    "decision": "allow",
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

    output = run_cli(
        [
            "--base-dir",
            str(tmp_path),
            "replay",
            "--date",
            "2026-04-24",
            "--target",
            "exec_bridge",
            "--replay-id",
            "replay_legacy_cli",
        ]
    )
    payload = json.loads(output)

    assert payload["governance_summary"] == {
        "decision": "allow",
        "posture": "auto_replay",
        "recommended_strategy": "direct_replay_candidate",
        "target_issue_codes": ["dispatch_pending"],
        "review_issue_codes": [],
        "governance_tags": ["auto_replay_eligible"],
    }
    assert payload["governance_sources"] == {
        "summary_source": CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED,
        "execution_projection_source": None,
    }


def test_cli_replay_view_preserves_stable_governance_sources_when_summary_omits_them(
    tmp_path, monkeypatch
):
    expected_result = build_stub_replay_operations_result(
        operations_summary=build_operations_summary(
            posture="auto_replay",
            posture_source="governance_summary.posture",
            governance_tags=["auto_replay_eligible"],
            target_issue_codes=["dispatch_pending"],
        ),
        governance_sources=build_stable_governance_sources(
            summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
            execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
        ),
    )

    class StubOperationsService:
        def get_replay_operations_view(self, **kwargs):
            return expected_result

    monkeypatch.setattr(
        "apps.engine.communication_ops_cli.build_operations_service",
        lambda *args, **kwargs: StubOperationsService(),
    )

    payload = json.loads(
        run_cli(
            [
                "--base-dir",
                str(tmp_path),
                "replay",
                "--date",
                "2026-04-24",
                "--target",
                "exec_bridge",
                "--replay-id",
                "replay_001",
            ]
        )
    )

    assert payload["governance_sources"] == {
        "summary_source": CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        "execution_projection_source": CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
    }


def test_cli_replay_view_keeps_stable_governance_sources_when_projection_missing(
    tmp_path, monkeypatch
):
    expected_result = build_stub_replay_operations_result(
        operations_summary=build_operations_summary(
            posture="auto_replay",
            posture_source="governance_summary.posture",
            governance_tags=["auto_replay_eligible"],
            target_issue_codes=["dispatch_pending"],
        ),
        governance_sources=build_stable_governance_sources(
            summary_source=CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
            execution_projection_source=CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
        ),
    )

    class StubOperationsService:
        def get_replay_operations_view(self, **kwargs):
            return expected_result

    monkeypatch.setattr(
        "apps.engine.communication_ops_cli.build_operations_service",
        lambda *args, **kwargs: StubOperationsService(),
    )

    payload = json.loads(
        run_cli(
            [
                "--base-dir",
                str(tmp_path),
                "replay",
                "--date",
                "2026-04-24",
                "--target",
                "exec_bridge",
                "--replay-id",
                "replay_missing_projection",
            ]
        )
    )

    assert payload["governance_sources"] == {
        "summary_source": CommunicationOperationsService.REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
        "execution_projection_source": CommunicationOperationsService.REPLAY_GOVERNANCE_PROJECTION_SOURCE_EXECUTION,
    }


def test_cli_and_shadow_json_and_session_and_sse_share_same_stable_contract(monkeypatch):
    from tests.engine.shadow_testkit import (
        build_fallback_cli_contract,
        build_fallback_session_payload,
        build_fallback_summary_result,
    )

    expected_result = build_fallback_cli_contract()
    {
        **build_fallback_session_payload(),
        **expected_result,
    }

    class StubOperationsService:
        def get_message_operations_view(self, **kwargs):
            return {
                "record": {"ignored": True},
                "trace": {"ignored": True},
                **expected_result,
            }

    monkeypatch.setattr(
        "apps.engine.communication_ops_cli.build_operations_service",
        lambda *args, **kwargs: StubOperationsService(),
    )

    cli_output = json.loads(
        run_cli(
            [
                "--base-dir",
                "D:/cursor",
                "message",
                "--date",
                "2026-04-24",
                "--target",
                "exec_bridge",
                "--message-id",
                "message_001",
            ]
        )
    )

    result = build_fallback_summary_result()
    result.communication_operations = build_fallback_cli_contract()
    shadow_payload = build_fallback_session_payload()
    monkeypatch.setattr(
        "apps.engine.main_v9_shadow.prepare_results",
        lambda _args: ([shadow_payload], "ignored", [result]),
    )

    shadow_json = json.loads(
        render_output_content(
            OutputPlan(mode="json", output_path=None, include_meta=True),
            [shadow_payload],
            default_text="ignored",
        )
    )
    session_events = list(iter_sse_messages_from_chunks(stream_session_sse(type("Args", (), {})())))
    sse_completed = session_events[-1]["data"]["data"]["results"]
    manager_completed = list(
        ShadowSessionManager(stream_plan=SessionStreamPlan(include_meta=True)).stream_run(
            type("Args", (), {})()
        )
    )[-1]["data"]["results"]

    expected_stable = extract_stable_summary_fields(cli_output)
    assert extract_stable_summary_fields(shadow_json["results"]) == expected_stable
    assert extract_stable_summary_fields(sse_completed) == expected_stable
    assert extract_stable_summary_fields(manager_completed) == expected_stable


def test_cli_targeted_replay_view(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    communication_writer = CommunicationRecordWriter(ledger_store=store)
    replay_writer = ReplayExecutionWriter(ledger_store=store)
    communication_reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    inspection = CommunicationInspectionService(
        record_reader=communication_reader,
        receipt_reader=FileQueueReceiptReader(receipt_dir=str(receipt_dir)),
    )
    CommunicationReplayService(inspection_service=inspection)
    replay_gate = CommunicationReplayGate()
    dispatcher = CommunicationDispatcher(
        adapter=StubCommunicationAdapter(),
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    CommunicationReplayExecutor(
        replay_gate=replay_gate,
        dispatcher=dispatcher,
        replay_execution_writer=replay_writer,
    )
    envelope_1 = build_envelope("message_401", "corr_targeted")
    envelope_2 = build_envelope("message_402", "corr_targeted")
    communication_writer.write_record(
        envelope_1,
        DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id="dispatch_message_401",
            message_id="message_401",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 1),
            target="exec_bridge",
            adapter_name="stub_adapter",
            attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        ),
    )
    communication_writer.write_record(
        envelope_2,
        DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id="dispatch_message_402",
            message_id="message_402",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
            target="exec_bridge",
            adapter_name="stub_adapter",
            attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        ),
    )
    receipt_path = receipt_dir / "2026-04-24" / "exec_bridge"
    receipt_path.mkdir(parents=True, exist_ok=True)
    (receipt_path / "message_401.ack.json").write_text(
        json.dumps(
            {
                "message_id": "message_401",
                "ack_status": "acknowledged",
                "received_at": "2026-04-24T12:00:03",
            }
        ),
        encoding="utf-8",
    )
