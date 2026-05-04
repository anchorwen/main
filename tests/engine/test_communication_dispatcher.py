from datetime import datetime, timedelta

from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.decision_intent import DecisionIntent
from core.contracts.enums import (
    CommunicationMessageType,
    CommunicationPriority,
    DecisionAction,
    DecisionSide,
    DispatchStatus,
)
from core.deployment.domain_keys import (
    DISPATCH_FAILURE_REASON_LIVE_DISPATCH_DISABLED,
    DISPATCH_FAILURE_REASON_LIVE_READ_ONLY,
    DISPATCH_FAILURE_REASON_SYMBOL_NOT_LIVE_ENABLED,
)
from core.protocol.schema_versions import (
    SCHEMA_COMMUNICATION_ENVELOPE,
    SCHEMA_DECISION_COMPILER,
    SCHEMA_DECISION_INTENT,
    SCHEMA_INTENT_MESSAGE_BUILDER,
)
from core.protocol.services.communication_adapter_registry import CommunicationAdapterRegistry
from core.protocol.services.communication_dispatcher import CommunicationDispatcher
from core.protocol.services.intent_message_builder import IntentMessageBuilder
from core.protocol.services.stub_communication_adapter import StubCommunicationAdapter


class NamedStubAdapter(StubCommunicationAdapter):
    pass


class FailingAdapter:
    def __init__(
        self, adapter_name: str = "failing_adapter", error_message: str = "dispatch failed"
    ):
        self.adapter_name = adapter_name
        self._error_message = error_message

    def dispatch(self, request, envelope):
        raise RuntimeError(self._error_message)


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


def build_expired_envelope():
    return CommunicationEnvelope(
        schema_version=SCHEMA_COMMUNICATION_ENVELOPE,
        message_id="message_expired",
        correlation_id="corr_expired",
        causation_id="intent_expired",
        event_time=datetime(2026, 4, 24, 12, 0, 0),
        producer="decision_engine",
        target="exec_bridge",
        message_type=CommunicationMessageType.DECISION_INTENT,
        priority=CommunicationPriority.NORMAL,
        payload={"intent_id": "intent_expired"},
        deadline_at=datetime(2026, 4, 24, 12, 0, 1),
    )


def test_intent_message_builder_builds_decision_intent_envelope():
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    intent = build_intent(priority="normal")

    envelope = builder.build(intent, correlation_id="corr_001")

    assert envelope.correlation_id == "corr_001"
    assert envelope.causation_id == "intent_001"
    assert envelope.message_type == CommunicationMessageType.DECISION_INTENT
    assert envelope.priority == CommunicationPriority.NORMAL
    assert envelope.idempotency_key == "intent_001"
    assert envelope.payload["action"] == DecisionAction.OPEN
    assert envelope.trace["builder_version"] == SCHEMA_INTENT_MESSAGE_BUILDER


def test_intent_message_builder_maps_high_priority_to_high():
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    intent = build_intent(priority="high")

    envelope = builder.build(intent, correlation_id="corr_002", causation_id="record_001")

    assert envelope.priority == CommunicationPriority.HIGH
    assert envelope.causation_id == "record_001"


