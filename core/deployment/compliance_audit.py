"""Compliance audit reporting.

Builds compliance-oriented reports from the release registry,
operations timeline, readiness, gate, and SLO evidence.
"""
from datetime import datetime
from pathlib import Path
import json

from core.deployment.domain_keys import (
    COMPLIANCE_CHECK_ALPHA_BUDGET_EVIDENCE_REGISTERED,
    COMPLIANCE_CHECK_ALPHA_BUDGET_WARNINGS_CLEAR,
    COMPLIANCE_CHECK_ALL_REGISTERED_RELEASES_CERTIFIED,
    COMPLIANCE_CHECK_LATEST_RELEASE_HAS_EVIDENCE,
    COMPLIANCE_CHECK_NO_FAILED_TIMELINE_EVENTS,
    COMPLIANCE_CHECK_OPERATIONS_TIMELINE_PRESENT,
    COMPLIANCE_CHECK_READINESS_CLEAN,
    COMPLIANCE_CHECK_REGISTRY_FINAL_AUDIT_CLEARED,
    COMPLIANCE_CHECK_REGISTRY_DEEP_VALIDATION_COVERAGE_COMPLETE,
    COMPLIANCE_CHECK_REGISTRY_DEEP_VALIDATION_PRESENT,
    COMPLIANCE_CHECK_REGISTRY_OPS_MATURITY_THRESHOLD,
    COMPLIANCE_CHECK_RELEASE_GATE_NOT_BLOCKING,
    COMPLIANCE_CHECK_RELEASE_REGISTRY_PRESENT,
    COMPLIANCE_CHECK_SLO_HEALTHY,
    COMPLIANCE_LEVEL_FAIL,
    COMPLIANCE_LEVEL_PASS,
    COMPLIANCE_LEVEL_WARN,
    PAYLOAD_KEY_ACTION,
    PAYLOAD_KEY_ALPHA_BUDGET,
    PAYLOAD_KEY_ALPHA_BUDGET_EVIDENCE_COUNT,
    PAYLOAD_KEY_ALPHA_BUDGET_WARNING_TOTAL,
    PAYLOAD_KEY_CHECKS,
    PAYLOAD_KEY_CHECK_COUNT,
    PAYLOAD_KEY_CERTIFIED_COUNT,
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_DETAIL,
    PAYLOAD_KEY_ERROR_BUDGET,
    PAYLOAD_KEY_EVIDENCE,
    PAYLOAD_KEY_EVIDENCE_COUNT,
    PAYLOAD_KEY_EVIDENCE_OPERATIONS_TIMELINE,
    PAYLOAD_KEY_EVIDENCE_RELEASE_REGISTRY,
    PAYLOAD_KEY_EVIDENCE_VERIFIED,
    PAYLOAD_KEY_EVENT_COUNT,
    PAYLOAD_KEY_FAILED_CHECKS,
    PAYLOAD_KEY_FAILED_COUNT,
    PAYLOAD_KEY_FAILED_EVENT_COUNT,
    PAYLOAD_KEY_FAILED_OBJECTIVES,
    PAYLOAD_KEY_FINAL_AUDIT,
    PAYLOAD_KEY_GENERATED_AT,
    PAYLOAD_KEY_ID,
    PAYLOAD_KEY_LATEST,
    PAYLOAD_KEY_LATEST_ID,
    PAYLOAD_KEY_LEVEL,
    PAYLOAD_KEY_MIN_SCORE,
    PAYLOAD_KEY_MISSING_EVIDENCE_COUNT,
    PAYLOAD_KEY_MISSING_SCORE_COUNT,
    PAYLOAD_KEY_NAME,
    PAYLOAD_KEY_NOT_READY_COUNT,
    PAYLOAD_KEY_OPS_MATURITY,
    PAYLOAD_KEY_OUTPUT_PATH,
    PAYLOAD_KEY_READINESS,
    PAYLOAD_KEY_RELEASE_GATE,
    PAYLOAD_KEY_PASSED,
    PAYLOAD_KEY_PRIORITY,
    PAYLOAD_KEY_READY,
    PAYLOAD_KEY_READY_COUNT,
    PAYLOAD_KEY_RECOMMENDATIONS,
    PAYLOAD_KEY_RECORD_COUNT,
    PAYLOAD_KEY_REGISTRY_FINAL_AUDIT,
    PAYLOAD_KEY_REGISTRY_OPS_MATURITY,
    PAYLOAD_KEY_REGISTRY_RECORD_COUNT,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_SCORE_AVG,
    PAYLOAD_KEY_SCORE_MAX,
    PAYLOAD_KEY_SCORE_MIN,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_STATUS_COUNTS,
    PAYLOAD_KEY_STRATEGY_COUNT_WITH_SCORE,
    PAYLOAD_KEY_SLO,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_TIMELINE_EVENT_COUNT,
    PAYLOAD_KEY_UNKNOWN_READINESS_COUNT,
    PAYLOAD_KEY_VALIDATION_MODE_COUNTS,
    PAYLOAD_KEY_WARNING_COUNT,
    PAYLOAD_KEY_WARNING_RELEASE_COUNT,
    PAYLOAD_KEY_WARNING_TOTAL,
    RECOMMENDATION_PRIORITY_CRITICAL,
    RECOMMENDATION_PRIORITY_HIGH,
    RECOMMENDATION_PRIORITY_LOW,
    RECOMMENDATION_PRIORITY_MEDIUM,
    RELEASE_PIPELINE_GATE_DECISION_ALLOW,
    SLO_STATUS_HEALTHY,
    TIMELINE_STATUS_FAILED,
    VALIDATION_MODE_DEEP,
)
from core.deployment.schema_versions import SCHEMA_COMPLIANCE_AUDIT
from core.deployment.governance_summary import build_governance_summary
from core.deployment.validation_mode import resolve_validation_mode


