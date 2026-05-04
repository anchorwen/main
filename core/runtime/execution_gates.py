"""Runtime execution gates."""

from core.contracts.ids import new_verdict_id
from core.execution.gateway_contracts import OrderRequest
from core.runtime.approval_contracts import ExecutionApproval
from core.strategies.contracts import Signal


class RuntimeRiskGate:
    """Hard risk gate for runtime order requests."""

    def __init__(
        self,
        *,
        max_quantity: float | None = None,
        allowed_symbols: set[str] | None = None,
        allowed_sides: set[str] | None = None,
        max_notional: float | None = None,
    ):
        self._max_quantity = max_quantity
        self._allowed_symbols = allowed_symbols
        self._allowed_sides = allowed_sides or {"buy", "sell"}
        self._max_notional = max_notional

    def approve(
        self, signal: Signal, order: OrderRequest, market: dict | None = None
    ) -> ExecutionApproval:
        reasons = []
        market = market or {}
        if self._max_quantity is not None and order.quantity > self._max_quantity:
            reasons.append(f"quantity_limit_exceeded({order.quantity}>{self._max_quantity})")
        if self._allowed_symbols is not None and order.symbol not in self._allowed_symbols:
            reasons.append(f"symbol_not_allowed({order.symbol})")
        if order.side not in self._allowed_sides:
            reasons.append(f"side_not_allowed({order.side})")
        if self._max_notional is not None:
            price = (
                market.get("price")
                or market.get("last")
                or market.get("ask")
                or market.get("bid")
                or 0
            )
            if order.quantity * float(price) > self._max_notional:
                reasons.append(
                    f"notional_limit_exceeded({order.quantity * float(price):.2f}"
                    f">{self._max_notional:.2f})"
                )
        approval_id = new_verdict_id().replace("verdict_", "approval_", 1)
        if reasons:
            return ExecutionApproval.deny(approval_id, signal, order, "runtime_risk", reasons)
        return ExecutionApproval.allow(approval_id, signal, order, "runtime_risk")


class RuntimeGovernanceGate:
    """Governance gate for strategy/venue/system execution constraints."""

    def __init__(
        self,
        *,
        allowed_strategy_ids: set[str] | None = None,
        frozen_strategy_ids: set[str] | None = None,
        allowed_venues: set[str] | None = None,
        system_halted: bool = False,
    ):
        self._allowed_strategy_ids = allowed_strategy_ids
        self._frozen_strategy_ids = frozen_strategy_ids or set()
        self._allowed_venues = allowed_venues
        self._system_halted = system_halted

    def approve(
        self, signal: Signal, order: OrderRequest, market: dict | None = None
    ) -> ExecutionApproval:
        reasons = []
        if self._system_halted:
            reasons.append("system_halted")
        if signal.strategy_id in self._frozen_strategy_ids:
            reasons.append(f"strategy_frozen({signal.strategy_id})")
        if (
            self._allowed_strategy_ids is not None
            and signal.strategy_id not in self._allowed_strategy_ids
        ):
            reasons.append(f"strategy_not_allowed({signal.strategy_id})")
        if self._allowed_venues is not None and order.venue not in self._allowed_venues:
            reasons.append(f"venue_not_allowed({order.venue})")
        approval_id = new_verdict_id().replace("verdict_", "approval_", 1)
        if reasons:
            return ExecutionApproval.deny(approval_id, signal, order, "runtime_governance", reasons)
        return ExecutionApproval.allow(approval_id, signal, order, "runtime_governance")


class RuntimeExecutionApprovalChain:
    """Runs all configured gates before execution."""

    def __init__(self, gates: list):
        self._gates = list(gates)

    def approve(
        self, signal: Signal, order: OrderRequest, market: dict | None = None
    ) -> list[ExecutionApproval]:
        approvals = []
        for gate in self._gates:
            approval = gate.approve(signal, order, market or {})
            approvals.append(approval)
            if not approval.approved:
                break
        return approvals
