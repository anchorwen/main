import json
from datetime import UTC, datetime
from pathlib import Path


class StructuredAuditLog:
    """Append-only structured audit log for governance and compliance.

    Every significant system action is recorded as a JSON line with
    a fixed schema, enabling downstream querying, alerting, and
    forensic analysis.
    """

    SEVERITY_INFO = "info"
    SEVERITY_WARNING = "warning"
    SEVERITY_ERROR = "error"
    SEVERITY_CRITICAL = "critical"

    def __init__(self, base_dir: str):
        self._base_dir = Path(base_dir)

    def log(
        self,
        *,
        event_type: str,
        severity: str = "info",
        actor: str = "system",
        subject: str | None = None,
        detail: dict | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> dict:
        entry = {
            "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "event_type": event_type,
            "severity": severity,
            "actor": actor,
            "subject": subject,
            "detail": detail or {},
            "trace_id": trace_id,
            "span_id": span_id,
        }
        self._write(entry)
        return entry

    def log_decision(
        self,
        *,
        intent_id: str,
        verdict_status: str,
        symbol: str,
        action: str,
        risk_tier: str,
        trace_id: str | None = None,
    ) -> dict:
        return self.log(
            event_type="decision_cycle",
            actor="runtime_loop",
            subject=symbol,
            detail={
                "intent_id": intent_id,
                "verdict_status": verdict_status,
                "action": action,
                "risk_tier": risk_tier,
            },
            trace_id=trace_id,
        )

    def log_dispatch(
        self,
        *,
        message_id: str,
        target: str,
        status: str,
        adapter_name: str,
        trace_id: str | None = None,
    ) -> dict:
        return self.log(
            event_type="communication_dispatch",
            actor="dispatcher",
            subject=target,
            detail={
                "message_id": message_id,
                "status": status,
                "adapter_name": adapter_name,
            },
            trace_id=trace_id,
        )

    def log_risk_verdict(
        self,
        *,
        intent_id: str,
        status: str,
        risk_tier: str,
        blocking_reasons: list,
        trace_id: str | None = None,
    ) -> dict:
        severity = self.SEVERITY_WARNING if blocking_reasons else self.SEVERITY_INFO
        return self.log(
            event_type="risk_verdict",
            severity=severity,
            actor="risk_evaluation_service",
            detail={
                "intent_id": intent_id,
                "status": status,
                "risk_tier": risk_tier,
                "blocking_reasons": blocking_reasons,
            },
            trace_id=trace_id,
        )

    def log_governance_signal(
        self,
        *,
        brain_id: str,
        signal_type: str,
        recommendation: str,
        health_signal: str,
        trace_id: str | None = None,
    ) -> dict:
        severity = self.SEVERITY_CRITICAL if recommendation == "freeze" else self.SEVERITY_WARNING
        return self.log(
            event_type="governance_signal",
            severity=severity,
            actor="feedback_loop",
            subject=brain_id,
            detail={
                "signal_type": signal_type,
                "recommendation": recommendation,
                "health_signal": health_signal,
            },
            trace_id=trace_id,
        )

    def log_reconciliation(
        self, *, message_id: str, status: str, mismatches: list, trace_id: str | None = None
    ) -> dict:
        severity = self.SEVERITY_ERROR if status == "breached" else self.SEVERITY_INFO
        return self.log(
            event_type="reconciliation",
            severity=severity,
            actor="reconciliation_service",
            subject=message_id,
            detail={
                "status": status,
                "mismatch_count": len(mismatches),
                "mismatches": mismatches,
            },
            trace_id=trace_id,
        )

    def read_entries(self, *, date_key: str | None = None) -> list[dict]:
        if date_key:
            path = self._log_path(date_key)
            return self._read_jsonl(path) if path.exists() else []
        entries = []
        if self._base_dir.exists():
            for p in sorted(self._base_dir.glob("*/audit.jsonl")):
                entries.extend(self._read_jsonl(p))
        return entries

    def _write(self, entry: dict) -> None:
        date_key = entry["timestamp"][:10]
        path = self._log_path(date_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _log_path(self, date_key: str) -> Path:
        return self._base_dir / date_key / "audit.jsonl"

    def _read_jsonl(self, path: Path) -> list[dict]:
        entries = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries
