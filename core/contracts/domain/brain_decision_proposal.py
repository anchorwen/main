from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict

from core.contracts.enums import BrainRole, BrainStatus
from core.deployment.domain_keys import CONTRACT_ERROR_GENERATED_AT_BEFORE_EVENT_TIME


@dataclass
class BrainDecisionProposal:
    schema_version: str
    proposal_id: str
    snapshot_id: str
    brain_id: str
    brain_role: BrainRole | str
    brain_status: BrainStatus | str
    model_version: str
    event_time: datetime
    generated_at: datetime
    prediction: Dict[str, Any] = field(default_factory=dict)
    applicability: Dict[str, Any] = field(default_factory=dict)
    rationale: Dict[str, Any] = field(default_factory=dict)
    health: Dict[str, Any] = field(default_factory=dict)
    extensions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.generated_at < self.event_time:
            raise ValueError(CONTRACT_ERROR_GENERATED_AT_BEFORE_EVENT_TIME)


