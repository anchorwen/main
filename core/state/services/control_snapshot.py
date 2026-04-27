from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List


@dataclass
class ControlSnapshot:
    captured_at: datetime
    mode_state: Any
    active_overrides: List[Any]
    budget_snapshot: Dict[str, Any]
    brain_registry_snapshot: List[Dict[str, Any]]


