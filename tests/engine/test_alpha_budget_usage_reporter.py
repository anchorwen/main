"""Alpha budget usage reporter tests."""

from core.alpha.schema_versions import SCHEMA_ALPHA_RISK_BUDGET
from core.runtime.alpha_budget_usage_reporter import AlphaBudgetUsageReporter
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE, SCHEMA_ALPHA_BUDGET_USAGE_REPORT


def test_alpha_budget_usage_reporter_joins_usage_and_budget():
    usage = {
        "schema_version": SCHEMA_ALPHA_BUDGET_USAGE,
        "usage_date": "2026-01-01",
        "counts": {"alpha1": 2, "orphan": 1},
    }
    risk_budget = {
        "schema_version": SCHEMA_ALPHA_RISK_BUDGET,
        "budgets": {
            "alpha1": {
                "enabled": True,
                "risk_tier": "standard",
                "max_notional": 10000.0,
                "max_order_notional": 1000.0,
                "max_daily_orders": 5,
            },
            "alpha2": {
                "enabled": False,
                "risk_tier": "none",
                "max_notional": 0.0,
                "max_order_notional": 0.0,
                "max_daily_orders": 0,
            },
        },
    }
    report = AlphaBudgetUsageReporter().build(usage, risk_budget)
    assert report["schema_version"] == SCHEMA_ALPHA_BUDGET_USAGE_REPORT
    assert report["alpha_count"] == 3
    assert report["budgets"]["alpha1"]["used_daily_orders"] == 2
    assert report["budgets"]["alpha1"]["remaining_daily_orders"] == 3
    assert report["budgets"]["alpha1"]["usage_ratio"] == 0.4
    assert report["budgets"]["alpha2"]["remaining_daily_orders"] == 0
    assert report["budgets"]["alpha2"]["usage_ratio"] is None
    assert report["budgets"]["orphan"]["used_daily_orders"] == 1
    assert report["budgets"]["orphan"]["enabled"] is False
    assert report["warning_count"] == 1
    assert report["warnings"] == [
        {"alpha_id": "orphan", "type": "usage_without_budget", "used_daily_orders": 1}
    ]


def test_alpha_budget_usage_reporter_warns_high_and_exhausted_usage():
    usage = {
        "schema_version": SCHEMA_ALPHA_BUDGET_USAGE,
        "usage_date": "2026-01-01",
        "counts": {"high": 4, "exhausted": 5},
    }
    risk_budget = {
        "schema_version": SCHEMA_ALPHA_RISK_BUDGET,
        "budgets": {
            "high": {"enabled": True, "risk_tier": "standard", "max_daily_orders": 5},
            "exhausted": {"enabled": True, "risk_tier": "standard", "max_daily_orders": 5},
        },
    }
    report = AlphaBudgetUsageReporter().build(usage, risk_budget)
    assert report["warning_count"] == 2
    assert report["warnings"] == [
        {
            "alpha_id": "exhausted",
            "type": "daily_usage_exhausted",
            "usage_ratio": 1.0,
            "threshold": 1.0,
        },
        {"alpha_id": "high", "type": "daily_usage_high", "usage_ratio": 0.8, "threshold": 0.8},
    ]


def test_alpha_budget_usage_reporter_warns_disabled_alpha_with_usage():
    usage = {
        "schema_version": SCHEMA_ALPHA_BUDGET_USAGE,
        "usage_date": "2026-01-01",
        "counts": {"alpha1": 1},
    }
    risk_budget = {
        "schema_version": SCHEMA_ALPHA_RISK_BUDGET,
        "budgets": {
            "alpha1": {"enabled": False, "risk_tier": "none", "max_daily_orders": 5},
        },
    }
    report = AlphaBudgetUsageReporter().build(usage, risk_budget)
    assert report["warnings"] == [
        {"alpha_id": "alpha1", "type": "disabled_alpha_has_usage", "used_daily_orders": 1}
    ]
