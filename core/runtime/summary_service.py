"""Runtime summary service for dashboard/operator views."""

from datetime import UTC, datetime
from statistics import mean
from typing import Any

from core.runtime.evidence_reader import RuntimeEvidenceReader
from core.runtime.schema_versions import SCHEMA_RUNTIME_SUMMARY


class RuntimeSummaryService:
    """Aggregates runtime evidence into dashboard-friendly summaries."""

    def __init__(self, evidence_reader: RuntimeEvidenceReader):
        self._reader = evidence_reader

    def summarize(self, limit: int | None = None) -> dict[str, Any]:
        cycle_ids = self._reader.list_cycle_ids()
        records = []
        for cycle_id in cycle_ids:
            record = self._reader.latest_cycle(cycle_id)
            if record:
                records.append(record)
        records.sort(key=lambda item: item.get("generated_at", ""), reverse=True)
        if limit is not None:
            records = records[:limit]
        return {
            "schema_version": SCHEMA_RUNTIME_SUMMARY,
            "generated_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "cycle_count": len(records),
            "latest_cycle_id": records[0]["runtime_cycle_id"] if records else None,
            "totals": self._totals(records),
            "averages": self._averages(records),
            "per_strategy": self._per_strategy(records),
            "per_venue": self._per_venue(records),
            "cycles": [self._cycle_summary(record) for record in records],
        }

    def _totals(self, records: list[dict]) -> dict[str, Any]:
        approvals = [a for r in records for a in self._payload(r).get("approvals", [])]
        return {
            "signals": sum(r.get("signal_count", 0) for r in records),
            "orders": sum(r.get("order_count", 0) for r in records),
            "approvals": sum(r.get("approval_count", 0) for r in records),
            "skipped": sum(r.get("skipped_count", 0) for r in records),
            "denied_approvals": len([a for a in approvals if not a.get("approved", False)]),
            "filled_orders": sum(
                (self._quality(r).get("filled_order_count") or 0) for r in records
            ),
            "rejected_orders": sum(
                (self._quality(r).get("rejected_order_count") or 0) for r in records
            ),
        }

    def _averages(self, records: list[dict]) -> dict[str, Any]:
        return {
            "fill_ratio": self._avg([self._quality(r).get("average_fill_ratio") for r in records]),
            "latency_ms": self._avg([self._quality(r).get("average_latency_ms") for r in records]),
            "arrival_slippage_bps": self._avg(
                [self._quality(r).get("average_arrival_slippage_bps") for r in records]
            ),
        }

    def _per_strategy(self, records: list[dict]) -> dict[str, dict[str, Any]]:
        summary: dict[str, dict[str, Any]] = {}
        for record in records:
            payload = self._payload(record)
            for signal in payload.get("signals", []):
                sid = signal.get("strategy_id", "unknown")
                bucket = summary.setdefault(sid, {"signals": 0, "orders": 0, "denied": 0})
                bucket["signals"] += 1
            for approval in payload.get("approvals", []):
                sid = self._strategy_for_signal(payload, approval.get("signal_id"))
                bucket = summary.setdefault(sid, {"signals": 0, "orders": 0, "denied": 0})
                if not approval.get("approved", False):
                    bucket["denied"] += 1
            for metric in (payload.get("quality_report") or {}).get("order_metrics", []):
                sid = metric.get("strategy_id") or "unknown"
                bucket = summary.setdefault(sid, {"signals": 0, "orders": 0, "denied": 0})
                bucket["orders"] += 1
        return summary

    def _per_venue(self, records: list[dict]) -> dict[str, dict[str, Any]]:
        summary: dict[str, dict[str, Any]] = {}
        for record in records:
            venue_summary = (self._payload(record).get("quality_report") or {}).get(
                "venue_summary"
            ) or {}
            for venue, values in venue_summary.items():
                bucket = summary.setdefault(
                    venue,
                    {
                        "orders": 0,
                        "filled_orders": 0,
                        "rejected_orders": 0,
                        "fill_ratios": [],
                        "latencies": [],
                    },
                )
                bucket["orders"] += values.get("order_count", 0)
                bucket["filled_orders"] += values.get("filled_order_count", 0)
                bucket["rejected_orders"] += values.get("rejected_order_count", 0)
                bucket["fill_ratios"].append(values.get("average_fill_ratio"))
                bucket["latencies"].append(values.get("average_latency_ms"))
        return {
            venue: {
                "orders": data["orders"],
                "filled_orders": data["filled_orders"],
                "rejected_orders": data["rejected_orders"],
                "average_fill_ratio": self._avg(data["fill_ratios"]),
                "average_latency_ms": self._avg(data["latencies"]),
            }
            for venue, data in summary.items()
        }

    def _cycle_summary(self, record: dict) -> dict[str, Any]:
        quality = self._quality(record)
        return {
            "runtime_cycle_id": record.get("runtime_cycle_id"),
            "generated_at": record.get("generated_at"),
            "signal_count": record.get("signal_count", 0),
            "order_count": record.get("order_count", 0),
            "approval_count": record.get("approval_count", 0),
            "skipped_count": record.get("skipped_count", 0),
            "filled_order_count": quality.get("filled_order_count"),
            "average_fill_ratio": quality.get("average_fill_ratio"),
        }

    def _payload(self, record: dict) -> dict:
        return record.get("payload") or {}

    def _quality(self, record: dict) -> dict:
        return self._payload(record).get("quality_report") or record.get("quality_summary") or {}

    def _strategy_for_signal(self, payload: dict, signal_id: str | None) -> str:
        for signal in payload.get("signals", []):
            if signal.get("signal_id") == signal_id:
                return signal.get("strategy_id", "unknown")
        return "unknown"

    def _avg(self, values: list) -> float | None:
        clean = [float(v) for v in values if v is not None]
        return round(mean(clean), 6) if clean else None
