"""Postmortem report generation.

Builds audit-ready postmortem reports from the operations timeline,
current diagnostics, SLO status, and release gate evidence.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.contracts.domain_keys import (
    ACTION_OWNER_ENGINEERING,
    ACTION_OWNER_OPERATIONS,
    ACTION_OWNER_RELEASE,
    ACTION_OWNER_RELIABILITY,
    ACTION_OWNER_RISK,
    ACTION_TEXT_ALPHA_BUDGET_EVIDENCE_MISSING,
    ACTION_TEXT_ALPHA_BUDGET_WARNINGS_PRESENT,
    ACTION_TEXT_ARCHIVE_REPORT,
    ACTION_TEXT_AUDIT_ERRORS,
    ACTION_TEXT_RELEASE_GATE_BLOCKED,
    ACTION_TEXT_SLO_BREACH,
    ACTION_TEXT_TIMELINE_FAILURES,
    EVIDENCE_SECTION_ENGINE_CONFIG,
    FINDING_ID_ALPHA_BUDGET_EVIDENCE_MISSING,
    FINDING_ID_ALPHA_BUDGET_WARNINGS_PRESENT,
    FINDING_ID_AUDIT_ERRORS,
    FINDING_ID_NO_MATERIAL_FINDINGS,
    FINDING_ID_RELEASE_GATE_BLOCKED,
    FINDING_ID_SLO_BREACH,
    FINDING_ID_TIMELINE_FAILURES,
    FINDING_SEVERITY_CRITICAL,
    FINDING_SEVERITY_HIGH,
    FINDING_SEVERITY_LOW,
    FINDING_SEVERITY_MEDIUM,
    PAYLOAD_KEY_ACTION,
    PAYLOAD_KEY_ACTOR,
    PAYLOAD_KEY_ALERTS,
    PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE,
    PAYLOAD_KEY_ALPHA_BUDGET_MISSING_EVIDENCE,
    PAYLOAD_KEY_ALPHA_BUDGET_WARNINGS,
    PAYLOAD_KEY_AUDIT_SUMMARY,
    PAYLOAD_KEY_AVAILABLE,
    PAYLOAD_KEY_BLOCKING_SIGNALS,
    PAYLOAD_KEY_CORRECTIVE_ACTIONS,
    PAYLOAD_KEY_CURRENT,
    PAYLOAD_KEY_CUSTOMER_IMPACT,
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_DIAGNOSTICS,
    PAYLOAD_KEY_ENGINE_CONFIG_TIMELINE_EVENTS,
    PAYLOAD_KEY_ERROR,
    PAYLOAD_KEY_ERROR_BUDGET,
    PAYLOAD_KEY_EVENT_COUNT,
    PAYLOAD_KEY_EVENT_TYPE,
    PAYLOAD_KEY_EVENTS,
    PAYLOAD_KEY_EVIDENCE,
    PAYLOAD_KEY_EVIDENCE_COUNT,
    PAYLOAD_KEY_FAILED_EVENT_COUNT,
    PAYLOAD_KEY_FAILED_EVENT_TYPES,
    PAYLOAD_KEY_FAILED_OBJECTIVES,
    PAYLOAD_KEY_FINANCIAL_IMPACT,
    PAYLOAD_KEY_FINDINGS,
    PAYLOAD_KEY_GENERATED_AT,
    PAYLOAD_KEY_ID,
    PAYLOAD_KEY_IMPACT,
    PAYLOAD_KEY_INCIDENT,
    PAYLOAD_KEY_LAST_EVENT_AT,
    PAYLOAD_KEY_METRICS,
    PAYLOAD_KEY_MISSING_EVIDENCE_COUNT,
    PAYLOAD_KEY_OUTPUT_PATH,
    PAYLOAD_KEY_OWNER,
    PAYLOAD_KEY_PRIORITY,
    PAYLOAD_KEY_RELEASE_BLOCKED,
    PAYLOAD_KEY_RELEASE_GATE,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_SEVERITY,
    PAYLOAD_KEY_SEVERITY_COUNTS,
    PAYLOAD_KEY_SLO,
    PAYLOAD_KEY_SLO_BREACHING,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_STATUS_COUNTS,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_TIMELINE,
    PAYLOAD_KEY_TIMELINE_SUMMARY,
    PAYLOAD_KEY_TIMESTAMP,
    PAYLOAD_KEY_TITLE,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_WARNING_EVENT_COUNT,
    PAYLOAD_KEY_WARNING_TOTAL,
    POSTMORTEM_IMPACT_VALUE_UNKNOWN,
    POSTMORTEM_STATUS_ACTION_REQUIRED,
    POSTMORTEM_STATUS_CLOSED,
    POSTMORTEM_STATUS_CRITICAL,
    RECOMMENDATION_PRIORITY_CRITICAL,
    RECOMMENDATION_PRIORITY_HIGH,
    RECOMMENDATION_PRIORITY_LOW,
    RECOMMENDATION_PRIORITY_MEDIUM,
    RELEASE_PIPELINE_GATE_DECISION_BLOCK,
    RELEASE_PIPELINE_POSTMORTEM_SEVERITY,
    SLO_STATUS_BREACHING,
    TIMELINE_EVENT_ALPHA_BUDGET_GOVERNANCE,
    TIMELINE_EVENT_ENGINE_CONFIG,
    TIMELINE_STATUS_FAILED,
)
from core.deployment.atomic_file_writer import atomic_write_json
from core.deployment.governance_summary import extract_governance_summary
from core.deployment.schema_versions import SCHEMA_POSTMORTEM_REPORT
from core.deployment.validation_mode import resolve_validation_mode


class PostmortemReportService:
    """Generates structured incident or release postmortem reports."""

    def __init__(self, container):
        self._container = container

    def generate(
        self,
        *,
        incident_id: str,
        title: str = "Operational Postmortem",
        severity: str = RELEASE_PIPELINE_POSTMORTEM_SEVERITY,
        output: str | None = None,
        validation_mode: str | None = None,
    ) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        timeline = self._container.operations_timeline.list_events()
        timeline_summary = self._container.operations_timeline.summarize()
        diagnostics = self._build_diagnostics()
        gate = self._container.release_gate.evaluate(strict=False, validation_mode=validation_mode)
        slo = self._container.slo_service.evaluate()
        compliance = self._container.compliance_audit.generate(validation_mode=validation_mode)
        compliance_summary = compliance.get(PAYLOAD_KEY_SUMMARY) or {}
        alpha_budget = self._build_alpha_budget_analysis(timeline)
        engine_config = self._build_engine_config_timeline(timeline)
        impact = self._build_impact(timeline, gate, slo, alpha_budget, engine_config)
        findings = self._build_findings(timeline, gate, slo, diagnostics, alpha_budget)
        actions = self._build_actions(findings)
        report = {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_POSTMORTEM_REPORT,
            PAYLOAD_KEY_GENERATED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
            PAYLOAD_KEY_INCIDENT: {
                PAYLOAD_KEY_ID: incident_id,
                PAYLOAD_KEY_TITLE: title,
                PAYLOAD_KEY_SEVERITY: severity,
                PAYLOAD_KEY_STATUS: self._infer_status(findings),
            },
            PAYLOAD_KEY_TIMELINE_SUMMARY: timeline_summary,
            PAYLOAD_KEY_SUMMARY: extract_governance_summary(compliance_summary),
            PAYLOAD_KEY_TIMELINE: self._compact_timeline(timeline),
            PAYLOAD_KEY_IMPACT: impact,
            PAYLOAD_KEY_FINDINGS: findings,
            PAYLOAD_KEY_CORRECTIVE_ACTIONS: actions,
            PAYLOAD_KEY_EVIDENCE: {
                PAYLOAD_KEY_RELEASE_GATE: {
                    PAYLOAD_KEY_DECISION: gate.get(PAYLOAD_KEY_DECISION),
                    PAYLOAD_KEY_SUMMARY: gate.get(PAYLOAD_KEY_SUMMARY),
                },
                PAYLOAD_KEY_SLO: {
                    PAYLOAD_KEY_STATUS: slo.get(PAYLOAD_KEY_STATUS),
                    PAYLOAD_KEY_FAILED_OBJECTIVES: slo.get(PAYLOAD_KEY_FAILED_OBJECTIVES, []),
                    PAYLOAD_KEY_ERROR_BUDGET: slo.get(PAYLOAD_KEY_ERROR_BUDGET, {}),
                },
                PAYLOAD_KEY_DIAGNOSTICS: diagnostics,
                PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE: alpha_budget,
                EVIDENCE_SECTION_ENGINE_CONFIG: {
                    PAYLOAD_KEY_TIMELINE: engine_config,
                    PAYLOAD_KEY_CURRENT: self._container.evidence_bundle.engine_config_snapshot(),
                },
            },
        }
        if output:
            report[PAYLOAD_KEY_OUTPUT_PATH] = self.save_report(report, output)
        return report

    def save_report(self, report: dict, path: str) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, report)
        return str(target)

    def _build_diagnostics(self) -> dict:
        diagnostics = getattr(self._container, "diagnostics", None)
        if diagnostics is None:
            return {PAYLOAD_KEY_AVAILABLE: False}
        snapshot = diagnostics.build_snapshot()
        return {
            PAYLOAD_KEY_AVAILABLE: True,
            PAYLOAD_KEY_ALERTS: snapshot.get(PAYLOAD_KEY_ALERTS) or [],
            PAYLOAD_KEY_AUDIT_SUMMARY: snapshot.get(PAYLOAD_KEY_AUDIT_SUMMARY) or {},
            PAYLOAD_KEY_METRICS: snapshot.get(PAYLOAD_KEY_METRICS) or {},
        }

    def _compact_timeline(self, events: list[dict]) -> list[dict]:
        return [
            {
                PAYLOAD_KEY_ID: event.get(PAYLOAD_KEY_ID),
                PAYLOAD_KEY_TIMESTAMP: event.get(PAYLOAD_KEY_TIMESTAMP),
                PAYLOAD_KEY_EVENT_TYPE: event.get(PAYLOAD_KEY_EVENT_TYPE),
                PAYLOAD_KEY_STATUS: event.get(PAYLOAD_KEY_STATUS),
                PAYLOAD_KEY_ACTOR: event.get(PAYLOAD_KEY_ACTOR),
                PAYLOAD_KEY_SUMMARY: event.get(PAYLOAD_KEY_SUMMARY, {}),
            }
            for event in events
        ]

    def _build_engine_config_timeline(self, timeline: list[dict]) -> dict:
        events = [
            e for e in timeline if e.get(PAYLOAD_KEY_EVENT_TYPE) == TIMELINE_EVENT_ENGINE_CONFIG
        ]
        return {
            PAYLOAD_KEY_EVENT_COUNT: len(events),
            PAYLOAD_KEY_LAST_EVENT_AT: events[-1].get(PAYLOAD_KEY_TIMESTAMP) if events else None,
            PAYLOAD_KEY_EVENTS: [
                {
                    PAYLOAD_KEY_ID: e.get(PAYLOAD_KEY_ID),
                    PAYLOAD_KEY_TIMESTAMP: e.get(PAYLOAD_KEY_TIMESTAMP),
                    PAYLOAD_KEY_ACTOR: e.get(PAYLOAD_KEY_ACTOR),
                    PAYLOAD_KEY_STATUS: e.get(PAYLOAD_KEY_STATUS),
                    PAYLOAD_KEY_SUMMARY: e.get(PAYLOAD_KEY_SUMMARY, {}),
                }
                for e in events
            ],
        }

    def _build_alpha_budget_analysis(self, timeline: list[dict]) -> dict:
        events = [
            event
            for event in timeline
            if event.get(PAYLOAD_KEY_EVENT_TYPE) == TIMELINE_EVENT_ALPHA_BUDGET_GOVERNANCE
        ]
        warning_total = sum(
            event.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_WARNING_TOTAL, 0) for event in events
        )
        missing_evidence_count = sum(
            event.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0)
            for event in events
        )
        evidence_count = sum(
            event.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_EVIDENCE_COUNT, 0)
            for event in events
        )
        statuses: dict[str, int] = {}
        for event in events:
            k = str(event.get(PAYLOAD_KEY_STATUS))
            statuses[k] = statuses.get(k, 0) + 1
        return {
            PAYLOAD_KEY_EVENT_COUNT: len(events),
            PAYLOAD_KEY_STATUS_COUNTS: statuses,
            PAYLOAD_KEY_EVIDENCE_COUNT: evidence_count,
            PAYLOAD_KEY_MISSING_EVIDENCE_COUNT: missing_evidence_count,
            PAYLOAD_KEY_WARNING_TOTAL: warning_total,
            PAYLOAD_KEY_WARNING_EVENT_COUNT: len(
                [
                    event
                    for event in events
                    if event.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_WARNING_TOTAL, 0) > 0
                ]
            ),
            PAYLOAD_KEY_EVENTS: [
                {
                    PAYLOAD_KEY_ID: event.get(PAYLOAD_KEY_ID),
                    PAYLOAD_KEY_TIMESTAMP: event.get(PAYLOAD_KEY_TIMESTAMP),
                    PAYLOAD_KEY_ACTOR: event.get(PAYLOAD_KEY_ACTOR),
                    PAYLOAD_KEY_STATUS: event.get(PAYLOAD_KEY_STATUS),
                    PAYLOAD_KEY_SUMMARY: event.get(PAYLOAD_KEY_SUMMARY, {}),
                }
                for event in events
            ],
        }

    def _build_impact(
        self,
        timeline: list[dict],
        gate: dict,
        slo: dict,
        alpha_budget: dict | None = None,
        engine_config: dict | None = None,
    ) -> dict:
        failed_events = [
            event for event in timeline if event.get(PAYLOAD_KEY_STATUS) == TIMELINE_STATUS_FAILED
        ]
        alpha_budget = alpha_budget or {}
        engine_config = engine_config or {}
        return {
            PAYLOAD_KEY_FAILED_EVENT_COUNT: len(failed_events),
            PAYLOAD_KEY_FAILED_EVENT_TYPES: sorted(
                {event.get(PAYLOAD_KEY_EVENT_TYPE) for event in failed_events}
            ),
            PAYLOAD_KEY_RELEASE_BLOCKED: gate.get(PAYLOAD_KEY_DECISION)
            == RELEASE_PIPELINE_GATE_DECISION_BLOCK,
            PAYLOAD_KEY_SLO_BREACHING: slo.get(PAYLOAD_KEY_STATUS) == SLO_STATUS_BREACHING,
            PAYLOAD_KEY_ALPHA_BUDGET_WARNINGS: alpha_budget.get(PAYLOAD_KEY_WARNING_TOTAL, 0),
            PAYLOAD_KEY_ALPHA_BUDGET_MISSING_EVIDENCE: alpha_budget.get(
                PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0
            ),
            PAYLOAD_KEY_ENGINE_CONFIG_TIMELINE_EVENTS: engine_config.get(
                PAYLOAD_KEY_EVENT_COUNT, 0
            ),
            PAYLOAD_KEY_CUSTOMER_IMPACT: POSTMORTEM_IMPACT_VALUE_UNKNOWN,
            PAYLOAD_KEY_FINANCIAL_IMPACT: POSTMORTEM_IMPACT_VALUE_UNKNOWN,
        }

    def _build_findings(
        self,
        timeline: list[dict],
        gate: dict,
        slo: dict,
        diagnostics: dict,
        alpha_budget: dict | None = None,
    ) -> list[dict]:
        findings: list[dict[str, Any]] = []
        failed_events = [
            event for event in timeline if event.get(PAYLOAD_KEY_STATUS) == TIMELINE_STATUS_FAILED
        ]
        if failed_events:
            findings.append(
                {
                    PAYLOAD_KEY_ID: FINDING_ID_TIMELINE_FAILURES,
                    PAYLOAD_KEY_SEVERITY: FINDING_SEVERITY_HIGH,
                    PAYLOAD_KEY_SUMMARY: (
                        f"{len(failed_events)} failed operational event(s) recorded"
                    ),
                    PAYLOAD_KEY_EVIDENCE: [event.get(PAYLOAD_KEY_ID) for event in failed_events],
                }
            )
        if gate.get(PAYLOAD_KEY_DECISION) == RELEASE_PIPELINE_GATE_DECISION_BLOCK:
            findings.append(
                {
                    PAYLOAD_KEY_ID: FINDING_ID_RELEASE_GATE_BLOCKED,
                    PAYLOAD_KEY_SEVERITY: FINDING_SEVERITY_CRITICAL,
                    PAYLOAD_KEY_SUMMARY: "Release gate is blocking deployment",
                    PAYLOAD_KEY_EVIDENCE: gate.get(PAYLOAD_KEY_SUMMARY, {}).get(
                        PAYLOAD_KEY_BLOCKING_SIGNALS, []
                    ),
                }
            )
        if slo.get(PAYLOAD_KEY_STATUS) == SLO_STATUS_BREACHING:
            findings.append(
                {
                    PAYLOAD_KEY_ID: FINDING_ID_SLO_BREACH,
                    PAYLOAD_KEY_SEVERITY: FINDING_SEVERITY_HIGH,
                    PAYLOAD_KEY_SUMMARY: "SLO report is breaching one or more objectives",
                    PAYLOAD_KEY_EVIDENCE: slo.get(PAYLOAD_KEY_FAILED_OBJECTIVES, []),
                }
            )
        alpha_budget = alpha_budget or {}
        if alpha_budget.get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0) > 0:
            findings.append(
                {
                    PAYLOAD_KEY_ID: FINDING_ID_ALPHA_BUDGET_EVIDENCE_MISSING,
                    PAYLOAD_KEY_SEVERITY: FINDING_SEVERITY_MEDIUM,
                    PAYLOAD_KEY_SUMMARY: (
                        "Alpha budget governance evidence is missing"
                        " from one or more release events"
                    ),
                    PAYLOAD_KEY_EVIDENCE: alpha_budget,
                }
            )
        if alpha_budget.get(PAYLOAD_KEY_WARNING_TOTAL, 0) > 0:
            findings.append(
                {
                    PAYLOAD_KEY_ID: FINDING_ID_ALPHA_BUDGET_WARNINGS_PRESENT,
                    PAYLOAD_KEY_SEVERITY: FINDING_SEVERITY_MEDIUM,
                    PAYLOAD_KEY_SUMMARY: (
                        f"Alpha budget governance recorded"
                        f" {alpha_budget.get(PAYLOAD_KEY_WARNING_TOTAL, 0)} warning(s)"
                    ),
                    PAYLOAD_KEY_EVIDENCE: alpha_budget,
                }
            )
        audit_summary = (
            diagnostics.get(PAYLOAD_KEY_AUDIT_SUMMARY, {})
            if diagnostics.get(PAYLOAD_KEY_AVAILABLE)
            else {}
        )
        error_count = (audit_summary.get(PAYLOAD_KEY_SEVERITY_COUNTS) or {}).get(
            PAYLOAD_KEY_ERROR, 0
        )
        if error_count:
            findings.append(
                {
                    PAYLOAD_KEY_ID: FINDING_ID_AUDIT_ERRORS,
                    PAYLOAD_KEY_SEVERITY: FINDING_SEVERITY_MEDIUM,
                    PAYLOAD_KEY_SUMMARY: f"Audit log contains {error_count} error event(s)",
                    PAYLOAD_KEY_EVIDENCE: audit_summary,
                }
            )
        if not findings:
            findings.append(
                {
                    PAYLOAD_KEY_ID: FINDING_ID_NO_MATERIAL_FINDINGS,
                    PAYLOAD_KEY_SEVERITY: FINDING_SEVERITY_LOW,
                    PAYLOAD_KEY_SUMMARY: "No material operational failures detected",
                    PAYLOAD_KEY_EVIDENCE: [],
                }
            )
        return findings

    def _build_actions(self, findings: list[dict]) -> list[dict]:
        actions = []
        for finding in findings:
            if finding[PAYLOAD_KEY_ID] == FINDING_ID_TIMELINE_FAILURES:
                actions.append(
                    {
                        PAYLOAD_KEY_OWNER: ACTION_OWNER_OPERATIONS,
                        PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_HIGH,
                        PAYLOAD_KEY_ACTION: ACTION_TEXT_TIMELINE_FAILURES,
                    }
                )
            elif finding[PAYLOAD_KEY_ID] == FINDING_ID_RELEASE_GATE_BLOCKED:
                actions.append(
                    {
                        PAYLOAD_KEY_OWNER: ACTION_OWNER_RELEASE,
                        PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_CRITICAL,
                        PAYLOAD_KEY_ACTION: ACTION_TEXT_RELEASE_GATE_BLOCKED,
                    }
                )
            elif finding[PAYLOAD_KEY_ID] == FINDING_ID_SLO_BREACH:
                actions.append(
                    {
                        PAYLOAD_KEY_OWNER: ACTION_OWNER_RELIABILITY,
                        PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_HIGH,
                        PAYLOAD_KEY_ACTION: ACTION_TEXT_SLO_BREACH,
                    }
                )
            elif finding[PAYLOAD_KEY_ID] == FINDING_ID_AUDIT_ERRORS:
                actions.append(
                    {
                        PAYLOAD_KEY_OWNER: ACTION_OWNER_ENGINEERING,
                        PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_MEDIUM,
                        PAYLOAD_KEY_ACTION: ACTION_TEXT_AUDIT_ERRORS,
                    }
                )
            elif finding[PAYLOAD_KEY_ID] == FINDING_ID_ALPHA_BUDGET_EVIDENCE_MISSING:
                actions.append(
                    {
                        PAYLOAD_KEY_OWNER: ACTION_OWNER_RELEASE,
                        PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_MEDIUM,
                        PAYLOAD_KEY_ACTION: ACTION_TEXT_ALPHA_BUDGET_EVIDENCE_MISSING,
                    }
                )
            elif finding[PAYLOAD_KEY_ID] == FINDING_ID_ALPHA_BUDGET_WARNINGS_PRESENT:
                actions.append(
                    {
                        PAYLOAD_KEY_OWNER: ACTION_OWNER_RISK,
                        PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_MEDIUM,
                        PAYLOAD_KEY_ACTION: ACTION_TEXT_ALPHA_BUDGET_WARNINGS_PRESENT,
                    }
                )
        if not actions:
            actions.append(
                {
                    PAYLOAD_KEY_OWNER: ACTION_OWNER_OPERATIONS,
                    PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_LOW,
                    PAYLOAD_KEY_ACTION: ACTION_TEXT_ARCHIVE_REPORT,
                }
            )
        return actions

    def _infer_status(self, findings: list[dict]) -> str:
        severities = {finding.get(PAYLOAD_KEY_SEVERITY) for finding in findings}
        if FINDING_SEVERITY_CRITICAL in severities:
            return POSTMORTEM_STATUS_CRITICAL
        if FINDING_SEVERITY_HIGH in severities:
            return POSTMORTEM_STATUS_ACTION_REQUIRED
        return POSTMORTEM_STATUS_CLOSED
