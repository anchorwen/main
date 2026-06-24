"""Release readiness report generation.

Builds a machine-readable deployment evidence bundle from the current
container: environment config, health, services, diagnostics, and
operational capabilities.
"""

import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

from core.contracts.domain_keys import (
    COMPLIANCE_CHECK_ALPHA_BUDGET_EVIDENCE_REGISTERED,
    COMPLIANCE_CHECK_ALPHA_BUDGET_WARNINGS_CLEAR,
    HEALTH_STATUS_ALIVE,
    HEALTH_STATUS_READY,
    PAYLOAD_KEY_ALERT_COUNT,
    PAYLOAD_KEY_ALERTS,
    PAYLOAD_KEY_ALPHA_BUDGET,
    PAYLOAD_KEY_ALPHA_BUDGET_EVIDENCE_COUNT,
    PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE,
    PAYLOAD_KEY_ALPHA_BUDGET_MISSING_EVIDENCE_COUNT,
    PAYLOAD_KEY_ALPHA_BUDGET_TIMELINE_EVENT_COUNT,
    PAYLOAD_KEY_ALPHA_BUDGET_WARNING_TOTAL,
    PAYLOAD_KEY_AUDIT_ENTRY_COUNT,
    PAYLOAD_KEY_AUDIT_SUMMARY,
    PAYLOAD_KEY_AVAILABLE,
    PAYLOAD_KEY_BASE_DIR,
    PAYLOAD_KEY_BRAIN_COUNT,
    PAYLOAD_KEY_BRAIN_HEALTH,
    PAYLOAD_KEY_CAPABILITIES,
    PAYLOAD_KEY_CHECK_COUNT,
    PAYLOAD_KEY_CHECKS,
    PAYLOAD_KEY_COUNTERS,
    PAYLOAD_KEY_DETAIL,
    PAYLOAD_KEY_DETAILS,
    PAYLOAD_KEY_DIAGNOSTICS,
    PAYLOAD_KEY_ENABLE_AUDIT_LOG,
    PAYLOAD_KEY_ENABLE_FEEDBACK_LOOP,
    PAYLOAD_KEY_ENABLE_IDEMPOTENCY,
    PAYLOAD_KEY_ENABLE_METRICS,
    PAYLOAD_KEY_ENGINE_CONFIG_POLL_INTERVAL_SECONDS,
    PAYLOAD_KEY_ENTRY_COUNT,
    PAYLOAD_KEY_ENVIRONMENT,
    PAYLOAD_KEY_EVIDENCE_COUNT,
    PAYLOAD_KEY_FAILED_CHECK_COUNT,
    PAYLOAD_KEY_FAILED_CHECKS,
    PAYLOAD_KEY_GENERATED_AT,
    PAYLOAD_KEY_HEALTH,
    PAYLOAD_KEY_IMPLEMENTATION,
    PAYLOAD_KEY_ITEMS,
    PAYLOAD_KEY_LIVENESS,
    PAYLOAD_KEY_MAX_DRAWDOWN_PCT,
    PAYLOAD_KEY_MAX_NOTIONAL_EXPOSURE,
    PAYLOAD_KEY_MAX_OPEN_POSITIONS,
    PAYLOAD_KEY_METRIC_COUNTER_COUNT,
    PAYLOAD_KEY_METRICS,
    PAYLOAD_KEY_MISSING,
    PAYLOAD_KEY_MISSING_COUNT,
    PAYLOAD_KEY_MISSING_EVIDENCE_COUNT,
    PAYLOAD_KEY_NAME,
    PAYLOAD_KEY_OPS_MATURITY_MIN_SCORE,
    PAYLOAD_KEY_PASSED,
    PAYLOAD_KEY_PLATFORM,
    PAYLOAD_KEY_PRESENT,
    PAYLOAD_KEY_PRESENT_COUNT,
    PAYLOAD_KEY_PYTHON_VERSION,
    PAYLOAD_KEY_READINESS,
    PAYLOAD_KEY_READINESS_CAPABILITIES_AVAILABLE,
    PAYLOAD_KEY_READINESS_CAPABILITIES_TOTAL,
    PAYLOAD_KEY_READY,
    PAYLOAD_KEY_RECORD_COUNT,
    PAYLOAD_KEY_REQUIRED_COUNT,
    PAYLOAD_KEY_RUNTIME,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_SERVICES,
    PAYLOAD_KEY_SERVICES_PRESENT,
    PAYLOAD_KEY_SERVICES_REQUIRED,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_SYSTEM_MODE,
    PAYLOAD_KEY_TIMELINE_EVENT_COUNT,
    PAYLOAD_KEY_TIMELINE_MISSING_EVIDENCE_COUNT,
    PAYLOAD_KEY_TIMELINE_WARNING_TOTAL,
    PAYLOAD_KEY_TOTAL,
    PAYLOAD_KEY_TYPE,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_VERSION,
    PAYLOAD_KEY_WARNING_RELEASE_COUNT,
    PAYLOAD_KEY_WARNING_TOTAL,
    READINESS_CHECK_BASE_DIR_CONFIGURED,
    READINESS_CHECK_LIVENESS_ALIVE,
    READINESS_CHECK_READINESS_READY,
    READINESS_CHECK_REQUIRED_SERVICES_PRESENT,
    READINESS_SVC_ALERT_SERVICE,
    READINESS_SVC_AUDIT_LOG,
    READINESS_SVC_BRAIN_REGISTRY,
    READINESS_SVC_COMMUNICATION_READER,
    READINESS_SVC_COMMUNICATION_WRITER,
    READINESS_SVC_CONFIG_HOT_RELOAD,
    READINESS_SVC_DECISION_COMPILER,
    READINESS_SVC_DECISION_RECORD_WRITER,
    READINESS_SVC_DIAGNOSTICS,
    READINESS_SVC_DISPATCHER,
    READINESS_SVC_EXECUTION_EVENT_READER,
    READINESS_SVC_EXECUTION_EVENT_WRITER,
    READINESS_SVC_EXECUTION_MANAGER,
    READINESS_SVC_FEATURE_SERVICE,
    READINESS_SVC_GOVERNANCE_RULE_ENGINE,
    READINESS_SVC_GOVERNANCE_SERVICE,
    READINESS_SVC_HEALTH_CHECK,
    READINESS_SVC_INSPECTION_SERVICE,
    READINESS_SVC_LEDGER_STORE,
    READINESS_SVC_MARKET_CONTEXT,
    READINESS_SVC_MESSAGE_BUILDER,
    READINESS_SVC_METRICS,
    READINESS_SVC_OPERATIONS_SERVICE,
    READINESS_SVC_PARLIAMENT_SERVICE,
    READINESS_SVC_POSITION_TRACKER,
    READINESS_SVC_RECONCILIATION_SERVICE,
    READINESS_SVC_REPLAY_GATE,
    READINESS_SVC_REPLAY_SERVICE,
    READINESS_SVC_RISK_SERVICE,
    READINESS_SVC_RUNTIME_LOOP,
    READINESS_SVC_VENUE_ROUTER,
    TIMELINE_EVENT_ALPHA_BUDGET_GOVERNANCE,
    VALIDATION_MODE_DEEP,
    VALIDATION_MODE_FAST,
)
from core.deployment.atomic_file_writer import atomic_write_json
from core.deployment.capability_registry import (
    CapabilityRegistry,
    build_default_release_capability_registry,
)
from core.deployment.governance_summary import extract_governance_summary
from core.deployment.schema_versions import SCHEMA_RELEASE_READINESS
from core.deployment.validation_mode import resolve_validation_mode


