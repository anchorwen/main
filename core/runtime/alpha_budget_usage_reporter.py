"""Alpha budget usage reporting."""

from typing import Any

from core.runtime.alpha_budget_contracts import AlphaRiskBudgetContractValidator
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE_REPORT


class AlphaBudgetUsageReporter:
    """Builds operator reports by joining usage counters with risk budgets."""

    def __init__(self, *, high_usage_threshold: float = 0.8):
        self._high_usage_threshold = high_usage_threshold

    def build(self, usage: dict[str, Any], risk_budget: dict[str, Any]) -> dict[str, Any]:
        budget_payload = AlphaRiskBudgetContractValidator.validate(risk_budget)
        counts = usage.get("counts") or {}
        budgets = budget_payload.get("budgets") or {}
        alpha_ids = sorted(set(counts) | set(budgets))
        report_budgets = {}
        warnings = []
        for alpha_id in alpha_ids:
            budget = budgets.get(alpha_id) or {}
            used = int(counts.get(alpha_id, 0))
            max_daily = budget.get("max_daily_orders")
            remaining = None if max_daily is None else max(0, int(max_daily) - used)
            usage_ratio = None if not max_daily else round(used / int(max_daily), 6)
            report_budgets[alpha_id] = {
                "used_daily_orders": used,
                "max_daily_orders": max_daily,
                "remaining_daily_orders": remaining,
                "usage_ratio": usage_ratio,
                "enabled": budget.get("enabled", False),
                "risk_tier": budget.get("risk_tier"),
                "max_notional": budget.get("max_notional"),
                "max_order_notional": budget.get("max_order_notional"),
            }
            warnings.extend(self._warnings_for(alpha_id, used, budget, usage_ratio))
        return {
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": usage.get("usage_date"),
            "source_schema_version": usage.get("schema_version"),
            "risk_budget_schema_version": budget_payload.get("schema_version"),
            "alpha_count": len(report_budgets),
            "warning_count": len(warnings),
            "budgets": report_budgets,
            "counts": {key: int(value) for key, value in counts.items()},
            "warnings": warnings,
        }

    def _warnings_for(
        self, alpha_id: str, used: int, budget: dict[str, Any], usage_ratio: float | None
    ) -> list[dict[str, Any]]:
        warnings = []
        if not budget:
            if used > 0:
                warnings.append(
                    {
                        "alpha_id": alpha_id,
                        "type": "usage_without_budget",
                        "used_daily_orders": used,
                    }
                )
            return warnings
        enabled = budget.get("enabled", False)
        if not enabled and used > 0:
            warnings.append(
                {
                    "alpha_id": alpha_id,
                    "type": "disabled_alpha_has_usage",
                    "used_daily_orders": used,
                }
            )
        if usage_ratio is not None:
            if usage_ratio >= 1.0:
                warnings.append(
                    {
                        "alpha_id": alpha_id,
                        "type": "daily_usage_exhausted",
                        "usage_ratio": usage_ratio,
                        "threshold": 1.0,
                    }
                )
            elif usage_ratio >= self._high_usage_threshold:
                warnings.append(
                    {
                        "alpha_id": alpha_id,
                        "type": "daily_usage_high",
                        "usage_ratio": usage_ratio,
                        "threshold": self._high_usage_threshold,
                    }
                )
        return warnings
