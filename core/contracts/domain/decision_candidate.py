from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List

from core.deployment.domain_keys import CONTRACT_ERROR_GENERATED_AT_BEFORE_EVENT_TIME


@dataclass
class DecisionCandidate:
    schema_version: str
    candidate_id: str
    snapshot_id: str
    event_time: datetime
    generated_at: datetime
    regime_state: Dict[str, Any] = field(default_factory=dict)
    consensus: Dict[str, Any] = field(default_factory=dict)
    supporting_brains: List[str] = field(default_factory=list)
    opposing_brains: List[str] = field(default_factory=list)
    execution_feasibility: Dict[str, Any] = field(default_factory=dict)
    risk_comments: Dict[str, Any] = field(default_factory=dict)
    candidate_summary: Dict[str, Any] = field(default_factory=dict)
    trace: Dict[str, Any] = field(default_factory=dict)
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.generated_at < self.event_time:
            raise ValueError(CONTRACT_ERROR_GENERATED_AT_BEFORE_EVENT_TIME)


