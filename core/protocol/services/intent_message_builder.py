from datetime import timedelta

from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.protocol.schema_versions import (
    SCHEMA_COMMUNICATION_ENVELOPE,
    SCHEMA_INTENT_MESSAGE_BUILDER,
)
from core.contracts.enums import CommunicationMessageType, CommunicationPriority
from core.contracts.ids import new_message_id


class IntentMessageBuilder:
    def __init__(self, producer: str, target: str, default_deadline_seconds: int = 5):
        self._producer = producer
        self._target = target
        self._default_deadline_seconds = default_deadline_seconds

    def build(self, intent, *, correlation_id: str, causation_id: str | None = None) -> CommunicationEnvelope:
        return CommunicationEnvelope(
            schema_version=SCHEMA_COMMUNICATION_ENVELOPE,
            message_id=new_message_id(),
            correlation_id=correlation_id,
            causation_id=causation_id or intent.intent_id,
            event_time=intent.event_time,
            producer=self._producer,
            target=self._target,
            message_type=CommunicationMessageType.DECISION_INTENT,
            priority=self._resolve_priority(intent),
            payload={
                "intent_id": intent.intent_id,
                "candidate_id": intent.candidate_id,
                "snapshot_id": intent.snapshot_id,
                "symbol": intent.symbol,
                "venue": intent.venue,
                "action": intent.action,
                "side": intent.side,
                "conviction": intent.conviction,
                "priority": intent.priority,
                "suggested_risk_fraction": intent.suggested_risk_fraction,
                "expected_edge_bps": intent.expected_edge_bps,
                "expected_hold_seconds": intent.expected_hold_seconds,
                "reason_tags": list(intent.reason_tags),
            },
            deadline_at=intent.event_time + timedelta(seconds=self._default_deadline_seconds),
            idempotency_key=intent.intent_id,
            trace={
                "intent_trace": intent.trace,
                "builder_version": SCHEMA_INTENT_MESSAGE_BUILDER,
            },
            extensions={
                "intent_extensions": intent.extensions,
            },
        )

    def _resolve_priority(self, intent):
        if getattr(intent, "priority", "normal") == "high":
            return CommunicationPriority.HIGH
        return CommunicationPriority.NORMAL