def test_communication_dispatcher_uses_direct_stub_adapter_and_returns_result():
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    dispatcher = CommunicationDispatcher(
        adapter=StubCommunicationAdapter(),
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    intent = build_intent()
    envelope = builder.build(intent, correlation_id="corr_003")

    result = dispatcher.dispatch(
        envelope,
        route_policy={"channel": "primary"},
        transport_hints={"mode": "stub"},
        governance={"system_mode": "normal"},
    )

    assert result.dispatch_id.startswith("dispatch_")
    assert result.message_id == envelope.message_id
    assert result.status == DispatchStatus.PROTOCOL_VALIDATED
    assert result.target == "exec_bridge"
    assert result.transport_metadata["stub"] is True
    assert result.protocol_metadata["validated"] is True
    assert result.adapter_name == "stub_adapter"
    assert result.attempts[0]["adapter_name"] == "stub_adapter"
    assert result.attempts[0]["status"] == "succeeded"


def test_communication_dispatcher_blocks_dispatch_when_live_read_only_enabled():
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    dispatcher = CommunicationDispatcher(
        adapter=StubCommunicationAdapter(),
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
        live_read_only=True,
    )
    envelope = builder.build(build_intent(), correlation_id="corr_read_only")

    result = dispatcher.dispatch(envelope)

    assert result.status == DispatchStatus.FAILED
    assert result.adapter_name == "live_read_only_guard"
    assert result.failure_reason == DISPATCH_FAILURE_REASON_LIVE_READ_ONLY
    assert result.attempts == [
        {
            "adapter_name": "live_read_only_guard",
            "status": "failed",
            "reason": DISPATCH_FAILURE_REASON_LIVE_READ_ONLY,
        }
    ]
    assert result.trace["live_read_only"] is True


def test_communication_dispatcher_blocks_dispatch_when_live_dispatch_disabled():
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    dispatcher = CommunicationDispatcher(
        adapter=StubCommunicationAdapter(),
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
        live_dispatch_enabled=False,
    )
    envelope = builder.build(build_intent(), correlation_id="corr_gate_disabled")

    result = dispatcher.dispatch(envelope)

    assert result.status == DispatchStatus.FAILED
    assert result.adapter_name == "live_dispatch_gate"
    assert result.failure_reason == DISPATCH_FAILURE_REASON_LIVE_DISPATCH_DISABLED
    assert result.trace["live_dispatch_enabled"] is False
    assert result.trace["symbol"] == "XAUUSD"


def test_communication_dispatcher_blocks_dispatch_for_symbol_not_in_allowlist():
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    dispatcher = CommunicationDispatcher(
        adapter=StubCommunicationAdapter(),
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
        live_dispatch_enabled=True,
        live_allowed_symbols=("EURUSD",),
    )
    envelope = builder.build(build_intent(), correlation_id="corr_symbol_gate")

    result = dispatcher.dispatch(envelope)

    assert result.status == DispatchStatus.FAILED
    assert result.adapter_name == "live_symbol_gate"
    assert result.failure_reason == DISPATCH_FAILURE_REASON_SYMBOL_NOT_LIVE_ENABLED
    assert result.trace["symbol"] == "XAUUSD"
    assert result.trace["live_allowed_symbols"] == ["EURUSD"]


def test_communication_dispatcher_routes_by_target_via_registry():
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    registry = CommunicationAdapterRegistry(
        adapters={
            "exec_bridge": NamedStubAdapter(adapter_name="exec_adapter"),
            "default": NamedStubAdapter(adapter_name="default_adapter"),
        },
        default_adapter_name="default",
    )
    dispatcher = CommunicationDispatcher(
        adapter_registry=registry,
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    envelope = builder.build(build_intent(), correlation_id="corr_004")

    result = dispatcher.dispatch(envelope)

    assert result.adapter_name == "exec_adapter"
    assert result.target == "exec_bridge"


def test_communication_dispatcher_routes_by_message_type_when_target_missing():
    builder = IntentMessageBuilder(producer="decision_engine", target="unmapped_target")
    registry = CommunicationAdapterRegistry(
        adapters={
            CommunicationMessageType.DECISION_INTENT.value: NamedStubAdapter(
                adapter_name="intent_adapter"
            ),
        }
    )
    dispatcher = CommunicationDispatcher(
        adapter_registry=registry,
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    envelope = builder.build(build_intent(), correlation_id="corr_005")

    result = dispatcher.dispatch(envelope)

    assert result.adapter_name == "intent_adapter"


def test_communication_dispatcher_uses_default_registry_adapter_when_no_exact_match():
    builder = IntentMessageBuilder(producer="decision_engine", target="unknown_target")
    registry = CommunicationAdapterRegistry(
        adapters={
            "default": NamedStubAdapter(adapter_name="default_adapter"),
        },
        default_adapter_name="default",
    )
    dispatcher = CommunicationDispatcher(
        adapter_registry=registry,
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    envelope = builder.build(build_intent(), correlation_id="corr_006")

    result = dispatcher.dispatch(envelope)

    assert result.adapter_name == "default_adapter"


def test_communication_dispatcher_prefers_explicit_adapter_from_route_policy():
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    registry = CommunicationAdapterRegistry(
        adapters={
            "exec_bridge": NamedStubAdapter(adapter_name="exec_adapter"),
            "backup_adapter": NamedStubAdapter(adapter_name="backup_adapter"),
        }
    )
    dispatcher = CommunicationDispatcher(
        adapter_registry=registry,
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    envelope = builder.build(build_intent(), correlation_id="corr_007")

    result = dispatcher.dispatch(envelope, route_policy={"adapter": "backup_adapter"})

    assert result.adapter_name == "backup_adapter"


def test_communication_dispatcher_routes_by_channel_before_target():
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    registry = CommunicationAdapterRegistry(
        adapters={
            "channel:backup": NamedStubAdapter(adapter_name="backup_channel_adapter"),
            "exec_bridge": NamedStubAdapter(adapter_name="exec_adapter"),
        }
    )
    dispatcher = CommunicationDispatcher(
        adapter_registry=registry,
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    envelope = builder.build(build_intent(), correlation_id="corr_008")

    result = dispatcher.dispatch(envelope, route_policy={"channel": "backup"})

    assert result.adapter_name == "backup_channel_adapter"


def test_communication_dispatcher_routes_to_degraded_adapter_from_governance():
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    registry = CommunicationAdapterRegistry(
        adapters={
            "mode:degraded": NamedStubAdapter(adapter_name="degraded_adapter"),
            "exec_bridge": NamedStubAdapter(adapter_name="exec_adapter"),
        }
    )
    dispatcher = CommunicationDispatcher(
        adapter_registry=registry,
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    envelope = builder.build(build_intent(), correlation_id="corr_009")

    result = dispatcher.dispatch(envelope, governance={"system_mode": "degraded"})

    assert result.adapter_name == "degraded_adapter"


def test_communication_dispatcher_routes_by_transport_hint_when_present():
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    registry = CommunicationAdapterRegistry(
        adapters={
            "transport:sync": NamedStubAdapter(adapter_name="sync_adapter"),
            "exec_bridge": NamedStubAdapter(adapter_name="exec_adapter"),
        }
    )
    dispatcher = CommunicationDispatcher(
        adapter_registry=registry,
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    envelope = builder.build(build_intent(), correlation_id="corr_010")

    result = dispatcher.dispatch(envelope, transport_hints={"mode": "sync"})

    assert result.adapter_name == "sync_adapter"


def test_communication_dispatcher_degrades_to_fallback_adapter_when_primary_fails():
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    registry = CommunicationAdapterRegistry(
        adapters={
            "exec_bridge": FailingAdapter(
                adapter_name="exec_adapter", error_message="primary down"
            ),
            "backup_adapter": NamedStubAdapter(adapter_name="backup_adapter"),
        }
    )
    dispatcher = CommunicationDispatcher(
        adapter_registry=registry,
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    envelope = builder.build(build_intent(), correlation_id="corr_011")

    result = dispatcher.dispatch(envelope, route_policy={"fallback_adapter": "backup_adapter"})

    assert result.status == DispatchStatus.DEGRADED
    assert result.adapter_name == "backup_adapter"
    assert result.fallback_adapter_name == "backup_adapter"
    assert result.degrade_reason == "primary down"
    assert result.trace["failed_adapter"] == "exec_adapter"
    assert result.attempts == [
        {"adapter_name": "exec_adapter", "status": "failed", "reason": "primary down"},
        {"adapter_name": "backup_adapter", "status": "degraded", "reason": "fallback_success"},
    ]


def test_communication_dispatcher_returns_failed_result_when_no_fallback_available():
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    registry = CommunicationAdapterRegistry(
        adapters={
            "exec_bridge": FailingAdapter(
                adapter_name="exec_adapter", error_message="primary down"
            ),
        }
    )
    dispatcher = CommunicationDispatcher(
        adapter_registry=registry,
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    envelope = builder.build(build_intent(), correlation_id="corr_012")

    result = dispatcher.dispatch(envelope)

    assert result.status == DispatchStatus.FAILED
    assert result.adapter_name == "exec_adapter"
    assert result.failure_reason == "primary down"
    assert result.attempts == [
        {"adapter_name": "exec_adapter", "status": "failed", "reason": "primary down"},
    ]


def test_communication_dispatcher_returns_failed_result_when_fallback_also_fails():
    builder = IntentMessageBuilder(producer="decision_engine", target="exec_bridge")
    registry = CommunicationAdapterRegistry(
        adapters={
            "exec_bridge": FailingAdapter(
                adapter_name="exec_adapter", error_message="primary down"
            ),
            "backup_adapter": FailingAdapter(
                adapter_name="backup_adapter", error_message="fallback down"
            ),
        }
    )
    dispatcher = CommunicationDispatcher(
        adapter_registry=registry,
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    envelope = builder.build(build_intent(), correlation_id="corr_013")

    result = dispatcher.dispatch(envelope, route_policy={"fallback_adapter": "backup_adapter"})

    assert result.status == DispatchStatus.FAILED
    assert result.adapter_name == "exec_adapter"
    assert result.fallback_adapter_name == "backup_adapter"
    assert "primary=primary down" in result.failure_reason
    assert "fallback=fallback down" in result.failure_reason
    assert result.attempts == [
        {"adapter_name": "exec_adapter", "status": "failed", "reason": "primary down"},
        {"adapter_name": "backup_adapter", "status": "failed", "reason": "fallback down"},
    ]


def test_communication_dispatcher_fails_fast_when_deadline_exceeded_before_attempt():
    dispatcher = CommunicationDispatcher(
        adapter=StubCommunicationAdapter(),
        clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
    )
    envelope = build_expired_envelope()

    result = dispatcher.dispatch(envelope)

    assert result.status == DispatchStatus.FAILED
    assert result.adapter_name == "deadline_guard"
    assert result.failure_reason == "dispatch deadline exceeded before attempt"
    assert result.attempts == [
        {"adapter_name": "deadline_guard", "status": "failed", "reason": "deadline_exceeded"},
    ]


def test_intent_message_builder_sets_default_deadline_window():
    builder = IntentMessageBuilder(
        producer="decision_engine", target="exec_bridge", default_deadline_seconds=5
    )
    intent = build_intent()

    envelope = builder.build(intent, correlation_id="corr_014")

    assert envelope.deadline_at == intent.event_time + timedelta(seconds=5)
