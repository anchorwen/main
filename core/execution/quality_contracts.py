"""Execution quality analytics contracts."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ExecutionBenchmark:
    order_id: str
    decision_price: float | None = None
    arrival_price: float | None = None
    submitted_price: float | None = None
    vwap_price: float | None = None
    benchmark_time: datetime | None = None
    strategy_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionQualityMetric:
    order_id: str
    correlation_id: str
    symbol: str
    side: str
    venue: str
    order_type: str
    status: str
    requested_quantity: float
    filled_quantity: float
    average_fill_price: float
    fill_ratio: float
    partial_fill_ratio: float
    decision_slippage_bps: float | None
    arrival_slippage_bps: float | None
    submitted_slippage_bps: float | None
    vwap_slippage_bps: float | None = None
    latency_ms: float = 0.0
    fill_count: int = 0
    reject_reason: str | None = None
    strategy_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "correlation_id": self.correlation_id,
            "symbol": self.symbol,
            "side": self.side,
            "venue": self.venue,
            "order_type": self.order_type,
            "status": self.status,
            "requested_quantity": self.requested_quantity,
            "filled_quantity": self.filled_quantity,
            "average_fill_price": self.average_fill_price,
            "fill_ratio": self.fill_ratio,
            "partial_fill_ratio": self.partial_fill_ratio,
            "decision_slippage_bps": self.decision_slippage_bps,
            "arrival_slippage_bps": self.arrival_slippage_bps,
            "submitted_slippage_bps": self.submitted_slippage_bps,
            "vwap_slippage_bps": self.vwap_slippage_bps,
            "latency_ms": self.latency_ms,
            "fill_count": self.fill_count,
            "reject_reason": self.reject_reason,
            "strategy_id": self.strategy_id,
        }


@dataclass(frozen=True)
class ImplementationShortfall:
    """Decomposition of arrival-price slippage into components.

    All values in bps; positive = cost to the strategy.
    """

    order_id: str
    symbol: str
    side: str
    decision_price: float
    arrival_price: float
    average_fill_price: float | None = None
    filled_quantity: float = 0.0
    requested_quantity: float = 0.0
    total_shortfall_bps: float = 0.0
    delay_cost_bps: float = 0.0
    market_impact_bps: float = 0.0
    opportunity_cost_bps: float = 0.0
    fill_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "decision_price": self.decision_price,
            "arrival_price": self.arrival_price,
            "average_fill_price": self.average_fill_price,
            "filled_quantity": self.filled_quantity,
            "requested_quantity": self.requested_quantity,
            "total_shortfall_bps": self.total_shortfall_bps,
            "delay_cost_bps": self.delay_cost_bps,
            "market_impact_bps": self.market_impact_bps,
            "opportunity_cost_bps": self.opportunity_cost_bps,
            "fill_rate": self.fill_rate,
        }


@dataclass(frozen=True)
class ExecutionQualityReport:
    schema_version: str
    generated_at: datetime
    order_count: int
    filled_order_count: int
    rejected_order_count: int
    average_fill_ratio: float
    average_latency_ms: float
    average_decision_slippage_bps: float | None
    average_arrival_slippage_bps: float | None
    average_submitted_slippage_bps: float | None
    order_metrics: list[ExecutionQualityMetric] = field(default_factory=list)
    average_vwap_slippage_bps: float | None = None
    venue_summary: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at.isoformat(),
            "order_count": self.order_count,
            "filled_order_count": self.filled_order_count,
            "rejected_order_count": self.rejected_order_count,
            "average_fill_ratio": self.average_fill_ratio,
            "average_latency_ms": self.average_latency_ms,
            "average_decision_slippage_bps": self.average_decision_slippage_bps,
            "average_arrival_slippage_bps": self.average_arrival_slippage_bps,
            "average_submitted_slippage_bps": self.average_submitted_slippage_bps,
            "average_vwap_slippage_bps": self.average_vwap_slippage_bps,
            "venue_summary": self.venue_summary,
            "order_metrics": [m.to_dict() for m in self.order_metrics],
        }
