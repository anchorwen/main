from datetime import datetime
from pathlib import Path

from apps.engine.runtime_loop import RuntimeLoop
from core.contracts.domain.decision_intent import DecisionIntent
from core.contracts.enums import DecisionAction, DecisionSide, DispatchStatus
from core.ledger.services.communication_inspection_service import CommunicationInspectionService
from core.ledger.services.communication_operations_service import CommunicationOperationsService
from core.ledger.services.communication_record_reader import CommunicationRecordReader
from core.ledger.services.communication_record_writer import CommunicationRecordWriter
from core.ledger.services.communication_replay_gate import CommunicationReplayGate
from core.ledger.services.communication_replay_service import CommunicationReplayService
from core.ledger.services.replay_execution_reader import ReplayExecutionReader
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.ledger.stream_names import (
    LEDGER_STREAM_COMMUNICATIONS,
    LEDGER_STREAM_DECISIONS,
    stream_jsonl_filename,
)
from core.protocol.schema_versions import SCHEMA_DECISION_COMPILER, SCHEMA_DECISION_INTENT
from core.protocol.services.communication_dispatcher import CommunicationDispatcher
from core.protocol.services.intent_message_builder import IntentMessageBuilder
from core.protocol.services.stub_communication_adapter import StubCommunicationAdapter


def build_runtime_intent(event_time: datetime | None = None):
    event_time = event_time or datetime(2026, 4, 24, 12, 0, 0)
    compiled_at = max(event_time, datetime(2026, 4, 24, 12, 0, 1))
    return DecisionIntent(
        schema_version=SCHEMA_DECISION_INTENT,
        intent_id="intent_001",
        candidate_id="candidate_001",
        snapshot_id="snapshot_001",
        event_time=event_time,
        compiled_at=compiled_at,
        symbol="XAUUSD",
        venue="MT5",
        action=DecisionAction.OPEN,
        side=DecisionSide.LONG,
        conviction=0.88,
        priority="high",
        reason_tags=["v9_shadow", "open", "long"],
        trace={"compiler_version": SCHEMA_DECISION_COMPILER},
    )


def build_passive_runtime_intent(event_time: datetime | None = None):
    event_time = event_time or datetime(2026, 4, 24, 12, 0, 0)
    compiled_at = max(event_time, datetime(2026, 4, 24, 12, 0, 1))
    return DecisionIntent(
        schema_version=SCHEMA_DECISION_INTENT,
        intent_id="intent_passive",
        candidate_id="candidate_001",
        snapshot_id="snapshot_001",
        event_time=event_time,
        compiled_at=compiled_at,
        symbol="XAUUSD",
        venue="MT5",
        action=DecisionAction.OBSERVE,
        side=DecisionSide.FLAT,
        conviction=0.5,
        priority="normal",
        reason_tags=["passive"],
        trace={"compiler_version": SCHEMA_DECISION_COMPILER},
    )


