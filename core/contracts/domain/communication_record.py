from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict

from core.contracts.schema_versions import SCHEMA_COMMUNICATION_RECORD
from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.dispatch_result import DispatchResult
from core.deployment.domain_keys import (
    CONTRACT_ERROR_CORRELATION_ID_REQUIRED,
    CONTRACT_ERROR_MESSAGE_ID_REQUIRED,
    CONTRACT_ERROR_RECORDED_AT_BEFORE_EVENT_TIME,
)


@dataclass
class CommunicationRecord:
    schema_version: str
    record_id: str
    message_id: str
    correlation_id: str
    event_time: datetime
    recorded_at: datetime
    channel: Dict[str, Any] = field(default_factory=dict)
    envelope: Dict[str, Any] = field(default_factory=dict)
    dispatch: Dict[str, Any] = field(default_factory=dict)
    outcome: Dict[str, Any] = field(default_factory=dict)
    trace: Dict[str, Any] = field(default_factory=dict)
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.recorded_at < self.event_time:
            raise ValueError(CONTRACT_ERROR_RECORDED_AT_BEFORE_EVENT_TIME)
        if not self.message_id:
            raise ValueError(CONTRACT_ERROR_MESSAGE_ID_REQUIRED)
        if not self.correlation_id:
            raise ValueError(CONTRACT_ERROR_CORRELATION_ID_REQUIRED)

    @classmethod
    def from_dispatch(cls, *, record_id: str, envelope: CommunicationEnvelope, dispatch_result: DispatchResult) -> "CommunicationRecord":
        return cls(
            schema_version=SCHEMA_COMMUNICATION_RECORD,
            record_id=record_id,
            message_id=envelope.message_id,
            correlation_id=envelope.correlation_id,
            event_time=envelope.event_time,
            recorded_at=dispatch_result.recorded_at,
            channel={
                "producer": envelope.producer,
                "target": envelope.target,
                "message_type": envelope.message_type,
                "priority": envelope.priority,
            },
            envelope={
                "schema_version": envelope.schema_version,
                "causation_id": envelope.causation_id,
                "deadline_at": envelope.deadline_at,
                "idempotency_key": envelope.idempotency_key,
                "payload": envelope.payload,
            },
            dispatch={
                "dispatch_id": dispatch_result.dispatch_id,
                "recorded_at": dispatch_result.recorded_at,
                "adapter_name": dispatch_result.adapter_name,
                "fallback_adapter_name": dispatch_result.fallback_adapter_name,
                "status": dispatch_result.status,
                "ack_id": dispatch_result.ack_id,
                "attempts": dispatch_result.attempts,
                "transport_metadata": dispatch_result.transport_metadata,
                "protocol_metadata": dispatch_result.protocol_metadata,
            },
            outcome={
                "failure_reason": dispatch_result.failure_reason,
                "degrade_reason": dispatch_result.degrade_reason,
            },
            trace={
                "envelope_trace": envelope.trace,
                "dispatch_trace": dispatch_result.trace,
            },
            extensions={
                "envelope_extensions": envelope.extensions,
                "dispatch_extensions": dispatch_result.extensions,
            },
        )

