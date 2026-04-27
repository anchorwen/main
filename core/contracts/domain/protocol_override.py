from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from core.contracts.enums import OverrideStatus


@dataclass
class ProtocolOverride:
    schema_version: str
    override_id: str
    status: OverrideStatus | str
    created_at: datetime
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    scope: Dict[str, Any] = field(default_factory=dict)
    adjustments: Dict[str, Any] = field(default_factory=dict)
    reason: Dict[str, Any] = field(default_factory=dict)
    governance: Dict[str, Any] = field(default_factory=dict)
    trace: Dict[str, Any] = field(default_factory=dict)
    extensions: Dict[str, Any] = field(default_factory=dict)


