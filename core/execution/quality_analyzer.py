"""Execution quality analytics."""
from datetime import datetime
from statistics import mean

from core.execution.schema_versions import SCHEMA_EXECUTION_QUALITY_REPORT
from core.execution.gateway_contracts import OrderState
from core.execution.quality_contracts import (
    ExecutionBenchmark,
    ExecutionQualityMetric,
    ExecutionQualityReport,
)


class ExecutionQualityAnalyzer:
    """Builds per-order execution quality metrics and aggregate reports."""

    def analyze_order(self, order: OrderState, benchmark: ExecutionBenchmark | None = None) -> ExecutionQualityMetric:
        benchmark = benchmark or ExecutionBenchmark(order_id=order.order_id)
        fill_ratio = self._ratio(order.filled_quantity, order.quantity)
        latency_ms = self._latency_ms(order)
        return ExecutionQualityMetric(
            order_id=order.order_id,
            correlation_id=order.correlation_id,
            symbol=order.symbol,
            side=order.side,
            venue=order.venue,
            order_type=order.order_type,
            status=order.status,
            requested_quantity=order.quantity,
            filled_quantity=order.filled_quantity,
            average_fill_price=order.average_price,
            fill_ratio=fill_ratio,
            partial_fill_ratio=round(1.0 - fill_ratio, 10),
            decision_slippage_bps=self._slippage_bps(order.side, benchmark.decision_price, order.average_price),
            arrival_slippage_bps=self._slippage_bps(order.side, benchmark.arrival_price, order.average_price),
            submitted_slippage_bps=self._slippage_bps(order.side, benchmark.submitted_price, order.average_price),
            latency_ms=latency_ms,
            fill_count=len(order.fills),
            reject_reason=order.rejection_reason,
            strategy_id=benchmark.strategy_id,
        )

    def build_report(self, orders: list[OrderState],
                     benchmarks: dict[str, ExecutionBenchmark] | None = None) -> ExecutionQualityReport:
        benchmarks = benchmarks or {}
        metrics = [self.analyze_order(order, benchmarks.get(order.order_id)) for order in orders]
        return ExecutionQualityReport(
            schema_version=SCHEMA_EXECUTION_QUALITY_REPORT,
            generated_at=datetime.utcnow(),
            order_count=len(metrics),
            filled_order_count=len([m for m in metrics if m.status == "filled"]),
            rejected_order_count=len([m for m in metrics if m.status == "rejected"]),
            average_fill_ratio=self._average([m.fill_ratio for m in metrics]),
            average_latency_ms=self._average([m.latency_ms for m in metrics]),
            average_decision_slippage_bps=self._nullable_average([m.decision_slippage_bps for m in metrics]),
            average_arrival_slippage_bps=self._nullable_average([m.arrival_slippage_bps for m in metrics]),
            average_submitted_slippage_bps=self._nullable_average([m.submitted_slippage_bps for m in metrics]),
            venue_summary=self._venue_summary(metrics),
            order_metrics=metrics,
        )

    def _slippage_bps(self, side: str, benchmark_price: float | None, fill_price: float) -> float | None:
        if benchmark_price is None or benchmark_price <= 0 or fill_price <= 0:
            return None
        if side == "buy":
            value = (fill_price - benchmark_price) / benchmark_price * 10000
        else:
            value = (benchmark_price - fill_price) / benchmark_price * 10000
        return round(value, 6)

    def _latency_ms(self, order: OrderState) -> float:
        if not order.fills:
            return round((order.updated_at - order.created_at).total_seconds() * 1000, 6)
        first_fill = min(fill.filled_at for fill in order.fills)
        return round((first_fill - order.created_at).total_seconds() * 1000, 6)

    def _ratio(self, numerator: float, denominator: float) -> float:
        if denominator <= 0:
            return 0.0
        return round(min(1.0, numerator / denominator), 10)

    def _average(self, values: list[float]) -> float:
        return round(mean(values), 6) if values else 0.0

    def _nullable_average(self, values: list[float | None]) -> float | None:
        clean = [v for v in values if v is not None]
        return round(mean(clean), 6) if clean else None

    def _venue_summary(self, metrics: list[ExecutionQualityMetric]) -> dict[str, dict]:
        venues = sorted({m.venue for m in metrics})
        summary = {}
        for venue in venues:
            scoped = [m for m in metrics if m.venue == venue]
            summary[venue] = {
                "order_count": len(scoped),
                "filled_order_count": len([m for m in scoped if m.status == "filled"]),
                "rejected_order_count": len([m for m in scoped if m.status == "rejected"]),
                "average_fill_ratio": self._average([m.fill_ratio for m in scoped]),
                "average_latency_ms": self._average([m.latency_ms for m in scoped]),
                "average_arrival_slippage_bps": self._nullable_average([
                    m.arrival_slippage_bps for m in scoped
                ]),
            }
        return summary
