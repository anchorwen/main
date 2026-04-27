from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict

from core.deployment.domain_keys import (
    CONTRACT_ERROR_CORRELATION_ID_REQUIRED,
    CONTRACT_ERROR_EVENT_ID_REQUIRED,
    CONTRACT_ERROR_EVENT_TYPE_INVALID_TEMPLATE,
    CONTRACT_ERROR_EVENT_TYPE_REQUIRED,
    CONTRACT_ERROR_MESSAGE_ID_REQUIRED,
    CONTRACT_ERROR_VENUE_REQUIRED,
)


@dataclass
class ExecutionEvent:
    """Represents a downstream execution lifecycle event.

    An execution event captures a state transition reported by the downstream
    execution venue (broker, exchange, OMS) in response to a previously
    dispatched communication.  The event_type field carries the canonical
    lifecycle verb while details holds venue-specific payload.

    Lifecycle progression (happy path):
        ack -> accepted -> partially_filled -> filled

    Alternative terminal states:
        ack -> rejected
        ack -> accepted -> cancelled
        ack -> accepted -> amended -> filled
    """

    schema_version: str
    event_id: str
    message_id: str
    correlation_id: str
    event_type: str
    event_time: datetime
    recorded_at: datetime
    venue: str
    venue_order_id: str | None = None
    quantity: Dict[str, Any] = field(default_factory=dict)
    price: Dict[str, Any] = field(default_factory=dict)
    details: Dict[str, Any] = field(default_factory=dict)
    trace: Dict[str, Any] = field(default_factory=dict)
    extensions: Dict[str, Any] = field(default_factory=dict)

    VALID_EVENT_TYPES = frozenset({
        "ack",
        "rejected",
        "accepted",
        "partially_filled",
        "filled",
        "cancelled",
        "amended",
        "expired",
    })

    TERMINAL_EVENT_TYPES = frozenset({
        "rejected",
        "filled",
        "cancelled",
        "expired",
    })

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError(CONTRACT_ERROR_EVENT_ID_REQUIRED)
        if not self.message_id:
            raise ValueError(CONTRACT_ERROR_MESSAGE_ID_REQUIRED)
        if not self.correlation_id:
            raise ValueError(CONTRACT_ERROR_CORRELATION_ID_REQUIRED)
        if not self.event_type:
            raise ValueError(CONTRACT_ERROR_EVENT_TYPE_REQUIRED)
        if self.event_type not in self.VALID_EVENT_TYPES:
            raise ValueError(
                CONTRACT_ERROR_EVENT_TYPE_INVALID_TEMPLATE.format(
                    valid_types=sorted(self.VALID_EVENT_TYPES),
                    event_type=self.event_type,
                )
            )
        if not self.venue:
            raise ValueError(CONTRACT_ERROR_VENUE_REQUIRED)

    @property
    def is_terminal(self) -> bool:
        return self.event_type in self.TERMINAL_EVENT_TYPES

    @property
    def is_fill(self) -> bool:
        return self.event_type in {"partially_filled", "filled"}
