"""Automated operational runbooks.

RunbookEngine provides machine-readable preflight, doctor, and
postmortem workflows that compose readiness, health, diagnostics,
alerts, and optional state persistence.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from core.contracts.domain_keys import (
    COMPLIANCE_CHECK_ALPHA_BUDGET_EVIDENCE_REGISTERED,
    COMPLIANCE_CHECK_ALPHA_BUDGET_WARNINGS_CLEAR,
    HEALTH_STATUS_ALIVE,
    HEALTH_STATUS_DOWN,
    HEALTH_STATUS_READY,
    PAYLOAD_KEY_ACTION,
    PAYLOAD_KEY_ACTION_PLAN,
    PAYLOAD_KEY_ALERT_COUNT,
    PAYLOAD_KEY_ALERTS,
    PAYLOAD_KEY_ALPHA_BUDGET,
    PAYLOAD_KEY_AUDIT_SUMMARY,
    PAYLOAD_KEY_AVAILABLE,
    PAYLOAD_KEY_BRAIN_FROZEN,
    PAYLOAD_KEY_CAPABILITIES,
    PAYLOAD_KEY_CHECK_COUNT,
    PAYLOAD_KEY_CHECKS,
    PAYLOAD_KEY_CIRCUIT_OPEN,
    PAYLOAD_KEY_CONFIG,
    PAYLOAD_KEY_COUNTERS,
    PAYLOAD_KEY_DETAIL,
    PAYLOAD_KEY_DETAILS,
    PAYLOAD_KEY_DIAGNOSTICS,
    PAYLOAD_KEY_ERROR,
    PAYLOAD_KEY_ERROR_RATE,
    PAYLOAD_KEY_ERRORS,
    PAYLOAD_KEY_EVIDENCE_COUNT,
    PAYLOAD_KEY_EXECUTION_PLAN,
    PAYLOAD_KEY_EXECUTION_PLAN_ITEMS,
    PAYLOAD_KEY_FAILED_CHECK_COUNT,
    PAYLOAD_KEY_FAILED_CHECKS,
    PAYLOAD_KEY_FAILURES,
    PAYLOAD_KEY_FINISHED_AT,
    PAYLOAD_KEY_HEALTH,
    PAYLOAD_KEY_ITEMS,
    PAYLOAD_KEY_LABEL,
    PAYLOAD_KEY_LIVENESS,
    PAYLOAD_KEY_METRICS,
    PAYLOAD_KEY_MISSING,
    PAYLOAD_KEY_MISSING_EVIDENCE_COUNT,
    PAYLOAD_KEY_NAME,
    PAYLOAD_KEY_ORDER,
    PAYLOAD_KEY_OUTPUT_PATH,
    PAYLOAD_KEY_PASSED,
    PAYLOAD_KEY_PAYLOAD,
    PAYLOAD_KEY_POSITION_LIMIT_HIT,
    PAYLOAD_KEY_PRIORITY,
    PAYLOAD_KEY_READINESS,
    PAYLOAD_KEY_READINESS_GAPS,
    PAYLOAD_KEY_READINESS_SUMMARY,
    PAYLOAD_KEY_READY,
    PAYLOAD_KEY_REASON,
    PAYLOAD_KEY_RECOMMENDATIONS,
    PAYLOAD_KEY_RECORD_COUNT,
    PAYLOAD_KEY_RESULT,
    PAYLOAD_KEY_RUNBOOK,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_SERVICES,
    PAYLOAD_KEY_SEVERITY,
    PAYLOAD_KEY_SEVERITY_COUNTS,
    PAYLOAD_KEY_SNAPSHOT,
    PAYLOAD_KEY_STARTED_AT,
    PAYLOAD_KEY_STATE_SNAPSHOT,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_THROTTLE_RATE,
    PAYLOAD_KEY_VALID,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_WARNING_RELEASE_COUNT,
    PAYLOAD_KEY_WARNING_TOTAL,
    PAYLOAD_KEY_WARNINGS,
    READINESS_CAP_REPLAY_OPERATIONS,
    READINESS_SVC_INSPECTION_SERVICE,
    READINESS_SVC_OPERATIONS_SERVICE,
    READINESS_SVC_REPLAY_GATE,
    READINESS_SVC_REPLAY_SERVICE,
    RECOMMENDATION_PRIORITY_CRITICAL,
    RECOMMENDATION_PRIORITY_HIGH,
    RECOMMENDATION_PRIORITY_LOW,
    RECOMMENDATION_PRIORITY_MEDIUM,
    RUNBOOK_NAME_DOCTOR,
    RUNBOOK_NAME_POSTMORTEM,
    RUNBOOK_NAME_PREFLIGHT,
    RUNBOOK_RECOMMENDATION_ACTION_ATTACH_ALPHA_BUDGET_EVIDENCE,
    RUNBOOK_RECOMMENDATION_ACTION_INSPECT_ALERTS,
    RUNBOOK_RECOMMENDATION_ACTION_INSPECT_READINESS,
    RUNBOOK_RECOMMENDATION_ACTION_NO_ACTION,
    RUNBOOK_RECOMMENDATION_ACTION_RESTORE_CAPABILITY_GAPS,
    RUNBOOK_RECOMMENDATION_ACTION_RESTORE_REPLAY_SERVICES,
    RUNBOOK_RECOMMENDATION_ACTION_RESTORE_REQUIRED_SERVICES,
    RUNBOOK_RECOMMENDATION_ACTION_REVIEW_ALPHA_BUDGET_WARNINGS,
    RUNBOOK_RECOMMENDATION_ACTION_REVIEW_AUDIT_ERRORS,
    RUNBOOK_STATUS_FAILED,
    RUNBOOK_STATUS_NOT_CONFIGURED,
    RUNBOOK_STATUS_PASSED,
    RUNBOOK_STATUS_SAVED,
    RUNBOOK_STATUS_UNKNOWN,
    VALIDATION_MODE_DEEP,
)
from core.deployment.schema_versions import SCHEMA_RELEASE_READINESS, SCHEMA_RUNBOOK_RESULT
from core.deployment.validation_mode import resolve_validation_mode
from core.observability.metric_names import (
    CYCLES_BLOCKED,
    CYCLES_CIRCUIT_OPEN,
    CYCLES_ERRORS,
    CYCLES_THROTTLED,
)


class RunbookEngine:
    """Executes operational runbooks against a ServiceContainer."""

    def __init__(self, container, persistence=None):
        self._container = container
        self._persistence = persistence

    def run(self, name: str, **kwargs) -> dict:
        handlers = {
            RUNBOOK_NAME_PREFLIGHT: self.preflight,
            RUNBOOK_NAME_DOCTOR: self.doctor,
            RUNBOOK_NAME_POSTMORTEM: self.postmortem,
        }
        if name not in handlers:
            return {
                PAYLOAD_KEY_RUNBOOK: name,
                PAYLOAD_KEY_STATUS: RUNBOOK_STATUS_UNKNOWN,
                PAYLOAD_KEY_ERROR: f"Unknown runbook: {name}",
                PAYLOAD_KEY_AVAILABLE: sorted(handlers),
            }
        return handlers[name](**kwargs)  # type: ignore[operator]

    def preflight(self, validation_mode: str | None = None) -> dict:
        """Release gate: readiness + config + health + capabilities."""
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        started_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
        readiness = self._container.release_readiness.build_report(validation_mode=validation_mode)
        config = self._validate_config()
        health = self._build_health()
        alpha_budget = self._alpha_budget_status()
        readiness_gaps = self._readiness_gaps(readiness)
        checks = [
            self._check(
                "release_ready",
                readiness.get(PAYLOAD_KEY_READY) is True,
                {
                    PAYLOAD_KEY_FAILURES: readiness.get(PAYLOAD_KEY_SUMMARY, {}).get(
                        PAYLOAD_KEY_FAILED_CHECKS, []
                    )
                },
            ),
            self._check(
                "config_valid",
                config.get(PAYLOAD_KEY_VALID) is True,
                {PAYLOAD_KEY_ERRORS: config.get(PAYLOAD_KEY_ERRORS, [])},
            ),
            self._check(
                "readiness_ready",
                health.get(PAYLOAD_KEY_READINESS, {}).get(PAYLOAD_KEY_STATUS)
                == HEALTH_STATUS_READY,
                {PAYLOAD_KEY_STATUS: health.get(PAYLOAD_KEY_READINESS, {}).get(PAYLOAD_KEY_STATUS)},
            ),
            self._check(
                "liveness_alive",
                health.get(PAYLOAD_KEY_LIVENESS, {}).get(PAYLOAD_KEY_STATUS) == HEALTH_STATUS_ALIVE,
                {PAYLOAD_KEY_STATUS: health.get(PAYLOAD_KEY_LIVENESS, {}).get(PAYLOAD_KEY_STATUS)},
            ),
        ]
        if validation_mode == VALIDATION_MODE_DEEP:
            checks.extend(
                [
                    self._check(
                        COMPLIANCE_CHECK_ALPHA_BUDGET_EVIDENCE_REGISTERED,
                        alpha_budget.get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0) == 0,
                        alpha_budget,
                    ),
                    self._check(
                        COMPLIANCE_CHECK_ALPHA_BUDGET_WARNINGS_CLEAR,
                        alpha_budget.get(PAYLOAD_KEY_WARNING_TOTAL, 0) == 0,
                        alpha_budget,
                    ),
                ]
            )
        return self._result(
            name=RUNBOOK_NAME_PREFLIGHT,
            started_at=started_at,
            checks=checks,
            validation_mode=validation_mode,
            payload={
                PAYLOAD_KEY_READINESS_SUMMARY: readiness.get(PAYLOAD_KEY_SUMMARY),
                PAYLOAD_KEY_READINESS_GAPS: readiness_gaps,
                PAYLOAD_KEY_CONFIG: config,
                PAYLOAD_KEY_HEALTH: health,
                PAYLOAD_KEY_ALPHA_BUDGET: alpha_budget,
            },
        )

    def doctor(self, validation_mode: str | None = None) -> dict:
        """Runtime diagnosis: health + diagnostics + alerts + recommendations."""
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        started_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
        readiness = self._container.release_readiness.build_report(validation_mode=validation_mode)
        health = self._build_health()
        diagnostics = self._build_diagnostics()
        alert_eval = self._evaluate_alerts(diagnostics)
        alpha_budget = self._alpha_budget_status()
        readiness_gaps = self._readiness_gaps(readiness)
        recommendations = self._build_recommendations(
            health,
            diagnostics,
            alert_eval,
            alpha_budget,
            validation_mode=validation_mode,
            readiness_gaps=readiness_gaps,
        )
        checks = [
            self._check(
                "readiness_not_down",
                health.get(PAYLOAD_KEY_READINESS, {}).get(PAYLOAD_KEY_STATUS) != HEALTH_STATUS_DOWN,
                {PAYLOAD_KEY_STATUS: health.get(PAYLOAD_KEY_READINESS, {}).get(PAYLOAD_KEY_STATUS)},
            ),
            self._check(
                "liveness_alive",
                health.get(PAYLOAD_KEY_LIVENESS, {}).get(PAYLOAD_KEY_STATUS) == HEALTH_STATUS_ALIVE,
                {PAYLOAD_KEY_STATUS: health.get(PAYLOAD_KEY_LIVENESS, {}).get(PAYLOAD_KEY_STATUS)},
            ),
            self._check(
                "no_critical_alerts",
                not any(
                    a.get(PAYLOAD_KEY_SEVERITY) == RECOMMENDATION_PRIORITY_CRITICAL
                    for a in alert_eval
                ),
                {PAYLOAD_KEY_ALERT_COUNT: len(alert_eval)},
            ),
        ]
        if validation_mode == VALIDATION_MODE_DEEP:
            checks.extend(
                [
                    self._check(
                        COMPLIANCE_CHECK_ALPHA_BUDGET_EVIDENCE_REGISTERED,
                        alpha_budget.get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0) == 0,
                        alpha_budget,
                    ),
                    self._check(
                        COMPLIANCE_CHECK_ALPHA_BUDGET_WARNINGS_CLEAR,
                        alpha_budget.get(PAYLOAD_KEY_WARNING_TOTAL, 0) == 0,
                        alpha_budget,
                    ),
                ]
            )
        return self._result(
            name=RUNBOOK_NAME_DOCTOR,
            started_at=started_at,
            checks=checks,
            validation_mode=validation_mode,
            payload={
                PAYLOAD_KEY_HEALTH: health,
                PAYLOAD_KEY_READINESS_SUMMARY: readiness.get(PAYLOAD_KEY_SUMMARY),
                PAYLOAD_KEY_READINESS_GAPS: readiness_gaps,
                PAYLOAD_KEY_DIAGNOSTICS: diagnostics,
                PAYLOAD_KEY_ALERTS: alert_eval,
                PAYLOAD_KEY_RECOMMENDATIONS: recommendations,
                PAYLOAD_KEY_ALPHA_BUDGET: alpha_budget,
            },
        )

    def postmortem(
        self,
        label: str | None = None,
        output: str | None = None,
        validation_mode: str | None = None,
    ) -> dict:
        """Evidence capture: diagnostics + readiness + optional state snapshot."""
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        started_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
        label = (
            label or f"postmortem_{datetime.now(UTC).replace(tzinfo=None).strftime('%Y%m%d%H%M%S')}"
        )
        readiness = self._container.release_readiness.build_report(validation_mode=validation_mode)
        diagnostics = self._build_diagnostics()
        state_snapshot = self._save_state(label)
        payload = {
            PAYLOAD_KEY_LABEL: label,
            PAYLOAD_KEY_READINESS: readiness,
            PAYLOAD_KEY_DIAGNOSTICS: diagnostics,
            PAYLOAD_KEY_STATE_SNAPSHOT: state_snapshot,
        }
        checks = [
            self._check(
                "diagnostics_available",
                diagnostics.get(PAYLOAD_KEY_AVAILABLE, False) is True,
                {PAYLOAD_KEY_AVAILABLE: diagnostics.get(PAYLOAD_KEY_AVAILABLE, False)},
            ),
            self._check(
                "readiness_report_generated",
                readiness.get(PAYLOAD_KEY_SCHEMA_VERSION) == SCHEMA_RELEASE_READINESS,
                {PAYLOAD_KEY_SCHEMA_VERSION: readiness.get(PAYLOAD_KEY_SCHEMA_VERSION)},
            ),
            self._check(
                "state_saved_or_not_configured",
                state_snapshot.get(PAYLOAD_KEY_STATUS)
                in {RUNBOOK_STATUS_SAVED, RUNBOOK_STATUS_NOT_CONFIGURED},
                state_snapshot,
            ),
        ]
        result = self._result(
            RUNBOOK_NAME_POSTMORTEM,
            started_at,
            checks,
            payload,
            validation_mode=validation_mode,
        )
        if output:
            result[PAYLOAD_KEY_OUTPUT_PATH] = self.save_result(result, output)
        return result

    def save_result(self, result: dict, path: str) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        return str(target)

    def _validate_config(self) -> dict:
        try:
            from core.deployment.operational_support import ConfigValidator

            return ConfigValidator().validate(self._container.config)
        except Exception as exc:  # BLE001:REVIEWED
            return {
                PAYLOAD_KEY_VALID: False,
                PAYLOAD_KEY_ERRORS: [str(exc)],
                PAYLOAD_KEY_WARNINGS: [],
            }

    def _build_health(self) -> dict:
        from core.deployment.health_check import HealthCheckService

        return HealthCheckService.safe_get_health(self._container)

    def _build_diagnostics(self) -> dict:
        from core.observability.diagnostics_dashboard import DiagnosticsDashboard

        return DiagnosticsDashboard.safe_get_snapshot(self._container)

    def _evaluate_alerts(self, diagnostics: dict) -> list[dict]:
        alert_service = getattr(self._container, "alert_service", None)
        if alert_service is None:
            return []
        snapshot = (
            diagnostics.get(PAYLOAD_KEY_SNAPSHOT, {})
            if diagnostics.get(PAYLOAD_KEY_AVAILABLE)
            else {}
        )
        metrics = snapshot.get(PAYLOAD_KEY_METRICS) or {}
        counters = metrics.get(PAYLOAD_KEY_COUNTERS) or {}
        context = {
            PAYLOAD_KEY_ERROR_RATE: counters.get(CYCLES_ERRORS, 0),
            PAYLOAD_KEY_CIRCUIT_OPEN: counters.get(CYCLES_CIRCUIT_OPEN, 0) > 0,
            PAYLOAD_KEY_THROTTLE_RATE: counters.get(CYCLES_THROTTLED, 0),
            PAYLOAD_KEY_BRAIN_FROZEN: 0,
            PAYLOAD_KEY_POSITION_LIMIT_HIT: counters.get(CYCLES_BLOCKED, 0),
        }
        fired = alert_service.evaluate(context)
        return [a.to_dict() if hasattr(a, "to_dict") else dict(a) for a in fired]

    def _build_recommendations(
        self,
        health: dict,
        diagnostics: dict,
        alerts: list[dict],
        alpha_budget: dict | None = None,
        *,
        validation_mode: str | None = None,
        readiness_gaps: dict | None = None,
    ) -> list[dict]:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        recs = []
        readiness_status = health.get(PAYLOAD_KEY_READINESS, {}).get(PAYLOAD_KEY_STATUS)
        readiness_reasons = []
        if readiness_status != HEALTH_STATUS_READY:
            readiness_reasons.append(f"readiness={readiness_status}")
        readiness_gaps = readiness_gaps or {
            PAYLOAD_KEY_MISSING: [],
            PAYLOAD_KEY_CAPABILITIES: [],
            PAYLOAD_KEY_RECOMMENDATIONS: [],
            PAYLOAD_KEY_ACTION_PLAN: {},
        }
        missing_services = readiness_gaps.get(PAYLOAD_KEY_MISSING, [])
        capability_gaps = readiness_gaps.get(PAYLOAD_KEY_CAPABILITIES, [])
        replay_missing = [
            service_name
            for service_name in [
                READINESS_SVC_INSPECTION_SERVICE,
                READINESS_SVC_REPLAY_SERVICE,
                READINESS_SVC_REPLAY_GATE,
                READINESS_SVC_OPERATIONS_SERVICE,
            ]
            if service_name in missing_services
        ]
        if replay_missing:
            readiness_reasons.append(f"missing replay services: {', '.join(replay_missing)}")
        if readiness_reasons:
            recs.append(
                {
                    PAYLOAD_KEY_ACTION: RUNBOOK_RECOMMENDATION_ACTION_INSPECT_READINESS,
                    PAYLOAD_KEY_REASON: "; ".join(readiness_reasons),
                    PAYLOAD_KEY_DETAILS: {
                        PAYLOAD_KEY_MISSING: list(missing_services),
                        PAYLOAD_KEY_CAPABILITIES: list(capability_gaps),
                        PAYLOAD_KEY_RECOMMENDATIONS: list(
                            readiness_gaps.get(PAYLOAD_KEY_RECOMMENDATIONS, [])
                        ),
                        PAYLOAD_KEY_ACTION_PLAN: dict(
                            readiness_gaps.get(PAYLOAD_KEY_ACTION_PLAN, {})
                        ),
                    },
                }
            )
        if alerts:
            recs.append(
                {
                    PAYLOAD_KEY_ACTION: RUNBOOK_RECOMMENDATION_ACTION_INSPECT_ALERTS,
                    PAYLOAD_KEY_REASON: f"{len(alerts)} alert(s) fired",
                }
            )
        snapshot = (
            diagnostics.get(PAYLOAD_KEY_SNAPSHOT, {})
            if diagnostics.get(PAYLOAD_KEY_AVAILABLE)
            else {}
        )
        audit = snapshot.get(PAYLOAD_KEY_AUDIT_SUMMARY) or {}
        if audit.get(PAYLOAD_KEY_SEVERITY_COUNTS, {}).get(PAYLOAD_KEY_ERROR, 0) > 0:
            recs.append(
                {
                    PAYLOAD_KEY_ACTION: RUNBOOK_RECOMMENDATION_ACTION_REVIEW_AUDIT_ERRORS,
                    PAYLOAD_KEY_REASON: "audit errors present",
                }
            )
        alpha_budget = alpha_budget or {}
        if validation_mode == VALIDATION_MODE_DEEP:
            if alpha_budget.get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0) > 0:
                recs.append(
                    {
                        PAYLOAD_KEY_ACTION: (
                            RUNBOOK_RECOMMENDATION_ACTION_ATTACH_ALPHA_BUDGET_EVIDENCE
                        ),
                        PAYLOAD_KEY_REASON: "alpha budget evidence missing",
                    }
                )
            if alpha_budget.get(PAYLOAD_KEY_WARNING_TOTAL, 0) > 0:
                recs.append(
                    {
                        PAYLOAD_KEY_ACTION: (
                            RUNBOOK_RECOMMENDATION_ACTION_REVIEW_ALPHA_BUDGET_WARNINGS
                        ),
                        PAYLOAD_KEY_REASON: "alpha budget warnings present",
                    }
                )
        if not recs:
            recs.append(
                {
                    PAYLOAD_KEY_ACTION: RUNBOOK_RECOMMENDATION_ACTION_NO_ACTION,
                    PAYLOAD_KEY_REASON: "system appears healthy",
                }
            )
        return recs

    def _readiness_gaps(self, readiness: dict | None) -> dict:
        readiness = readiness or {}
        missing_services = (readiness.get(PAYLOAD_KEY_SERVICES) or {}).get(PAYLOAD_KEY_MISSING, [])
        capability_items = (readiness.get(PAYLOAD_KEY_CAPABILITIES) or {}).get(
            PAYLOAD_KEY_ITEMS, {}
        )
        capability_gaps = [
            name for name, available in capability_items.items() if available is False
        ]
        recommended_actions = []
        if missing_services or capability_gaps:
            recommended_actions.append(RUNBOOK_RECOMMENDATION_ACTION_INSPECT_READINESS)
        if missing_services:
            recommended_actions.append(RUNBOOK_RECOMMENDATION_ACTION_RESTORE_REQUIRED_SERVICES)
        if capability_gaps:
            recommended_actions.append(RUNBOOK_RECOMMENDATION_ACTION_RESTORE_CAPABILITY_GAPS)
        if (
            any(
                service_name in missing_services
                for service_name in [
                    READINESS_SVC_INSPECTION_SERVICE,
                    READINESS_SVC_REPLAY_SERVICE,
                    READINESS_SVC_REPLAY_GATE,
                    READINESS_SVC_OPERATIONS_SERVICE,
                ]
            )
            or READINESS_CAP_REPLAY_OPERATIONS in capability_gaps
        ):
            recommended_actions.append(RUNBOOK_RECOMMENDATION_ACTION_RESTORE_REPLAY_SERVICES)
        recommended_actions = sorted(set(recommended_actions))
        replay_services = [
            service_name
            for service_name in [
                READINESS_SVC_INSPECTION_SERVICE,
                READINESS_SVC_REPLAY_SERVICE,
                READINESS_SVC_REPLAY_GATE,
                READINESS_SVC_OPERATIONS_SERVICE,
            ]
            if service_name in missing_services
        ]
        replay_capabilities = [
            capability_name
            for capability_name in [READINESS_CAP_REPLAY_OPERATIONS]
            if capability_name in capability_gaps
        ]
        action_plan: dict[str, dict] = {}
        if RUNBOOK_RECOMMENDATION_ACTION_INSPECT_READINESS in recommended_actions:
            action_plan[RUNBOOK_RECOMMENDATION_ACTION_INSPECT_READINESS] = {
                PAYLOAD_KEY_MISSING: list(missing_services),
                PAYLOAD_KEY_CAPABILITIES: list(capability_gaps),
                PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_LOW,
                PAYLOAD_KEY_REASON: "readiness gaps detected",
            }
        if RUNBOOK_RECOMMENDATION_ACTION_RESTORE_REQUIRED_SERVICES in recommended_actions:
            action_plan[RUNBOOK_RECOMMENDATION_ACTION_RESTORE_REQUIRED_SERVICES] = {
                PAYLOAD_KEY_MISSING: list(missing_services),
                PAYLOAD_KEY_CAPABILITIES: [],
                PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_MEDIUM,
                PAYLOAD_KEY_REASON: "required services missing",
            }
        if RUNBOOK_RECOMMENDATION_ACTION_RESTORE_CAPABILITY_GAPS in recommended_actions:
            action_plan[RUNBOOK_RECOMMENDATION_ACTION_RESTORE_CAPABILITY_GAPS] = {
                PAYLOAD_KEY_MISSING: [],
                PAYLOAD_KEY_CAPABILITIES: list(capability_gaps),
                PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_MEDIUM,
                PAYLOAD_KEY_REASON: "capability gaps detected",
            }
        if RUNBOOK_RECOMMENDATION_ACTION_RESTORE_REPLAY_SERVICES in recommended_actions:
            action_plan[RUNBOOK_RECOMMENDATION_ACTION_RESTORE_REPLAY_SERVICES] = {
                PAYLOAD_KEY_MISSING: replay_services,
                PAYLOAD_KEY_CAPABILITIES: replay_capabilities,
                PAYLOAD_KEY_PRIORITY: RECOMMENDATION_PRIORITY_HIGH,
                PAYLOAD_KEY_REASON: "replay services/capability gaps detected",
            }
        priority_rank = {
            RECOMMENDATION_PRIORITY_CRITICAL: 0,
            RECOMMENDATION_PRIORITY_HIGH: 1,
            RECOMMENDATION_PRIORITY_MEDIUM: 2,
            RECOMMENDATION_PRIORITY_LOW: 3,
        }
        execution_plan = sorted(
            action_plan.keys(),
            key=lambda action: (
                priority_rank.get(str(action_plan[action].get(PAYLOAD_KEY_PRIORITY)), 99),
                action,
            ),
        )
        for idx, action in enumerate(execution_plan, start=1):
            action_plan[action][PAYLOAD_KEY_ORDER] = idx
        execution_plan_items = [
            {
                PAYLOAD_KEY_ACTION: action,
                PAYLOAD_KEY_PRIORITY: action_plan[action].get(PAYLOAD_KEY_PRIORITY),
                PAYLOAD_KEY_ORDER: action_plan[action].get(PAYLOAD_KEY_ORDER),
                PAYLOAD_KEY_REASON: action_plan[action].get(PAYLOAD_KEY_REASON),
                PAYLOAD_KEY_MISSING: list(action_plan[action].get(PAYLOAD_KEY_MISSING, [])),
                PAYLOAD_KEY_CAPABILITIES: list(
                    action_plan[action].get(PAYLOAD_KEY_CAPABILITIES, [])
                ),
            }
            for action in execution_plan
        ]
        return {
            PAYLOAD_KEY_MISSING: missing_services,
            PAYLOAD_KEY_CAPABILITIES: capability_gaps,
            PAYLOAD_KEY_RECOMMENDATIONS: recommended_actions,
            PAYLOAD_KEY_ACTION_PLAN: action_plan,
            PAYLOAD_KEY_EXECUTION_PLAN: execution_plan,
            PAYLOAD_KEY_EXECUTION_PLAN_ITEMS: execution_plan_items,
        }

    def _alpha_budget_status(self) -> dict:
        registry = getattr(self._container, "release_registry", None)
        if registry is None:
            return {
                PAYLOAD_KEY_AVAILABLE: False,
                PAYLOAD_KEY_RECORD_COUNT: 0,
                PAYLOAD_KEY_EVIDENCE_COUNT: 0,
                PAYLOAD_KEY_MISSING_EVIDENCE_COUNT: 0,
                PAYLOAD_KEY_WARNING_TOTAL: 0,
                PAYLOAD_KEY_WARNING_RELEASE_COUNT: 0,
            }
        summary = registry.summarize()
        alpha_budget = summary.get(PAYLOAD_KEY_ALPHA_BUDGET, {})
        return {
            PAYLOAD_KEY_AVAILABLE: True,
            PAYLOAD_KEY_RECORD_COUNT: summary.get(PAYLOAD_KEY_RECORD_COUNT, 0),
            PAYLOAD_KEY_EVIDENCE_COUNT: alpha_budget.get(PAYLOAD_KEY_EVIDENCE_COUNT, 0),
            PAYLOAD_KEY_MISSING_EVIDENCE_COUNT: alpha_budget.get(
                PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0
            ),
            PAYLOAD_KEY_WARNING_TOTAL: alpha_budget.get(PAYLOAD_KEY_WARNING_TOTAL, 0),
            PAYLOAD_KEY_WARNING_RELEASE_COUNT: alpha_budget.get(
                PAYLOAD_KEY_WARNING_RELEASE_COUNT, 0
            ),
        }

    def _save_state(self, label: str) -> dict:
        if self._persistence is None:
            return {PAYLOAD_KEY_STATUS: RUNBOOK_STATUS_NOT_CONFIGURED}
        try:
            result = self._persistence.save_all(self._container, label=label)
            return {
                PAYLOAD_KEY_STATUS: RUNBOOK_STATUS_SAVED,
                PAYLOAD_KEY_LABEL: label,
                PAYLOAD_KEY_RESULT: result,
            }
        except Exception as exc:  # BLE001:REVIEWED
            return {
                PAYLOAD_KEY_STATUS: RUNBOOK_STATUS_FAILED,
                PAYLOAD_KEY_LABEL: label,
                PAYLOAD_KEY_ERROR: str(exc),
            }

    def _check(self, name: str, passed: bool, detail: dict | None = None) -> dict:
        return {
            PAYLOAD_KEY_NAME: name,
            PAYLOAD_KEY_PASSED: bool(passed),
            PAYLOAD_KEY_DETAIL: detail or {},
        }

    def _result(
        self,
        name: str,
        started_at: str,
        checks: list[dict],
        payload: dict,
        *,
        validation_mode: str | None = None,
    ) -> dict:
        failed = [c[PAYLOAD_KEY_NAME] for c in checks if not c[PAYLOAD_KEY_PASSED]]
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_RUNBOOK_RESULT,
            PAYLOAD_KEY_RUNBOOK: name,
            PAYLOAD_KEY_STARTED_AT: started_at,
            PAYLOAD_KEY_FINISHED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
            PAYLOAD_KEY_STATUS: RUNBOOK_STATUS_PASSED if not failed else RUNBOOK_STATUS_FAILED,
            PAYLOAD_KEY_PASSED: not failed,
            PAYLOAD_KEY_SUMMARY: {
                PAYLOAD_KEY_CHECK_COUNT: len(checks),
                PAYLOAD_KEY_FAILED_CHECK_COUNT: len(failed),
                PAYLOAD_KEY_FAILED_CHECKS: failed,
            },
            PAYLOAD_KEY_CHECKS: checks,
            PAYLOAD_KEY_PAYLOAD: payload,
        }
