"""Execution quality analytics with time-series slippage tracking."""

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
)
from core.execution.schema_versions import SCHEMA_EXECUTION_QUALITY_REPORT


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
