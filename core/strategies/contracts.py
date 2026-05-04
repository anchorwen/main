"""Strategy plugin contracts for Alpha agents.

This module defines the A1 Strategy Plugin Protocol. Strategies produce
signals only; they must not place orders or call execution gateways.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from core.strategies.schema_versions import SCHEMA_SIGNAL


@dataclass(frozen=True)
class StrategyMetadata:
    strategy_id: str
    name: str
    version: str
    author: str = "unknown"
    description: str = ""
    tags: tuple[str, ...] = ()
    risk_profile: str = "standard"


@dataclass(frozen=True)
class RequiredFeature:
    name: str
    timeframe: str = "M1"
    lookback: int = 1
    required: bool = True


@dataclass(frozen=True)
class Signal:
    schema_version: str
    signal_id: str
    strategy_id: str
    symbol: str
    side: str
    strength: float
    confidence: float
    generated_at: datetime
    horizon: str = "intraday"
    reason: str = ""
    features_used: dict[str, Any] = field(default_factory=dict)
    risk_hints: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_SIGNAL:
            raise ValueError(f"schema_version must be {SCHEMA_SIGNAL}")
        if self.side not in {"buy", "sell", "hold", "flat"}:
            raise ValueError("side must be one of buy, sell, hold, flat")
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError("strength must be between 0 and 1")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "side": self.side,
            "strength": self.strength,
            "confidence": self.confidence,
            "generated_at": self.generated_at.isoformat(),
            "horizon": self.horizon,
            "reason": self.reason,
            "features_used": self.features_used,
            "risk_hints": self.risk_hints,
            "extensions": self.extensions,
        }


@dataclass(frozen=True)
class StrategyHealth:
    status: str
    message: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {"healthy", "degraded", "frozen", "retired"}:
            raise ValueError("invalid strategy health status")


@runtime_checkable
class AlphaAgent(Protocol):
    """Protocol every strategy plugin must implement."""

    def metadata(self) -> StrategyMetadata: ...

    def required_features(self) -> list[RequiredFeature]: ...

    def warmup(self, context: dict[str, Any]) -> None: ...

    def generate_signal(self, feature_snapshot: Any, context: dict[str, Any]) -> Signal: ...

    def explain(self, signal: Signal) -> dict[str, Any]: ...

    def health(self) -> StrategyHealth: ...
