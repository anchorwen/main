from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.contracts.enums import OverrideStatus


@dataclass
class ProtocolOverride:
    schema_version: str
    override_id: str
    status: OverrideStatus | str
    created_at: datetime
    start_time: datetime | None
    end_time: datetime | None
    scope: dict[str, Any] = field(default_factory=dict)
    adjustments: dict[str, Any] = field(default_factory=dict)
    reason: dict[str, Any] = field(default_factory=dict)
    governance: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)
