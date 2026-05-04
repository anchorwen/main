"""Alpha risk budget export from allocation recommendations."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.alpha.schema_versions import SCHEMA_ALPHA_RISK_BUDGET


@dataclass(frozen=True)
class AlphaRiskBudgetPolicy:
    max_order_fraction: float = 0.10
    default_daily_orders: int = 5
    tier_daily_orders: dict[str, int] | None = None

    def daily_orders_for_tier(self, risk_tier: str) -> int:
        tiers = self.tier_daily_orders or {
            "standard": 20,
            "reduced": 10,
            "minimal": 3,
            "none": 0,
        }
        return tiers.get(risk_tier, self.default_daily_orders)


class AlphaRiskBudgetExporter:
    """Converts Alpha allocation recommendations into runtime risk budgets."""

    def __init__(self, policy: AlphaRiskBudgetPolicy | None = None):
        self._policy = policy or AlphaRiskBudgetPolicy()

    def export(self, allocation: dict[str, Any]) -> dict[str, Any]:
        recommendations = allocation.get("recommendations") or []
        budgets = {}
        for rec in recommendations:
            max_notional = float(rec.get("max_notional") or 0.0)
            risk_tier = rec.get("risk_tier", "none")
            budgets[rec["alpha_id"]] = {
                "state": rec.get("state"),
                "target_weight": rec.get("target_weight", 0.0),
                "score": rec.get("score", 0.0),
                "risk_tier": risk_tier,
                "max_notional": round(max_notional, 2),
                "max_order_notional": round(max_notional * self._policy.max_order_fraction, 2),
                "max_daily_orders": self._policy.daily_orders_for_tier(risk_tier),
                "enabled": max_notional > 0 and risk_tier != "none",
                "reason": rec.get("reason"),
            }
        return {
            "schema_version": SCHEMA_ALPHA_RISK_BUDGET,
            "generated_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "source_schema_version": allocation.get("schema_version"),
            "total_notional": allocation.get("total_notional", 0.0),
            "budget_count": len(budgets),
            "budgets": budgets,
        }