def test_runtime_loop_allowed_path_integrates_communication_chain(tmp_path):
    event_time = datetime(2026, 4, 24, 12, 0, 0)
    feature_snapshot = type(
        "FeatureSnapshot",
        (),
        {
            "snapshot_id": "snapshot_001",
            "event_time": event_time,
            "symbol": "XAUUSD",
            "venue": "MT5",
        },
    )()
    first_snapshot = type(
        "ControlSnapshot",
        (),
        {
            "mode_state": type(
                "ModeState", (), {"current_mode": type("Mode", (), {"value": "normal"})()}
            )(),
            "active_overrides": [],
        },
    )()
    second_snapshot = type(
        "ControlSnapshot",
        (),
        {
            "mode_state": type(
                "ModeState", (), {"current_mode": type("Mode", (), {"value": "normal"})()}
            )(),
            "active_overrides": [],
        },
    )()
    candidate = type(
        "Candidate",
        (),
        {
            "regime_state": {"primary_regime": "trend"},
        },
    )()
    record = type("Record", (), {"record_id": "record_001"})()
    expected_operations = {
        "operations_summary": {
            "posture": "action_required",
            "posture_source": "trace.delivery_state.delivery_posture",
            "governance_decision": "allow",
            "governance_posture": "auto_replay",
            "recommended_strategy": "direct_replay_candidate",
            "target_issue_codes": ["dispatch_pending"],
            "review_issue_codes": [],
            "governance_tags": ["auto_replay_eligible"],
            "governance_summary_source": None,
            "execution_projection_source": None,
        }
    }

    class DecisionRecordWriterStub:
        def seed_record(self, **kwargs):
            return record, Path(tmp_path) / "2026-04-24" / stream_jsonl_filename(
                "XAUUSD", LEDGER_STREAM_DECISIONS
            )

    class CommunicationRecordWriterStub:
        def write_record(self, envelope, dispatch_result):
            communication_record = type(
                "CommunicationRecord",
                (),
                {
                    "record_id": "communication_record_001",
                    "message_id": envelope.message_id,
                },
            )()
            return communication_record, Path(tmp_path) / "2026-04-24" / stream_jsonl_filename(
                "exec_bridge", LEDGER_STREAM_COMMUNICATIONS
            )

    class CommunicationOperationsServiceStub:
        def __init__(self):
            self.calls = []

        def get_message_operations_view(self, **kwargs):
            self.calls.append(kwargs)
            return expected_operations

    operations_service = CommunicationOperationsServiceStub()

    runtime_loop = RuntimeLoop(
        control_snapshot_service=type(
            "ControlSnapshotService",
            (),
            {
                "freeze": lambda self, symbol, regime: first_snapshot
                if regime is None
                else second_snapshot,
            },
        )(),
        feature_service=type(
            "FeatureService",
            (),
            {
                "build_snapshot": lambda self, trigger: feature_snapshot,
            },
        )(),
        brain_run_service=type(
            "BrainRunService",
            (),
            {
                "run_active_brains": lambda self,
                feature_snapshot,
                control_snapshot,
                feature_vector=None,
                feature_source=None: ["proposal"],
            },
        )(),
        parliament_adapter=type(
            "ParliamentAdapter",
            (),
            {
                "build_candidate": lambda self,
                feature_snapshot,
                proposals,
                control_snapshot: candidate,
            },
        )(),
        override_resolver=type(
            "OverrideResolver",
            (),
            {
                "resolve": lambda self, symbol, regime, mode, active_overrides: [],
            },
        )(),
        decision_compiler=type(
            "DecisionCompiler",
            (),
            {
                "compile_intent": lambda self,
                candidate,
                mode_state,
                active_overrides: build_runtime_intent(event_time=event_time),
            },
        )(),
        decision_record_writer=DecisionRecordWriterStub(),
        intent_message_builder=IntentMessageBuilder(
            producer="decision_engine", target="exec_bridge"
        ),
        communication_dispatcher=CommunicationDispatcher(
            adapter=StubCommunicationAdapter(),
            clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
        ),
        communication_record_writer=CommunicationRecordWriterStub(),
        communication_operations_service=operations_service,
    )

    result = runtime_loop.run_decision_cycle(
        trigger={"symbol": "XAUUSD"}, feature_source={"f": 1.0}
    )

    assert result.verdict.status.name == "ALLOW"
    assert result.dispatch_result.status == DispatchStatus.PROTOCOL_VALIDATED
    assert result.communication_record.record_id == "communication_record_001"  # type: ignore[reportOptionalMemberAccess]
    assert result.communication_ledger_path.name == stream_jsonl_filename(  # type: ignore[reportOptionalMemberAccess]
        "exec_bridge", LEDGER_STREAM_COMMUNICATIONS
    )
    assert result.communication_operations == expected_operations


