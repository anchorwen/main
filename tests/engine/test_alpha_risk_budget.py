"""Alpha risk budget exporter tests."""

from core.alpha.risk_budget import AlphaRiskBudgetExporter
from core.alpha.risk_budget import AlphaRiskBudgetPolicy
from core.alpha.schema_versions import SCHEMA_ALPHA_PORTFOLIO_ALLOCATION, SCHEMA_ALPHA_RISK_BUDGET


def _allocation():
    return {
        "schema_version": SCHEMA_ALPHA_PORTFOLIO_ALLOCATION,
        "total_notional": 1000.0,
        "recommendations": [
            {
                "alpha_id": "alpha1",
                "state": "active",
                "target_weight": 0.8,
                "score": 0.9,
                "max_notional": 800.0,
                "risk_tier": "standard",
                "reason": "allocatable",
            },
            {
                "alpha_id": "alpha2",
                "state": "retired",
                "target_weight": 0.0,
                "score": 0.0,
                "max_notional": 0.0,
                "risk_tier": "none",
                "reason": "state_not_allocatable:retired",
            },
        ],
    }


class TestAlphaRiskBudgetExporter:
    def test_export_risk_budget(self):
        budget = AlphaRiskBudgetExporter().export(_allocation())
        assert budget["schema_version"] == SCHEMA_ALPHA_RISK_BUDGET
        assert budget["source_schema_version"] == SCHEMA_ALPHA_PORTFOLIO_ALLOCATION
        assert budget["budget_count"] == 2
        alpha1 = budget["budgets"]["alpha1"]
        assert alpha1["enabled"] is True
        assert alpha1["max_notional"] == 800.0
        assert alpha1["max_order_notional"] == 80.0
        assert alpha1["max_daily_orders"] == 20
        alpha2 = budget["budgets"]["alpha2"]
        assert alpha2["enabled"] is False
        assert alpha2["max_daily_orders"] == 0

    def test_export_with_custom_policy(self):
        policy = AlphaRiskBudgetPolicy(max_order_fraction=0.25, tier_daily_orders={"standard": 7, "none": 0})
        budget = AlphaRiskBudgetExporter(policy).export(_allocation())
        assert budget["budgets"]["alpha1"]["max_order_notional"] == 200.0
        assert budget["budgets"]["alpha1"]["max_daily_orders"] == 7