class ComplianceAuditService:
    """Evaluates operational release compliance status."""

    def __init__(self, container):
        self._container = container

    def generate(
        self,
        *,
        output: str | None = None,
        validation_mode: str | None = None,
    ) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        registry_summary = self._container.release_registry.summarize()
        timeline_summary = self._container.operations_timeline.summarize()
        readiness = self._container.release_readiness.build_report(validation_mode=validation_mode)
        gate = self._container.release_gate.evaluate(strict=False, validation_mode=validation_mode)
        slo = self._container.slo_service.evaluate()
        checks = self._build_checks(registry_summary, timeline_summary, readiness, gate, slo)
        status = self._status(checks)
        report = {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_COMPLIANCE_AUDIT,
            PAYLOAD_KEY_GENERATED_AT: datetime.utcnow().isoformat(),
            PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
            PAYLOAD_KEY_STATUS: status,
            PAYLOAD_KEY_PASSED: status == COMPLIANCE_LEVEL_PASS,
            PAYLOAD_KEY_SUMMARY: {
                PAYLOAD_KEY_CHECK_COUNT: len(checks),
                PAYLOAD_KEY_FAILED_COUNT: len([c for c in checks if c[PAYLOAD_KEY_LEVEL] == COMPLIANCE_LEVEL_FAIL]),
                PAYLOAD_KEY_WARNING_COUNT: len([c for c in checks if c[PAYLOAD_KEY_LEVEL] == COMPLIANCE_LEVEL_WARN]),
                PAYLOAD_KEY_REGISTRY_RECORD_COUNT: registry_summary.get(PAYLOAD_KEY_RECORD_COUNT, 0),
                PAYLOAD_KEY_TIMELINE_EVENT_COUNT: timeline_summary.get(PAYLOAD_KEY_EVENT_COUNT, 0),
                PAYLOAD_KEY_VALIDATION_MODE_COUNTS: registry_summary.get(PAYLOAD_KEY_VALIDATION_MODE_COUNTS, {}),
                PAYLOAD_KEY_ALPHA_BUDGET_EVIDENCE_COUNT: registry_summary.get(PAYLOAD_KEY_ALPHA_BUDGET, {}).get(PAYLOAD_KEY_EVIDENCE_COUNT, 0),
                PAYLOAD_KEY_ALPHA_BUDGET_WARNING_TOTAL: registry_summary.get(PAYLOAD_KEY_ALPHA_BUDGET, {}).get(PAYLOAD_KEY_WARNING_TOTAL, 0),
                PAYLOAD_KEY_REGISTRY_FINAL_AUDIT: registry_summary.get(PAYLOAD_KEY_FINAL_AUDIT, {}),
                PAYLOAD_KEY_REGISTRY_OPS_MATURITY: registry_summary.get(PAYLOAD_KEY_OPS_MATURITY, {}),
                **self._governance_summary(checks),
            },
            PAYLOAD_KEY_CHECKS: checks,
            PAYLOAD_KEY_EVIDENCE: {
                PAYLOAD_KEY_EVIDENCE_RELEASE_REGISTRY: registry_summary,
                PAYLOAD_KEY_EVIDENCE_OPERATIONS_TIMELINE: timeline_summary,
                PAYLOAD_KEY_READINESS: {
                    PAYLOAD_KEY_READY: readiness.get(PAYLOAD_KEY_READY),
                    PAYLOAD_KEY_FAILED_CHECKS: readiness.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_FAILED_CHECKS, []),
                },
                PAYLOAD_KEY_RELEASE_GATE: {
                    PAYLOAD_KEY_DECISION: gate.get(PAYLOAD_KEY_DECISION),
                    PAYLOAD_KEY_SUMMARY: gate.get(PAYLOAD_KEY_SUMMARY, {}),
                },
                PAYLOAD_KEY_SLO: {
                    PAYLOAD_KEY_STATUS: slo.get(PAYLOAD_KEY_STATUS),
                    PAYLOAD_KEY_FAILED_OBJECTIVES: slo.get(PAYLOAD_KEY_FAILED_OBJECTIVES, []),
                    PAYLOAD_KEY_ERROR_BUDGET: slo.get(PAYLOAD_KEY_ERROR_BUDGET, {}),
                },
            },
            PAYLOAD_KEY_RECOMMENDATIONS: self._recommendations(checks),
        }
        if output:
            report[PAYLOAD_KEY_OUTPUT_PATH] = self.save_report(report, output)
        return report

    def save_report(self, report: dict, path: str) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return str(target)

    def _build_checks(
        self,
        registry_summary: dict,
        timeline_summary: dict,
        readiness: dict,
        gate: dict,
        slo: dict,
    ) -> list[dict]:
        checks = []
        record_count = registry_summary.get(PAYLOAD_KEY_RECORD_COUNT, 0)
        certified_count = registry_summary.get(PAYLOAD_KEY_CERTIFIED_COUNT, 0)
        timeline_failed = timeline_summary.get(PAYLOAD_KEY_STATUS_COUNTS, {}).get(TIMELINE_STATUS_FAILED, 0)
        checks.append(self._check(
            COMPLIANCE_CHECK_RELEASE_REGISTRY_PRESENT,
            COMPLIANCE_LEVEL_PASS if record_count > 0 else COMPLIANCE_LEVEL_WARN,
            {PAYLOAD_KEY_RECORD_COUNT: record_count},
        ))
        checks.append(self._check(
            COMPLIANCE_CHECK_ALL_REGISTERED_RELEASES_CERTIFIED,
            COMPLIANCE_LEVEL_PASS if record_count == certified_count else COMPLIANCE_LEVEL_FAIL,
            {PAYLOAD_KEY_RECORD_COUNT: record_count, PAYLOAD_KEY_CERTIFIED_COUNT: certified_count},
        ))
        checks.append(self._check(
            COMPLIANCE_CHECK_OPERATIONS_TIMELINE_PRESENT,
            COMPLIANCE_LEVEL_PASS if timeline_summary.get(PAYLOAD_KEY_EVENT_COUNT, 0) > 0 else COMPLIANCE_LEVEL_WARN,
            {PAYLOAD_KEY_EVENT_COUNT: timeline_summary.get(PAYLOAD_KEY_EVENT_COUNT, 0)},
        ))
        checks.append(self._check(
            COMPLIANCE_CHECK_NO_FAILED_TIMELINE_EVENTS,
            COMPLIANCE_LEVEL_PASS if timeline_failed == 0 else COMPLIANCE_LEVEL_FAIL,
            {PAYLOAD_KEY_FAILED_EVENT_COUNT: timeline_failed},
        ))
        checks.append(self._check(
            COMPLIANCE_CHECK_READINESS_CLEAN,
            COMPLIANCE_LEVEL_PASS if readiness.get(PAYLOAD_KEY_READY) else COMPLIANCE_LEVEL_FAIL,
            {PAYLOAD_KEY_READY: readiness.get(PAYLOAD_KEY_READY)},
        ))
        checks.append(self._check(
            COMPLIANCE_CHECK_RELEASE_GATE_NOT_BLOCKING,
            COMPLIANCE_LEVEL_PASS if gate.get(PAYLOAD_KEY_DECISION) in {RELEASE_PIPELINE_GATE_DECISION_ALLOW, COMPLIANCE_LEVEL_WARN} else COMPLIANCE_LEVEL_FAIL,
            {PAYLOAD_KEY_DECISION: gate.get(PAYLOAD_KEY_DECISION)},
        ))
        checks.append(self._check(
            COMPLIANCE_CHECK_SLO_HEALTHY,
            COMPLIANCE_LEVEL_PASS if slo.get(PAYLOAD_KEY_STATUS) == SLO_STATUS_HEALTHY else COMPLIANCE_LEVEL_FAIL,
            {PAYLOAD_KEY_STATUS: slo.get(PAYLOAD_KEY_STATUS), PAYLOAD_KEY_FAILED_OBJECTIVES: slo.get(PAYLOAD_KEY_FAILED_OBJECTIVES, [])},
        ))
        latest = registry_summary.get(PAYLOAD_KEY_LATEST) or {}
        checks.append(self._check(
            COMPLIANCE_CHECK_LATEST_RELEASE_HAS_EVIDENCE,
            COMPLIANCE_LEVEL_PASS if not latest or latest.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_EVIDENCE_VERIFIED) else COMPLIANCE_LEVEL_FAIL,
            {PAYLOAD_KEY_LATEST_ID: latest.get(PAYLOAD_KEY_ID), PAYLOAD_KEY_EVIDENCE_VERIFIED: latest.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_EVIDENCE_VERIFIED)},
        ))
        alpha_budget = registry_summary.get(PAYLOAD_KEY_ALPHA_BUDGET, {})
        checks.append(self._check(
            COMPLIANCE_CHECK_ALPHA_BUDGET_EVIDENCE_REGISTERED,
            COMPLIANCE_LEVEL_PASS if record_count == 0 or alpha_budget.get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0) == 0 else COMPLIANCE_LEVEL_WARN,
            {
                PAYLOAD_KEY_RECORD_COUNT: record_count,
                PAYLOAD_KEY_EVIDENCE_COUNT: alpha_budget.get(PAYLOAD_KEY_EVIDENCE_COUNT, 0),
                PAYLOAD_KEY_MISSING_EVIDENCE_COUNT: alpha_budget.get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0),
            },
        ))
        checks.append(self._check(
            COMPLIANCE_CHECK_ALPHA_BUDGET_WARNINGS_CLEAR,
            COMPLIANCE_LEVEL_PASS if alpha_budget.get(PAYLOAD_KEY_WARNING_TOTAL, 0) == 0 else COMPLIANCE_LEVEL_WARN,
            {
                PAYLOAD_KEY_WARNING_RELEASE_COUNT: alpha_budget.get(PAYLOAD_KEY_WARNING_RELEASE_COUNT, 0),
                PAYLOAD_KEY_WARNING_TOTAL: alpha_budget.get(PAYLOAD_KEY_WARNING_TOTAL, 0),
            },
        ))
        mode_counts = registry_summary.get(PAYLOAD_KEY_VALIDATION_MODE_COUNTS) or {}
        deep_count = int(mode_counts.get(VALIDATION_MODE_DEEP, 0) or 0)
        checks.append(self._check(
            COMPLIANCE_CHECK_REGISTRY_DEEP_VALIDATION_PRESENT,
            COMPLIANCE_LEVEL_PASS if record_count == 0 or deep_count > 0 else COMPLIANCE_LEVEL_WARN,
            {
                PAYLOAD_KEY_RECORD_COUNT: record_count,
                PAYLOAD_KEY_VALIDATION_MODE_COUNTS: mode_counts,
            },
        ))
        checks.append(self._check(
            COMPLIANCE_CHECK_REGISTRY_DEEP_VALIDATION_COVERAGE_COMPLETE,
            COMPLIANCE_LEVEL_PASS if record_count == 0 or deep_count in {0, record_count} else COMPLIANCE_LEVEL_WARN,
            {
                PAYLOAD_KEY_RECORD_COUNT: record_count,
                PAYLOAD_KEY_VALIDATION_MODE_COUNTS: mode_counts,
            },
        ))
        fa = registry_summary.get(PAYLOAD_KEY_FINAL_AUDIT) or {}
        om = registry_summary.get(PAYLOAD_KEY_OPS_MATURITY) or {}
        if record_count == 0:
            fa_level = COMPLIANCE_LEVEL_PASS
        elif (
            fa.get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0) > 0
            or fa.get(PAYLOAD_KEY_NOT_READY_COUNT, 0) > 0
            or fa.get(PAYLOAD_KEY_UNKNOWN_READINESS_COUNT, 0) > 0
        ):
            fa_level = COMPLIANCE_LEVEL_WARN
        else:
            fa_level = COMPLIANCE_LEVEL_PASS
        checks.append(self._check(
            COMPLIANCE_CHECK_REGISTRY_FINAL_AUDIT_CLEARED,
            fa_level,
            {
                PAYLOAD_KEY_RECORD_COUNT: record_count,
                PAYLOAD_KEY_EVIDENCE_COUNT: fa.get(PAYLOAD_KEY_EVIDENCE_COUNT, 0),
                PAYLOAD_KEY_READY_COUNT: fa.get(PAYLOAD_KEY_READY_COUNT, 0),
                PAYLOAD_KEY_NOT_READY_COUNT: fa.get(PAYLOAD_KEY_NOT_READY_COUNT, 0),
                PAYLOAD_KEY_UNKNOWN_READINESS_COUNT: fa.get(PAYLOAD_KEY_UNKNOWN_READINESS_COUNT, 0),
                PAYLOAD_KEY_MISSING_EVIDENCE_COUNT: fa.get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0),
            },
        ))
        min_ops = self._ops_maturity_min_score()
        if record_count == 0:
            om_level = COMPLIANCE_LEVEL_PASS
        elif om.get(PAYLOAD_KEY_MISSING_SCORE_COUNT, 0) > 0:
            om_level = COMPLIANCE_LEVEL_WARN
        elif om.get(PAYLOAD_KEY_SCORE_AVG) is not None and float(om.get(PAYLOAD_KEY_SCORE_AVG, 0)) < min_ops:
            om_level = COMPLIANCE_LEVEL_WARN
        else:
            om_level = COMPLIANCE_LEVEL_PASS
        checks.append(self._check(
            COMPLIANCE_CHECK_REGISTRY_OPS_MATURITY_THRESHOLD,
            om_level,
            {
                PAYLOAD_KEY_MIN_SCORE: min_ops,
                PAYLOAD_KEY_RECORD_COUNT: record_count,
                PAYLOAD_KEY_STRATEGY_COUNT_WITH_SCORE: om.get(PAYLOAD_KEY_STRATEGY_COUNT_WITH_SCORE, 0),
                PAYLOAD_KEY_SCORE_AVG: om.get(PAYLOAD_KEY_SCORE_AVG),
                PAYLOAD_KEY_SCORE_MIN: om.get(PAYLOAD_KEY_SCORE_MIN),
                PAYLOAD_KEY_SCORE_MAX: om.get(PAYLOAD_KEY_SCORE_MAX),
                PAYLOAD_KEY_MISSING_SCORE_COUNT: om.get(PAYLOAD_KEY_MISSING_SCORE_COUNT, 0),
            },
        ))
        return checks

    def _check(self, name: str, level: str, detail: dict) -> dict:
        return {PAYLOAD_KEY_NAME: name, PAYLOAD_KEY_LEVEL: level, PAYLOAD_KEY_PASSED: level == COMPLIANCE_LEVEL_PASS, PAYLOAD_KEY_DETAIL: detail}

    def _ops_maturity_min_score(self) -> float:
        return float(getattr(self._container.config, "ops_maturity_min_score", 60.0))

    def _status(self, checks: list[dict]) -> str:
        if any(check[PAYLOAD_KEY_LEVEL] == COMPLIANCE_LEVEL_FAIL for check in checks):
            return COMPLIANCE_LEVEL_FAIL
        if any(check[PAYLOAD_KEY_LEVEL] == COMPLIANCE_LEVEL_WARN for check in checks):
            return COMPLIANCE_LEVEL_WARN
        return COMPLIANCE_LEVEL_PASS

    @staticmethod
    def _governance_summary(checks: list[dict]) -> dict:
        focus_names = {
            COMPLIANCE_CHECK_REGISTRY_DEEP_VALIDATION_PRESENT,
            COMPLIANCE_CHECK_REGISTRY_DEEP_VALIDATION_COVERAGE_COMPLETE,
        }
        focus = [
            {
                PAYLOAD_KEY_NAME: item.get(PAYLOAD_KEY_NAME),
                PAYLOAD_KEY_LEVEL: item.get(PAYLOAD_KEY_LEVEL),
                PAYLOAD_KEY_DETAIL: item.get(PAYLOAD_KEY_DETAIL),
            }
            for item in checks
            if item.get(PAYLOAD_KEY_NAME) in focus_names
        ]
        focus.sort(key=lambda item: item[PAYLOAD_KEY_NAME])
        return build_governance_summary(
            focus=focus,
        )

    def _recommendations(self, checks: list[dict]) -> list[dict]:
        recs = []
        for check in checks:
            if check[PAYLOAD_KEY_LEVEL] == COMPLIANCE_LEVEL_PASS:
                continue
            if check[PAYLOAD_KEY_NAME] == COMPLIANCE_CHECK_RELEASE_REGISTRY_PRESENT:
                recs.append({PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_MEDIUM, PAYLOAD_KEY_ACTION: "Register at least one release certificate"})
            elif check[PAYLOAD_KEY_NAME] == COMPLIANCE_CHECK_ALL_REGISTERED_RELEASES_CERTIFIED:
                recs.append({PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_CRITICAL, PAYLOAD_KEY_ACTION: "Investigate rejected or uncertified release records"})
            elif check[PAYLOAD_KEY_NAME] == COMPLIANCE_CHECK_OPERATIONS_TIMELINE_PRESENT:
                recs.append({PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_MEDIUM, PAYLOAD_KEY_ACTION: "Record release pipeline events into operations timeline"})
            elif check[PAYLOAD_KEY_NAME] == COMPLIANCE_CHECK_NO_FAILED_TIMELINE_EVENTS:
                recs.append({PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_HIGH, PAYLOAD_KEY_ACTION: "Review failed operations timeline events"})
            elif check[PAYLOAD_KEY_NAME] == COMPLIANCE_CHECK_READINESS_CLEAN:
                recs.append({PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_CRITICAL, PAYLOAD_KEY_ACTION: "Fix release readiness failures"})
            elif check[PAYLOAD_KEY_NAME] == COMPLIANCE_CHECK_RELEASE_GATE_NOT_BLOCKING:
                recs.append({PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_CRITICAL, PAYLOAD_KEY_ACTION: "Resolve release gate blocking signals"})
            elif check[PAYLOAD_KEY_NAME] == COMPLIANCE_CHECK_SLO_HEALTHY:
                recs.append({PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_HIGH, PAYLOAD_KEY_ACTION: "Restore SLO compliance"})
            elif check[PAYLOAD_KEY_NAME] == COMPLIANCE_CHECK_LATEST_RELEASE_HAS_EVIDENCE:
                recs.append({PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_CRITICAL, PAYLOAD_KEY_ACTION: "Regenerate or verify latest release evidence bundle"})
            elif check[PAYLOAD_KEY_NAME] == COMPLIANCE_CHECK_ALPHA_BUDGET_EVIDENCE_REGISTERED:
                recs.append({PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_MEDIUM, PAYLOAD_KEY_ACTION: "Attach Alpha budget usage evidence to all release certificates"})
            elif check[PAYLOAD_KEY_NAME] == COMPLIANCE_CHECK_ALPHA_BUDGET_WARNINGS_CLEAR:
                recs.append({PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_MEDIUM, PAYLOAD_KEY_ACTION: "Review Alpha budget usage warnings before the next release"})
            elif check[PAYLOAD_KEY_NAME] == COMPLIANCE_CHECK_REGISTRY_FINAL_AUDIT_CLEARED:
                recs.append({PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_MEDIUM, PAYLOAD_KEY_ACTION: "Ensure every registered release has final audit evidence and a cleared final audit"})
            elif check[PAYLOAD_KEY_NAME] == COMPLIANCE_CHECK_REGISTRY_OPS_MATURITY_THRESHOLD:
                m = check[PAYLOAD_KEY_DETAIL].get(PAYLOAD_KEY_MIN_SCORE, 60.0)
                recs.append({
                    PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_MEDIUM,
                    PAYLOAD_KEY_ACTION: f"Raise ops maturity score to >= {m} and register releases with full maturity evidence",
                })
            elif check[PAYLOAD_KEY_NAME] == COMPLIANCE_CHECK_REGISTRY_DEEP_VALIDATION_PRESENT:
                recs.append({
                    PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_HIGH,
                    PAYLOAD_KEY_ACTION: "Run at least one deep validation release and register its certificate",
                })
            elif check[PAYLOAD_KEY_NAME] == COMPLIANCE_CHECK_REGISTRY_DEEP_VALIDATION_COVERAGE_COMPLETE:
                recs.append({
                    PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_MEDIUM,
                    PAYLOAD_KEY_ACTION: "Increase deep validation coverage across registered releases",
                })
        if not recs:
            recs.append({PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_LOW, PAYLOAD_KEY_ACTION: "Archive compliance audit report"})
        return recs
