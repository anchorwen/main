from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.contracts.domain_keys import CONTRACT_ERROR_GENERATED_AT_BEFORE_EVENT_TIME


@dataclass
class DecisionCandidate:
    schema_version: str
    candidate_id: str
    snapshot_id: str
    event_time: datetime
    generated_at: datetime
    regime_state: dict[str, Any] = field(default_factory=dict)
    consensus: dict[str, Any] = field(default_factory=dict)
    supporting_brains: list[str] = field(default_factory=list)
    opposing_brains: list[str] = field(default_factory=list)
    execution_feasibility: dict[str, Any] = field(default_factory=dict)
    risk_comments: dict[str, Any] = field(default_factory=dict)
    candidate_summary: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.generated_at < self.event_time:
            raise ValueError(CONTRACT_ERROR_GENERATED_AT_BEFORE_EVENT_TIME)
