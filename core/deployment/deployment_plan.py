"""Deployment rollout plan generation.

DeploymentPlanService turns release gate/evidence/SLO information into
a machine-readable rollout plan with phases, checkpoints, and rollback
triggers.
"""

from datetime import UTC, datetime
from pathlib import Path

from core.contracts.domain_keys import (
    DEPLOYMENT_PLAN_STATUS_INVALID,
    DEPLOYMENT_PLAN_STATUS_READY,
    EVIDENCE_SECTION_GATE,
    EVIDENCE_SECTION_PREFLIGHT,
    FINDING_SEVERITY_CRITICAL,
    FINDING_SEVERITY_HIGH,
    PAYLOAD_KEY_AVAILABLE_STRATEGIES,
    PAYLOAD_KEY_CHECKPOINTS,
    PAYLOAD_KEY_COMMAND,
    PAYLOAD_KEY_COMMANDS,
    PAYLOAD_KEY_CONDITION,
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_DESCRIPTION,
    PAYLOAD_KEY_ERROR,
    PAYLOAD_KEY_ERROR_BUDGET,
    PAYLOAD_KEY_EVIDENCE,
    PAYLOAD_KEY_EXECUTABLE,
    PAYLOAD_KEY_FAILED_OBJECTIVES,
    PAYLOAD_KEY_GENERATED_AT,
    PAYLOAD_KEY_NAME,
    PAYLOAD_KEY_PHASES,
    PAYLOAD_KEY_REQUIRED,
    PAYLOAD_KEY_ROLLBACK,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_SEVERITY,
    PAYLOAD_KEY_SLO,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_STRATEGY,
    PAYLOAD_KEY_STRICT,
    PAYLOAD_KEY_SUCCESS_CONDITION,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_VERSION,
    RELEASE_PIPELINE_GATE_DECISION_ALLOW,
    RELEASE_PIPELINE_GATE_DECISION_WARN,
    RELEASE_PIPELINE_STATUS_BLOCKED,
)
from core.deployment.atomic_file_writer import atomic_write_json
from core.deployment.schema_versions import SCHEMA_DEPLOYMENT_PLAN
from core.deployment.validation_mode import resolve_validation_mode
from core.observability.metric_names import CYCLES_CIRCUIT_OPEN


