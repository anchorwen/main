"""Final pre-production audit bundle.

Aggregates release readiness, compliance audit, control matrix, and
Alpha budget governance signals into a single gate-style report.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from core.contracts.domain_keys import (
    COMPLIANCE_CHECK_REGISTRY_DEEP_VALIDATION_COVERAGE_COMPLETE,
    COMPLIANCE_CHECK_REGISTRY_DEEP_VALIDATION_PRESENT,
    COMPLIANCE_LEVEL_FAIL,
    COMPLIANCE_LEVEL_PASS,
    COMPLIANCE_LEVEL_WARN,
    PAYLOAD_KEY_ALPHA_BUDGET,
    PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE,
    PAYLOAD_KEY_COMPLIANCE_AUDIT,
    PAYLOAD_KEY_COMPLIANCE_MATRIX,
    PAYLOAD_KEY_CONTROL_COUNT,
    PAYLOAD_KEY_DETAIL,
    PAYLOAD_KEY_EVIDENCE_COUNT,
    PAYLOAD_KEY_FAILED_CHECKS,
    PAYLOAD_KEY_FAILED_COUNT,
    PAYLOAD_KEY_FINAL_AUDIT,
    PAYLOAD_KEY_FINDINGS,
    PAYLOAD_KEY_GENERATED_AT,
    PAYLOAD_KEY_LEVEL,
    PAYLOAD_KEY_MISSING_EVIDENCE_COUNT,
    PAYLOAD_KEY_NAME,
    PAYLOAD_KEY_OPS_MATURITY,
    PAYLOAD_KEY_PASSED_COUNT,
    PAYLOAD_KEY_READINESS,
    PAYLOAD_KEY_READY,
    PAYLOAD_KEY_READY_FOR_PRODUCTION,
    PAYLOAD_KEY_RECORD_COUNT,
    PAYLOAD_KEY_REGISTRY,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_TIMELINE_EVENT_COUNT,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_VALIDATION_MODE_COUNTS,
    PAYLOAD_KEY_WARNING_COUNT,
    PAYLOAD_KEY_WARNING_TOTAL,
    VALIDATION_MODE_DEEP,
)
from core.deployment.governance_summary import build_governance_summary
from core.deployment.schema_versions import SCHEMA_FINAL_AUDIT
from core.deployment.validation_mode import resolve_validation_mode


class FinalAuditService:
    """Produces a consolidated final audit from existing deployment services."""

    def __init__(self, container):
        self._container = container

    def build_report(self, *, validation_mode: str | None = None) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        readiness = self._container.release_readiness.build_report(validation_mode=validation_mode)
        compliance = self._container.compliance_audit.generate(validation_mode=validation_mode)
        matrix = self._container.compliance_control_matrix.generate(validation_mode=validation_mode)
        registry = self._container.release_registry.summarize()
        ab = readiness.get(PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE) or {}
        compliance_status = compliance.get(PAYLOAD_KEY_STATUS)
        matrix_status = matrix.get(PAYLOAD_KEY_STATUS)
        readiness_ok = bool(readiness.get(PAYLOAD_KEY_READY))
        ready_for_production = (
            readiness_ok
            and compliance_status != COMPLIANCE_LEVEL_FAIL
            and matrix_status != COMPLIANCE_LEVEL_FAIL
        )
        findings = self._findings(
            readiness,
            compliance,
            matrix,
            registry,
            ab,
            ready_for_production,
        )
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_FINAL_AUDIT,
            PAYLOAD_KEY_GENERATED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
            PAYLOAD_KEY_READY_FOR_PRODUCTION: ready_for_production,
            PAYLOAD_KEY_FINDINGS: findings,
            PAYLOAD_KEY_SUMMARY: self._governance_summary(registry),
            PAYLOAD_KEY_READINESS: {
                PAYLOAD_KEY_READY: readiness.get(PAYLOAD_KEY_READY),
                PAYLOAD_KEY_SCHEMA_VERSION: readiness.get(PAYLOAD_KEY_SCHEMA_VERSION),
                PAYLOAD_KEY_SUMMARY: readiness.get(PAYLOAD_KEY_SUMMARY),
            },
            PAYLOAD_KEY_COMPLIANCE_AUDIT: {
                PAYLOAD_KEY_STATUS: compliance_status,
                PAYLOAD_KEY_SUMMARY: compliance.get(PAYLOAD_KEY_SUMMARY),
            },
            PAYLOAD_KEY_COMPLIANCE_MATRIX: {
                PAYLOAD_KEY_STATUS: matrix_status,
                PAYLOAD_KEY_CONTROL_COUNT: matrix.get(PAYLOAD_KEY_CONTROL_COUNT),
                PAYLOAD_KEY_PASSED_COUNT: matrix.get(PAYLOAD_KEY_PASSED_COUNT),
                PAYLOAD_KEY_WARNING_COUNT: matrix.get(PAYLOAD_KEY_WARNING_COUNT),
                PAYLOAD_KEY_FAILED_COUNT: matrix.get(PAYLOAD_KEY_FAILED_COUNT),
            },
            PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE: {
                PAYLOAD_KEY_MISSING_EVIDENCE_COUNT: ab.get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0),
                PAYLOAD_KEY_WARNING_TOTAL: ab.get(PAYLOAD_KEY_WARNING_TOTAL, 0),
                PAYLOAD_KEY_EVIDENCE_COUNT: ab.get(PAYLOAD_KEY_EVIDENCE_COUNT, 0),
                PAYLOAD_KEY_TIMELINE_EVENT_COUNT: ab.get(PAYLOAD_KEY_TIMELINE_EVENT_COUNT, 0),
            },
            PAYLOAD_KEY_REGISTRY: {
                PAYLOAD_KEY_RECORD_COUNT: registry.get(PAYLOAD_KEY_RECORD_COUNT, 0),
                PAYLOAD_KEY_VALIDATION_MODE: registry.get(PAYLOAD_KEY_VALIDATION_MODE),
                PAYLOAD_KEY_VALIDATION_MODE_COUNTS: registry.get(
                    PAYLOAD_KEY_VALIDATION_MODE_COUNTS, {}
                ),
                PAYLOAD_KEY_ALPHA_BUDGET: registry.get(PAYLOAD_KEY_ALPHA_BUDGET, {}),
                PAYLOAD_KEY_FINAL_AUDIT: registry.get(PAYLOAD_KEY_FINAL_AUDIT, {}),
                PAYLOAD_KEY_OPS_MATURITY: registry.get(PAYLOAD_KEY_OPS_MATURITY, {}),
            },
        }

    def _findings(
        self,
        readiness: dict,
        compliance: dict,
        matrix: dict,
        registry: dict,
        ab: dict,
        ready: bool,
    ) -> list[str]:
        if ready:
            return []
        out: list[str] = []
        if not readiness.get(PAYLOAD_KEY_READY):
            failed = (readiness.get(PAYLOAD_KEY_SUMMARY) or {}).get(PAYLOAD_KEY_FAILED_CHECKS) or []
            out.append(f"Release readiness failed checks: {failed}")
        if compliance.get(PAYLOAD_KEY_STATUS) == COMPLIANCE_LEVEL_FAIL:
            out.append("Compliance audit status is fail")
        if (matrix.get(PAYLOAD_KEY_FAILED_COUNT) or 0) > 0:
            out.append("Compliance control matrix has failed controls")
        if (
            ab.get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0) > 0
            and registry.get(PAYLOAD_KEY_RECORD_COUNT, 0) > 0
        ):
            out.append("Alpha budget evidence missing for one or more registered releases")
        if ab.get(PAYLOAD_KEY_WARNING_TOTAL, 0) > 0:
            out.append("Alpha budget warnings present; review before production")
        record_count = registry.get(PAYLOAD_KEY_RECORD_COUNT, 0) or 0
        mode_counts = registry.get(PAYLOAD_KEY_VALIDATION_MODE_COUNTS) or {}
        deep_count = int(mode_counts.get(VALIDATION_MODE_DEEP, 0) or 0)
        if record_count > 0 and deep_count == 0:
            out.append(
                "No deep validation-mode releases registered; run deep validation before production"
            )
        elif 0 < deep_count < record_count:
            out.append("Deep validation coverage is partial across registered releases")
        if not out:
            out.append("Final audit not cleared; see nested reports for detail")
        return out

    @staticmethod
    def _governance_summary(registry: dict) -> dict:
        record_count = registry.get(PAYLOAD_KEY_RECORD_COUNT, 0) or 0
        mode_counts = registry.get(PAYLOAD_KEY_VALIDATION_MODE_COUNTS) or {}
        deep_count = int(mode_counts.get(VALIDATION_MODE_DEEP, 0) or 0)
        presence_level = (
            COMPLIANCE_LEVEL_PASS if record_count == 0 or deep_count > 0 else COMPLIANCE_LEVEL_WARN
        )
        coverage_level = (
            COMPLIANCE_LEVEL_PASS
            if record_count == 0 or deep_count in {0, record_count}
            else COMPLIANCE_LEVEL_WARN
        )
        focus = [
            {
                PAYLOAD_KEY_NAME: COMPLIANCE_CHECK_REGISTRY_DEEP_VALIDATION_PRESENT,
                PAYLOAD_KEY_LEVEL: presence_level,
                PAYLOAD_KEY_DETAIL: {
                    PAYLOAD_KEY_RECORD_COUNT: record_count,
                    PAYLOAD_KEY_VALIDATION_MODE_COUNTS: mode_counts,
                },
            },
            {
                PAYLOAD_KEY_NAME: COMPLIANCE_CHECK_REGISTRY_DEEP_VALIDATION_COVERAGE_COMPLETE,
                PAYLOAD_KEY_LEVEL: coverage_level,
                PAYLOAD_KEY_DETAIL: {
                    PAYLOAD_KEY_RECORD_COUNT: record_count,
                    PAYLOAD_KEY_VALIDATION_MODE_COUNTS: mode_counts,
                },
            },
        ]
        return build_governance_summary(
            focus=focus,
        )

    def save_report(
        self,
        path: str,
        report: dict | None = None,
        *,
        validation_mode: str | None = None,
    ) -> str:
        payload = (
            report if report is not None else self.build_report(validation_mode=validation_mode)
        )
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return str(target)
