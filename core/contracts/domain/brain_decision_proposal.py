from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

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
    prediction: dict[str, Any] = field(default_factory=dict)
    applicability: dict[str, Any] = field(default_factory=dict)
    rationale: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)
    vote_weight: float = 1.0

    def __post_init__(self) -> None:
        if self.generated_at < self.event_time:
            raise ValueError(CONTRACT_ERROR_GENERATED_AT_BEFORE_EVENT_TIME)

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON staging / downstream consumers."""
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "snapshot_id": self.snapshot_id,
            "brain_id": self.brain_id,
            "brain_role": self.brain_role
            if isinstance(self.brain_role, str)
            else self.brain_role.value,
            "brain_status": self.brain_status
            if isinstance(self.brain_status, str)
            else self.brain_status.value,
            "model_version": self.model_version,
            "event_time": self.event_time.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "prediction": self.prediction,
            "applicability": self.applicability,
            "rationale": self.rationale,
            "health": self.health,
            "extensions": self.extensions,
            "vote_weight": self.vote_weight,
        }