class DeploymentPlanService:
    """Builds release rollout and rollback plans."""

    STRATEGIES = {"standard", "canary", "shadow"}

    def __init__(self, container):
        self._container = container

    def build_plan(
        self,
        *,
        version: str = "0.1.0",
        strategy: str = "standard",
        evidence_dir: str | None = None,
        strict_gate: bool = True,
        alpha_budget_usage_report: dict | None = None,
        validation_mode: str | None = None,
    ) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        if strategy not in self.STRATEGIES:
            return {
                PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_DEPLOYMENT_PLAN,
                PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
                PAYLOAD_KEY_STATUS: DEPLOYMENT_PLAN_STATUS_INVALID,
                PAYLOAD_KEY_ERROR: f"Unknown strategy: {strategy}",
                PAYLOAD_KEY_AVAILABLE_STRATEGIES: sorted(self.STRATEGIES),
            }

        gate = self._container.release_gate.evaluate(
            strict=strict_gate,
            alpha_budget_usage_report=alpha_budget_usage_report,
            validation_mode=validation_mode,
        )
        slo = self._container.slo_service.evaluate()
        evidence = None
        if evidence_dir:
            evidence = self._container.evidence_bundle.build_bundle(
                evidence_dir,
                label=f"deploy_{version.replace('.', '_')}_{strategy}",
                alpha_budget_usage_report=alpha_budget_usage_report,
                validation_mode=validation_mode,
            )

        phases = self._build_phases(strategy)
        rollback = self._build_rollback_triggers(strategy)
        checkpoints = self._build_checkpoints(strategy)
        executable = gate[PAYLOAD_KEY_DECISION] in {
            RELEASE_PIPELINE_GATE_DECISION_ALLOW,
            RELEASE_PIPELINE_GATE_DECISION_WARN,
        }
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_DEPLOYMENT_PLAN,
            PAYLOAD_KEY_GENERATED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_VERSION: version,
            PAYLOAD_KEY_STRATEGY: strategy,
            PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
            PAYLOAD_KEY_STATUS: DEPLOYMENT_PLAN_STATUS_READY
            if executable
            else RELEASE_PIPELINE_STATUS_BLOCKED,
            PAYLOAD_KEY_EXECUTABLE: executable,
            EVIDENCE_SECTION_GATE: {
                PAYLOAD_KEY_DECISION: gate[PAYLOAD_KEY_DECISION],
                PAYLOAD_KEY_STRICT: gate[PAYLOAD_KEY_STRICT],
                PAYLOAD_KEY_SUMMARY: gate[PAYLOAD_KEY_SUMMARY],
            },
            PAYLOAD_KEY_SLO: {
                PAYLOAD_KEY_STATUS: slo[PAYLOAD_KEY_STATUS],
                PAYLOAD_KEY_FAILED_OBJECTIVES: slo[PAYLOAD_KEY_FAILED_OBJECTIVES],
                PAYLOAD_KEY_ERROR_BUDGET: slo[PAYLOAD_KEY_ERROR_BUDGET],
            },
            PAYLOAD_KEY_EVIDENCE: evidence,
            PAYLOAD_KEY_PHASES: phases,
            PAYLOAD_KEY_CHECKPOINTS: checkpoints,
            PAYLOAD_KEY_ROLLBACK: rollback,
            PAYLOAD_KEY_COMMANDS: self._build_commands(
                version, strategy, validation_mode=validation_mode
            ),
        }

    def save_plan(self, path: str, **kwargs) -> str:
        plan = self.build_plan(**kwargs)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, plan)
        return str(target)

    def _build_phases(self, strategy: str) -> list[dict]:
        common = [
            {
                PAYLOAD_KEY_NAME: EVIDENCE_SECTION_PREFLIGHT,
                PAYLOAD_KEY_DESCRIPTION: "Run release gate and preflight runbook",
                PAYLOAD_KEY_REQUIRED: True,
                PAYLOAD_KEY_SUCCESS_CONDITION: "gate.decision in allow,warn",
            },
            {
                PAYLOAD_KEY_NAME: "evidence_capture",
                PAYLOAD_KEY_DESCRIPTION: "Generate evidence bundle for audit trail",
                PAYLOAD_KEY_REQUIRED: True,
                PAYLOAD_KEY_SUCCESS_CONDITION: "evidence manifest generated",
            },
        ]
        if strategy == "standard":
            rollout = [
                {
                    PAYLOAD_KEY_NAME: "deploy_all",
                    PAYLOAD_KEY_DESCRIPTION: "Deploy to all production workers",
                    PAYLOAD_KEY_REQUIRED: True,
                    PAYLOAD_KEY_SUCCESS_CONDITION: "health.readiness == ready",
                },
                {
                    PAYLOAD_KEY_NAME: "post_deploy_verify",
                    PAYLOAD_KEY_DESCRIPTION: "Run SLO and doctor checks",
                    PAYLOAD_KEY_REQUIRED: True,
                    PAYLOAD_KEY_SUCCESS_CONDITION: "slo.status == healthy",
                },
            ]
        elif strategy == "canary":
            rollout = [
                {
                    PAYLOAD_KEY_NAME: "deploy_canary_10pct",
                    PAYLOAD_KEY_DESCRIPTION: "Deploy to 10% traffic slice",
                    PAYLOAD_KEY_REQUIRED: True,
                    PAYLOAD_KEY_SUCCESS_CONDITION: "canary health ready and SLO healthy",
                },
                {
                    PAYLOAD_KEY_NAME: "promote_50pct",
                    PAYLOAD_KEY_DESCRIPTION: "Promote to 50% after checkpoint",
                    PAYLOAD_KEY_REQUIRED: True,
                    PAYLOAD_KEY_SUCCESS_CONDITION: "no rollback triggers fired",
                },
                {
                    PAYLOAD_KEY_NAME: "promote_100pct",
                    PAYLOAD_KEY_DESCRIPTION: "Promote to full production",
                    PAYLOAD_KEY_REQUIRED: True,
                    PAYLOAD_KEY_SUCCESS_CONDITION: "release gate remains allow/warn",
                },
            ]
        else:
            rollout = [
                {
                    PAYLOAD_KEY_NAME: "shadow_deploy",
                    PAYLOAD_KEY_DESCRIPTION: "Deploy without live execution",
                    PAYLOAD_KEY_REQUIRED: True,
                    PAYLOAD_KEY_SUCCESS_CONDITION: "shadow decisions persisted",
                },
                {
                    PAYLOAD_KEY_NAME: "shadow_compare",
                    PAYLOAD_KEY_DESCRIPTION: "Compare shadow outputs against baseline",
                    PAYLOAD_KEY_REQUIRED: True,
                    PAYLOAD_KEY_SUCCESS_CONDITION: "no critical divergence",
                },
            ]
        return common + rollout

    def _build_checkpoints(self, strategy: str) -> list[dict]:
        checkpoints = [
            {
                PAYLOAD_KEY_NAME: "readiness",
                PAYLOAD_KEY_COMMAND: "engine readiness",
                PAYLOAD_KEY_REQUIRED: True,
            },
            {
                PAYLOAD_KEY_NAME: EVIDENCE_SECTION_GATE,
                PAYLOAD_KEY_COMMAND: "engine gate",
                PAYLOAD_KEY_REQUIRED: True,
            },
            {
                PAYLOAD_KEY_NAME: PAYLOAD_KEY_SLO,
                PAYLOAD_KEY_COMMAND: "engine slo",
                PAYLOAD_KEY_REQUIRED: True,
            },
            {
                PAYLOAD_KEY_NAME: "doctor",
                PAYLOAD_KEY_COMMAND: "engine runbook doctor",
                PAYLOAD_KEY_REQUIRED: True,
            },
        ]
        if strategy == "canary":
            checkpoints.extend(
                [
                    {
                        PAYLOAD_KEY_NAME: "canary_10pct_slo",
                        PAYLOAD_KEY_COMMAND: "engine slo",
                        PAYLOAD_KEY_REQUIRED: True,
                    },
                    {
                        PAYLOAD_KEY_NAME: "canary_50pct_slo",
                        PAYLOAD_KEY_COMMAND: "engine slo",
                        PAYLOAD_KEY_REQUIRED: True,
                    },
                ]
            )
        if strategy == "shadow":
            checkpoints.append(
                {
                    PAYLOAD_KEY_NAME: "shadow_evidence",
                    PAYLOAD_KEY_COMMAND: "engine evidence build",
                    PAYLOAD_KEY_REQUIRED: True,
                }
            )
        return checkpoints

    def _build_rollback_triggers(self, strategy: str) -> list[dict]:
        triggers = [
            {
                PAYLOAD_KEY_NAME: "release_gate_block",
                PAYLOAD_KEY_CONDITION: "gate.decision == block",
                PAYLOAD_KEY_SEVERITY: FINDING_SEVERITY_CRITICAL,
            },
            {
                PAYLOAD_KEY_NAME: "slo_breach",
                PAYLOAD_KEY_CONDITION: "slo.status == breaching",
                PAYLOAD_KEY_SEVERITY: FINDING_SEVERITY_HIGH,
            },
            {
                PAYLOAD_KEY_NAME: "readiness_down",
                PAYLOAD_KEY_CONDITION: "health.readiness != ready",
                PAYLOAD_KEY_SEVERITY: FINDING_SEVERITY_CRITICAL,
            },
            {
                PAYLOAD_KEY_NAME: "circuit_open",
                PAYLOAD_KEY_CONDITION: f"{CYCLES_CIRCUIT_OPEN} > 0",
                PAYLOAD_KEY_SEVERITY: FINDING_SEVERITY_HIGH,
            },
        ]
        if strategy == "canary":
            triggers.append(
                {
                    PAYLOAD_KEY_NAME: "canary_error_budget_exhausted",
                    PAYLOAD_KEY_CONDITION: "error_budget.exhausted_count > 0",
                    PAYLOAD_KEY_SEVERITY: FINDING_SEVERITY_CRITICAL,
                }
            )
        return triggers

    def _build_commands(self, version: str, strategy: str, *, validation_mode: str) -> list[str]:
        mode_arg = f"--validation-mode {validation_mode}"
        return [
            f"python -m apps.engine.cli gate {mode_arg} --output reports/gate_{version}.json",
            (
                f"python -m apps.engine.cli evidence build {mode_arg}"
                f" --output-dir reports/evidence --label deploy_{version}_{strategy}"
            ),
            f"python -m apps.engine.cli runbook preflight {mode_arg}",
            f"python -m apps.engine.cli slo --output reports/slo_{version}.json",
        ]
