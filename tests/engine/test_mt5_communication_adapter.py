import json
from datetime import datetime

from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.decision_intent import DecisionIntent
from core.contracts.enums import (
    CommunicationMessageType,
    CommunicationPriority,
    DecisionAction,
    DecisionSide,
    DispatchStatus,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer
from core.protocol.schema_versions import SCHEMA_COMMUNICATION_ENVELOPE, SCHEMA_DECISION_INTENT
from core.protocol.services.communication_dispatcher import CommunicationDispatcher
from core.protocol.services.intent_message_builder import IntentMessageBuilder
from core.protocol.services.mt5_communication_adapter import MT5CommunicationAdapter


def _build_intent():
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
        priority="normal",
        suggested_risk_fraction=0.01,
        expected_edge_bps=15.0,
        expected_hold_seconds=120,
        reason_tags=["test"],
    )


def test_mt5_communication_adapter_writes_mt5_outbox(tmp_path):
    terminal = tmp_path / "terminal64.exe"
    terminal.write_text("", encoding="utf-8")
    outbox_dir = tmp_path / "mt5_outbox"
    adapter = MT5CommunicationAdapter(terminal_path=str(terminal), outbox_dir=str(outbox_dir))
    dispatcher = CommunicationDispatcher(
        adapter=adapter, clock=lambda: datetime(2026, 4, 24, 12, 0, 2)
    )
    envelope = IntentMessageBuilder(producer="decision_engine", target="exec_bridge").build(
        _build_intent(), correlation_id="corr_001"
    )

    result = dispatcher.dispatch(envelope)

    assert result.status == DispatchStatus.TRANSPORT_DELIVERED
    assert result.adapter_name == "mt5_adapter"
    outbox_path = outbox_dir / "2026-04-24" / "exec_bridge" / f"{envelope.message_id}.mt5.json"
    assert outbox_path.exists()
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["mt5"]["terminal_path"] == str(terminal)


def test_service_container_uses_mt5_adapter_when_configured(tmp_path):
    terminal = tmp_path / "terminal64.exe"
    terminal.write_text("", encoding="utf-8")
    outbox_dir = tmp_path / "mt5_outbox"
    cfg = EnvironmentConfig.production(
        base_dir=str(tmp_path / "data"),
        adapter_name="mt5",
        live_dispatch_enabled=True,
        live_allowed_symbols=("XAUUSD",),
        extensions={
            "mt5_terminal_path": str(terminal),
            "mt5_outbox_dir": str(outbox_dir),
        },
    )
    container = ServiceContainer(cfg).build()
    envelope = CommunicationEnvelope(
        schema_version=SCHEMA_COMMUNICATION_ENVELOPE,
        message_id="message_mt5_001",
        correlation_id="corr_mt5_001",
        causation_id=None,
        event_time=datetime(2026, 4, 24, 12, 0, 0),
        producer="decision_engine",
        target="exec_bridge",
        message_type=CommunicationMessageType.DECISION_INTENT,
        priority=CommunicationPriority.NORMAL,
        payload={"intent_id": "message_mt5_001", "symbol": "XAUUSD"},
    )

    result = container.dispatcher.dispatch(envelope)

    assert result.adapter_name == "mt5_adapter"
    assert result.status == DispatchStatus.TRANSPORT_DELIVERED
