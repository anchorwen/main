"""Runtime integration contracts for A1 execution pipeline."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.execution.gateway_contracts import OrderState
from core.execution.quality_contracts import ExecutionQualityReport
from core.runtime.approval_contracts import ExecutionApproval
from core.strategies.contracts import Signal


@dataclass(frozen=True)
class OrderSizingPolicy:
    base_quantity: float = 1.0
    min_confidence: float = 0.0
    min_strength: float = 0.0
    max_quantity: float = 100.0

    def __post_init__(self) -> None:
        if self.base_quantity <= 0:
            raise ValueError("base_quantity must be positive")
        if self.max_quantity <= 0:
            raise ValueError("max_quantity must be positive")
        if not 0 <= self.min_confidence <= 1:
            raise ValueError("min_confidence must be within [0, 1]")
        if not 0 <= self.min_strength <= 1:
            raise ValueError("min_strength must be within [0, 1]")


@dataclass(frozen=True)
class RuntimePipelineResult:
    schema_version: str
    runtime_cycle_id: str
    generated_at: datetime
    signals: list[Signal]
    orders: list[OrderState]
    quality_report: ExecutionQualityReport
    skipped_signals: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[ExecutionApproval] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runtime_cycle_id": self.runtime_cycle_id,
            "generated_at": self.generated_at.isoformat(),
            "signals": [s.to_dict() for s in self.signals],
            "orders": [o.to_dict() for o in self.orders],
            "quality_report": self.quality_report.to_dict(),
            "skipped_signals": self.skipped_signals,
            "approvals": [a.to_dict() for a in self.approvals],
        }
