"""Runtime risk and governance approval contracts."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.execution.gateway_contracts import OrderRequest
from core.runtime.schema_versions import SCHEMA_EXECUTION_APPROVAL
from core.strategies.contracts import Signal


@dataclass(frozen=True)
class ExecutionApproval:
    schema_version: str
    approval_id: str
    signal_id: str
    order_id: str
    approved: bool
    gate: str
    decided_at: datetime
    reasons: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_EXECUTION_APPROVAL:
            raise ValueError(f"schema_version must be {SCHEMA_EXECUTION_APPROVAL}")
        if not self.approved and not self.reasons:
            raise ValueError("denied approval must include reasons")

    @classmethod
    def allow(
        cls,
        approval_id: str,
        signal: Signal,
        order: OrderRequest,
        gate: str,
        constraints: dict[str, Any] | None = None,
    ) -> "ExecutionApproval":
        return cls(
            schema_version=SCHEMA_EXECUTION_APPROVAL,
            approval_id=approval_id,
            signal_id=signal.signal_id,
            order_id=order.order_id,
            approved=True,
            gate=gate,
            decided_at=datetime.now(UTC).replace(tzinfo=None),
            constraints=constraints or {},
        )

    @classmethod
    def deny(
        cls, approval_id: str, signal: Signal, order: OrderRequest, gate: str, reasons: list[str]
    ) -> "ExecutionApproval":
        return cls(
            schema_version=SCHEMA_EXECUTION_APPROVAL,
            approval_id=approval_id,
            signal_id=signal.signal_id,
            order_id=order.order_id,
            approved=False,
            gate=gate,
            decided_at=datetime.now(UTC).replace(tzinfo=None),
            reasons=reasons,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "approval_id": self.approval_id,
            "signal_id": self.signal_id,
            "order_id": self.order_id,
            "approved": self.approved,
            "gate": self.gate,
            "decided_at": self.decided_at.isoformat(),
            "reasons": self.reasons,
            "constraints": self.constraints,
        }
