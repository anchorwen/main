from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class ControlSnapshot:
    captured_at: datetime
    mode_state: Any
    active_overrides: list[Any]
    budget_snapshot: dict[str, Any]
    brain_registry_snapshot: list[dict[str, Any]]
