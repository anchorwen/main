"""Runtime gate for Alpha risk budget enforcement."""

from core.contracts.ids import new_verdict_id
from core.execution.gateway_contracts import OrderRequest
from core.runtime.alpha_budget_contracts import AlphaRiskBudgetContractValidator
from core.runtime.alpha_budget_usage_store import AlphaBudgetUsageStore
from core.runtime.approval_contracts import ExecutionApproval
from core.strategies.contracts import Signal


class AlphaRiskBudgetGate:
    """Approves orders against alpha_risk_budget.v1 constraints."""

    def __init__(
        self,
        risk_budget: dict,
        *,
        deny_missing: bool = True,
        usage_store: AlphaBudgetUsageStore | None = None,
    ):
        self._budget = AlphaRiskBudgetContractValidator.validate(risk_budget or {})
        self._deny_missing = deny_missing
        self._daily_counts: dict[str, int] = {}
        self._usage_store = usage_store

    def approve(
        self, signal: Signal, order: OrderRequest, market: dict | None = None
    ) -> ExecutionApproval:
        alpha_id = self._alpha_id(signal, order)
        budget = (self._budget.get("budgets") or {}).get(alpha_id)
        reasons = []
        constraints = {"alpha_id": alpha_id}
        if budget is None:
            if self._deny_missing:
                reasons.append(f"alpha_budget_missing({alpha_id})")
            return self._approval(signal, order, reasons, constraints)
        constraints.update(
            {  # type: ignore[reportArgumentType]
                "enabled": budget.get("enabled", False),
                "risk_tier": budget.get("risk_tier"),
                "max_notional": budget.get("max_notional"),
                "max_order_notional": budget.get("max_order_notional"),
                "max_daily_orders": budget.get("max_daily_orders"),
                "usage_count": self._count(alpha_id),
            }
        )
        if not budget.get("enabled", False):
            reasons.append(f"alpha_budget_disabled({alpha_id})")
        notional = self._notional(order, market or {})
        max_order = budget.get("max_order_notional")
        if max_order is not None and notional > float(max_order):
            reasons.append(f"alpha_order_notional_exceeded({notional:.2f}>{float(max_order):.2f})")
        max_daily = budget.get("max_daily_orders")
        if max_daily is not None and self._count(alpha_id) + 1 > int(max_daily):
            reasons.append(
                f"alpha_daily_order_limit_exceeded({self._count(alpha_id) + 1}>{int(max_daily)})"
            )
        approval = self._approval(signal, order, reasons, constraints)
        if approval.approved:
            self._increment(alpha_id)
        return approval

    def reset_counts(self) -> None:
        self._daily_counts.clear()
        if self._usage_store:
            self._usage_store.reset()

    def counts(self) -> dict[str, int]:
        return self._usage_store.counts() if self._usage_store else dict(self._daily_counts)

    def _approval(
        self, signal: Signal, order: OrderRequest, reasons: list[str], constraints: dict
    ) -> ExecutionApproval:
        approval_id = new_verdict_id().replace("verdict_", "approval_", 1)
        if reasons:
            return ExecutionApproval.deny(approval_id, signal, order, "alpha_risk_budget", reasons)
        return ExecutionApproval.allow(
            approval_id, signal, order, "alpha_risk_budget", constraints=constraints
        )

    def _alpha_id(self, signal: Signal, order: OrderRequest) -> str:
        return (
            order.metadata.get("alpha_id")
            or signal.extensions.get("alpha_id")
            or order.metadata.get("strategy_id")
            or signal.strategy_id
        )

    def _count(self, alpha_id: str) -> int:
        return (
            self._usage_store.get(alpha_id)
            if self._usage_store
            else self._daily_counts.get(alpha_id, 0)
        )

    def _increment(self, alpha_id: str) -> None:
        if self._usage_store:
            self._usage_store.increment(alpha_id)
        else:
            self._daily_counts[alpha_id] = self._daily_counts.get(alpha_id, 0) + 1

    def _notional(self, order: OrderRequest, market: dict) -> float:
        price = (
            market.get("price")
            or market.get("last")
            or market.get("ask")
            or market.get("bid")
            or order.limit_price
            or 0
        )
        return order.quantity * float(price)
