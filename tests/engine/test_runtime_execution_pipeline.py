"""A1 runtime integration pipeline tests."""

from datetime import UTC, datetime

import pytest

from core.execution.paper_gateway import PaperExecutionGateway
from core.runtime.execution_gateway_router import ExecutionGatewayRouter
from core.runtime.execution_pipeline import RuntimeExecutionPipeline
from core.runtime.integration_contracts import OrderSizingPolicy
from core.runtime.schema_versions import SCHEMA_RUNTIME_PIPELINE_RESULT
from core.runtime.signal_order_builder import SignalOrderRequestBuilder
from core.strategies.contracts import Signal
from core.strategies.examples import ThresholdAlphaAgent
from core.strategies.registry import StrategyPluginRegistry, StrategyPluginRunner
from core.strategies.schema_versions import SCHEMA_SIGNAL


def _signal(side="buy", strength=1.0, confidence=1.0, extensions=None):
    return Signal(
        schema_version=SCHEMA_SIGNAL,
        signal_id="sig1",
        strategy_id="alpha1",
        symbol="XAUUSD",
        side=side,
        strength=strength,
        confidence=confidence,
        generated_at=datetime.now(UTC).replace(tzinfo=None),
        extensions=extensions or {},
    )


class TestSignalOrderRequestBuilder:
    def test_builds_market_order_from_signal(self):
        builder = SignalOrderRequestBuilder(
            OrderSizingPolicy(base_quantity=10), default_venue="PAPER"
        )
        request = builder.build(_signal(strength=0.5, confidence=0.8), {"price": 2000})
        assert request.symbol == "XAUUSD"  # type: ignore[reportOptionalMemberAccess]
        assert request.side == "buy"  # type: ignore[reportOptionalMemberAccess]
        assert request.quantity == 4.0  # type: ignore[reportOptionalMemberAccess]
        assert request.venue == "PAPER"  # type: ignore[reportOptionalMemberAccess]
        assert request.metadata["strategy_id"] == "alpha1"  # type: ignore[reportOptionalMemberAccess]

    def test_skips_hold_flat_and_below_threshold(self):
        builder = SignalOrderRequestBuilder(
            OrderSizingPolicy(base_quantity=10, min_confidence=0.6, min_strength=0.2)
        )
        assert builder.build(_signal(side="hold"), {}) is None
        assert builder.build(_signal(side="flat"), {}) is None
        assert builder.build(_signal(confidence=0.5), {}) is None
        assert builder.build(_signal(strength=0.1), {}) is None

    def test_limit_order_uses_signal_or_market_price(self):
        builder = SignalOrderRequestBuilder()
        explicit = builder.build(
            _signal(extensions={"order_type": "limit", "limit_price": 1999.0}), {}
        )
        fallback = builder.build(_signal(extensions={"order_type": "limit"}), {"price": 2000.0})
        assert explicit.limit_price == 1999.0  # type: ignore[reportOptionalMemberAccess]
        assert fallback.limit_price == 2000.0  # type: ignore[reportOptionalMemberAccess]

    def test_sizing_policy_validation(self):
        with pytest.raises(ValueError):
            OrderSizingPolicy(base_quantity=0)
        with pytest.raises(ValueError):
            OrderSizingPolicy(max_quantity=0)
        with pytest.raises(ValueError):
            OrderSizingPolicy(min_confidence=2)


class TestExecutionGatewayRouter:
    def test_routes_to_registered_gateway(self):
        router = ExecutionGatewayRouter()
        gateway = PaperExecutionGateway()
        router.register("PAPER", gateway)
        request = SignalOrderRequestBuilder().build(_signal(), {"price": 2000})
        order = router.submit_order(request, {"price": 2000})  # type: ignore[reportArgumentType]
        assert order.status == "filled"
        assert len(router.list_orders()) == 1

    def test_missing_gateway_rejected(self):
        router = ExecutionGatewayRouter()
        request = SignalOrderRequestBuilder().build(_signal(), {"price": 2000})
        with pytest.raises(ValueError):
            router.submit_order(request, {"price": 2000})  # type: ignore[reportArgumentType]


class TestRuntimeExecutionPipeline:
    def test_end_to_end_threshold_strategy_to_paper_execution_and_quality_report(self):
        registry = StrategyPluginRegistry()
        agent = ThresholdAlphaAgent("alpha1", "ema_bias", 1.0, -1.0)
        registry.register(agent)
        runner = StrategyPluginRunner(registry)
        runner.warmup_all({})
        router = ExecutionGatewayRouter()
        router.register("PAPER", PaperExecutionGateway())
        pipeline = RuntimeExecutionPipeline(
            strategy_runner=runner,
            order_builder=SignalOrderRequestBuilder(
                OrderSizingPolicy(base_quantity=10), default_venue="PAPER"
            ),
            gateway_router=router,
        )
        result = pipeline.run({"ema_bias": 2.0}, {"price": 2000.0}, {})
        assert result.schema_version == SCHEMA_RUNTIME_PIPELINE_RESULT
        assert len(result.signals) == 1
        assert len(result.orders) == 1
        assert result.orders[0].status == "filled"
        assert result.quality_report.order_count == 1
        assert result.quality_report.order_metrics[0].strategy_id == "alpha1"
        assert result.to_dict()["quality_report"]["order_count"] == 1

    def test_pipeline_skips_non_actionable_signal(self):
        registry = StrategyPluginRegistry()
        agent = ThresholdAlphaAgent("alpha1", "ema_bias", 10.0, -10.0)
        registry.register(agent)
        runner = StrategyPluginRunner(registry)
        runner.warmup_all({})
        router = ExecutionGatewayRouter()
        router.register("PAPER", PaperExecutionGateway())
        pipeline = RuntimeExecutionPipeline(runner, SignalOrderRequestBuilder(), router)
        result = pipeline.run({"ema_bias": 0.0}, {"price": 2000.0}, {})
        assert len(result.orders) == 0
        assert len(result.skipped_signals) == 1
        assert result.quality_report.order_count == 0
