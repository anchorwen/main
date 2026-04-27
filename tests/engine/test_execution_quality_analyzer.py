"""Execution quality analytics tests."""
from datetime import datetime, timedelta

from core.execution.fill_simulator import FillSimulationConfig, FillSimulator
from core.execution.gateway_contracts import Fill, OrderRequest
from core.execution.order_state_machine import OrderStateMachine
from core.execution.paper_gateway import PaperExecutionGateway
from core.execution.quality_analyzer import ExecutionQualityAnalyzer
from core.execution.schema_versions import SCHEMA_EXECUTION_QUALITY_REPORT
from core.execution.quality_contracts import ExecutionBenchmark


def _request(order_id="ord1", side="buy", quantity=10.0, order_type="market", limit_price=None):
    return OrderRequest(
        order_id=order_id,
        correlation_id="corr1",
        symbol="XAUUSD",
        side=side,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
    )


class TestExecutionQualityAnalyzer:
    def test_buy_slippage_is_positive_when_fill_above_benchmark(self):
        gateway = PaperExecutionGateway()
        order = gateway.submit_order(_request(side="buy"), {"price": 101.0})
        metric = ExecutionQualityAnalyzer().analyze_order(
            order,
            ExecutionBenchmark(order_id="ord1", decision_price=100.0, strategy_id="alpha1"),
        )
        assert metric.decision_slippage_bps == 100.0
        assert metric.strategy_id == "alpha1"
        assert metric.fill_ratio == 1.0
        assert metric.partial_fill_ratio == 0.0

    def test_sell_slippage_is_positive_when_fill_below_benchmark(self):
        gateway = PaperExecutionGateway()
        order = gateway.submit_order(_request(side="sell"), {"price": 99.0})
        metric = ExecutionQualityAnalyzer().analyze_order(
            order,
            ExecutionBenchmark(order_id="ord1", arrival_price=100.0),
        )
        assert metric.arrival_slippage_bps == 100.0

    def test_favorable_buy_slippage_is_negative(self):
        gateway = PaperExecutionGateway()
        order = gateway.submit_order(_request(side="buy"), {"price": 99.0})
        metric = ExecutionQualityAnalyzer().analyze_order(
            order,
            ExecutionBenchmark(order_id="ord1", arrival_price=100.0),
        )
        assert metric.arrival_slippage_bps == -100.0

    def test_partial_fill_metric(self):
        gateway = PaperExecutionGateway(fill_simulator=FillSimulator(FillSimulationConfig(max_fill_ratio=0.25)))
        order = gateway.submit_order(_request(quantity=10), {"price": 100.0})
        metric = ExecutionQualityAnalyzer().analyze_order(order, ExecutionBenchmark(order_id="ord1", submitted_price=99.0))
        assert order.status == "partial"
        assert metric.fill_ratio == 0.25
        assert metric.partial_fill_ratio == 0.75
        assert metric.submitted_slippage_bps == 101.010101

    def test_latency_uses_first_fill_time(self):
        sm = OrderStateMachine()
        created = datetime.utcnow() - timedelta(milliseconds=125)
        request = OrderRequest(
            order_id="ord1",
            correlation_id="corr1",
            symbol="XAUUSD",
            side="buy",
            quantity=1,
            created_at=created,
        )
        order = sm.create(request, "PAPER")
        sm.acknowledge(order)
        sm.accept(order)
        sm.apply_fill(order, Fill(fill_id="fill1", order_id="ord1", quantity=1, price=100))
        metric = ExecutionQualityAnalyzer().analyze_order(order)
        assert metric.latency_ms >= 0
        assert metric.latency_ms >= 100

    def test_report_aggregates_orders_and_venues(self):
        gateway = PaperExecutionGateway()
        order1 = gateway.submit_order(_request(order_id="ord1", side="buy"), {"price": 101.0})
        order2 = gateway.submit_order(_request(order_id="ord2", side="sell"), {"price": 99.0})
        report = ExecutionQualityAnalyzer().build_report(
            [order1, order2],
            {
                "ord1": ExecutionBenchmark(order_id="ord1", arrival_price=100.0),
                "ord2": ExecutionBenchmark(order_id="ord2", arrival_price=100.0),
            },
        )
        assert report.schema_version == SCHEMA_EXECUTION_QUALITY_REPORT
        assert report.order_count == 2
        assert report.filled_order_count == 2
        assert report.average_fill_ratio == 1.0
        assert report.average_arrival_slippage_bps == 100.0
        assert report.venue_summary["PAPER"]["order_count"] == 2
        assert report.to_dict()["order_metrics"][0]["order_id"] == "ord1"

    def test_report_handles_empty_orders(self):
        report = ExecutionQualityAnalyzer().build_report([])
        assert report.order_count == 0
        assert report.average_fill_ratio == 0.0
        assert report.average_arrival_slippage_bps is None
        assert report.venue_summary == {}

    def test_rejected_order_metrics(self):
        sm = OrderStateMachine()
        request = _request()
        order = sm.create(request, "PAPER")
        sm.acknowledge(order)
        sm.reject(order, "risk_block")
        report = ExecutionQualityAnalyzer().build_report([order])
        metric = report.order_metrics[0]
        assert report.rejected_order_count == 1
        assert metric.reject_reason == "risk_block"
        assert metric.fill_ratio == 0.0
