"""Strategy evaluate() interface protocol — canonical contract enforced by mypy.

Institutional mandate (L3 — Interface Contract Consolidation):
    Both StrategyLine (ML strategies) and RuleEngineStrategyWrapper (rule-based
    strategies) must satisfy this protocol.  The single-parameter contract
    (StrategyEvaluationContext) ensures that adding a new field never changes
    any evaluate() signature — the Parameter Object Pattern eliminates the
    recurring "signature drift → TypeError" bug class permanently.

    Prior to this protocol, the evaluate() interface was defined by convention
    across two parallel implementations with 28 parameters.  Three separate
    incidents (strategy_atr, governance_state, microstructure_gate) demonstrated
    that convention-based interfaces are not sufficient for institutional-grade
    reliability.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.execution.strategy_context import StrategyEvaluationContext
from core.execution.strategy_decision import StrategyDecision


@runtime_checkable
class StrategyEvaluateProtocol(Protocol):
    """Canonical evaluate() contract for ALL strategy implementations.

    Single-parameter design: ``context: StrategyEvaluationContext`` bundles
    all input state (28 fields as of 2026-07-20).  Adding a field to the
    context dataclass does NOT change this signature — the contract is
    permanently stable.

    Both ``StrategyLine`` (ML strategies) and ``RuleEngineStrategyWrapper``
    (rule-based strategies) explicitly implement this protocol.  mypy verifies
    conformance at type-check time, preventing the class of TypeError crashes
    that occurred when parameters were added to one implementation but not
    the other.
    """

    def evaluate(self, context: StrategyEvaluationContext) -> StrategyDecision: ...


__all__ = ["StrategyEvaluateProtocol"]
