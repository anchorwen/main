from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from core.contracts.enums import CommunicationMessageType, CommunicationPriority
from core.deployment.domain_keys import (
    CONTRACT_ERROR_CORRELATION_ID_REQUIRED,
    CONTRACT_ERROR_DEADLINE_AT_BEFORE_EVENT_TIME,
    CONTRACT_ERROR_MESSAGE_ID_REQUIRED,
    CONTRACT_ERROR_PRODUCER_REQUIRED,
    CONTRACT_ERROR_TARGET_REQUIRED,
)


@dataclass
class CommunicationEnvelope:
    schema_version: str
    message_id: str
    correlation_id: str
    causation_id: Optional[str]
    event_time: datetime
    producer: str
    target: str
    message_type: CommunicationMessageType | str
    priority: CommunicationPriority | str
    payload: Dict[str, Any] = field(default_factory=dict)
    deadline_at: Optional[datetime] = None
    idempotency_key: Optional[str] = None
    trace: Dict[str, Any] = field(default_factory=dict)
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.message_id:
            raise ValueError(CONTRACT_ERROR_MESSAGE_ID_REQUIRED)
        if not self.correlation_id:
            raise ValueError(CONTRACT_ERROR_CORRELATION_ID_REQUIRED)
        if not self.producer:
            raise ValueError(CONTRACT_ERROR_PRODUCER_REQUIRED)
        if not self.target:
            raise ValueError(CONTRACT_ERROR_TARGET_REQUIRED)
        if self.deadline_at is not None and self.deadline_at < self.event_time:
            raise ValueError(CONTRACT_ERROR_DEADLINE_AT_BEFORE_EVENT_TIME)





