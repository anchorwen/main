"""A1 runtime integration pipeline."""
from datetime import datetime

from core.contracts.ids import new_runtime_cycle_id
from core.execution.quality_analyzer import ExecutionQualityAnalyzer
from core.execution.quality_contracts import ExecutionBenchmark
from core.runtime.execution_gateway_router import ExecutionGatewayRouter
from core.runtime.integration_contracts import RuntimePipelineResult
from core.runtime.signal_order_builder import SignalOrderRequestBuilder
from core.runtime.schema_versions import SCHEMA_RUNTIME_PIPELINE_RESULT
from core.strategies.registry import StrategyPluginRunner


class RuntimeExecutionPipeline:
    """Runs strategies, gates orders, routes execution, and reports quality."""

    def __init__(self, strategy_runner: StrategyPluginRunner,
                 order_builder: SignalOrderRequestBuilder,
                 gateway_router: ExecutionGatewayRouter,
                 quality_analyzer: ExecutionQualityAnalyzer | None = None,
                 approval_chain=None,
                 evidence_writer=None):
        self._strategy_runner = strategy_runner
        self._order_builder = order_builder
        self._router = gateway_router
        self._quality = quality_analyzer or ExecutionQualityAnalyzer()
        self._approval_chain = approval_chain
        self._evidence_writer = evidence_writer

    def run(self, feature_snapshot, market: dict, context: dict | None = None) -> RuntimePipelineResult:
        context = context or {}
        runtime_cycle_id = context.get("runtime_cycle_id") or new_runtime_cycle_id()
        signals = self._strategy_runner.run_all(feature_snapshot, context)
        orders = []
        skipped = []
        approvals = []
        benchmarks = {}
        for signal in signals:
            request = self._order_builder.build(signal, market)
            if request is None:
                skipped.append({
                    "signal_id": signal.signal_id,
                    "strategy_id": signal.strategy_id,
                    "side": signal.side,
                    "reason": "not_actionable_or_below_threshold",
                })
                continue
            gate_approvals = self._approve(signal, request, market)
            approvals.extend(gate_approvals)
            denied = [approval for approval in gate_approvals if not approval.approved]
            if denied:
                skipped.append({
                    "signal_id": signal.signal_id,
                    "strategy_id": signal.strategy_id,
                    "side": signal.side,
                    "order_id": request.order_id,
                    "reason": "execution_denied",
                    "denied_by": denied[-1].gate,
                    "details": denied[-1].reasons,
                })
                continue
            order = self._router.submit_order(request, market)
            orders.append(order)
            benchmarks[order.order_id] = self._benchmark_for(signal, request.order_id, market)
        report = self._quality.build_report(orders, benchmarks)
        result = RuntimePipelineResult(
            schema_version=SCHEMA_RUNTIME_PIPELINE_RESULT,
            runtime_cycle_id=runtime_cycle_id,
            generated_at=datetime.utcnow(),
            signals=signals,
            orders=orders,
            quality_report=report,
            skipped_signals=skipped,
            approvals=approvals,
        )
        if self._evidence_writer:
            self._evidence_writer.write_result(runtime_cycle_id=runtime_cycle_id, result=result)
        return result

    def _approve(self, signal, request, market: dict) -> list:
        if self._approval_chain is None:
            return []
        return self._approval_chain.approve(signal, request, market)

    def _benchmark_for(self, signal, order_id: str, market: dict) -> ExecutionBenchmark:
        price = market.get("arrival_price") or market.get("price") or market.get("last")
        submitted = market.get("submitted_price") or price
        return ExecutionBenchmark(
            order_id=order_id,
            decision_price=signal.extensions.get("decision_price") or price,
            arrival_price=price,
            submitted_price=submitted,
            benchmark_time=signal.generated_at,
            strategy_id=signal.strategy_id,
            metadata={"signal_id": signal.signal_id},
        )
