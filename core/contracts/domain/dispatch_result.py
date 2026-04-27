from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict

from core.contracts.enums import DispatchStatus
from core.deployment.domain_keys import (
    CONTRACT_ERROR_ADAPTER_NAME_REQUIRED,
    CONTRACT_ERROR_DISPATCH_ID_REQUIRED,
    CONTRACT_ERROR_FAILURE_REASON_REQUIRED_WHEN_STATUS_FAILED,
    CONTRACT_ERROR_STATUS_REQUIRED,
)


@dataclass
class DispatchResult:
    schema_version: str
    dispatch_id: str
    status: str
    recorded_at: datetime
    adapter_name: str
    message_id: str | None = None
    target: str | None = None
    fallback_adapter_name: str | None = None
    ack_id: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    transport_metadata: Dict[str, Any] = field(default_factory=dict)
    protocol_metadata: Dict[str, Any] = field(default_factory=dict)
    failure_reason: str | None = None
    degrade_reason: str | None = None
    trace: Dict[str, Any] = field(default_factory=dict)
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.dispatch_id:
            raise ValueError(CONTRACT_ERROR_DISPATCH_ID_REQUIRED)
        if not self.adapter_name:
            raise ValueError(CONTRACT_ERROR_ADAPTER_NAME_REQUIRED)
        if not self.status:
            raise ValueError(CONTRACT_ERROR_STATUS_REQUIRED)
        if self.status == DispatchStatus.FAILED and not self.failure_reason:
            raise ValueError(CONTRACT_ERROR_FAILURE_REASON_REQUIRED_WHEN_STATUS_FAILED)

    def __getitem__(self, key: str):
        return getattr(self, key)

