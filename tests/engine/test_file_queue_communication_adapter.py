import json
from datetime import datetime, timedelta

from apps.engine.communication_ops_cli import run_cli
from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.decision_intent import DecisionIntent
from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import (
    CommunicationMessageType,
    CommunicationPriority,
    DecisionAction,
    DecisionSide,
    DispatchStatus,
)
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
from core.protocol.schema_versions import (
    SCHEMA_COMMUNICATION_ENVELOPE,
    SCHEMA_DECISION_COMPILER,
    SCHEMA_DECISION_INTENT,
    SCHEMA_DISPATCH_RESULT,
)
from core.protocol.services.communication_adapter_registry import CommunicationAdapterRegistry
from core.protocol.services.communication_dispatcher import CommunicationDispatcher
from core.protocol.services.file_queue_communication_adapter import FileQueueCommunicationAdapter
from core.protocol.services.file_queue_receipt_reader import FileQueueReceiptReader
from core.protocol.services.intent_message_builder import IntentMessageBuilder
from core.protocol.services.stub_communication_adapter import StubCommunicationAdapter


class NamedStubAdapter(StubCommunicationAdapter):
    pass


def build_intent(priority: str = "normal"):
    return DecisionIntent(
        schema_version=SCHEMA_DECISION_INTENT,
        intent_id="intent_001",
        candidate_id="candidate_001",
        snapshot_id="snapshot_001",
        event_time=datetime(2026, 4, 24, 12, 0, 0),
        compiled_at=datetime(2026, 4, 24, 12, 0, 1),
        symbol="XAUUSD",
        venue="MT5",
        action=DecisionAction.OPEN,
        side=DecisionSide.LONG,
        conviction=0.82,
        priority=priority,
        suggested_risk_fraction=0.01,
        expected_edge_bps=15.0,
        expected_hold_seconds=120,
        reason_tags=["v9_shadow", "open", "long"],
        trace={"compiler_version": SCHEMA_DECISION_COMPILER},
        extensions={"source": "test"},
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


def build_result(message_id: str, *, status=DispatchStatus.PROTOCOL_VALIDATED):
    return DispatchResult(
        schema_version=SCHEMA_DISPATCH_RESULT,
        dispatch_id=f"dispatch_{message_id}",
        message_id=message_id,
        status=status,
        recorded_at=datetime(2026, 4, 24, 12, 0, 1),
        target="exec_bridge",
        adapter_name="stub_adapter",
        attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
    )


def write_receipt(
    receipt_dir, *, message_id: str, target: str = "exec_bridge", ack_status: str = "acknowledged"
):
    receipt_path = receipt_dir / "2026-04-24" / target
    receipt_path.mkdir(parents=True, exist_ok=True)
    target_file = receipt_path / f"{message_id}.ack.json"
    target_file.write_text(
        json.dumps(
            {
                "message_id": message_id,
                "ack_status": ack_status,
                "received_at": "2026-04-24T12:00:03",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return target_file


def test_file_queue_receipt_reader_reads_ack_file(tmp_path):
    receipt_dir = tmp_path / "receipts"
    write_receipt(receipt_dir, message_id="message_001")
    reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))

    receipt = reader.find_by_message_id(
        date_key="2026-04-24", target="exec_bridge", message_id="message_001"
    )

    assert receipt is not None
    assert receipt["message_id"] == "message_001"
    assert receipt["ack_status"] == "acknowledged"


def test_operations_service_message_view_includes_receipt_and_receipt_aware_trace(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    receipt_dir = tmp_path / "receipts"
    writer = CommunicationRecordWriter(ledger_store=store)
    communication_reader = CommunicationRecordReader(base_dir=str(tmp_path))
    replay_reader = ReplayExecutionReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir))
    inspection = CommunicationInspectionService(
        record_reader=communication_reader, receipt_reader=receipt_reader
    )
    replay_service = CommunicationReplayService(inspection_service=inspection)
    replay_gate = CommunicationReplayGate()
    operations = CommunicationOperationsService(
        communication_reader=communication_reader,
        inspection_service=inspection,
        replay_service=replay_service,
        replay_gate=replay_gate,
        replay_reader=replay_reader,
        receipt_reader=receipt_reader,
    )

    envelope = build_envelope("message_ops", "corr_ops")
    writer.write_record(
        envelope, build_result("message_ops", status=DispatchStatus.TRANSPORT_DELIVERED)
    )
    write_receipt(receipt_dir, message_id="message_ops")

    view = operations.get_message_operations_view(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_ops",
    )

    assert view["receipt"] is not None
    assert view["receipt"]["ack_status"] == "acknowledged"
    assert view["trace"]["delivery_state"]["phase"] == "receipt_acknowledged"
    assert view["trace"]["delivery_state"]["delivery_posture"] == "healthy"


def test_cli_message_view_includes_receipt_when_receipt_dir_provided(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    receipt_dir = tmp_path / "receipts"
    envelope = build_envelope("message_cli", "corr_cli")
    writer.write_record(envelope, build_result("message_cli"))
    write_receipt(receipt_dir, message_id="message_cli")

    output = run_cli(
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
            "message_cli",
        ]
    )
    payload = json.loads(output)

    assert payload["receipt"] is not None
    assert payload["receipt"]["message_id"] == "message_cli"
    assert payload["replay_gate"]["decision"] == "allow"


def test_file_queue_adapter_writes_outbox_message(tmp_path):
    outbox_dir = tmp_path / "outbox"
    adapter = FileQueueCommunicationAdapter(outbox_dir=str(outbox_dir))
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    intent = build_intent()
    envelope = builder.build(intent, correlation_id="corr_001")
    dispatcher = CommunicationDispatcher(
        adapter=adapter, clock=lambda: datetime(2026, 4, 24, 12, 0, 2)
    )

    result = dispatcher.dispatch(envelope)

    assert result.status == DispatchStatus.TRANSPORT_DELIVERED
    outbox_path = outbox_dir / "2026-04-24" / "exec_bridge" / f"{envelope.message_id}.json"
    assert outbox_path.exists()
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["envelope"]["message_id"] == envelope.message_id
    assert payload["request"]["dispatch_id"] == result.dispatch_id


def test_dispatcher_routes_to_file_queue_adapter_via_registry(tmp_path):
    outbox_dir = tmp_path / "outbox"
    adapter = FileQueueCommunicationAdapter(
        outbox_dir=str(outbox_dir), adapter_name="file_queue_primary"
    )
    registry = CommunicationAdapterRegistry(
        adapters={
            "exec_bridge": adapter,
            "default": NamedStubAdapter(adapter_name="default_adapter"),
        },
        default_adapter_name="default",
    )
    dispatcher = CommunicationDispatcher(
        adapter_registry=registry,
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    envelope = builder.build(build_intent(), correlation_id="corr_002")

    result = dispatcher.dispatch(envelope)

    assert result.adapter_name == "file_queue_primary"
    assert result.status == DispatchStatus.TRANSPORT_DELIVERED
    assert (outbox_dir / "2026-04-24" / "exec_bridge" / f"{envelope.message_id}.json").exists()


def test_replay_executor_can_dispatch_to_file_queue_adapter(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    outbox_dir = tmp_path / "outbox"
    communication_writer = CommunicationRecordWriter(ledger_store=store)
    replay_writer = ReplayExecutionWriter(ledger_store=store)
    communication_reader = CommunicationRecordReader(base_dir=str(tmp_path))
    inspection = CommunicationInspectionService(record_reader=communication_reader)
    replay_service = CommunicationReplayService(inspection_service=inspection)
    replay_gate = CommunicationReplayGate()
    dispatcher = CommunicationDispatcher(
        adapter=FileQueueCommunicationAdapter(outbox_dir=str(outbox_dir)),
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    executor = CommunicationReplayExecutor(
        replay_gate=replay_gate,
        dispatcher=dispatcher,
        replay_execution_writer=replay_writer,
    )

    envelope = build_envelope("message_replay", "corr_replay")
    communication_writer.write_record(envelope, build_result("message_replay"))
    replay_plan = replay_service.build_message_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_replay",
    )

    result = executor.execute_message_replay(replay_plan, envelope)

    assert result["status"] == "executed"
    assert result["dispatch_result"].adapter_name == "file_queue_adapter"
    assert (outbox_dir / "2026-04-24" / "exec_bridge" / "message_replay.json").exists()


def test_cli_message_view_still_works_with_file_queue_written_record(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    outbox_dir = tmp_path / "outbox"
    adapter = FileQueueCommunicationAdapter(outbox_dir=str(outbox_dir))
    dispatcher = CommunicationDispatcher(
        adapter=adapter, clock=lambda: datetime(2026, 4, 24, 12, 0, 2)
    )
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    envelope = builder.build(build_intent(), correlation_id="corr_cli")

    dispatch_result = dispatcher.dispatch(envelope)
    writer.write_record(envelope, dispatch_result)

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
            envelope.message_id,
        ]
    )
    payload = json.loads(output)

    assert payload["record"]["message_id"] == envelope.message_id
    assert payload["trace"]["adapter_name"] == "file_queue_adapter"
    assert payload["replay_gate"]["decision"] == "allow"
