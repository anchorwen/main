from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.contracts.domain_keys import (
    CONTRACT_ERROR_ABSTAIN_ACTION_REQUIRES_FLAT_SIDE,
    CONTRACT_ERROR_COMPILED_AT_BEFORE_EVENT_TIME,
    CONTRACT_ERROR_CONVICTION_OUT_OF_RANGE_TEMPLATE,
    CONTRACT_ERROR_OBSERVE_ACTION_REQUIRES_FLAT_SIDE,
)
from core.contracts.enums import DecisionAction, DecisionSide


@dataclass
class DecisionIntent:
    schema_version: str
    intent_id: str
    candidate_id: str
    snapshot_id: str
    event_time: datetime
    compiled_at: datetime
    symbol: str
    venue: str
    action: DecisionAction
    side: DecisionSide
    conviction: float
    priority: str
    suggested_risk_fraction: float | None = None
    expected_edge_bps: float | None = None
    expected_hold_seconds: int | None = None
    reason_tags: list[str] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.conviction <= 1.0:
            raise ValueError(
                CONTRACT_ERROR_CONVICTION_OUT_OF_RANGE_TEMPLATE.format(
                    conviction=self.conviction,
                )
            )
        if self.action == DecisionAction.ABSTAIN and self.side != DecisionSide.FLAT:
            raise ValueError(CONTRACT_ERROR_ABSTAIN_ACTION_REQUIRES_FLAT_SIDE)
        if self.action == DecisionAction.OBSERVE and self.side != DecisionSide.FLAT:
            raise ValueError(CONTRACT_ERROR_OBSERVE_ACTION_REQUIRES_FLAT_SIDE)
        if self.compiled_at < self.event_time:
            raise ValueError(CONTRACT_ERROR_COMPILED_AT_BEFORE_EVENT_TIME)

    def is_actionable(self) -> bool:
        return self.action in {
            DecisionAction.OPEN,
            DecisionAction.CLOSE,
            DecisionAction.REDUCE,
            DecisionAction.REVERSE,
        }

    def is_open_intent(self) -> bool:
        return self.action == DecisionAction.OPEN

    def is_close_intent(self) -> bool:
        return self.action in {DecisionAction.CLOSE, DecisionAction.REDUCE}

    def is_passive(self) -> bool:
        return self.action in {DecisionAction.ABSTAIN, DecisionAction.OBSERVE}
