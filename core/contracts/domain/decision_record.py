from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict

from core.deployment.domain_keys import (
    CONTRACT_ERROR_INTENT_ID_REQUIRED,
    CONTRACT_ERROR_RECORDED_AT_BEFORE_EVENT_TIME,
    CONTRACT_ERROR_VERDICT_ID_REQUIRED,
)


@dataclass
class DecisionRecord:
    schema_version: str
    record_id: str
    snapshot_id: str
    intent_id: str
    verdict_id: str
    event_time: datetime
    recorded_at: datetime
    context: Dict[str, Any] = field(default_factory=dict)
    inputs: Dict[str, Any] = field(default_factory=dict)
    execution: Dict[str, Any] = field(default_factory=dict)
    outcome: Dict[str, Any] = field(default_factory=dict)
    attribution: Dict[str, Any] = field(default_factory=dict)
    labels: Dict[str, Any] = field(default_factory=dict)
    trace: Dict[str, Any] = field(default_factory=dict)
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.recorded_at < self.event_time:
            raise ValueError(CONTRACT_ERROR_RECORDED_AT_BEFORE_EVENT_TIME)
        if not self.intent_id:
            raise ValueError(CONTRACT_ERROR_INTENT_ID_REQUIRED)
        if not self.verdict_id:
            raise ValueError(CONTRACT_ERROR_VERDICT_ID_REQUIRED)

    def attach_execution(self, execution_payload: Dict[str, Any]) -> None:
        self.execution.update(execution_payload)

    def attach_outcome(self, outcome_payload: Dict[str, Any]) -> None:
        self.outcome.update(outcome_payload)

    def attach_attribution(self, attribution_payload: Dict[str, Any]) -> None:
        self.attribution.update(attribution_payload)
