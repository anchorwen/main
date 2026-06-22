"""Alpha performance store with JSON file persistence."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from core.alpha.schema_versions import (
    SCHEMA_ALPHA_PERFORMANCE_SNAPSHOT,
    SCHEMA_ALPHA_PERFORMANCE_STORE,
    SCHEMA_ALPHA_PERFORMANCE_SUMMARY,
)


@dataclass(frozen=True)
class AlphaPerformanceSnapshot:
    alpha_id: str
    metrics: dict[str, Any]
    captured_at: datetime = field(default_factory=datetime.utcnow)
    source: str = "manual"
    window: str = "latest"

    def __post_init__(self) -> None:
        if not self.alpha_id:
            raise ValueError("alpha_id is required")
        if not isinstance(self.metrics, dict):
            raise ValueError("metrics must be a dict")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_ALPHA_PERFORMANCE_SNAPSHOT,
            "alpha_id": self.alpha_id,
            "captured_at": self.captured_at.isoformat(),
            "source": self.source,
            "window": self.window,
            "metrics": self.metrics,
        }


class AlphaPerformanceStore:
    """Stores per-alpha performance snapshots and ranking views."""

    def __init__(self):
        self._snapshots: dict[str, list[AlphaPerformanceSnapshot]] = {}

    def record_snapshot(
        self, alpha_id: str, metrics: dict[str, Any], source: str = "manual", window: str = "latest"
    ) -> AlphaPerformanceSnapshot:
        snapshot = AlphaPerformanceSnapshot(
            alpha_id=alpha_id, metrics=metrics, source=source, window=window
        )
        self._snapshots.setdefault(alpha_id, []).append(snapshot)
        self._snapshots[alpha_id].sort(key=lambda item: item.captured_at)
        return snapshot

    # ── DQAF-20260622-053: sanitization APIs ──

    def remove_alpha(self, alpha_id: str) -> bool:
        """Remove all snapshots for *alpha_id*.  Returns ``True`` if removed.

        Idempotent — safe to call with an unknown *alpha_id*.
        """
        if alpha_id in self._snapshots:
            del self._snapshots[alpha_id]
            return True
        return False

    def list_ids(self) -> list[str]:
        """Return every *alpha_id* that has at least one recorded snapshot."""
        return sorted(self._snapshots.keys())

    # ── query ──

    def history(self, alpha_id: str) -> list[AlphaPerformanceSnapshot]:
        return list(self._snapshots.get(alpha_id, []))

    def latest(self, alpha_id: str) -> AlphaPerformanceSnapshot | None:
        history = self.history(alpha_id)
        return history[-1] if history else None

    def summarize(self, alpha_id: str) -> dict[str, Any]:
        history = self.history(alpha_id)
        latest = history[-1] if history else None
        return {
            "schema_version": SCHEMA_ALPHA_PERFORMANCE_SUMMARY,
            "alpha_id": alpha_id,
            "snapshot_count": len(history),
            "latest": latest.to_dict() if latest else None,
            "aggregates": self._aggregate(history),
        }

    def rank(self, metric: str, descending: bool = True) -> list[dict[str, Any]]:
        rows = []
        for alpha_id in sorted(self._snapshots):
            latest = self.latest(alpha_id)
            value = (latest.metrics or {}).get(metric) if latest else None
            if value is not None:
                rows.append(
                    {
                        "alpha_id": alpha_id,
                        "metric": metric,
                        "value": value,
                        "snapshot": latest.to_dict() if latest is not None else {},
                    }
                )
        return sorted(rows, key=lambda item: item["value"], reverse=descending)

    def ingest_runtime_summary(
        self, runtime_summary: dict[str, Any], alpha_id_by_strategy: dict[str, str] | None = None
    ) -> list[AlphaPerformanceSnapshot]:
        snapshots = []
        strategy_summary = runtime_summary.get("per_strategy") or {}
        averages = runtime_summary.get("averages") or {}
        totals = runtime_summary.get("totals") or {}
        alpha_id_by_strategy = alpha_id_by_strategy or {}
        for strategy_id, values in strategy_summary.items():
            alpha_id = alpha_id_by_strategy.get(strategy_id, strategy_id)
            signals = values.get("signals", 0)
            orders = values.get("orders", 0)
            metrics = {
                "strategy_id": strategy_id,
                "signal_count": signals,
                "order_count": orders,
                "denied_count": values.get("denied", 0),
                "filled_order_count": totals.get("filled_orders", 0) if orders else 0,
                "fill_ratio": averages.get("fill_ratio") if orders else None,
                "average_latency_ms": averages.get("latency_ms"),
                "average_slippage_bps": averages.get("arrival_slippage_bps"),
                "paper_cycles": runtime_summary.get("cycle_count", 0),
                "orders_per_signal": round(orders / signals, 6) if signals else None,
            }
            snapshots.append(
                self.record_snapshot(
                    alpha_id,
                    metrics,
                    source="runtime_summary",
                    window="runtime_summary",
                )
            )
        return snapshots

    def ingest_live_bridge_report(
        self,
        alpha_id: str,
        report: dict[str, Any],
        *,
        journal_source_path: str | None = None,
        symbol_filter: str | None = None,
    ) -> AlphaPerformanceSnapshot:
        """Map ``trade_quality_report.build_report`` payload into a performance snapshot."""
        counts = report.get("counts") or {}
        accepted = int(counts.get("accepted", 0))
        rejected = int(counts.get("rejected", 0))
        total = int(report.get("total", 0))
        reasons_raw = dict(report.get("rejected_reasons") or {})
        top_reasons = dict(sorted(reasons_raw.items(), key=lambda kv: (-kv[1], kv[0]))[:10])
        consecutive = int(report.get("live_consecutive_rejected_tail", 0))
        metrics: dict[str, Any] = {
            "live_bridge": True,
            "date_key": report.get("date_key"),
            "symbol_filter": symbol_filter,
            "journal_source_path": journal_source_path or report.get("journal_path"),
            "live_total": total,
            "live_accepted": accepted,
            "live_rejected": rejected,
            "live_rejection_rate": report.get("rejection_rate"),
            "live_acceptance_rate": report.get("acceptance_rate"),
            "live_rejected_reasons_top": top_reasons,
            "live_consecutive_rejected": consecutive,
        }
        return self.record_snapshot(
            alpha_id,
            metrics,
            source="live_bridge_report",
            window=str(report.get("date_key") or "live_bridge"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_ALPHA_PERFORMANCE_STORE,
            "alpha_count": len(self._snapshots),
            "summaries": [self.summarize(alpha_id) for alpha_id in sorted(self._snapshots)],
        }

    # ── persistence ──

    def save(self, path: str | Path) -> Path:
        # DQAF-046 Plan B: route through StateWriter gate.
        from core.state.catalog import lookup
        from core.state.writer import StateWriter

        writer = StateWriter.from_state_path(path)
        writer.write_artifact(lookup("ALPHA_PERFORMANCE"), writer._symbol, self.to_dict())
        return Path(path)

    @classmethod
    def load(cls, path: str | Path) -> "AlphaPerformanceStore":
        src = Path(path)
        if not src.exists():
            raise FileNotFoundError(f"alpha performance store file not found: {src}")
        data = json.loads(src.read_text(encoding="utf-8"))
        store = cls()
        for summary in data.get("summaries", []):
            latest = summary.get("latest")
            if latest:
                store.record_snapshot(
                    alpha_id=latest["alpha_id"],
                    metrics=latest.get("metrics", {}),
                    source=latest.get("source", "loaded"),
                    window=latest.get("window", "latest"),
                )
        return store

    def _aggregate(self, history: list[AlphaPerformanceSnapshot]) -> dict[str, Any]:
        numeric_values: dict[str, list[float]] = {}
        for snapshot in history:
            for key, value in snapshot.metrics.items():
                if isinstance(value, int | float):
                    numeric_values.setdefault(key, []).append(float(value))
        return {
            key: {
                "latest": values[-1],
                "average": round(mean(values), 6),
                "min": min(values),
                "max": max(values),
            }
            for key, values in numeric_values.items()
        }
