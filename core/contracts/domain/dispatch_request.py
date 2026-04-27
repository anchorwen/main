from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.deployment.domain_keys import (
    CONTRACT_ERROR_DISPATCH_ID_REQUIRED,
    CONTRACT_ERROR_REQUESTED_AT_BEFORE_EVENT_TIME,
)


@dataclass
class DispatchRequest:
    schema_version: str
    dispatch_id: str
    envelope: CommunicationEnvelope
    requested_at: datetime
    route_policy: Dict[str, Any] = field(default_factory=dict)
    transport_hints: Dict[str, Any] = field(default_factory=dict)
    governance: Dict[str, Any] = field(default_factory=dict)
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.dispatch_id:
            raise ValueError(CONTRACT_ERROR_DISPATCH_ID_REQUIRED)
        if self.requested_at < self.envelope.event_time:
            raise ValueError(CONTRACT_ERROR_REQUESTED_AT_BEFORE_EVENT_TIME)





