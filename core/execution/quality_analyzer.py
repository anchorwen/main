"""Execution quality analytics with time-series slippage tracking and VWAP benchmarking."""

import json
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

from core.execution.gateway_contracts import OrderState
from core.execution.quality_contracts import (
    ExecutionBenchmark,
    ExecutionQualityMetric,
    ExecutionQualityReport,
    ImplementationShortfall,
)
from core.execution.schema_versions import SCHEMA_EXECUTION_QUALITY_REPORT

# ── VWAP benchmark ────────────────────────────────────────────────────────


def compute_vwap(fills: list[dict[str, float]]) -> float | None:
    """Compute Volume-Weighted Average Price from a list of fills.

    Each fill is a dict with ``price`` and ``volume`` keys.
    Returns None if no fills or total volume is zero.
    """
    total_volume = 0.0
    total_value = 0.0
    for f in fills:
        price = float(f.get("price", 0))
        volume = float(f.get("volume", 0))
        if price > 0 and volume > 0:
            total_value += price * volume
            total_volume += volume
    if total_volume <= 0:
        return None
    return round(total_value / total_volume, 6)


class SlippageTracker:
    """Time-series slippage recorder for trend analysis.

    Appends one JSONL record per trade with slippage, session, and day-of-week.
    Enables queries like "Friday afternoon slippage vs Tuesday morning".
    """

    def __init__(self, data_path: str | Path):
        self._path = Path(data_path)

    def record(
        self,
        *,
        symbol: str,
        strategy: str,
        side: str,
        volume: float,
        decision_price: float | None,
        fill_price: float,
        timestamp: str = "",
        session: str = "",
    ) -> None:
        slippage_bps = None
        if decision_price and decision_price > 0 and fill_price > 0:
            if side == "buy":
                slippage_bps = round((fill_price - decision_price) / decision_price * 10000, 4)
            else:
                slippage_bps = round((decision_price - fill_price) / decision_price * 10000, 4)

        if timestamp:
            try:
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                dt = datetime.now(UTC).replace(tzinfo=None)
        else:
            dt = datetime.now(UTC).replace(tzinfo=None)

        record = {
            "time": dt.isoformat(),
            "symbol": symbol,
            "strategy": strategy,
            "side": side,
            "volume": volume,
            "decision_price": decision_price,
            "fill_price": fill_price,
            "slippage_bps": slippage_bps,
            "day_of_week": dt.strftime("%A"),
            "hour_utc": dt.hour,
            "session": session,
        }

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def query(
        self, *, min_time: str = "", max_time: str = "", day: str = ""
    ) -> list[dict[str, Any]]:
        """Query slippage records with optional filters."""
        results: list[dict[str, Any]] = []
        if not self._path.exists():
            return results
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if min_time and r.get("time", "") < min_time:
                continue
            if max_time and r.get("time", "") > max_time:
                continue
            if day and r.get("day_of_week", "") != day:
                continue
            results.append(r)
        return results

    def summary(self) -> dict[str, Any]:
        """Aggregate slippage summary by day-of-week and hour."""
        if not self._path.exists():
            return {"total_records": 0}

        records = self.query()
        if not records:
            return {"total_records": 0}

        by_day: dict[str, list[float]] = {}
        by_hour: dict[int, list[float]] = {}
        all_slips: list[float] = []

        for r in records:
            s = r.get("slippage_bps")
            if s is None:
                continue
            all_slips.append(s)
            d = r.get("day_of_week", "Unknown")
            h = r.get("hour_utc", 99)
            by_day.setdefault(d, []).append(s)
            by_hour.setdefault(h, []).append(s)

        day_summary = {}
        for d, vals in sorted(by_day.items()):
            day_summary[d] = {
                "count": len(vals),
                "avg_bps": round(mean(vals), 2),
                "min_bps": round(min(vals), 2),
                "max_bps": round(max(vals), 2),
            }

        hour_summary = {}
        for h, vals in sorted(by_hour.items()):
            hour_summary[str(h)] = {
                "count": len(vals),
                "avg_bps": round(mean(vals), 2),
                "min_bps": round(min(vals), 2),
                "max_bps": round(max(vals), 2),
            }

        return {
            "total_records": len(records),
            "overall_avg_bps": round(mean(all_slips), 2),
            "overall_max_bps": round(max(all_slips), 2),
            "by_day": day_summary,
            "by_hour_utc": hour_summary,
        }


