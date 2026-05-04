from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.contracts.enums import RiskDecisionStatus, SystemMode
from core.deployment.domain_keys import (
    CONTRACT_ERROR_ALLOW_LIMITED_VERDICT_MISSING_CONSTRAINTS,
    CONTRACT_ERROR_ALLOW_VERDICT_HAS_BLOCKING_REASON,
    CONTRACT_ERROR_DENY_VERDICT_REQUIRES_BLOCKING_REASON,
)


@dataclass
class RiskVerdict:
    schema_version: str
    verdict_id: str
    intent_id: str
    evaluated_at: datetime
    status: RiskDecisionStatus
    mode: SystemMode | str
    risk_tier: str
    blocking_reasons: list[str] = field(default_factory=list)
    warning_reasons: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status == RiskDecisionStatus.DENY and not self.blocking_reasons:
            raise ValueError(CONTRACT_ERROR_DENY_VERDICT_REQUIRES_BLOCKING_REASON)
        if self.status == RiskDecisionStatus.ALLOW and self.blocking_reasons:
            raise ValueError(CONTRACT_ERROR_ALLOW_VERDICT_HAS_BLOCKING_REASON)
        if self.status == RiskDecisionStatus.ALLOW_LIMITED and not self.constraints:
            raise ValueError(CONTRACT_ERROR_ALLOW_LIMITED_VERDICT_MISSING_CONSTRAINTS)

    def is_allowed(self) -> bool:
        return self.status in {
            RiskDecisionStatus.ALLOW,
            RiskDecisionStatus.ALLOW_LIMITED,
        }

    def is_blocked(self) -> bool:
        return self.status in {
            RiskDecisionStatus.DENY,
            RiskDecisionStatus.DEFER,
        }

    def is_limited(self) -> bool:
        return self.status == RiskDecisionStatus.ALLOW_LIMITED

    def requires_reduce_only(self) -> bool:
        return self.status in {
            RiskDecisionStatus.FORCE_REDUCE,
            RiskDecisionStatus.LIQUIDATE_ONLY,
        } or self.constraints.get("force_reduce_only", False)
