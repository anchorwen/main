"""Compliance control matrix generation.

Maps operational/release governance controls to concrete evidence,
status, gaps, and remediation actions.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from core.deployment.domain_keys import (
    COMPLIANCE_LEVEL_FAIL,
    COMPLIANCE_LEVEL_PASS,
    COMPLIANCE_LEVEL_WARN,
    PAYLOAD_KEY_ALPHA_BUDGET,
    PAYLOAD_KEY_CERTIFIED_COUNT,
    PAYLOAD_KEY_CONTROL_COUNT,
    PAYLOAD_KEY_CONTROL_ID,
    PAYLOAD_KEY_CONTROLS,
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_EVENT_COUNT,
    PAYLOAD_KEY_EVIDENCE,
    PAYLOAD_KEY_EVIDENCE_COUNT,
    PAYLOAD_KEY_EVIDENCE_SOURCE,
    PAYLOAD_KEY_FAILED_CHECKS,
    PAYLOAD_KEY_FAILED_COUNT,
    PAYLOAD_KEY_FAILED_EVENT_COUNT,
    PAYLOAD_KEY_FAILED_OBJECTIVES,
    PAYLOAD_KEY_FINAL_AUDIT,
    PAYLOAD_KEY_GAP,
    PAYLOAD_KEY_GENERATED_AT,
    PAYLOAD_KEY_MIN_SCORE,
    PAYLOAD_KEY_MISSING_EVIDENCE_COUNT,
    PAYLOAD_KEY_MISSING_SCORE_COUNT,
    PAYLOAD_KEY_NOT_READY_COUNT,
    PAYLOAD_KEY_OBJECTIVE,
    PAYLOAD_KEY_OPS_MATURITY,
    PAYLOAD_KEY_OUTPUT_PATH,
    PAYLOAD_KEY_PASSED,
    PAYLOAD_KEY_PASSED_COUNT,
    PAYLOAD_KEY_PATH,
    PAYLOAD_KEY_READY,
    PAYLOAD_KEY_RECORD_COUNT,
    PAYLOAD_KEY_REGISTRY_PATH,
    PAYLOAD_KEY_REMEDIATION,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_SCORE_AVG,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_STATUS_COUNTS,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_TIMELINE_PATH,
    PAYLOAD_KEY_UNKNOWN_READINESS_COUNT,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_VALIDATION_MODE_COUNTS,
    PAYLOAD_KEY_WARNING_COUNT,
    PAYLOAD_KEY_WARNING_RELEASE_COUNT,
    PAYLOAD_KEY_WARNING_TOTAL,
    RELEASE_PIPELINE_GATE_DECISION_ALLOW,
    SLO_STATUS_HEALTHY,
    TIMELINE_STATUS_FAILED,
    VALIDATION_MODE_DEEP,
)
from core.deployment.governance_summary import build_governance_summary
from core.deployment.schema_versions import SCHEMA_COMPLIANCE_CONTROL_MATRIX
from core.deployment.validation_mode import resolve_validation_mode


class ComplianceControlMatrixService:
    """Builds an audit control matrix from existing release evidence."""

    def __init__(self, container):
        self._container = container

    def generate(
        self,
        *,
        output: str | None = None,
        validation_mode: str | None = None,
    ) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        registry = self._container.release_registry.summarize()
        timeline = self._container.operations_timeline.summarize()
        gate = self._container.release_gate.evaluate(strict=False, validation_mode=validation_mode)
        slo = self._container.slo_service.evaluate()
        readiness = self._container.release_readiness.build_report(validation_mode=validation_mode)
        audit = self._container.compliance_audit.generate(validation_mode=validation_mode)
        controls = self._build_controls(registry, timeline, gate, slo, readiness, audit)
        report = {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_COMPLIANCE_CONTROL_MATRIX,
            PAYLOAD_KEY_GENERATED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
            PAYLOAD_KEY_STATUS: self._status(controls),
            PAYLOAD_KEY_CONTROL_COUNT: len(controls),
            PAYLOAD_KEY_PASSED_COUNT: len(
                [c for c in controls if c[PAYLOAD_KEY_STATUS] == COMPLIANCE_LEVEL_PASS]
            ),
            PAYLOAD_KEY_WARNING_COUNT: len(
                [c for c in controls if c[PAYLOAD_KEY_STATUS] == COMPLIANCE_LEVEL_WARN]
            ),
            PAYLOAD_KEY_FAILED_COUNT: len(
                [c for c in controls if c[PAYLOAD_KEY_STATUS] == COMPLIANCE_LEVEL_FAIL]
            ),
            PAYLOAD_KEY_CONTROLS: controls,
            PAYLOAD_KEY_SUMMARY: self._governance_summary(controls),
        }
        if output:
            report[PAYLOAD_KEY_OUTPUT_PATH] = self.save_report(report, output)
        return report

    def save_report(self, report: dict, path: str) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return str(target)

    def _build_controls(
        self,
        registry: dict,
        timeline: dict,
        gate: dict,
        slo: dict,
        readiness: dict,
        audit: dict,
    ) -> list[dict]:
        min_ops = float(getattr(self._container.config, "ops_maturity_min_score", 60.0))
        return [
            self._control(
                "REL-001",
                "Release certificates are registered",
                "Release Registry",
                COMPLIANCE_LEVEL_PASS
                if registry.get(PAYLOAD_KEY_RECORD_COUNT, 0) > 0
                else COMPLIANCE_LEVEL_WARN,
                {
                    PAYLOAD_KEY_REGISTRY_PATH: registry.get(PAYLOAD_KEY_PATH),
                    PAYLOAD_KEY_RECORD_COUNT: registry.get(PAYLOAD_KEY_RECORD_COUNT, 0),
                    PAYLOAD_KEY_VALIDATION_MODE_COUNTS: registry.get(
                        PAYLOAD_KEY_VALIDATION_MODE_COUNTS, {}
                    ),
                },
                "Register release certificates after each release pipeline run",
            ),
            self._control(
                "REL-002",
                "Registered releases are certified",
                "Release Registry",
                COMPLIANCE_LEVEL_PASS
                if registry.get(PAYLOAD_KEY_RECORD_COUNT, 0)
                == registry.get(PAYLOAD_KEY_CERTIFIED_COUNT, 0)
                else COMPLIANCE_LEVEL_FAIL,
                {
                    PAYLOAD_KEY_RECORD_COUNT: registry.get(PAYLOAD_KEY_RECORD_COUNT, 0),
                    PAYLOAD_KEY_CERTIFIED_COUNT: registry.get(PAYLOAD_KEY_CERTIFIED_COUNT, 0),
                },
                "Investigate rejected certificates and block promotion until resolved",
            ),
            self._control(
                "REL-003",
                "Release gate is not blocking",
                "Release Gate",
                COMPLIANCE_LEVEL_PASS
                if gate.get(PAYLOAD_KEY_DECISION)
                in {RELEASE_PIPELINE_GATE_DECISION_ALLOW, COMPLIANCE_LEVEL_WARN}
                else COMPLIANCE_LEVEL_FAIL,
                {
                    PAYLOAD_KEY_DECISION: gate.get(PAYLOAD_KEY_DECISION),
                    PAYLOAD_KEY_SUMMARY: gate.get(PAYLOAD_KEY_SUMMARY, {}),
                },
                "Resolve blocking release gate signals",
            ),
            self._control(
                "REL-004",
                "Release readiness is clean",
                "Release Readiness",
                COMPLIANCE_LEVEL_PASS
                if readiness.get(PAYLOAD_KEY_READY)
                else COMPLIANCE_LEVEL_FAIL,
                {
                    PAYLOAD_KEY_READY: readiness.get(PAYLOAD_KEY_READY),
                    PAYLOAD_KEY_FAILED_CHECKS: readiness.get(PAYLOAD_KEY_SUMMARY, {}).get(
                        PAYLOAD_KEY_FAILED_CHECKS, []
                    ),
                },
                "Fix readiness failed checks before release",
            ),
            self._control(
                "OBS-001",
                "SLOs are healthy",
                "SLO Report",
                COMPLIANCE_LEVEL_PASS
                if slo.get(PAYLOAD_KEY_STATUS) == SLO_STATUS_HEALTHY
                else COMPLIANCE_LEVEL_FAIL,
                {
                    PAYLOAD_KEY_STATUS: slo.get(PAYLOAD_KEY_STATUS),
                    PAYLOAD_KEY_FAILED_OBJECTIVES: slo.get(PAYLOAD_KEY_FAILED_OBJECTIVES, []),
                },
                "Restore SLO compliance and review error budget burn",
            ),
            self._control(
                "AUD-001",
                "Operations timeline is populated",
                "Operations Timeline",
                COMPLIANCE_LEVEL_PASS
                if timeline.get(PAYLOAD_KEY_EVENT_COUNT, 0) > 0
                else COMPLIANCE_LEVEL_WARN,
                {
                    PAYLOAD_KEY_TIMELINE_PATH: timeline.get(PAYLOAD_KEY_PATH),
                    PAYLOAD_KEY_EVENT_COUNT: timeline.get(PAYLOAD_KEY_EVENT_COUNT, 0),
                },
                "Record release pipeline events into operations timeline",
            ),
            self._control(
                "AUD-002",
                "Operations timeline has no failed events",
                "Operations Timeline",
                COMPLIANCE_LEVEL_PASS
                if timeline.get(PAYLOAD_KEY_STATUS_COUNTS, {}).get(TIMELINE_STATUS_FAILED, 0) == 0
                else COMPLIANCE_LEVEL_FAIL,
                {
                    PAYLOAD_KEY_FAILED_EVENT_COUNT: timeline.get(PAYLOAD_KEY_STATUS_COUNTS, {}).get(
                        TIMELINE_STATUS_FAILED, 0
                    )
                },
                "Review failed events and attach postmortem reports",
            ),
            self._control(
                "AUD-003",
                "Compliance audit is not failing",
                "Compliance Audit",
                COMPLIANCE_LEVEL_PASS
                if audit.get(PAYLOAD_KEY_STATUS) in {COMPLIANCE_LEVEL_PASS, COMPLIANCE_LEVEL_WARN}
                else COMPLIANCE_LEVEL_FAIL,
                {
                    PAYLOAD_KEY_STATUS: audit.get(PAYLOAD_KEY_STATUS),
                    PAYLOAD_KEY_SUMMARY: audit.get(PAYLOAD_KEY_SUMMARY, {}),
                },
                "Resolve failing compliance audit checks",
            ),
            self._control(
                "ALPHA-001",
                "Alpha budget usage evidence is registered for releases",
                "Release Registry",
                COMPLIANCE_LEVEL_PASS
                if registry.get(PAYLOAD_KEY_RECORD_COUNT, 0) == 0
                or registry.get(PAYLOAD_KEY_ALPHA_BUDGET, {}).get(
                    PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0
                )
                == 0
                else COMPLIANCE_LEVEL_WARN,
                {
                    PAYLOAD_KEY_RECORD_COUNT: registry.get(PAYLOAD_KEY_RECORD_COUNT, 0),
                    PAYLOAD_KEY_EVIDENCE_COUNT: registry.get(PAYLOAD_KEY_ALPHA_BUDGET, {}).get(
                        PAYLOAD_KEY_EVIDENCE_COUNT, 0
                    ),
                    PAYLOAD_KEY_MISSING_EVIDENCE_COUNT: registry.get(
                        PAYLOAD_KEY_ALPHA_BUDGET, {}
                    ).get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0),
                },
                "Attach Alpha budget usage evidence to every release certificate",
            ),
            self._control(
                "ALPHA-002",
                "Alpha budget usage warnings are reviewed before release",
                "Release Registry",
                COMPLIANCE_LEVEL_PASS
                if registry.get(PAYLOAD_KEY_ALPHA_BUDGET, {}).get(PAYLOAD_KEY_WARNING_TOTAL, 0) == 0
                else COMPLIANCE_LEVEL_WARN,
                {
                    PAYLOAD_KEY_WARNING_RELEASE_COUNT: registry.get(
                        PAYLOAD_KEY_ALPHA_BUDGET, {}
                    ).get(PAYLOAD_KEY_WARNING_RELEASE_COUNT, 0),
                    PAYLOAD_KEY_WARNING_TOTAL: registry.get(PAYLOAD_KEY_ALPHA_BUDGET, {}).get(
                        PAYLOAD_KEY_WARNING_TOTAL, 0
                    ),
                },
                "Review Alpha budget usage warnings before production promotion",
            ),
            self._control(
                "GOV-001",
                "Registered releases have final audit evidence and cleared readiness",
                "Release Registry",
                self._governance_final_audit_status(registry),
                {
                    PAYLOAD_KEY_RECORD_COUNT: registry.get(PAYLOAD_KEY_RECORD_COUNT, 0),
                    **(registry.get(PAYLOAD_KEY_FINAL_AUDIT) or {}),
                },
                (
                    "Re-run release pipeline, register fresh certificates,"
                    " or attach final audit for each release"
                ),
            ),
            self._control(
                "GOV-002",
                f"Operations maturity score meets minimum threshold (>= {min_ops})",
                "Release Registry",
                self._governance_ops_maturity_status(registry),
                {
                    PAYLOAD_KEY_MIN_SCORE: min_ops,
                    PAYLOAD_KEY_RECORD_COUNT: registry.get(PAYLOAD_KEY_RECORD_COUNT, 0),
                    **(registry.get(PAYLOAD_KEY_OPS_MATURITY) or {}),
                },
                (
                    "Improve runbook/observability signals and re-register"
                    " with ops maturity report attached"
                ),
            ),
            self._control(
                "GOV-003",
                "At least one registered release uses deep validation mode",
                "Release Registry",
                self._governance_deep_validation_presence_status(registry),
                {
                    PAYLOAD_KEY_RECORD_COUNT: registry.get(PAYLOAD_KEY_RECORD_COUNT, 0),
                    PAYLOAD_KEY_VALIDATION_MODE_COUNTS: registry.get(
                        PAYLOAD_KEY_VALIDATION_MODE_COUNTS, {}
                    ),
                },
                "Run at least one deep validation release and register its certificate",
            ),
            self._control(
                "GOV-004",
                "Deep validation coverage across registered releases is complete",
                "Release Registry",
                self._governance_deep_validation_coverage_status(registry),
                {
                    PAYLOAD_KEY_RECORD_COUNT: registry.get(PAYLOAD_KEY_RECORD_COUNT, 0),
                    PAYLOAD_KEY_VALIDATION_MODE_COUNTS: registry.get(
                        PAYLOAD_KEY_VALIDATION_MODE_COUNTS, {}
                    ),
                },
                "Increase deep validation coverage across registered releases",
            ),
        ]

    def _control(
        self,
        control_id: str,
        objective: str,
        evidence_source: str,
        status: str,
        evidence: dict,
        remediation: str,
    ) -> dict:
        return {
            PAYLOAD_KEY_CONTROL_ID: control_id,
            PAYLOAD_KEY_OBJECTIVE: objective,
            PAYLOAD_KEY_EVIDENCE_SOURCE: evidence_source,
            PAYLOAD_KEY_STATUS: status,
            PAYLOAD_KEY_PASSED: status == COMPLIANCE_LEVEL_PASS,
            PAYLOAD_KEY_EVIDENCE: evidence,
            PAYLOAD_KEY_GAP: None if status == COMPLIANCE_LEVEL_PASS else remediation,
            PAYLOAD_KEY_REMEDIATION: remediation if status != COMPLIANCE_LEVEL_PASS else None,
        }

    def _status(self, controls: list[dict]) -> str:
        if any(c[PAYLOAD_KEY_STATUS] == COMPLIANCE_LEVEL_FAIL for c in controls):
            return COMPLIANCE_LEVEL_FAIL
        if any(c[PAYLOAD_KEY_STATUS] == COMPLIANCE_LEVEL_WARN for c in controls):
            return COMPLIANCE_LEVEL_WARN
        return COMPLIANCE_LEVEL_PASS

    @staticmethod
    def _governance_summary(controls: list[dict]) -> dict:
        focus_ids = {"GOV-003", "GOV-004"}
        focus = [
            {
                PAYLOAD_KEY_CONTROL_ID: item.get(PAYLOAD_KEY_CONTROL_ID),
                PAYLOAD_KEY_STATUS: item.get(PAYLOAD_KEY_STATUS),
                PAYLOAD_KEY_REMEDIATION: item.get(PAYLOAD_KEY_REMEDIATION),
            }
            for item in controls
            if item.get(PAYLOAD_KEY_CONTROL_ID) in focus_ids
        ]
        focus.sort(key=lambda item: item[PAYLOAD_KEY_CONTROL_ID])  # type: ignore[reportArgumentType]
        return build_governance_summary(
            focus=focus,
        )

    @staticmethod
    def _governance_final_audit_status(registry: dict) -> str:
        rc = registry.get(PAYLOAD_KEY_RECORD_COUNT, 0) or 0
        if rc == 0:
            return COMPLIANCE_LEVEL_PASS
        fa = registry.get(PAYLOAD_KEY_FINAL_AUDIT) or {}
        if (
            fa.get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0) > 0
            or fa.get(PAYLOAD_KEY_NOT_READY_COUNT, 0) > 0
            or fa.get(PAYLOAD_KEY_UNKNOWN_READINESS_COUNT, 0) > 0
        ):
            return COMPLIANCE_LEVEL_WARN
        return COMPLIANCE_LEVEL_PASS

    def _governance_ops_maturity_status(self, registry: dict) -> str:
        min_ops = float(getattr(self._container.config, "ops_maturity_min_score", 60.0))
        rc = registry.get(PAYLOAD_KEY_RECORD_COUNT, 0) or 0
        if rc == 0:
            return COMPLIANCE_LEVEL_PASS
        om = registry.get(PAYLOAD_KEY_OPS_MATURITY) or {}
        if (om.get(PAYLOAD_KEY_MISSING_SCORE_COUNT, 0) or 0) > 0:
            return COMPLIANCE_LEVEL_WARN
        avg = om.get(PAYLOAD_KEY_SCORE_AVG)
        if avg is not None and float(avg) < min_ops:
            return COMPLIANCE_LEVEL_WARN
        return COMPLIANCE_LEVEL_PASS

    @staticmethod
    def _governance_deep_validation_presence_status(registry: dict) -> str:
        rc = registry.get(PAYLOAD_KEY_RECORD_COUNT, 0) or 0
        if rc == 0:
            return COMPLIANCE_LEVEL_PASS
        counts = registry.get(PAYLOAD_KEY_VALIDATION_MODE_COUNTS) or {}
        deep_count = int(counts.get(VALIDATION_MODE_DEEP, 0) or 0)
        return COMPLIANCE_LEVEL_PASS if deep_count > 0 else COMPLIANCE_LEVEL_WARN

    @staticmethod
    def _governance_deep_validation_coverage_status(registry: dict) -> str:
        rc = registry.get(PAYLOAD_KEY_RECORD_COUNT, 0) or 0
        if rc == 0:
            return COMPLIANCE_LEVEL_PASS
        counts = registry.get(PAYLOAD_KEY_VALIDATION_MODE_COUNTS) or {}
        deep_count = int(counts.get(VALIDATION_MODE_DEEP, 0) or 0)
        if deep_count == 0 or deep_count == rc:
            return COMPLIANCE_LEVEL_PASS
        return COMPLIANCE_LEVEL_WARN
