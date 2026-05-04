"""Contract validators for Alpha risk budget artifacts."""

from datetime import date
from typing import Any

from core.alpha.schema_versions import SCHEMA_ALPHA_RISK_BUDGET
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE


class AlphaBudgetContractError(ValueError):
    """Raised when an Alpha budget artifact violates its contract."""


class AlphaRiskBudgetContractValidator:
    """Validates alpha_risk_budget.v1 artifacts before runtime use."""

    @classmethod
    def validate(cls, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise AlphaBudgetContractError("alpha_risk_budget must be a JSON object")
        if payload.get("schema_version") != SCHEMA_ALPHA_RISK_BUDGET:
            raise AlphaBudgetContractError(f"schema_version must be {SCHEMA_ALPHA_RISK_BUDGET}")
        budgets = payload.get("budgets")
        if not isinstance(budgets, dict):
            raise AlphaBudgetContractError("budgets must be an object")
        for alpha_id, budget in budgets.items():
            cls._validate_alpha_id(alpha_id)
            cls._validate_budget(alpha_id, budget)
        return payload

    @classmethod
    def _validate_alpha_id(cls, alpha_id: Any) -> None:
        if not isinstance(alpha_id, str) or not alpha_id:
            raise AlphaBudgetContractError("budget alpha_id keys must be non-empty strings")

    @classmethod
    def _validate_budget(cls, alpha_id: str, budget: Any) -> None:
        if not isinstance(budget, dict):
            raise AlphaBudgetContractError(f"budget for {alpha_id} must be an object")
        if not isinstance(budget.get("enabled"), bool):
            raise AlphaBudgetContractError(f"budget for {alpha_id} enabled must be bool")
        for key in ("max_notional", "max_order_notional"):
            if key in budget and budget[key] is not None:
                cls._validate_non_negative_number(alpha_id, key, budget[key])
        if "max_daily_orders" in budget and budget["max_daily_orders"] is not None:
            value = budget["max_daily_orders"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AlphaBudgetContractError(
                    f"budget for {alpha_id} max_daily_orders must be a non-negative integer"
                )

    @classmethod
    def _validate_non_negative_number(cls, alpha_id: str, key: str, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise AlphaBudgetContractError(
                f"budget for {alpha_id} {key} must be a non-negative number"
            )


class AlphaBudgetUsageContractValidator:
    """Validates alpha_budget_usage.v1 artifacts before ops/runtime use."""

    @classmethod
    def validate(cls, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise AlphaBudgetContractError("alpha_budget_usage must be a JSON object")
        if payload.get("schema_version") != SCHEMA_ALPHA_BUDGET_USAGE:
            raise AlphaBudgetContractError(f"schema_version must be {SCHEMA_ALPHA_BUDGET_USAGE}")
        cls._validate_date(payload.get("usage_date"))
        counts = payload.get("counts")
        if not isinstance(counts, dict):
            raise AlphaBudgetContractError("counts must be an object")
        for alpha_id, count in counts.items():
            if not isinstance(alpha_id, str) or not alpha_id:
                raise AlphaBudgetContractError(
                    "usage count alpha_id keys must be non-empty strings"
                )
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise AlphaBudgetContractError(
                    f"usage count for {alpha_id} must be a non-negative integer"
                )
        return payload

    @classmethod
    def _validate_date(cls, value: Any) -> None:
        if not isinstance(value, str):
            raise AlphaBudgetContractError("usage_date must be ISO date string")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise AlphaBudgetContractError("usage_date must be ISO date string") from exc
