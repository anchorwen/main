from datetime import UTC, datetime
from typing import Any

from core.observability.metric_names import DISPATCH_REJECTED, RECONCILIATION_BREACHED


class DiagnosticsDashboard:
    """Aggregates metrics, audit entries, and brain summaries into a
    single diagnostic snapshot for operational visibility.
    """

    def __init__(
        self,
        metrics_collector=None,
        audit_log=None,
        brain_performance_tracker=None,
    ):
        self._metrics = metrics_collector
        self._audit = audit_log
        self._tracker = brain_performance_tracker

    @staticmethod
    def safe_get_snapshot(container: Any) -> dict:
        """Return ``{available: True, snapshot: ...}`` or ``{available: False}``.

        Safe accessor for the diagnostics dashboard attached to a service
        container.  Callers that just need the raw snapshot (evidence bundle,
        runbook engine) use this instead of duplicating the guard clause.
        """
        diagnostics = getattr(container, "diagnostics", None)
        if diagnostics is None:
            return {"available": False}
        return {"available": True, "snapshot": diagnostics.build_snapshot()}

    def build_snapshot(self, *, date_key: str | None = None) -> dict:
        return {
            "generated_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "metrics": self._build_metrics_section(),
            "brain_health": self._build_brain_health_section(),
            "audit_summary": self._build_audit_summary(date_key),
            "alerts": self._build_alerts(),
        }

    def _build_metrics_section(self) -> dict | None:
        if self._metrics is None:
            return None
        snap = self._metrics.snapshot()
        return {
            "counters": snap.get("counters", {}),
            "gauges": snap.get("gauges", {}),
            "histogram_summaries": {
                k: {
                    "count": v["count"],
                    "mean": v["mean"],
                    "p95": v["p95"],
                }
                for k, v in snap.get("histograms", {}).items()
            },
        }

    def _build_brain_health_section(self) -> dict | None:
        if self._tracker is None:
            return None
        summaries = self._tracker.get_all_summaries()
        return {
            "brain_count": len(summaries),
            "healthy_count": sum(1 for s in summaries if s["health_signal"] == "healthy"),
            "degraded_count": sum(
                1 for s in summaries if s["health_signal"] in {"degraded", "critical"}
            ),
            "brains": {
                s["brain_id"]: {
                    "health": s["health_signal"],
                    "recommendation": s["recommendation"],
                    "composite_mean": s["composite_mean"],
                    "sample_count": s["sample_count"],
                }
                for s in summaries
            },
        }

    def _build_audit_summary(self, date_key: str | None) -> dict | None:
        if self._audit is None:
            return None
        entries = self._audit.read_entries(date_key=date_key)
        severity_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        for e in entries:
            sev = e.get("severity", "info")
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
            et = e.get("event_type", "unknown")
            type_counts[et] = type_counts.get(et, 0) + 1
        return {
            "entry_count": len(entries),
            "severity_counts": severity_counts,
            "event_type_counts": type_counts,
        }

    def _build_alerts(self) -> list[dict]:
        alerts = []

        if self._tracker:
            for s in self._tracker.get_all_summaries():
                if s["health_signal"] == "critical":
                    alerts.append(
                        {
                            "level": "critical",
                            "source": "brain_health",
                            "brain_id": s["brain_id"],
                            "message": (
                                f"Brain {s['brain_id']} health critical,"
                                f" recommendation: {s['recommendation']}"
                            ),
                        }
                    )
                elif s["health_signal"] == "degraded":
                    alerts.append(
                        {
                            "level": "warning",
                            "source": "brain_health",
                            "brain_id": s["brain_id"],
                            "message": (
                                f"Brain {s['brain_id']} health degraded,"
                                f" recommendation: {s['recommendation']}"
                            ),
                        }
                    )

        if self._metrics:
            breach_count = self._metrics.get_counter(RECONCILIATION_BREACHED)
            if breach_count > 0:
                alerts.append(
                    {
                        "level": "error",
                        "source": "reconciliation",
                        "message": f"Reconciliation breaches detected: {int(breach_count)}",
                    }
                )
            reject_count = self._metrics.get_counter(DISPATCH_REJECTED)
            if reject_count > 5:
                alerts.append(
                    {
                        "level": "warning",
                        "source": "dispatch",
                        "message": f"High dispatch rejection rate: {int(reject_count)} rejections",
                    }
                )

        return alerts
