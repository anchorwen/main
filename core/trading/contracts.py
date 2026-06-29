"""Strongly-typed data contracts for V6 Shared Trading Infrastructure.

All types are frozen dataclasses or IntEnums — they carry data, not behavior.
This keeps the trading layers decoupled from brain adapters, execution engines,
and feature stores.

Design (v6_integration_blueprint.pdf §5):
  - LifecycleStage: integer enum for 5-stage position FSM
  - RefinementResult: Layer A output — pass/reject + size modulation
  - ExitVerdict: Layer B2 output — priority-ordered exit decision
  - StageInfo: Layer B1 diagnostic — stage transition record
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class LifecycleStage(IntEnum):
    """Five-stage position lifecycle finite state machine.

    Integer-valued so downstream code can compare (stage >= MANAGED) without
    string parsing.  State transitions are strictly monotonic except for
    AT_RISK → MANAGED (Schmitt trigger recovery) and CLOSING → IDLE (reset).
    Any reverse or skip transition triggers a risk invariant violation.

    Reference: God's Eye V6.0 multi_tf_stage_gate.py BasketStage enum.
    """

    IDLE = 0
    """No position open — monitoring for entry signals."""

    ENTRY_CONFIRMED = 1
    """Position opened, waiting for higher-TF confirmation (M15 for M5 entries)."""

    MANAGED = 2
    """Higher-TF confirmed — active management with full context (M30+H1)."""

    AT_RISK = 3
    """Confirmation failed or regime deteriorated — tighter SL, no new layers."""

    CLOSING = 4
    """Exit triggered — waiting for MT5 close execution and ledger reconciliation."""


@dataclass(frozen=True, slots=True)
class RefinementResult:
    """Layer A output: signal refinement verdict.

    Produced by SignalRefinementGate after evaluating regime suitability,
    signal quality, and multi-TF confirmation.  All fields are immutable
    so the verdict cannot be tampered with downstream.

    Attributes:
        is_approved: True if the signal passes all refinement gates.
        size_multiplier: Multiplier applied to position volume [0, 1].
            1.0 = full size, 0.0 = blocked.  Partial values reduce exposure
            in marginal regimes without fully blocking.
        adjusted_confidence: Confidence after refinement (capped, not raw).
        suppression_reason: If not approved, which gate blocked the signal.
        component_scores: Per-gate quality scores for diagnostics.
    """

    is_approved: bool
    size_multiplier: float
    adjusted_confidence: float = 0.0
    suppression_reason: str = ""
    component_scores: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate ranges at construction time."""
        if not 0.0 <= self.size_multiplier <= 1.0:
            raise ValueError(f"size_multiplier must be in [0, 1], got {self.size_multiplier}")
        if not 0.0 <= self.adjusted_confidence <= 1.0:
            raise ValueError(
                f"adjusted_confidence must be in [0, 1], got {self.adjusted_confidence}"
            )


@dataclass(frozen=True, slots=True)
class ExitVerdict:
    """Layer B2 output: priority-ordered exit decision.

    Produced by ExitPriorityQueue after evaluating all 7 priority levels.
    First-match-wins: the queue returns the highest-priority triggered exit.

    Attributes:
        is_triggered: True if any priority level fired.
        priority_level: Which level fired (1-7, 0 = no exit / hold).
        exit_code: Canonical ExitReason value string (e.g. "basket_tp").
        target_price: Optional price target for limit/algo close orders.
        execution_mode: "MARKET" for immediate, "LIMIT" for target_price,
            "ALGO" for delegated execution.
        details: Diagnostic data from the triggering priority level.
    """

    is_triggered: bool
    priority_level: int  # 1-7, 0 = hold
    exit_code: str = ""
    target_price: float | None = None
    execution_mode: str = "MARKET"  # MARKET | LIMIT | ALGO
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate priority_level range."""
        if not 0 <= self.priority_level <= 7:
            raise ValueError(f"priority_level must be in [0, 7], got {self.priority_level}")
        if self.execution_mode not in ("MARKET", "LIMIT", "ALGO"):
            raise ValueError(f"execution_mode must be MARKET/LIMIT/ALGO, got {self.execution_mode}")

    @property
    def is_emergency(self) -> bool:
        """True for emergency exits that bypass retry gates (P4a CB_EMERGENCY)."""
        return self.priority_level == 4 and "emergency" in self.exit_code


@dataclass(frozen=True, slots=True)
class StageInfo:
    """Layer B1 diagnostic: record of a lifecycle stage transition.

    Immutable — written once per transition for audit trail.
    """

    from_stage: str
    to_stage: str
    reason: str
    cycle: int  # loop_iteration when transition occurred
    bar: int = 0  # M5 bar index (0 if N/A)

    @property
    def is_escalation(self) -> bool:
        """True if this transition moves toward higher risk (IDLE→...→CLOSING)."""
        _order = {"IDLE": 0, "ENTRY_CONFIRMED": 1, "MANAGED": 2, "AT_RISK": 3, "CLOSING": 4}
        return _order.get(self.to_stage, -1) > _order.get(self.from_stage, -1)

    @property
    def is_recovery(self) -> bool:
        """True if this transition moves away from risk (AT_RISK→MANAGED)."""
        return self.from_stage == "AT_RISK" and self.to_stage == "MANAGED"
