"""Runtime cycle replay and reconciliation readiness reports."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.runtime.evidence_reader import RuntimeEvidenceReader
from core.runtime.schema_versions import SCHEMA_RUNTIME_EVIDENCE_RECORD, SCHEMA_RUNTIME_REPLAY_REPORT


@dataclass(frozen=True)
class RuntimeReplayReport:
    schema_version: str
    runtime_cycle_id: str
    generated_at: datetime
    evidence_found: bool
    replayable: bool
    counts_match: bool
    approvals_present: bool
    quality_present: bool
    signal_count: int = 0
    order_count: int = 0
    approval_count: int = 0
    skipped_count: int = 0
    issues: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runtime_cycle_id": self.runtime_cycle_id,
            "generated_at": self.generated_at.isoformat(),
            "evidence_found": self.evidence_found,
            "replayable": self.replayable,
            "counts_match": self.counts_match,
            "approvals_present": self.approvals_present,
            "quality_present": self.quality_present,
            "signal_count": self.signal_count,
            "order_count": self.order_count,
            "approval_count": self.approval_count,
            "skipped_count": self.skipped_count,
            "issues": self.issues,
            "summary": self.summary,
        }


class RuntimeCycleReplay:
    """Validates runtime evidence records for replay/readiness."""

    def __init__(self, evidence_reader: RuntimeEvidenceReader):
        self._reader = evidence_reader

    def replay(self, runtime_cycle_id: str) -> RuntimeReplayReport:
        record = self._reader.latest_cycle(runtime_cycle_id)
        if record is None:
            return RuntimeReplayReport(
                schema_version=SCHEMA_RUNTIME_REPLAY_REPORT,
                runtime_cycle_id=runtime_cycle_id,
                generated_at=datetime.utcnow(),
                evidence_found=False,
                replayable=False,
                counts_match=False,
                approvals_present=False,
                quality_present=False,
                issues=["evidence_not_found"],
            )
        return self._validate_record(record)

    def _validate_record(self, record: dict) -> RuntimeReplayReport:
        issues = []
        payload = record.get("payload") or {}
        signals = payload.get("signals") or []
        orders = payload.get("orders") or []
        approvals = payload.get("approvals") or []
        skipped = payload.get("skipped_signals") or []
        quality = payload.get("quality_report") or {}

        counts_match = True
        expected = {
            "signal_count": len(signals),
            "order_count": len(orders),
            "approval_count": len(approvals),
            "skipped_count": len(skipped),
        }
        for key, actual in expected.items():
            if record.get(key) != actual:
                counts_match = False
                issues.append(f"{key}_mismatch({record.get(key)}!={actual})")

        if record.get("schema_version") != SCHEMA_RUNTIME_EVIDENCE_RECORD:
            issues.append("unsupported_schema_version")
        if payload.get("runtime_cycle_id") != record.get("runtime_cycle_id"):
            issues.append("runtime_cycle_id_mismatch")
        quality_present = bool(quality) and "order_count" in quality
        if not quality_present:
            issues.append("quality_report_missing")
        approvals_present = bool(approvals) or record.get("approval_count", 0) == 0
        if record.get("approval_count", 0) > 0 and not approvals:
            issues.append("approvals_missing")
        replayable = not issues
        return RuntimeReplayReport(
            schema_version=SCHEMA_RUNTIME_REPLAY_REPORT,
            runtime_cycle_id=record.get("runtime_cycle_id", "unknown"),
            generated_at=datetime.utcnow(),
            evidence_found=True,
            replayable=replayable,
            counts_match=counts_match,
            approvals_present=approvals_present,
            quality_present=quality_present,
            signal_count=len(signals),
            order_count=len(orders),
            approval_count=len(approvals),
            skipped_count=len(skipped),
            issues=issues,
            summary={
                "quality_order_count": quality.get("order_count"),
                "filled_order_count": quality.get("filled_order_count"),
                "average_fill_ratio": quality.get("average_fill_ratio"),
                "denied_count": len([a for a in approvals if not a.get("approved", False)]),
            },
        )