def test_runtime_loop_uses_intent_event_date_for_operations_lookup_when_trigger_crosses_midnight(
    tmp_path,
):
    event_time = datetime(2026, 4, 24, 23, 59, 58)
    feature_snapshot = type(
        "FeatureSnapshot",
        (),
        {
            "snapshot_id": "snapshot_001",
            "event_time": datetime(2026, 4, 25, 0, 0, 1),
            "symbol": "XAUUSD",
            "venue": "MT5",
        },
    )()
    first_snapshot = type(
        "ControlSnapshot",
        (),
        {
            "mode_state": type(
                "ModeState", (), {"current_mode": type("Mode", (), {"value": "normal"})()}
            )(),
            "active_overrides": [],
        },
    )()
    second_snapshot = type(
        "ControlSnapshot",
        (),
        {
            "mode_state": type(
                "ModeState", (), {"current_mode": type("Mode", (), {"value": "normal"})()}
            )(),
            "active_overrides": [],
        },
    )()
    candidate = type(
        "Candidate",
        (),
        {
            "regime_state": {"primary_regime": "trend"},
        },
    )()
    record = type("Record", (), {"record_id": "record_001"})()

    class DecisionRecordWriterStub:
        def seed_record(self, **kwargs):
            return record, Path(tmp_path) / "2026-04-25" / stream_jsonl_filename(
                "XAUUSD", LEDGER_STREAM_DECISIONS
            )

    class CommunicationRecordWriterStub:
        def write_record(self, envelope, dispatch_result):
            communication_record = type(
                "CommunicationRecord",
                (),
                {
                    "record_id": "communication_record_001",
                    "message_id": envelope.message_id,
                },
            )()
            return communication_record, Path(tmp_path) / "2026-04-24" / stream_jsonl_filename(
                "exec_bridge", LEDGER_STREAM_COMMUNICATIONS
            )

    class CommunicationOperationsServiceStub:
        def __init__(self):
            self.calls = []

        def get_message_operations_view(self, **kwargs):
            self.calls.append(kwargs)
            return {"operations_summary": {"posture": "action_required"}}

    operations_service = CommunicationOperationsServiceStub()

    runtime_loop = RuntimeLoop(
        control_snapshot_service=type(
            "ControlSnapshotService",
            (),
            {
                "freeze": lambda self, symbol, regime: first_snapshot
                if regime is None
                else second_snapshot,
            },
        )(),
        feature_service=type(
            "FeatureService",
            (),
            {
                "build_snapshot": lambda self, trigger: feature_snapshot,
            },
        )(),
        brain_run_service=type(
            "BrainRunService",
            (),
            {
                "run_active_brains": lambda self,
                feature_snapshot,
                control_snapshot,
                feature_vector=None,
                feature_source=None: ["proposal"],
            },
        )(),
        parliament_adapter=type(
            "ParliamentAdapter",
            (),
            {
                "build_candidate": lambda self,
                feature_snapshot,
                proposals,
                control_snapshot: candidate,
            },
        )(),
        override_resolver=type(
            "OverrideResolver",
            (),
            {
                "resolve": lambda self, symbol, regime, mode, active_overrides: [],
            },
        )(),
        decision_compiler=type(
            "DecisionCompiler",
            (),
            {
                "compile_intent": lambda self,
                candidate,
                mode_state,
                active_overrides: build_runtime_intent(event_time=event_time),
            },
        )(),
        decision_record_writer=DecisionRecordWriterStub(),
        intent_message_builder=IntentMessageBuilder(
            producer="decision_engine", target="exec_bridge"
        ),
        communication_dispatcher=CommunicationDispatcher(
            adapter=StubCommunicationAdapter(),
            clock=lambda: datetime(2026, 4, 25, 0, 0, 2),
        ),
        communication_record_writer=CommunicationRecordWriterStub(),
        communication_operations_service=operations_service,
    )

    runtime_loop.run_decision_cycle(trigger={"symbol": "XAUUSD"}, feature_source={"f": 1.0})

    assert len(operations_service.calls) == 1
    assert operations_service.calls[0]["date_key"] == "2026-04-24"
    assert operations_service.calls[0]["target"] == "exec_bridge"
    assert operations_service.calls[0]["message_id"].startswith("message_")


def test_runtime_loop_falls_back_to_record_summary_when_operations_view_is_unavailable(tmp_path):
    event_time = datetime(2026, 4, 24, 12, 0, 0)
    feature_snapshot = type(
        "FeatureSnapshot",
        (),
        {
            "snapshot_id": "snapshot_001",
            "event_time": event_time,
            "symbol": "XAUUSD",
            "venue": "MT5",
        },
    )()
    first_snapshot = type(
        "ControlSnapshot",
        (),
        {
            "mode_state": type(
                "ModeState", (), {"current_mode": type("Mode", (), {"value": "normal"})()}
            )(),
            "active_overrides": [],
        },
    )()
    second_snapshot = type(
        "ControlSnapshot",
        (),
        {
            "mode_state": type(
                "ModeState", (), {"current_mode": type("Mode", (), {"value": "normal"})()}
            )(),
            "active_overrides": [],
        },
    )()
    candidate = type(
        "Candidate",
        (),
        {
            "regime_state": {"primary_regime": "trend"},
        },
    )()
    record = type("Record", (), {"record_id": "record_001"})()

    class DecisionRecordWriterStub:
        def seed_record(self, **kwargs):
            return record, Path(tmp_path) / "2026-04-24" / stream_jsonl_filename(
                "XAUUSD", LEDGER_STREAM_DECISIONS
            )

    class CommunicationRecordWriterStub:
        def write_record(self, envelope, dispatch_result):
            communication_record = type(
                "CommunicationRecord",
                (),
                {
                    "record_id": "communication_record_001",
                    "message_id": envelope.message_id,
                },
            )()
            return communication_record, Path(tmp_path) / "2026-04-24" / stream_jsonl_filename(
                "exec_bridge", LEDGER_STREAM_COMMUNICATIONS
            )

    class CommunicationOperationsServiceStub:
        def get_message_operations_view(self, **kwargs):
            return None

    runtime_loop = RuntimeLoop(
        control_snapshot_service=type(
            "ControlSnapshotService",
            (),
            {
                "freeze": lambda self, symbol, regime: first_snapshot
                if regime is None
                else second_snapshot,
            },
        )(),
        feature_service=type(
            "FeatureService",
            (),
            {
                "build_snapshot": lambda self, trigger: feature_snapshot,
            },
        )(),
        brain_run_service=type(
            "BrainRunService",
            (),
            {
                "run_active_brains": lambda self,
                feature_snapshot,
                control_snapshot,
                feature_vector=None,
                feature_source=None: ["proposal"],
            },
        )(),
        parliament_adapter=type(
            "ParliamentAdapter",
            (),
            {
                "build_candidate": lambda self,
                feature_snapshot,
                proposals,
                control_snapshot: candidate,
            },
        )(),
        override_resolver=type(
            "OverrideResolver",
            (),
            {
                "resolve": lambda self, symbol, regime, mode, active_overrides: [],
            },
        )(),
        decision_compiler=type(
            "DecisionCompiler",
            (),
            {
                "compile_intent": lambda self,
                candidate,
                mode_state,
                active_overrides: build_runtime_intent(event_time=event_time),
            },
        )(),
        decision_record_writer=DecisionRecordWriterStub(),
        intent_message_builder=IntentMessageBuilder(
            producer="decision_engine", target="exec_bridge"
        ),
        communication_dispatcher=CommunicationDispatcher(
            adapter=StubCommunicationAdapter(),
            clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
        ),
        communication_record_writer=CommunicationRecordWriterStub(),
        communication_operations_service=CommunicationOperationsServiceStub(),
    )

    result = runtime_loop.run_decision_cycle(
        trigger={"symbol": "XAUUSD"}, feature_source={"f": 1.0}
    )

    assert result.communication_record.record_id == "communication_record_001"  # type: ignore[reportOptionalMemberAccess]
    assert result.communication_ledger_path.name == stream_jsonl_filename(  # type: ignore[reportOptionalMemberAccess]
        "exec_bridge", LEDGER_STREAM_COMMUNICATIONS
    )
    assert result.communication_operations is None


