from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.contracts.enums import SystemMode


@dataclass
class SystemModeState:
    schema_version: str
    mode_state_id: str
    current_mode: SystemMode | str
    entered_at: datetime
    previous_mode: SystemMode | str | None
    reason: str
    constraints: dict[str, Any] = field(default_factory=dict)
    health_snapshot: dict[str, Any] = field(default_factory=dict)
    transition_policy: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)
