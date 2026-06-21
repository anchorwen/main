"""Alpha lifecycle contracts for B0 Alpha Factory."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class AlphaLifecycleState(str, Enum):
    CANDIDATE = "candidate"
    BACKTEST_PASSED = "backtest_passed"
    PAPER_TRADING = "paper_trading"
    PROBATION_LIVE = "probation_live"
    ACTIVE = "active"
    THROTTLED = "throttled"
    RETIRED = "retired"


@dataclass(frozen=True)
class AlphaRecord:
    alpha_id: str
    name: str
    version: str
    state: AlphaLifecycleState | str = AlphaLifecycleState.CANDIDATE
    strategy_id: str | None = None
    strategy_class: str | None = None  # FIX-016: e.g. "swing", "statarb"
    assets: list[str] | None = None  # FIX-016: e.g. ["BTCUSDc"]
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    performance: dict[str, Any] = field(default_factory=dict)
    risk_profile: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.alpha_id:
            raise ValueError("alpha_id is required")
        if not self.name:
            raise ValueError("name is required")
        if not self.version:
            raise ValueError("version is required")
        if isinstance(self.state, str) and self.state not in {s.value for s in AlphaLifecycleState}:
            raise ValueError(f"invalid alpha lifecycle state: {self.state}")

    @property
    def state_value(self) -> str:
        return self.state.value if isinstance(self.state, AlphaLifecycleState) else self.state

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict. DQAF-20260622-049: added strategy_class + assets."""
        return {
            "alpha_id": self.alpha_id,
            "name": self.name,
            "version": self.version,
            "state": self.state_value,
            "strategy_id": self.strategy_id,
            "strategy_class": self.strategy_class,
            "assets": self.assets,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "tags": list(self.tags),
            "metadata": self.metadata,
            "performance": self.performance,
            "risk_profile": self.risk_profile,
        }


@dataclass(frozen=True)
class AlphaTransitionRecord:
    alpha_id: str
    from_state: str
    to_state: str
    reason: str
    transitioned_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "alpha_id": self.alpha_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
            "transitioned_at": self.transitioned_at.isoformat(),
        }
