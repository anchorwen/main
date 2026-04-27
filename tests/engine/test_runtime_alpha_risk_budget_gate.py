"""Runtime Alpha risk budget gate tests."""
from datetime import datetime

from core.alpha.schema_versions import SCHEMA_ALPHA_RISK_BUDGET
from core.execution.gateway_contracts import OrderRequest
from core.runtime.alpha_budget_usage_store import AlphaBudgetUsageStore
from core.runtime.alpha_risk_budget_gate import AlphaRiskBudgetGate
from core.runtime.execution_gates import RuntimeExecutionApprovalChain
from core.runtime.execution_pipeline import RuntimeExecutionPipeline
from core.runtime.execution_gateway_router import ExecutionGatewayRouter
from core.runtime.integration_contracts import OrderSizingPolicy
from core.runtime.signal_order_builder import SignalOrderRequestBuilder
from core.execution.paper_gateway import PaperExecutionGateway
from core.strategies.contracts import Signal
from core.strategies.examples import ThresholdAlphaAgent
from core.strategies.registry import StrategyPluginRegistry, StrategyPluginRunner
from core.strategies.schema_versions import SCHEMA_SIGNAL


def _budget(**overrides):
    item = {
        "state": "active",
        "target_weight": 1.0,
        "score": 1.0,
        "risk_tier": "standard",
        "max_notional": 10000.0,
        "max_order_notional": 1000.0,
        "max_daily_orders": 2,
        "enabled": True,
        "reason": "allocatable",
    }
    item.update(overrides)
    return {"schema_version": SCHEMA_ALPHA_RISK_BUDGET, "budgets": {"alpha1": item}}


def _signal(alpha_id="alpha1"):
    return Signal(
        schema_version=SCHEMA_SIGNAL,
        signal_id="sig1",
        strategy_id="alpha1",
        symbol="XAUUSD",
        side="buy",
        strength=1.0,
        confidence=1.0,
        generated_at=datetime.utcnow(),
        extensions={"alpha_id": alpha_id},
    )


def _order(alpha_id="alpha1", quantity=1.0):
    return OrderRequest(
        order_id="order1",
        correlation_id="sig1",
        symbol="XAUUSD",
        side="buy",
        quantity=quantity,
        order_type="market",
        venue="PAPER",
        created_at=datetime.utcnow(),
        metadata={"strategy_id": "alpha1", "alpha_id": alpha_id},
    )


class TestAlphaRiskBudgetGate:
    def test_allows_within_budget_and_tracks_counts(self):
        gate = AlphaRiskBudgetGate(_budget())
        approval = gate.approve(_signal(), _order(), {"price": 500})
        assert approval.approved is True
        assert approval.gate == "alpha_risk_budget"
        assert approval.constraints["alpha_id"] == "alpha1"
        assert gate.counts() == {"alpha1": 1}

    def test_denies_missing_budget_by_default(self):
        approval = AlphaRiskBudgetGate(_budget()).approve(_signal("missing"), _order("missing"), {"price": 100})
        assert approval.approved is False
        assert approval.reasons == ["alpha_budget_missing(missing)"]

    def test_can_allow_missing_budget_when_configured(self):
        approval = AlphaRiskBudgetGate(_budget(), deny_missing=False).approve(_signal("missing"), _order("missing"), {"price": 100})
        assert approval.approved is True

    def test_denies_disabled_budget(self):
        approval = AlphaRiskBudgetGate(_budget(enabled=False)).approve(_signal(), _order(), {"price": 100})
        assert approval.approved is False
        assert approval.reasons == ["alpha_budget_disabled(alpha1)"]

    def test_denies_order_notional_exceeded(self):
        approval = AlphaRiskBudgetGate(_budget(max_order_notional=100)).approve(_signal(), _order(quantity=2), {"price": 100})
        assert approval.approved is False
        assert "alpha_order_notional_exceeded(200.00>100.00)" in approval.reasons

    def test_denies_daily_order_limit_exceeded(self):
        gate = AlphaRiskBudgetGate(_budget(max_daily_orders=1))
        assert gate.approve(_signal(), _order("alpha1", 1), {"price": 100}).approved is True
        second = gate.approve(_signal(), _order("alpha1", 1), {"price": 100})
        assert second.approved is False
        assert second.reasons == ["alpha_daily_order_limit_exceeded(2>1)"]
        gate.reset_counts()
        assert gate.counts() == {}

    def test_pipeline_denies_by_alpha_budget_gate(self):
        registry = StrategyPluginRegistry()
        registry.register(ThresholdAlphaAgent("alpha1", "ema_bias", 1.0, -1.0))
        runner = StrategyPluginRunner(registry)
        runner.warmup_all({})
        router = ExecutionGatewayRouter()
        router.register("PAPER", PaperExecutionGateway())
        pipeline = RuntimeExecutionPipeline(
            strategy_runner=runner,
            order_builder=SignalOrderRequestBuilder(OrderSizingPolicy(base_quantity=10), default_venue="PAPER"),
            gateway_router=router,
            approval_chain=RuntimeExecutionApprovalChain([AlphaRiskBudgetGate(_budget(max_order_notional=1))]),
        )
        result = pipeline.run({"ema_bias": 2.0}, {"price": 2000}, {"runtime_cycle_id": "cycle_budget"})
        assert len(result.orders) == 0
        assert result.approvals[0].gate == "alpha_risk_budget"
        assert result.skipped_signals[0]["reason"] == "execution_denied"


    def test_persistent_usage_store_enforces_across_gate_instances(self, tmp_path):
        usage_path = tmp_path / "alpha_budget_usage.json"
        first = AlphaRiskBudgetGate(_budget(max_daily_orders=1), usage_store=AlphaBudgetUsageStore(usage_path))
        assert first.approve(_signal(), _order(), {"price": 100}).approved is True
        second = AlphaRiskBudgetGate(_budget(max_daily_orders=1), usage_store=AlphaBudgetUsageStore(usage_path))
        approval = second.approve(_signal(), _order(), {"price": 100})
        assert approval.approved is False
        assert approval.reasons == ["alpha_daily_order_limit_exceeded(2>1)"]
        assert AlphaBudgetUsageStore(usage_path).counts() == {"alpha1": 1}

    def test_usage_store_resets_on_new_date(self, tmp_path):
        usage_path = tmp_path / "alpha_budget_usage.json"
        store = AlphaBudgetUsageStore(usage_path, usage_date="2026-01-01")
        store.increment("alpha1")
        assert store.counts() == {"alpha1": 1}
        next_day = AlphaBudgetUsageStore(usage_path, usage_date="2026-01-02")
        assert next_day.counts() == {}
        assert next_day.to_dict()["usage_date"] == "2026-01-02"