def compute_implementation_shortfall(
    *,
    order_id: str,
    symbol: str,
    side: str,
    decision_price: float,
    arrival_price: float,
    average_fill_price: float | None = None,
    filled_quantity: float = 0.0,
    requested_quantity: float = 0.0,
    submitted_price: float | None = None,
) -> ImplementationShortfall:
    """Decompose execution shortfall into delay, impact, and opportunity cost.

    Implementation Shortfall (Perold 1988) decomposes the total slippage from
    decision time to final fill:

    - **delay_cost**: price move between decision and arrival/submission
    - **market_impact**: price move between arrival and fill (execution cost)
    - **opportunity_cost**: cost of unfilled quantity (if partial fill)

    All values in bps; positive = unfavourable (cost to buyer).
    """
    fill_price = average_fill_price or arrival_price
    # Buy/Long: cost = fill - decision (higher fill = worse)
    # Sell/Short: cost = decision - fill (lower fill = worse)
    side_mult = 1.0 if side.lower() in ("buy", "long") else -1.0
    fill_rate = filled_quantity / requested_quantity if requested_quantity > 0 else 1.0

    def _bps(ref_price: float, exec_price: float) -> float:
        if ref_price <= 0:
            return 0.0
        return round(side_mult * (exec_price - ref_price) / ref_price * 10000, 6)

    # Arrival price for delay calc: use submitted_price if available, else arrival_price
    exec_start = submitted_price if submitted_price and submitted_price > 0 else arrival_price

    delay_cost = _bps(decision_price, exec_start)
    market_impact = _bps(exec_start, fill_price)
    total_shortfall = _bps(decision_price, fill_price)

    # Opportunity cost: unfilled portion valued at spread cost
    unfilled_ratio = max(0.0, 1.0 - fill_rate)
    opportunity_cost = round(unfilled_ratio * abs(total_shortfall), 6) if fill_rate < 1.0 else 0.0

    # Adjust market_impact to ensure decomposition sums to total
    residual = round(total_shortfall - delay_cost - market_impact - opportunity_cost, 6)
    market_impact = round(market_impact + residual, 6)

    return ImplementationShortfall(
        order_id=order_id,
        symbol=symbol,
        side=side,
        decision_price=decision_price,
        arrival_price=arrival_price,
        average_fill_price=fill_price,
        filled_quantity=filled_quantity,
        requested_quantity=requested_quantity,
        total_shortfall_bps=total_shortfall,
        delay_cost_bps=delay_cost,
        market_impact_bps=market_impact,
        opportunity_cost_bps=opportunity_cost,
        fill_rate=round(fill_rate, 6),
    )


class ExecutionQualityAnalyzer:
    """Builds per-order execution quality metrics and aggregate reports."""

    def analyze_order(
        self, order: OrderState, benchmark: ExecutionBenchmark | None = None
    ) -> ExecutionQualityMetric:
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
            decision_slippage_bps=self._slippage_bps(
                order.side, benchmark.decision_price, order.average_price
            ),
            arrival_slippage_bps=self._slippage_bps(
                order.side, benchmark.arrival_price, order.average_price
            ),
            submitted_slippage_bps=self._slippage_bps(
                order.side, benchmark.submitted_price, order.average_price
            ),
            vwap_slippage_bps=self._slippage_bps(
                order.side, benchmark.vwap_price, order.average_price
            ),
            latency_ms=latency_ms,
            fill_count=len(order.fills),
            reject_reason=order.rejection_reason,
            strategy_id=benchmark.strategy_id,
        )

    def build_report(
        self, orders: list[OrderState], benchmarks: dict[str, ExecutionBenchmark] | None = None
    ) -> ExecutionQualityReport:
        benchmarks = benchmarks or {}
        metrics = [self.analyze_order(order, benchmarks.get(order.order_id)) for order in orders]
        return ExecutionQualityReport(
            schema_version=SCHEMA_EXECUTION_QUALITY_REPORT,
            generated_at=datetime.now(UTC).replace(tzinfo=None),
            order_count=len(metrics),
            filled_order_count=len([m for m in metrics if m.status == "filled"]),
            rejected_order_count=len([m for m in metrics if m.status == "rejected"]),
            average_fill_ratio=self._average([m.fill_ratio for m in metrics]),
            average_latency_ms=self._average([m.latency_ms for m in metrics]),
            average_decision_slippage_bps=self._nullable_average(
                [m.decision_slippage_bps for m in metrics]
            ),
            average_arrival_slippage_bps=self._nullable_average(
                [m.arrival_slippage_bps for m in metrics]
            ),
            average_submitted_slippage_bps=self._nullable_average(
                [m.submitted_slippage_bps for m in metrics]
            ),
            average_vwap_slippage_bps=self._nullable_average(
                [m.vwap_slippage_bps for m in metrics]
            ),
            venue_summary=self._venue_summary(metrics),
            order_metrics=metrics,
        )

    def _slippage_bps(
        self, side: str, benchmark_price: float | None, fill_price: float
    ) -> float | None:
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
                "average_arrival_slippage_bps": self._nullable_average(
                    [m.arrival_slippage_bps for m in scoped]
                ),
            }
        return summary