def test_runtime_loop_skips_communication_when_intent_not_actionable(tmp_path):
    event_time = datetime(2026, 4, 24, 12, 0, 0)
    feature_snapshot = type(
        "FeatureSnapshot",
        (),
        {
            "snapshot_id": "snapshot_001",
            "event_time": event_time,
            "symbol": "XAUUSD",
            "venue": "MT5",
        },
    )()
    first_snapshot = type(
        "ControlSnapshot",
        (),
        {
            "mode_state": type(
                "ModeState", (), {"current_mode": type("Mode", (), {"value": "normal"})()}
            )(),
            "active_overrides": [],
        },
    )()
    second_snapshot = type(
        "ControlSnapshot",
        (),
        {
            "mode_state": type(
                "ModeState", (), {"current_mode": type("Mode", (), {"value": "normal"})()}
            )(),
            "active_overrides": [],
        },
    )()
    candidate = type(
        "Candidate",
        (),
        {
            "regime_state": {"primary_regime": "trend"},
        },
    )()
    record = type("Record", (), {"record_id": "record_001"})()

    class DecisionRecordWriterStub:
        def seed_record(self, **kwargs):
            return record, Path(tmp_path) / "2026-04-24" / stream_jsonl_filename(
                "XAUUSD", LEDGER_STREAM_DECISIONS
            )

    class CommunicationRecordWriterFailIfCalled:
        def write_record(self, envelope, dispatch_result):
            raise AssertionError("communication writer must not run for passive intents")

    class CommunicationOperationsServiceFailIfCalled:
        def get_message_operations_view(self, **kwargs):
            raise AssertionError("operations service must not run for passive intents")

    runtime_loop = RuntimeLoop(
        control_snapshot_service=type(
            "ControlSnapshotService",
            (),
            {
                "freeze": lambda self, symbol, regime: first_snapshot
                if regime is None
                else second_snapshot,
            },
        )(),
        feature_service=type(
            "FeatureService",
            (),
            {
                "build_snapshot": lambda self, trigger: feature_snapshot,
            },
        )(),
        brain_run_service=type(
            "BrainRunService",
            (),
            {
                "run_active_brains": lambda self,
                feature_snapshot,
                control_snapshot,
                feature_vector=None,
                feature_source=None: ["proposal"],
            },
        )(),
        parliament_adapter=type(
            "ParliamentAdapter",
            (),
            {
                "build_candidate": lambda self,
                feature_snapshot,
                proposals,
                control_snapshot: candidate,
            },
        )(),
        override_resolver=type(
            "OverrideResolver",
            (),
            {
                "resolve": lambda self, symbol, regime, mode, active_overrides: [],
            },
        )(),
        decision_compiler=type(
            "DecisionCompiler",
            (),
            {
                "compile_intent": lambda self,
                candidate,
                mode_state,
                active_overrides: build_passive_runtime_intent(
                    event_time=event_time,
                ),
            },
        )(),
        decision_record_writer=DecisionRecordWriterStub(),
        intent_message_builder=IntentMessageBuilder(
            producer="decision_engine", target="exec_bridge"
        ),
        communication_dispatcher=CommunicationDispatcher(
            adapter=StubCommunicationAdapter(),
            clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
        ),
        communication_record_writer=CommunicationRecordWriterFailIfCalled(),
        communication_operations_service=CommunicationOperationsServiceFailIfCalled(),
    )

    result = runtime_loop.run_decision_cycle(
        trigger={"symbol": "XAUUSD"}, feature_source={"f": 1.0}
    )

    assert result.verdict.status.name == "DENY"
    assert result.dispatch_result["status"] == "skipped"
    assert result.communication_record is None
    assert result.communication_ledger_path is None
    assert result.communication_operations is None


