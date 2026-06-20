"""Release/deployment gate evaluation.

ReleaseGateService combines readiness, runbook preflight, SLO status,
and configuration validation into a deployment gate decision.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from core.contracts.domain_keys import (
    EVIDENCE_SECTION_ALPHA_BUDGET_USAGE,
    EVIDENCE_SECTION_PREFLIGHT,
    PAYLOAD_KEY_ALLOW_COUNT,
    PAYLOAD_KEY_ALLOWED,
    PAYLOAD_KEY_ALPHA_COUNT,
    PAYLOAD_KEY_BLOCK_COUNT,
    PAYLOAD_KEY_BLOCKING_SIGNALS,
    PAYLOAD_KEY_CONFIG,
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_DETAIL,
    PAYLOAD_KEY_ERROR,
    PAYLOAD_KEY_ERROR_BUDGET,
    PAYLOAD_KEY_ERRORS,
    PAYLOAD_KEY_EVIDENCE,
    PAYLOAD_KEY_FAILED_CHECKS,
    PAYLOAD_KEY_FAILED_OBJECTIVES,
    PAYLOAD_KEY_GENERATED_AT,
    PAYLOAD_KEY_LEVEL,
    PAYLOAD_KEY_NAME,
    PAYLOAD_KEY_PASSED,
    PAYLOAD_KEY_READINESS,
    PAYLOAD_KEY_READY,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_SIGNAL_COUNT,
    PAYLOAD_KEY_SIGNALS,
    PAYLOAD_KEY_SLO,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_STRICT,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_USAGE_DATE,
    PAYLOAD_KEY_VALID,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_WARN_COUNT,
    PAYLOAD_KEY_WARNING_COUNT,
    PAYLOAD_KEY_WARNING_SIGNALS,
    PAYLOAD_KEY_WARNINGS,
    RELEASE_GATE_FAILED_CHECK_PREFLIGHT_EXCEPTION,
    RELEASE_GATE_FAILED_CHECK_READINESS_EXCEPTION,
    RELEASE_GATE_FAILED_OBJECTIVE_SLO_EXCEPTION,
    RELEASE_PIPELINE_GATE_DECISION_ALLOW,
    RELEASE_PIPELINE_GATE_DECISION_BLOCK,
    RELEASE_PIPELINE_GATE_DECISION_WARN,
    SLO_STATUS_BREACHING,
    SLO_STATUS_HEALTHY,
)
from core.deployment.schema_versions import SCHEMA_RELEASE_GATE
from core.deployment.validation_mode import resolve_validation_mode
from core.runtime.fault_handler import fail_open_guard


class ReleaseGateService:
    """Produces allow/warn/block decisions for deployment automation."""

    DECISION_ALLOW = RELEASE_PIPELINE_GATE_DECISION_ALLOW
    DECISION_WARN = RELEASE_PIPELINE_GATE_DECISION_WARN
    DECISION_BLOCK = RELEASE_PIPELINE_GATE_DECISION_BLOCK

    def __init__(self, container):
        self._container = container

    def evaluate(
        self,
        *,
        strict: bool = True,
        alpha_budget_usage_report: dict | None = None,
        validation_mode: str | None = None,
    ) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        readiness = self._safe_readiness(validation_mode=validation_mode)
        preflight = self._safe_preflight(validation_mode=validation_mode)
        slo = self._safe_slo()
        config = self._safe_config()
        signals = self._build_signals(readiness, preflight, slo, config, alpha_budget_usage_report)
        decision = self._decide(signals, strict=strict)
        evidence = {
            PAYLOAD_KEY_READINESS: self._compact_readiness(readiness),
            EVIDENCE_SECTION_PREFLIGHT: self._compact_runbook(preflight),
            PAYLOAD_KEY_SLO: self._compact_slo(slo),
            PAYLOAD_KEY_CONFIG: self._compact_config(config),
        }
        if alpha_budget_usage_report is not None:
            evidence[EVIDENCE_SECTION_ALPHA_BUDGET_USAGE] = self._compact_alpha_budget(
                alpha_budget_usage_report
            )
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_RELEASE_GATE,
            PAYLOAD_KEY_GENERATED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
            PAYLOAD_KEY_STRICT: strict,
            PAYLOAD_KEY_DECISION: decision,
            PAYLOAD_KEY_ALLOWED: decision == self.DECISION_ALLOW,
            PAYLOAD_KEY_SIGNALS: signals,
            PAYLOAD_KEY_SUMMARY: self._summary(signals, decision),
            PAYLOAD_KEY_EVIDENCE: evidence,
        }

    def save_report(
        self,
        path: str,
        *,
        strict: bool = True,
        alpha_budget_usage_report: dict | None = None,
        validation_mode: str | None = None,
    ) -> str:
        report = self.evaluate(
            strict=strict,
            alpha_budget_usage_report=alpha_budget_usage_report,
            validation_mode=validation_mode,
        )
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return str(target)

    def _safe_readiness(self, *, validation_mode: str | None = None) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        try:
            return self._container.release_readiness.build_report(validation_mode=validation_mode)
        except Exception as exc:  # BLE001:FOG
            with fail_open_guard("release_gate:_safe_readiness"):
                return {
                    PAYLOAD_KEY_READY: False,
                    PAYLOAD_KEY_ERROR: str(exc),
                    PAYLOAD_KEY_SUMMARY: {
                        PAYLOAD_KEY_FAILED_CHECKS: [RELEASE_GATE_FAILED_CHECK_READINESS_EXCEPTION]
                    },
                }
    def _safe_preflight(self, *, validation_mode: str | None = None) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        try:
            return self._container.runbook_engine.preflight(validation_mode=validation_mode)
        except Exception as exc:  # BLE001:FOG
            with fail_open_guard("release_gate:_safe_preflight"):
                return {
                    PAYLOAD_KEY_PASSED: False,
                    PAYLOAD_KEY_ERROR: str(exc),
                    PAYLOAD_KEY_SUMMARY: {
                        PAYLOAD_KEY_FAILED_CHECKS: [RELEASE_GATE_FAILED_CHECK_PREFLIGHT_EXCEPTION]
                    },
                }
    def _safe_slo(self) -> dict:
        try:
            return self._container.slo_service.evaluate()
        except Exception as exc:  # BLE001:FOG
            with fail_open_guard("release_gate:_safe_slo"):
                return {
                    PAYLOAD_KEY_STATUS: SLO_STATUS_BREACHING,
                    PAYLOAD_KEY_ERROR: str(exc),
                    PAYLOAD_KEY_FAILED_OBJECTIVES: [RELEASE_GATE_FAILED_OBJECTIVE_SLO_EXCEPTION],
                }
    def _safe_config(self) -> dict:
        try:
            from core.deployment.operational_support import ConfigValidator

            return ConfigValidator().validate(self._container.config)
        except Exception as exc:  # BLE001:FOG
            with fail_open_guard("release_gate:_safe_config"):
                return {
                    PAYLOAD_KEY_VALID: False,
                    PAYLOAD_KEY_ERRORS: [str(exc)],
                    PAYLOAD_KEY_WARNINGS: [],
                }
    def _build_signals(
        self,
        readiness: dict,
        preflight: dict,
        slo: dict,
        config: dict,
        alpha_budget_usage_report: dict | None = None,
    ) -> list[dict]:
        signals = [
            {
                PAYLOAD_KEY_NAME: PAYLOAD_KEY_READINESS,
                PAYLOAD_KEY_LEVEL: RELEASE_PIPELINE_GATE_DECISION_BLOCK
                if not readiness.get(PAYLOAD_KEY_READY, False)
                else RELEASE_PIPELINE_GATE_DECISION_ALLOW,
                PAYLOAD_KEY_PASSED: bool(readiness.get(PAYLOAD_KEY_READY, False)),
                PAYLOAD_KEY_DETAIL: {
                    PAYLOAD_KEY_FAILED_CHECKS: readiness.get(PAYLOAD_KEY_SUMMARY, {}).get(
                        PAYLOAD_KEY_FAILED_CHECKS, []
                    )
                },
            },
            {
                PAYLOAD_KEY_NAME: EVIDENCE_SECTION_PREFLIGHT,
                PAYLOAD_KEY_LEVEL: RELEASE_PIPELINE_GATE_DECISION_BLOCK
                if not preflight.get(PAYLOAD_KEY_PASSED, False)
                else RELEASE_PIPELINE_GATE_DECISION_ALLOW,
                PAYLOAD_KEY_PASSED: bool(preflight.get(PAYLOAD_KEY_PASSED, False)),
                PAYLOAD_KEY_DETAIL: {
                    PAYLOAD_KEY_FAILED_CHECKS: preflight.get(PAYLOAD_KEY_SUMMARY, {}).get(
                        PAYLOAD_KEY_FAILED_CHECKS, []
                    )
                },
            },
            {
                PAYLOAD_KEY_NAME: PAYLOAD_KEY_SLO,
                PAYLOAD_KEY_LEVEL: RELEASE_PIPELINE_GATE_DECISION_WARN
                if slo.get(PAYLOAD_KEY_STATUS) != SLO_STATUS_HEALTHY
                else RELEASE_PIPELINE_GATE_DECISION_ALLOW,
                PAYLOAD_KEY_PASSED: slo.get(PAYLOAD_KEY_STATUS) == SLO_STATUS_HEALTHY,
                PAYLOAD_KEY_DETAIL: {
                    PAYLOAD_KEY_FAILED_OBJECTIVES: slo.get(PAYLOAD_KEY_FAILED_OBJECTIVES, [])
                },
            },
            {
                PAYLOAD_KEY_NAME: PAYLOAD_KEY_CONFIG,
                PAYLOAD_KEY_LEVEL: RELEASE_PIPELINE_GATE_DECISION_BLOCK
                if not config.get(PAYLOAD_KEY_VALID, False)
                else (
                    RELEASE_PIPELINE_GATE_DECISION_WARN
                    if config.get(PAYLOAD_KEY_WARNINGS)
                    else RELEASE_PIPELINE_GATE_DECISION_ALLOW
                ),
                PAYLOAD_KEY_PASSED: bool(config.get(PAYLOAD_KEY_VALID, False)),
                PAYLOAD_KEY_DETAIL: {
                    PAYLOAD_KEY_ERRORS: config.get(PAYLOAD_KEY_ERRORS, []),
                    PAYLOAD_KEY_WARNINGS: config.get(PAYLOAD_KEY_WARNINGS, []),
                },
            },
        ]
        if alpha_budget_usage_report is not None:
            signals.append(self._alpha_budget_signal(alpha_budget_usage_report))
        return signals

    def _alpha_budget_signal(self, report: dict) -> dict:
        warning_count = int(report.get(PAYLOAD_KEY_WARNING_COUNT, 0))
        return {
            PAYLOAD_KEY_NAME: EVIDENCE_SECTION_ALPHA_BUDGET_USAGE,
            PAYLOAD_KEY_LEVEL: RELEASE_PIPELINE_GATE_DECISION_WARN
            if warning_count > 0
            else RELEASE_PIPELINE_GATE_DECISION_ALLOW,
            PAYLOAD_KEY_PASSED: warning_count == 0,
            PAYLOAD_KEY_DETAIL: {
                PAYLOAD_KEY_WARNING_COUNT: warning_count,
                PAYLOAD_KEY_WARNINGS: report.get(PAYLOAD_KEY_WARNINGS, []),
            },
        }

    def _decide(self, signals: list[dict], *, strict: bool) -> str:
        if any(s[PAYLOAD_KEY_LEVEL] == RELEASE_PIPELINE_GATE_DECISION_BLOCK for s in signals):
            return self.DECISION_BLOCK
        if any(s[PAYLOAD_KEY_LEVEL] == RELEASE_PIPELINE_GATE_DECISION_WARN for s in signals):
            return self.DECISION_BLOCK if strict else self.DECISION_WARN
        return self.DECISION_ALLOW

    def _summary(self, signals: list[dict], decision: str) -> dict:
        levels = {
            RELEASE_PIPELINE_GATE_DECISION_ALLOW: 0,
            RELEASE_PIPELINE_GATE_DECISION_WARN: 0,
            RELEASE_PIPELINE_GATE_DECISION_BLOCK: 0,
        }
        for s in signals:
            levels[s[PAYLOAD_KEY_LEVEL]] = levels.get(s[PAYLOAD_KEY_LEVEL], 0) + 1
        return {
            PAYLOAD_KEY_DECISION: decision,
            PAYLOAD_KEY_SIGNAL_COUNT: len(signals),
            PAYLOAD_KEY_ALLOW_COUNT: levels.get(RELEASE_PIPELINE_GATE_DECISION_ALLOW, 0),
            PAYLOAD_KEY_WARN_COUNT: levels.get(RELEASE_PIPELINE_GATE_DECISION_WARN, 0),
            PAYLOAD_KEY_BLOCK_COUNT: levels.get(RELEASE_PIPELINE_GATE_DECISION_BLOCK, 0),
            PAYLOAD_KEY_BLOCKING_SIGNALS: [
                s[PAYLOAD_KEY_NAME]
                for s in signals
                if s[PAYLOAD_KEY_LEVEL] == RELEASE_PIPELINE_GATE_DECISION_BLOCK
            ],
            PAYLOAD_KEY_WARNING_SIGNALS: [
                s[PAYLOAD_KEY_NAME]
                for s in signals
                if s[PAYLOAD_KEY_LEVEL] == RELEASE_PIPELINE_GATE_DECISION_WARN
            ],
        }

    def _compact_readiness(self, report: dict) -> dict:
        return {
            PAYLOAD_KEY_READY: report.get(PAYLOAD_KEY_READY, False),
            PAYLOAD_KEY_FAILED_CHECKS: report.get(PAYLOAD_KEY_SUMMARY, {}).get(
                PAYLOAD_KEY_FAILED_CHECKS, []
            ),
        }

    def _compact_runbook(self, result: dict) -> dict:
        return {
            PAYLOAD_KEY_PASSED: result.get(PAYLOAD_KEY_PASSED, False),
            PAYLOAD_KEY_FAILED_CHECKS: result.get(PAYLOAD_KEY_SUMMARY, {}).get(
                PAYLOAD_KEY_FAILED_CHECKS, []
            ),
        }

    def _compact_slo(self, report: dict) -> dict:
        return {
            PAYLOAD_KEY_STATUS: report.get(PAYLOAD_KEY_STATUS),
            PAYLOAD_KEY_FAILED_OBJECTIVES: report.get(PAYLOAD_KEY_FAILED_OBJECTIVES, []),
            PAYLOAD_KEY_ERROR_BUDGET: report.get(PAYLOAD_KEY_ERROR_BUDGET, {}),
        }

    def _compact_config(self, result: dict) -> dict:
        return {
            PAYLOAD_KEY_VALID: result.get(PAYLOAD_KEY_VALID, False),
            PAYLOAD_KEY_ERRORS: result.get(PAYLOAD_KEY_ERRORS, []),
            PAYLOAD_KEY_WARNINGS: result.get(PAYLOAD_KEY_WARNINGS, []),
        }

    def _compact_alpha_budget(self, report: dict) -> dict:
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: report.get(PAYLOAD_KEY_SCHEMA_VERSION),
            PAYLOAD_KEY_USAGE_DATE: report.get(PAYLOAD_KEY_USAGE_DATE),
            PAYLOAD_KEY_ALPHA_COUNT: report.get(PAYLOAD_KEY_ALPHA_COUNT, 0),
            PAYLOAD_KEY_WARNING_COUNT: report.get(PAYLOAD_KEY_WARNING_COUNT, 0),
            PAYLOAD_KEY_WARNINGS: report.get(PAYLOAD_KEY_WARNINGS, []),
        }