class ReleaseReadinessService:
    """Aggregates runtime evidence for release decisions."""

    REQUIRED_SERVICES = [
        READINESS_SVC_LEDGER_STORE,
        READINESS_SVC_COMMUNICATION_WRITER,
        READINESS_SVC_COMMUNICATION_READER,
        READINESS_SVC_EXECUTION_EVENT_WRITER,
        READINESS_SVC_EXECUTION_EVENT_READER,
        READINESS_SVC_RECONCILIATION_SERVICE,
        READINESS_SVC_DISPATCHER,
        READINESS_SVC_MESSAGE_BUILDER,
        READINESS_SVC_INSPECTION_SERVICE,
        READINESS_SVC_REPLAY_SERVICE,
        READINESS_SVC_REPLAY_GATE,
        READINESS_SVC_OPERATIONS_SERVICE,
        READINESS_SVC_RISK_SERVICE,
        READINESS_SVC_METRICS,
        READINESS_SVC_AUDIT_LOG,
        READINESS_SVC_DIAGNOSTICS,
        READINESS_SVC_GOVERNANCE_SERVICE,
        READINESS_SVC_GOVERNANCE_RULE_ENGINE,
        READINESS_SVC_PARLIAMENT_SERVICE,
        READINESS_SVC_POSITION_TRACKER,
        READINESS_SVC_MARKET_CONTEXT,
        READINESS_SVC_EXECUTION_MANAGER,
        READINESS_SVC_HEALTH_CHECK,
        READINESS_SVC_FEATURE_SERVICE,
        READINESS_SVC_BRAIN_REGISTRY,
        READINESS_SVC_DECISION_COMPILER,
        READINESS_SVC_DECISION_RECORD_WRITER,
        READINESS_SVC_RUNTIME_LOOP,
        READINESS_SVC_VENUE_ROUTER,
        READINESS_SVC_ALERT_SERVICE,
        READINESS_SVC_CONFIG_HOT_RELOAD,
    ]

    def __init__(
        self,
        container,
        version: str = "0.1.0",
        capability_registry: CapabilityRegistry | None = None,
    ):
        self._container = container
        self._version = version
        self._capability_registry = (
            capability_registry or build_default_release_capability_registry()
        )

    def build_report(self, *, validation_mode: str | None = None) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        self._ensure_runtime_loop()
        health = self._build_health()
        services = self._build_services()
        config = self._build_config()
        diagnostics = self._build_diagnostics()
        capabilities = self._build_capabilities()
        alpha_budget = self._build_alpha_budget_governance()
        checks = self._build_checks(
            health,
            services,
            config,
            capabilities,
            alpha_budget,
            validation_mode=validation_mode,
        )
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_RELEASE_READINESS,
            PAYLOAD_KEY_GENERATED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_VERSION: self._version,
            PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
            PAYLOAD_KEY_RUNTIME: self._build_runtime(),
            PAYLOAD_KEY_ENVIRONMENT: config,
            PAYLOAD_KEY_HEALTH: health,
            PAYLOAD_KEY_SERVICES: services,
            PAYLOAD_KEY_CAPABILITIES: capabilities,
            PAYLOAD_KEY_DIAGNOSTICS: diagnostics,
            PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE: alpha_budget,
            PAYLOAD_KEY_CHECKS: checks,
            PAYLOAD_KEY_READY: all(item[PAYLOAD_KEY_PASSED] for item in checks),
            PAYLOAD_KEY_SUMMARY: self._build_summary(checks, services, capabilities, alpha_budget),
        }

    def save_report(self, path: str, *, validation_mode: str | None = None) -> str:
        report = self.build_report(validation_mode=validation_mode)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, report)
        return str(target)

    def _ensure_runtime_loop(self) -> None:
        if getattr(self._container, "runtime_loop", None) is None:
            build_runtime_loop = getattr(self._container, "build_runtime_loop", None)
            if build_runtime_loop is not None:
                build_runtime_loop()

    def _build_runtime(self) -> dict:
        return {
            PAYLOAD_KEY_PYTHON_VERSION: sys.version.split()[0],
            PAYLOAD_KEY_PLATFORM: platform.platform(),
            PAYLOAD_KEY_IMPLEMENTATION: platform.python_implementation(),
        }

    def _build_config(self) -> dict:
        cfg = self._container.config
        keys = [
            PAYLOAD_KEY_ENVIRONMENT,
            PAYLOAD_KEY_BASE_DIR,
            PAYLOAD_KEY_SYSTEM_MODE,
            PAYLOAD_KEY_ENABLE_METRICS,
            PAYLOAD_KEY_ENABLE_AUDIT_LOG,
            PAYLOAD_KEY_ENABLE_FEEDBACK_LOOP,
            PAYLOAD_KEY_ENABLE_IDEMPOTENCY,
            PAYLOAD_KEY_MAX_OPEN_POSITIONS,
            PAYLOAD_KEY_MAX_NOTIONAL_EXPOSURE,
            PAYLOAD_KEY_MAX_DRAWDOWN_PCT,
            PAYLOAD_KEY_OPS_MATURITY_MIN_SCORE,
            PAYLOAD_KEY_ENGINE_CONFIG_POLL_INTERVAL_SECONDS,
        ]
        return {k: getattr(cfg, k, None) for k in keys}

    def _build_health(self) -> dict:
        from core.deployment.health_check import HealthCheckService

        return HealthCheckService.safe_get_health(self._container)

    def _build_services(self) -> dict:
        details = {}
        missing = []
        present = []
        for name in self.REQUIRED_SERVICES:
            value = getattr(self._container, name, None)
            ok = value is not None
            details[name] = {
                PAYLOAD_KEY_PRESENT: ok,
                PAYLOAD_KEY_TYPE: value.__class__.__name__ if ok else None,
            }
            if ok:
                present.append(name)
            else:
                missing.append(name)
        return {
            PAYLOAD_KEY_REQUIRED_COUNT: len(self.REQUIRED_SERVICES),
            PAYLOAD_KEY_PRESENT_COUNT: len(present),
            PAYLOAD_KEY_MISSING_COUNT: len(missing),
            PAYLOAD_KEY_MISSING: missing,
            PAYLOAD_KEY_DETAILS: details,
        }

    def _build_diagnostics(self) -> dict:
        diag = getattr(self._container, "diagnostics", None)
        if diag is None:
            return {PAYLOAD_KEY_AVAILABLE: False}
        snap = diag.build_snapshot()
        metrics = snap.get(PAYLOAD_KEY_METRICS) or {}
        brain_health = snap.get(PAYLOAD_KEY_BRAIN_HEALTH) or {}
        audit_summary = snap.get(PAYLOAD_KEY_AUDIT_SUMMARY) or {}
        return {
            PAYLOAD_KEY_AVAILABLE: True,
            PAYLOAD_KEY_METRIC_COUNTER_COUNT: len(metrics.get(PAYLOAD_KEY_COUNTERS, {})),
            PAYLOAD_KEY_BRAIN_COUNT: brain_health.get(PAYLOAD_KEY_BRAIN_COUNT, 0),
            PAYLOAD_KEY_AUDIT_ENTRY_COUNT: audit_summary.get(PAYLOAD_KEY_ENTRY_COUNT, 0),
            PAYLOAD_KEY_ALERT_COUNT: len(snap.get(PAYLOAD_KEY_ALERTS, [])),
        }

    def _build_capabilities(self) -> dict:
        capability_status = {}
        for cap in self._capability_registry.list_names():
            capability_status[cap] = self._capability_registry.evaluate(self._container, cap)
        return {
            PAYLOAD_KEY_TOTAL: len(capability_status),
            PAYLOAD_KEY_AVAILABLE: sum(1 for v in capability_status.values() if v),
            PAYLOAD_KEY_ITEMS: capability_status,
        }

    def _build_checks(
        self,
        health: dict,
        services: dict,
        config: dict,
        capabilities: dict,
        alpha_budget: dict | None = None,
        validation_mode: str = VALIDATION_MODE_DEEP,
    ) -> list[dict]:
        readiness_status = (health.get(PAYLOAD_KEY_READINESS) or {}).get(PAYLOAD_KEY_STATUS)
        liveness_status = (health.get(PAYLOAD_KEY_LIVENESS) or {}).get(PAYLOAD_KEY_STATUS)
        alpha_budget = alpha_budget or {}
        core_checks = [
            {
                PAYLOAD_KEY_NAME: READINESS_CHECK_READINESS_READY,
                PAYLOAD_KEY_PASSED: readiness_status == HEALTH_STATUS_READY,
                PAYLOAD_KEY_DETAIL: {PAYLOAD_KEY_STATUS: readiness_status},
            },
            {
                PAYLOAD_KEY_NAME: READINESS_CHECK_LIVENESS_ALIVE,
                PAYLOAD_KEY_PASSED: liveness_status == HEALTH_STATUS_ALIVE,
                PAYLOAD_KEY_DETAIL: {PAYLOAD_KEY_STATUS: liveness_status},
            },
            {
                PAYLOAD_KEY_NAME: READINESS_CHECK_REQUIRED_SERVICES_PRESENT,
                PAYLOAD_KEY_PASSED: services[PAYLOAD_KEY_MISSING_COUNT] == 0,
                PAYLOAD_KEY_DETAIL: {PAYLOAD_KEY_MISSING: services[PAYLOAD_KEY_MISSING]},
            },
            {
                PAYLOAD_KEY_NAME: PAYLOAD_KEY_READINESS_CAPABILITIES_AVAILABLE,
                PAYLOAD_KEY_PASSED: capabilities[PAYLOAD_KEY_AVAILABLE]
                == capabilities[PAYLOAD_KEY_TOTAL],
                PAYLOAD_KEY_DETAIL: {
                    PAYLOAD_KEY_AVAILABLE: capabilities[PAYLOAD_KEY_AVAILABLE],
                    PAYLOAD_KEY_TOTAL: capabilities[PAYLOAD_KEY_TOTAL],
                },
            },
            {
                PAYLOAD_KEY_NAME: READINESS_CHECK_BASE_DIR_CONFIGURED,
                PAYLOAD_KEY_PASSED: bool(config.get(PAYLOAD_KEY_BASE_DIR)),
                PAYLOAD_KEY_DETAIL: {PAYLOAD_KEY_BASE_DIR: config.get(PAYLOAD_KEY_BASE_DIR)},
            },
        ]
        if validation_mode == VALIDATION_MODE_FAST:
            return core_checks
        return core_checks + [
            {
                PAYLOAD_KEY_NAME: COMPLIANCE_CHECK_ALPHA_BUDGET_EVIDENCE_REGISTERED,
                PAYLOAD_KEY_PASSED: alpha_budget.get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0) == 0,
                PAYLOAD_KEY_DETAIL: alpha_budget,
            },
            {
                PAYLOAD_KEY_NAME: COMPLIANCE_CHECK_ALPHA_BUDGET_WARNINGS_CLEAR,
                PAYLOAD_KEY_PASSED: alpha_budget.get(PAYLOAD_KEY_WARNING_TOTAL, 0) == 0,
                PAYLOAD_KEY_DETAIL: alpha_budget,
            },
        ]

    def _build_alpha_budget_governance(self) -> dict:
        registry = getattr(self._container, "release_registry", None)
        timeline = getattr(self._container, "operations_timeline", None)
        registry_summary = registry.summarize() if registry is not None else {}
        alpha_budget = registry_summary.get(PAYLOAD_KEY_ALPHA_BUDGET, {})
        timeline_events = (
            timeline.list_events(event_type=TIMELINE_EVENT_ALPHA_BUDGET_GOVERNANCE)
            if timeline is not None
            else []
        )
        timeline_warning_total = sum(
            event.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_WARNING_TOTAL, 0)
            for event in timeline_events
        )
        timeline_missing_evidence = sum(
            event.get(PAYLOAD_KEY_SUMMARY, {}).get(PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0)
            for event in timeline_events
        )
        return {
            PAYLOAD_KEY_AVAILABLE: registry is not None,
            PAYLOAD_KEY_RECORD_COUNT: registry_summary.get(PAYLOAD_KEY_RECORD_COUNT, 0),
            PAYLOAD_KEY_EVIDENCE_COUNT: alpha_budget.get(PAYLOAD_KEY_EVIDENCE_COUNT, 0),
            PAYLOAD_KEY_MISSING_EVIDENCE_COUNT: alpha_budget.get(
                PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0
            ),
            PAYLOAD_KEY_WARNING_TOTAL: alpha_budget.get(PAYLOAD_KEY_WARNING_TOTAL, 0),
            PAYLOAD_KEY_WARNING_RELEASE_COUNT: alpha_budget.get(
                PAYLOAD_KEY_WARNING_RELEASE_COUNT, 0
            ),
            **extract_governance_summary(registry_summary),
            PAYLOAD_KEY_TIMELINE_EVENT_COUNT: len(timeline_events),
            PAYLOAD_KEY_TIMELINE_WARNING_TOTAL: timeline_warning_total,
            PAYLOAD_KEY_TIMELINE_MISSING_EVIDENCE_COUNT: timeline_missing_evidence,
        }

    def _build_summary(
        self,
        checks: list[dict],
        services: dict,
        capabilities: dict,
        alpha_budget: dict | None = None,
    ) -> dict:
        failed = [c[PAYLOAD_KEY_NAME] for c in checks if not c[PAYLOAD_KEY_PASSED]]
        alpha_budget = alpha_budget or {}
        return {
            PAYLOAD_KEY_CHECK_COUNT: len(checks),
            PAYLOAD_KEY_FAILED_CHECK_COUNT: len(failed),
            PAYLOAD_KEY_FAILED_CHECKS: failed,
            PAYLOAD_KEY_SERVICES_PRESENT: services[PAYLOAD_KEY_PRESENT_COUNT],
            PAYLOAD_KEY_SERVICES_REQUIRED: services[PAYLOAD_KEY_REQUIRED_COUNT],
            PAYLOAD_KEY_READINESS_CAPABILITIES_AVAILABLE: capabilities[PAYLOAD_KEY_AVAILABLE],
            PAYLOAD_KEY_READINESS_CAPABILITIES_TOTAL: capabilities[PAYLOAD_KEY_TOTAL],
            PAYLOAD_KEY_ALPHA_BUDGET_EVIDENCE_COUNT: alpha_budget.get(
                PAYLOAD_KEY_EVIDENCE_COUNT, 0
            ),
            PAYLOAD_KEY_ALPHA_BUDGET_MISSING_EVIDENCE_COUNT: alpha_budget.get(
                PAYLOAD_KEY_MISSING_EVIDENCE_COUNT, 0
            ),
            PAYLOAD_KEY_ALPHA_BUDGET_WARNING_TOTAL: alpha_budget.get(PAYLOAD_KEY_WARNING_TOTAL, 0),
            PAYLOAD_KEY_ALPHA_BUDGET_TIMELINE_EVENT_COUNT: alpha_budget.get(
                PAYLOAD_KEY_TIMELINE_EVENT_COUNT, 0
            ),
            **extract_governance_summary(alpha_budget),
        }
