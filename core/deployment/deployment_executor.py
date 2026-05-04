"""Deployment plan executor.

Executes deployment plans in safe dry-run mode. It does not perform
real infrastructure changes; instead it evaluates each phase/checkpoint
against current services and produces an execution record.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from core.deployment.domain_keys import (
    DEPLOYMENT_EXECUTION_FAILURE_INVALID_PLAN,
    DEPLOYMENT_EXECUTION_FAILURE_NOT_EXECUTABLE,
    DEPLOYMENT_EXECUTION_FAILURE_ROLLBACK_FIRED,
    DEPLOYMENT_EXECUTION_STATUS_FAILED,
    DEPLOYMENT_EXECUTION_STATUS_SUCCEEDED,
    DEPLOYMENT_EXECUTION_TRIGGER_SEVERITY_UNKNOWN,
    DEPLOYMENT_PLAN_STATUS_INVALID,
    EVIDENCE_SECTION_GATE,
    EVIDENCE_SECTION_PREFLIGHT,
    HEALTH_STATUS_READY,
    PAYLOAD_KEY_CHECKPOINT,
    PAYLOAD_KEY_CHECKPOINT_COUNT,
    PAYLOAD_KEY_CHECKPOINT_RESULTS,
    PAYLOAD_KEY_CHECKPOINTS,
    PAYLOAD_KEY_COMMAND,
    PAYLOAD_KEY_COUNTERS,
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_DESCRIPTION,
    PAYLOAD_KEY_DETAIL,
    PAYLOAD_KEY_DRY_RUN,
    PAYLOAD_KEY_ERROR_BUDGET,
    PAYLOAD_KEY_EVIDENCE,
    PAYLOAD_KEY_EXECUTABLE,
    PAYLOAD_KEY_EXECUTION_FAILURES,
    PAYLOAD_KEY_EXHAUSTED_COUNT,
    PAYLOAD_KEY_FAILURE_COUNT,
    PAYLOAD_KEY_FINISHED_AT,
    PAYLOAD_KEY_FIRED,
    PAYLOAD_KEY_FIRED_COUNT,
    PAYLOAD_KEY_GATE_DECISION,
    PAYLOAD_KEY_NAME,
    PAYLOAD_KEY_PASSED,
    PAYLOAD_KEY_PHASE,
    PAYLOAD_KEY_PHASE_COUNT,
    PAYLOAD_KEY_PHASE_RESULTS,
    PAYLOAD_KEY_PHASES,
    PAYLOAD_KEY_READY,
    PAYLOAD_KEY_RECOMMENDATION,
    PAYLOAD_KEY_REQUIRED,
    PAYLOAD_KEY_ROLLBACK,
    PAYLOAD_KEY_RUNBOOK_STATUS,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_SEVERITY,
    PAYLOAD_KEY_SLO_STATUS,
    PAYLOAD_KEY_STARTED_AT,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_STRATEGY,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_VERSION,
    RELEASE_PIPELINE_GATE_DECISION_ALLOW,
    RELEASE_PIPELINE_GATE_DECISION_BLOCK,
    RELEASE_PIPELINE_GATE_DECISION_WARN,
    RELEASE_PIPELINE_KEY_DRY_RUN,
    RELEASE_PIPELINE_STATUS_BLOCKED,
    ROLLBACK_RECOMMENDATION_CONTINUE,
    ROLLBACK_RECOMMENDATION_ROLLBACK,
    SLO_STATUS_BREACHING,
    SLO_STATUS_HEALTHY,
)
from core.deployment.schema_versions import SCHEMA_DEPLOYMENT_EXECUTION
from core.deployment.validation_mode import resolve_validation_mode
from core.observability.metric_names import CYCLES_CIRCUIT_OPEN


class DeploymentExecutor:
    """Runs deployment plans as safe operational simulations."""

    def __init__(self, container):
        self._container = container

    def execute(
        self,
        plan: dict | None = None,
        *,
        dry_run: bool = True,
        validation_mode: str | None = None,
    ) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        plan = plan or self._container.deployment_plan.build_plan(validation_mode=validation_mode)
        started_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
        if plan.get(PAYLOAD_KEY_STATUS) == DEPLOYMENT_PLAN_STATUS_INVALID:
            return self._result(
                started_at,
                plan,
                [],
                DEPLOYMENT_EXECUTION_STATUS_FAILED,
                [DEPLOYMENT_EXECUTION_FAILURE_INVALID_PLAN],
            )
        if not plan.get(PAYLOAD_KEY_EXECUTABLE, False):
            return self._result(
                started_at,
                plan,
                [],
                RELEASE_PIPELINE_STATUS_BLOCKED,
                [DEPLOYMENT_EXECUTION_FAILURE_NOT_EXECUTABLE],
            )

        phase_results = []
        failures: list[str] = []
        for phase in plan.get(PAYLOAD_KEY_PHASES, []):
            result = self._execute_phase(phase, dry_run=dry_run, validation_mode=validation_mode)
            phase_results.append(result)
            if not result[PAYLOAD_KEY_PASSED]:
                failures.append(phase[PAYLOAD_KEY_NAME])
                if phase.get(PAYLOAD_KEY_REQUIRED, True):
                    break

        checkpoint_results: list[dict] = []
        if not failures:
            for checkpoint in plan.get(PAYLOAD_KEY_CHECKPOINTS, []):
                result = self._execute_checkpoint(checkpoint, validation_mode=validation_mode)
                checkpoint_results.append(result)
                if not result[PAYLOAD_KEY_PASSED] and checkpoint.get(PAYLOAD_KEY_REQUIRED, True):
                    failures.append(checkpoint[PAYLOAD_KEY_NAME])
                    break

        rollback = self._evaluate_rollback_triggers(plan, validation_mode=validation_mode)
        if rollback[PAYLOAD_KEY_FIRED_COUNT] > 0:
            failures.append(DEPLOYMENT_EXECUTION_FAILURE_ROLLBACK_FIRED)

        status = (
            DEPLOYMENT_EXECUTION_STATUS_SUCCEEDED
            if not failures
            else DEPLOYMENT_EXECUTION_STATUS_FAILED
        )
        return self._result(
            started_at,
            plan,
            phase_results,
            status,
            failures,
            checkpoint_results=checkpoint_results,
            rollback=rollback,
            dry_run=dry_run,
            validation_mode=validation_mode,
        )

    def execute_from_file(
        self,
        path: str,
        *,
        dry_run: bool = True,
        validation_mode: str | None = None,
    ) -> dict:
        plan = json.loads(Path(path).read_text(encoding="utf-8"))
        return self.execute(plan, dry_run=dry_run, validation_mode=validation_mode)

    def save_result(self, result: dict, path: str) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        return str(target)

    def _execute_phase(
        self,
        phase: dict,
        *,
        dry_run: bool,
        validation_mode: str | None = None,
    ) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        name = phase[PAYLOAD_KEY_NAME]
        passed = True
        detail = {
            RELEASE_PIPELINE_KEY_DRY_RUN: dry_run,
            PAYLOAD_KEY_DESCRIPTION: phase.get(PAYLOAD_KEY_DESCRIPTION, ""),
        }

        if name == EVIDENCE_SECTION_PREFLIGHT:
            preflight = self._container.runbook_engine.preflight(validation_mode=validation_mode)
            passed = preflight.get(PAYLOAD_KEY_PASSED, False)
            detail[PAYLOAD_KEY_RUNBOOK_STATUS] = preflight.get(PAYLOAD_KEY_STATUS)
        elif name == "evidence_capture":
            passed = True
            detail[PAYLOAD_KEY_EVIDENCE] = "already_planned_or_skipped"
        elif name in {
            "deploy_all",
            "deploy_canary_10pct",
            "promote_50pct",
            "promote_100pct",
            "shadow_deploy",
        }:
            gate = self._container.release_gate.evaluate(validation_mode=validation_mode)
            passed = gate.get(PAYLOAD_KEY_DECISION) in {
                RELEASE_PIPELINE_GATE_DECISION_ALLOW,
                RELEASE_PIPELINE_GATE_DECISION_WARN,
            }
            detail[PAYLOAD_KEY_GATE_DECISION] = gate.get(PAYLOAD_KEY_DECISION)
        elif name in {"post_deploy_verify", "shadow_compare"}:
            slo = self._container.slo_service.evaluate()
            passed = slo.get(PAYLOAD_KEY_STATUS) == SLO_STATUS_HEALTHY
            detail[PAYLOAD_KEY_SLO_STATUS] = slo.get(PAYLOAD_KEY_STATUS)
        return {
            PAYLOAD_KEY_PHASE: name,
            PAYLOAD_KEY_PASSED: bool(passed),
            PAYLOAD_KEY_REQUIRED: phase.get(PAYLOAD_KEY_REQUIRED, True),
            PAYLOAD_KEY_DETAIL: detail,
        }

    def _execute_checkpoint(self, checkpoint: dict, *, validation_mode: str | None = None) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        name = checkpoint[PAYLOAD_KEY_NAME]
        passed = True
        detail = {PAYLOAD_KEY_COMMAND: checkpoint.get(PAYLOAD_KEY_COMMAND)}
        if name == "readiness":
            report = self._container.release_readiness.build_report(validation_mode=validation_mode)
            passed = report.get(PAYLOAD_KEY_READY, False)
            detail[PAYLOAD_KEY_READY] = report.get(PAYLOAD_KEY_READY, False)
        elif name == EVIDENCE_SECTION_GATE:
            gate = self._container.release_gate.evaluate(validation_mode=validation_mode)
            passed = gate.get(PAYLOAD_KEY_DECISION) in {
                RELEASE_PIPELINE_GATE_DECISION_ALLOW,
                RELEASE_PIPELINE_GATE_DECISION_WARN,
            }
            detail[PAYLOAD_KEY_DECISION] = gate.get(PAYLOAD_KEY_DECISION)
        elif "slo" in name:
            slo = self._container.slo_service.evaluate()
            passed = slo.get(PAYLOAD_KEY_STATUS) == SLO_STATUS_HEALTHY
            detail[PAYLOAD_KEY_STATUS] = slo.get(PAYLOAD_KEY_STATUS)
        elif name == "doctor":
            doctor = self._container.runbook_engine.doctor(validation_mode=validation_mode)
            passed = doctor.get(PAYLOAD_KEY_PASSED, False)
            detail[PAYLOAD_KEY_STATUS] = doctor.get(PAYLOAD_KEY_STATUS)
        elif name == "shadow_evidence":
            passed = True
            detail[PAYLOAD_KEY_EVIDENCE] = "checkpoint_acknowledged"
        return {
            PAYLOAD_KEY_CHECKPOINT: name,
            PAYLOAD_KEY_PASSED: bool(passed),
            PAYLOAD_KEY_REQUIRED: checkpoint.get(PAYLOAD_KEY_REQUIRED, True),
            PAYLOAD_KEY_DETAIL: detail,
        }

    def _evaluate_rollback_triggers(
        self, plan: dict, *, validation_mode: str | None = None
    ) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        gate = self._container.release_gate.evaluate(validation_mode=validation_mode)
        slo = self._container.slo_service.evaluate()
        health = self._container.health_check.readiness()
        counters = (
            self._container.metrics.snapshot().get(PAYLOAD_KEY_COUNTERS, {})
            if self._container.metrics
            else {}
        )
        fired = []
        for trigger in plan.get(PAYLOAD_KEY_ROLLBACK, []):
            name = trigger[PAYLOAD_KEY_NAME]
            hit = False
            if name == "release_gate_block":
                hit = gate.get(PAYLOAD_KEY_DECISION) == RELEASE_PIPELINE_GATE_DECISION_BLOCK
            elif name == "slo_breach":
                hit = slo.get(PAYLOAD_KEY_STATUS) == SLO_STATUS_BREACHING
            elif name == "readiness_down":
                hit = health.get(PAYLOAD_KEY_STATUS) != HEALTH_STATUS_READY
            elif name == "circuit_open":
                hit = counters.get(CYCLES_CIRCUIT_OPEN, 0) > 0
            elif name == "canary_error_budget_exhausted":
                hit = (slo.get(PAYLOAD_KEY_ERROR_BUDGET) or {}).get(
                    PAYLOAD_KEY_EXHAUSTED_COUNT, 0
                ) > 0
            if hit:
                fired.append(
                    {
                        PAYLOAD_KEY_NAME: name,
                        PAYLOAD_KEY_SEVERITY: trigger.get(
                            PAYLOAD_KEY_SEVERITY, DEPLOYMENT_EXECUTION_TRIGGER_SEVERITY_UNKNOWN
                        ),
                    }
                )
        return {
            PAYLOAD_KEY_FIRED_COUNT: len(fired),
            PAYLOAD_KEY_FIRED: fired,
            PAYLOAD_KEY_RECOMMENDATION: ROLLBACK_RECOMMENDATION_ROLLBACK
            if fired
            else ROLLBACK_RECOMMENDATION_CONTINUE,
        }

    def _result(
        self,
        started_at: str,
        plan: dict,
        phase_results: list[dict],
        status: str,
        failures: list[str],
        *,
        checkpoint_results: list[dict] | None = None,
        rollback: dict | None = None,
        dry_run: bool = True,
        validation_mode: str | None = None,
    ) -> dict:
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_DEPLOYMENT_EXECUTION,
            PAYLOAD_KEY_STARTED_AT: started_at,
            PAYLOAD_KEY_FINISHED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
            PAYLOAD_KEY_DRY_RUN: dry_run,
            PAYLOAD_KEY_STATUS: status,
            PAYLOAD_KEY_PASSED: status == DEPLOYMENT_EXECUTION_STATUS_SUCCEEDED,
            PAYLOAD_KEY_VERSION: plan.get(PAYLOAD_KEY_VERSION),
            PAYLOAD_KEY_STRATEGY: plan.get(PAYLOAD_KEY_STRATEGY),
            PAYLOAD_KEY_SUMMARY: {
                PAYLOAD_KEY_PHASE_COUNT: len(phase_results),
                PAYLOAD_KEY_CHECKPOINT_COUNT: len(checkpoint_results or []),
                PAYLOAD_KEY_FAILURE_COUNT: len(failures),
                PAYLOAD_KEY_EXECUTION_FAILURES: failures,
            },
            PAYLOAD_KEY_PHASE_RESULTS: phase_results,
            PAYLOAD_KEY_CHECKPOINT_RESULTS: checkpoint_results or [],
            PAYLOAD_KEY_ROLLBACK: rollback
            or {
                PAYLOAD_KEY_FIRED_COUNT: 0,
                PAYLOAD_KEY_FIRED: [],
                PAYLOAD_KEY_RECOMMENDATION: ROLLBACK_RECOMMENDATION_CONTINUE,
            },
        }