def test_runtime_loop_end_to_end_communication_stack_matches_operations_view(tmp_path):
    event_time = datetime(2026, 4, 24, 12, 0, 0)
    feature_snapshot = type(
        "FeatureSnapshot",
        (),
        {
            "snapshot_id": "snapshot_001",
            "event_time": event_time,
            "symbol": "XAUUSD",
            "venue": "MT5",
        },
    )()
    first_snapshot = type(
        "ControlSnapshot",
        (),
        {
            "mode_state": type(
                "ModeState", (), {"current_mode": type("Mode", (), {"value": "normal"})()}
            )(),
            "active_overrides": [],
        },
    )()
    second_snapshot = type(
        "ControlSnapshot",
        (),
        {
            "mode_state": type(
                "ModeState", (), {"current_mode": type("Mode", (), {"value": "normal"})()}
            )(),
            "active_overrides": [],
        },
    )()
    candidate = type(
        "Candidate",
        (),
        {
            "regime_state": {"primary_regime": "trend"},
        },
    )()
    record = type("Record", (), {"record_id": "record_001"})()

    class DecisionRecordWriterStub:
        def seed_record(self, **kwargs):
            return record, Path(tmp_path) / "2026-04-24" / stream_jsonl_filename(
                "XAUUSD", LEDGER_STREAM_DECISIONS
            )

    store = JsonlLedgerStore(str(tmp_path))
    communication_writer = CommunicationRecordWriter(ledger_store=store)
    communication_reader = CommunicationRecordReader(base_dir=str(tmp_path))
    replay_reader = ReplayExecutionReader(base_dir=str(tmp_path))
    inspection = CommunicationInspectionService(
        record_reader=communication_reader, receipt_reader=None
    )
    replay_service = CommunicationReplayService(inspection_service=inspection)
    replay_gate = CommunicationReplayGate()
    operations = CommunicationOperationsService(
        communication_reader=communication_reader,
        inspection_service=inspection,
        replay_service=replay_service,
        replay_gate=replay_gate,
        replay_reader=replay_reader,
        receipt_reader=None,
    )

    runtime_loop = RuntimeLoop(
        control_snapshot_service=type(
            "ControlSnapshotService",
            (),
            {
                "freeze": lambda self, symbol, regime: first_snapshot
                if regime is None
                else second_snapshot,
            },
        )(),
        feature_service=type(
            "FeatureService",
            (),
            {
                "build_snapshot": lambda self, trigger: feature_snapshot,
            },
        )(),
        brain_run_service=type(
            "BrainRunService",
            (),
            {
                "run_active_brains": lambda self,
                feature_snapshot,
                control_snapshot,
                feature_vector=None,
                feature_source=None: ["proposal"],
            },
        )(),
        parliament_adapter=type(
            "ParliamentAdapter",
            (),
            {
                "build_candidate": lambda self,
                feature_snapshot,
                proposals,
                control_snapshot: candidate,
            },
        )(),
        override_resolver=type(
            "OverrideResolver",
            (),
            {
                "resolve": lambda self, symbol, regime, mode, active_overrides: [],
            },
        )(),
        decision_compiler=type(
            "DecisionCompiler",
            (),
            {
                "compile_intent": lambda self,
                candidate,
                mode_state,
                active_overrides: build_runtime_intent(event_time=event_time),
            },
        )(),
        decision_record_writer=DecisionRecordWriterStub(),
        intent_message_builder=IntentMessageBuilder(
            producer="decision_engine", target="exec_bridge"
        ),
        communication_dispatcher=CommunicationDispatcher(
            adapter=StubCommunicationAdapter(),
            clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
        ),
        communication_record_writer=communication_writer,
        communication_operations_service=operations,
    )

    result = runtime_loop.run_decision_cycle(
        trigger={"symbol": "XAUUSD"}, feature_source={"f": 1.0}
    )

    assert result.verdict.status.name == "ALLOW"
    assert result.dispatch_result.status == DispatchStatus.PROTOCOL_VALIDATED
    msg_id = result.communication_record.message_id  # type: ignore[reportOptionalMemberAccess]
    assert result.communication_ledger_path.exists()  # type: ignore[reportOptionalMemberAccess]

    roundtrip = communication_reader.find_by_message_id(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id=msg_id,
    )
    assert roundtrip is not None
    assert roundtrip["message_id"] == msg_id

    view = result.communication_operations
    assert view is not None
    assert view["record"]["message_id"] == msg_id
    assert view["replay_plan"] is not None
    assert view["replay_gate"] is not None
