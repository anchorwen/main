"""Runtime governance integration tests."""
from datetime import datetime

import pytest

from core.execution.paper_gateway import PaperExecutionGateway
from core.runtime.approval_contracts import ExecutionApproval
from core.runtime.schema_versions import SCHEMA_EXECUTION_APPROVAL
from core.runtime.execution_gateway_router import ExecutionGatewayRouter
from core.runtime.execution_gates import RuntimeExecutionApprovalChain, RuntimeGovernanceGate, RuntimeRiskGate
from core.runtime.execution_pipeline import RuntimeExecutionPipeline
from core.runtime.integration_contracts import OrderSizingPolicy
from core.runtime.signal_order_builder import SignalOrderRequestBuilder
from core.strategies.contracts import Signal
from core.strategies.examples import ThresholdAlphaAgent
from core.strategies.schema_versions import SCHEMA_SIGNAL
from core.strategies.registry import StrategyPluginRegistry, StrategyPluginRunner


def _signal(side="buy", strength=1.0, confidence=1.0, strategy_id="alpha1", symbol="XAUUSD"):
    return Signal(
        schema_version=SCHEMA_SIGNAL,
        signal_id="sig1",
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        strength=strength,
        confidence=confidence,
        generated_at=datetime.utcnow(),
    )


def _order(signal=None, quantity=1.0, venue="PAPER"):
    signal = signal or _signal()
    return SignalOrderRequestBuilder(OrderSizingPolicy(base_quantity=quantity), default_venue=venue).build(signal, {"price": 100})


def _pipeline(approval_chain=None):
    registry = StrategyPluginRegistry()
    agent = ThresholdAlphaAgent("alpha1", "ema_bias", 1.0, -1.0)
    registry.register(agent)
    runner = StrategyPluginRunner(registry)
    runner.warmup_all({})
    router = ExecutionGatewayRouter()
    gateway = PaperExecutionGateway()
    router.register("PAPER", gateway)
    return RuntimeExecutionPipeline(
        strategy_runner=runner,
        order_builder=SignalOrderRequestBuilder(OrderSizingPolicy(base_quantity=10), default_venue="PAPER"),
        gateway_router=router,
        approval_chain=approval_chain,
    ), gateway


class TestExecutionApproval:
    def test_denied_approval_requires_reason(self):
        with pytest.raises(ValueError):
            ExecutionApproval(
                schema_version=SCHEMA_EXECUTION_APPROVAL,
                approval_id="a1",
                signal_id="s1",
                order_id="o1",
                approved=False,
                gate="risk",
                decided_at=datetime.utcnow(),
            )

    def test_approval_to_dict(self):
        signal = _signal()
        order = _order(signal)
        approval = ExecutionApproval.allow("a1", signal, order, "runtime_risk")
        assert approval.to_dict()["approved"] is True


class TestRuntimeRiskGate:
    def test_quantity_symbol_side_and_notional_limits(self):
        signal = _signal(symbol="XAUUSD")
        order = _order(signal, quantity=10)
        gate = RuntimeRiskGate(max_quantity=5, allowed_symbols={"EURUSD"}, max_notional=500)
        approval = gate.approve(signal, order, {"price": 100})
        assert approval.approved is False
        assert "quantity_limit_exceeded(10.0>5)" in approval.reasons
        assert "symbol_not_allowed(XAUUSD)" in approval.reasons
        assert "notional_limit_exceeded(1000.00>500.00)" in approval.reasons

    def test_risk_gate_allows_valid_order(self):
        signal = _signal()
        order = _order(signal, quantity=1)
        approval = RuntimeRiskGate(max_quantity=5, allowed_symbols={"XAUUSD"}, max_notional=500).approve(
            signal, order, {"price": 100}
        )
        assert approval.approved is True


class TestRuntimeGovernanceGate:
    def test_system_halt_frozen_strategy_and_venue_blocks(self):
        signal = _signal(strategy_id="alpha1")
        order = _order(signal, venue="PAPER")
        gate = RuntimeGovernanceGate(
            allowed_strategy_ids={"alpha2"},
            frozen_strategy_ids={"alpha1"},
            allowed_venues={"FIX"},
            system_halted=True,
        )
        approval = gate.approve(signal, order, {})
        assert approval.approved is False
        assert "system_halted" in approval.reasons
        assert "strategy_frozen(alpha1)" in approval.reasons
        assert "strategy_not_allowed(alpha1)" in approval.reasons
        assert "venue_not_allowed(PAPER)" in approval.reasons

    def test_governance_gate_allows_valid_order(self):
        signal = _signal(strategy_id="alpha1")
        order = _order(signal, venue="PAPER")
        gate = RuntimeGovernanceGate(allowed_strategy_ids={"alpha1"}, allowed_venues={"PAPER"})
        assert gate.approve(signal, order, {}).approved is True


class TestRuntimeExecutionApprovalChain:
    def test_chain_stops_after_denial(self):
        signal = _signal()
        order = _order(signal, quantity=10)
        chain = RuntimeExecutionApprovalChain([
            RuntimeRiskGate(max_quantity=5),
            RuntimeGovernanceGate(system_halted=True),
        ])
        approvals = chain.approve(signal, order, {"price": 100})
        assert len(approvals) == 1
        assert approvals[0].gate == "runtime_risk"
        assert approvals[0].approved is False


class TestRuntimePipelineGovernanceIntegration:
    def test_pipeline_denied_by_risk_does_not_execute(self):
        chain = RuntimeExecutionApprovalChain([
            RuntimeRiskGate(max_quantity=1),
            RuntimeGovernanceGate(allowed_strategy_ids={"alpha1"}, allowed_venues={"PAPER"}),
        ])
        pipeline, gateway = _pipeline(chain)
        result = pipeline.run({"ema_bias": 2.0}, {"price": 2000.0}, {})
        assert len(result.orders) == 0
        assert len(gateway.list_orders()) == 0
        assert result.skipped_signals[0]["reason"] == "execution_denied"
        assert result.skipped_signals[0]["denied_by"] == "runtime_risk"
        assert result.approvals[0].approved is False

    def test_pipeline_denied_by_governance_does_not_execute(self):
        chain = RuntimeExecutionApprovalChain([
            RuntimeRiskGate(max_quantity=100),
            RuntimeGovernanceGate(frozen_strategy_ids={"alpha1"}),
        ])
        pipeline, gateway = _pipeline(chain)
        result = pipeline.run({"ema_bias": 2.0}, {"price": 2000.0}, {})
        assert len(result.orders) == 0
        assert len(gateway.list_orders()) == 0
        assert result.skipped_signals[0]["denied_by"] == "runtime_governance"
        assert result.approvals[-1].approved is False

    def test_pipeline_approved_order_executes_and_records_approvals(self):
        chain = RuntimeExecutionApprovalChain([
            RuntimeRiskGate(max_quantity=100, allowed_symbols={"XAUUSD"}, max_notional=50_000),
            RuntimeGovernanceGate(allowed_strategy_ids={"alpha1"}, allowed_venues={"PAPER"}),
        ])
        pipeline, gateway = _pipeline(chain)
        result = pipeline.run({"ema_bias": 2.0}, {"price": 2000.0}, {})
        assert len(result.orders) == 1
        assert len(gateway.list_orders()) == 1
        assert [approval.gate for approval in result.approvals] == ["runtime_risk", "runtime_governance"]
        assert all(approval.approved for approval in result.approvals)
        assert result.to_dict()["approvals"][0]["approved"] is True
