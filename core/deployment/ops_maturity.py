"""Operations maturity scoring from release and compliance signals.

Computes a 0–100 score with per-pillar breakdown, including Alpha budget
governance coverage from registry and readiness checks.
"""

from datetime import UTC, datetime
from pathlib import Path

from core.contracts.domain_keys import (
    COMPLIANCE_LEVEL_PASS,
    COMPLIANCE_LEVEL_WARN,
    PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE,
    PAYLOAD_KEY_CHECK_PASSED,
    PAYLOAD_KEY_CHECK_TOTAL,
    PAYLOAD_KEY_CHECKS,
    PAYLOAD_KEY_FROM_READINESS,
    PAYLOAD_KEY_GENERATED_AT,
    PAYLOAD_KEY_GRADE,
    PAYLOAD_KEY_MATURITY_SCORE,
    PAYLOAD_KEY_MAX_SCORE,
    PAYLOAD_KEY_MEETS_THRESHOLD,
    PAYLOAD_KEY_MIN_SCORE_THRESHOLD,
    PAYLOAD_KEY_MISSING_EVIDENCE_COUNT,
    PAYLOAD_KEY_PASSED,
    PAYLOAD_KEY_PILLAR_COMPLIANCE,
    PAYLOAD_KEY_PILLAR_RELEASE_READINESS,
    PAYLOAD_KEY_PILLARS,
    PAYLOAD_KEY_POINTS,
    PAYLOAD_KEY_RECORD_COUNT,
    PAYLOAD_KEY_REGISTRY,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_SLO,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_TIMELINE_EVENT_COUNT,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_VALIDATION_MODE_COUNTS,
    PAYLOAD_KEY_WARNING_TOTAL,
    PAYLOAD_KEY_WEIGHT,
    SLO_STATUS_HEALTHY,
)
from core.deployment.atomic_file_writer import atomic_write_json
from core.deployment.governance_summary import extract_governance_summary
from core.deployment.schema_versions import SCHEMA_OPS_MATURITY
from core.deployment.validation_mode import resolve_validation_mode


class OpsMaturityService:
    """Derives an operations maturity score from existing service reports."""

    def __init__(self, container):
        self._container = container

    def evaluate(self, *, validation_mode: str | None = None) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        readiness = self._container.release_readiness.build_report(validation_mode=validation_mode)
        compliance = self._container.compliance_audit.generate(validation_mode=validation_mode)
        compliance_summary = compliance.get(PAYLOAD_KEY_SUMMARY) or {}
        registry = self._container.release_registry.summarize()
        slo = self._container.slo_service.evaluate()
        checks = readiness.get(PAYLOAD_KEY_CHECKS) or []
        total = len(checks) or 1
        passed = sum(1 for c in checks if c.get(PAYLOAD_KEY_PASSED))
        readiness_pillar = round(25.0 * passed / total, 2)

        cstatus = compliance.get(PAYLOAD_KEY_STATUS)
        if cstatus == COMPLIANCE_LEVEL_PASS:
            compliance_pillar = 25.0
        elif cstatus == COMPLIANCE_LEVEL_WARN:
            compliance_pillar = 15.0
        else:
            compliance_pillar = 0.0

        slo_pillar = 25.0 if slo.get(PAYLOAD_KEY_STATUS) == SLO_STATUS_HEALTHY else 0.0

        ab = readiness.get(PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE) or {}
        me = ab.get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0) or 0
        wt = ab.get(PAYLOAD_KEY_WARNING_TOTAL, 0) or 0
        rec = ab.get(PAYLOAD_KEY_RECORD_COUNT, 0) or 0
        if rec == 0:
            alpha_pillar = 25.0
        elif me == 0 and wt == 0:
            alpha_pillar = 25.0
        elif me == 0:
            alpha_pillar = 12.0
        else:
            alpha_pillar = 0.0

        score = min(
            100.0,
            round(readiness_pillar + compliance_pillar + slo_pillar + alpha_pillar, 2),
        )
        min_threshold = float(getattr(self._container.config, "ops_maturity_min_score", 60.0))
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_OPS_MATURITY,
            PAYLOAD_KEY_GENERATED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
            PAYLOAD_KEY_MATURITY_SCORE: score,
            PAYLOAD_KEY_MAX_SCORE: 100.0,
            PAYLOAD_KEY_MIN_SCORE_THRESHOLD: min_threshold,
            PAYLOAD_KEY_MEETS_THRESHOLD: score >= min_threshold,
            PAYLOAD_KEY_GRADE: self._grade(score),
            PAYLOAD_KEY_PILLARS: {
                PAYLOAD_KEY_PILLAR_RELEASE_READINESS: {
                    PAYLOAD_KEY_WEIGHT: 25,
                    PAYLOAD_KEY_POINTS: readiness_pillar,
                    PAYLOAD_KEY_CHECK_PASSED: passed,
                    PAYLOAD_KEY_CHECK_TOTAL: total,
                },
                PAYLOAD_KEY_PILLAR_COMPLIANCE: {
                    PAYLOAD_KEY_WEIGHT: 25,
                    PAYLOAD_KEY_POINTS: compliance_pillar,
                    PAYLOAD_KEY_STATUS: cstatus,
                },
                PAYLOAD_KEY_SLO: {
                    PAYLOAD_KEY_WEIGHT: 25,
                    PAYLOAD_KEY_POINTS: slo_pillar,
                    PAYLOAD_KEY_STATUS: slo.get(PAYLOAD_KEY_STATUS),
                },
                PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE: {
                    PAYLOAD_KEY_WEIGHT: 25,
                    PAYLOAD_KEY_POINTS: alpha_pillar,
                    PAYLOAD_KEY_MISSING_EVIDENCE_COUNT: me,
                    PAYLOAD_KEY_WARNING_TOTAL: wt,
                },
            },
            PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE: {
                PAYLOAD_KEY_FROM_READINESS: {
                    PAYLOAD_KEY_MISSING_EVIDENCE_COUNT: ab.get(
                        PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0
                    ),
                    PAYLOAD_KEY_WARNING_TOTAL: ab.get(PAYLOAD_KEY_WARNING_TOTAL, 0),
                    PAYLOAD_KEY_TIMELINE_EVENT_COUNT: ab.get(PAYLOAD_KEY_TIMELINE_EVENT_COUNT, 0),
                },
                PAYLOAD_KEY_REGISTRY: {
                    PAYLOAD_KEY_RECORD_COUNT: registry.get(PAYLOAD_KEY_RECORD_COUNT, 0),
                    PAYLOAD_KEY_VALIDATION_MODE: registry.get(PAYLOAD_KEY_VALIDATION_MODE),
                    PAYLOAD_KEY_VALIDATION_MODE_COUNTS: registry.get(
                        PAYLOAD_KEY_VALIDATION_MODE_COUNTS, {}
                    ),
                },
            },
            PAYLOAD_KEY_SUMMARY: extract_governance_summary(compliance_summary),
        }

    @staticmethod
    def _grade(score: float) -> str:
        if score >= 90:
            return "A"
        if score >= 75:
            return "B"
        if score >= 60:
            return "C"
        if score >= 40:
            return "D"
        return "F"

    def save_report(self, path: str, *, validation_mode: str | None = None) -> str:
        report = self.evaluate(validation_mode=validation_mode)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, report)
        return str(target)
