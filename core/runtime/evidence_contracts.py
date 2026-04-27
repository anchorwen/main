"""Runtime evidence record contracts."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.runtime.integration_contracts import RuntimePipelineResult
from core.runtime.schema_versions import SCHEMA_RUNTIME_EVIDENCE_RECORD


@dataclass(frozen=True)
class RuntimeEvidenceRecord:
    schema_version: str
    evidence_id: str
    runtime_cycle_id: str
    generated_at: datetime
    signal_count: int
    order_count: int
    approval_count: int
    skipped_count: int
    quality_summary: dict[str, Any]
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_pipeline_result(cls, *, evidence_id: str, runtime_cycle_id: str,
                             result: RuntimePipelineResult) -> "RuntimeEvidenceRecord":
        quality = result.quality_report
        return cls(
            schema_version=SCHEMA_RUNTIME_EVIDENCE_RECORD,
            evidence_id=evidence_id,
            runtime_cycle_id=runtime_cycle_id,
            generated_at=datetime.utcnow(),
            signal_count=len(result.signals),
            order_count=len(result.orders),
            approval_count=len(result.approvals),
            skipped_count=len(result.skipped_signals),
            quality_summary={
                "order_count": quality.order_count,
                "filled_order_count": quality.filled_order_count,
                "rejected_order_count": quality.rejected_order_count,
                "average_fill_ratio": quality.average_fill_ratio,
                "average_latency_ms": quality.average_latency_ms,
                "average_arrival_slippage_bps": quality.average_arrival_slippage_bps,
            },
            payload=result.to_dict(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "runtime_cycle_id": self.runtime_cycle_id,
            "generated_at": self.generated_at.isoformat(),
            "signal_count": self.signal_count,
            "order_count": self.order_count,
            "approval_count": self.approval_count,
            "skipped_count": self.skipped_count,
            "quality_summary": self.quality_summary,
            "payload": self.payload,
        }
