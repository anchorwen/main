"""Rollback drill execution.

RollbackDrillService produces a safe dry-run rollback exercise record.
It does not mutate infrastructure; it validates rollback prerequisites,
constructs steps, evaluates checkpoints, and records risks.
"""

from datetime import UTC, datetime
from pathlib import Path

from core.contracts.domain_keys import (
    HEALTH_STATUS_ALIVE,
    HEALTH_STATUS_READY,
    PAYLOAD_KEY_CHECKPOINT_COUNT,
    PAYLOAD_KEY_CHECKPOINTS,
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_DESCRIPTION,
    PAYLOAD_KEY_DETAIL,
    PAYLOAD_KEY_DRY_RUN,
    PAYLOAD_KEY_ERROR,
    PAYLOAD_KEY_FAILED_CHECKPOINTS,
    PAYLOAD_KEY_FAILED_COUNT,
    PAYLOAD_KEY_FAILED_OBJECTIVES,
    PAYLOAD_KEY_FAILED_PREREQUISITES,
    PAYLOAD_KEY_FINISHED_AT,
    PAYLOAD_KEY_LEVEL,
    PAYLOAD_KEY_MITIGATION,
    PAYLOAD_KEY_NAME,
    PAYLOAD_KEY_ORDER,
    PAYLOAD_KEY_OUTPUT_PATH,
    PAYLOAD_KEY_PASSED,
    PAYLOAD_KEY_PREREQUISITE_COUNT,
    PAYLOAD_KEY_PREREQUISITES,
    PAYLOAD_KEY_READY,
    PAYLOAD_KEY_REASON,
    PAYLOAD_KEY_RECOMMENDATION,
    PAYLOAD_KEY_RISK_COUNT,
    PAYLOAD_KEY_RISKS,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_STARTED_AT,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_STEP_COUNT,
    PAYLOAD_KEY_STEPS,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_VERIFIED,
    PAYLOAD_KEY_VERSION,
    RELEASE_PIPELINE_DEFAULT_VERSION,
    RELEASE_PIPELINE_GATE_DECISION_ALLOW,
    RELEASE_PIPELINE_GATE_DECISION_BLOCK,
    RELEASE_PIPELINE_GATE_DECISION_WARN,
    RELEASE_PIPELINE_STATUS_FAILED,
    RELEASE_PIPELINE_STATUS_PASSED,
    ROLLBACK_CHECKPOINT_DOCTOR,
    ROLLBACK_CHECKPOINT_LIVENESS,
    ROLLBACK_CHECKPOINT_READINESS,
    ROLLBACK_CHECKPOINT_SLO,
    ROLLBACK_DRILL_DEFAULT_REASON,
    ROLLBACK_EVIDENCE_STATUS_NOT_PROVIDED,
    ROLLBACK_PREREQUISITE_EVIDENCE_MANIFEST_OPTIONAL,
    ROLLBACK_PREREQUISITE_EVIDENCE_MANIFEST_VERIFIED,
    ROLLBACK_PREREQUISITE_READINESS_REPORT_AVAILABLE,
    ROLLBACK_PREREQUISITE_RELEASE_GATE_AVAILABLE,
    ROLLBACK_RECOMMENDATION_FIX_PREREQUISITES,
    ROLLBACK_RECOMMENDATION_READY,
    ROLLBACK_RISK_CHECKPOINT_FAILURES,
    ROLLBACK_RISK_DRY_RUN_ONLY,
    ROLLBACK_RISK_LEVEL_HIGH,
    ROLLBACK_RISK_LEVEL_LOW,
    ROLLBACK_RISK_MISSING_PREREQUISITES,
    ROLLBACK_STEP_ANNOUNCE,
    ROLLBACK_STEP_CAPTURE_EVIDENCE,
    ROLLBACK_STEP_FREEZE_DEPLOYMENTS,
    ROLLBACK_STEP_RESTART_SERVICES,
    ROLLBACK_STEP_RESTORE_ARTIFACT,
    ROLLBACK_STEP_VERIFY_HEALTH,
    SLO_STATUS_HEALTHY,
)
from core.deployment.atomic_file_writer import atomic_write_json
from core.deployment.schema_versions import SCHEMA_RELEASE_READINESS, SCHEMA_ROLLBACK_DRILL
from core.deployment.validation_mode import resolve_validation_mode


class RollbackDrillService:
    """Builds and executes dry-run rollback drills."""

    def __init__(self, container):
        self._container = container

    def run(
        self,
        *,
        version: str = RELEASE_PIPELINE_DEFAULT_VERSION,
        reason: str = ROLLBACK_DRILL_DEFAULT_REASON,
        evidence_manifest: str | None = None,
        output: str | None = None,
        validation_mode: str | None = None,
    ) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        started_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
        prerequisites = self._build_prerequisites(
            evidence_manifest, validation_mode=validation_mode
        )
        steps = self._build_steps(version, reason)
        checkpoints = self._build_checkpoints(validation_mode=validation_mode)
        risks = self._build_risks(prerequisites, checkpoints)
        passed = all(p[PAYLOAD_KEY_PASSED] for p in prerequisites) and all(
            c[PAYLOAD_KEY_PASSED] for c in checkpoints
        )
        result = {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_ROLLBACK_DRILL,
            PAYLOAD_KEY_STARTED_AT: started_at,
            PAYLOAD_KEY_FINISHED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
            PAYLOAD_KEY_VERSION: version,
            PAYLOAD_KEY_REASON: reason,
            PAYLOAD_KEY_DRY_RUN: True,
            PAYLOAD_KEY_STATUS: RELEASE_PIPELINE_STATUS_PASSED
            if passed
            else RELEASE_PIPELINE_STATUS_FAILED,
            PAYLOAD_KEY_PASSED: passed,
            PAYLOAD_KEY_SUMMARY: {
                PAYLOAD_KEY_PREREQUISITE_COUNT: len(prerequisites),
                PAYLOAD_KEY_CHECKPOINT_COUNT: len(checkpoints),
                PAYLOAD_KEY_STEP_COUNT: len(steps),
                PAYLOAD_KEY_RISK_COUNT: len(risks),
                PAYLOAD_KEY_FAILED_PREREQUISITES: [
                    p[PAYLOAD_KEY_NAME] for p in prerequisites if not p[PAYLOAD_KEY_PASSED]
                ],
                PAYLOAD_KEY_FAILED_CHECKPOINTS: [
                    c[PAYLOAD_KEY_NAME] for c in checkpoints if not c[PAYLOAD_KEY_PASSED]
                ],
            },
            PAYLOAD_KEY_PREREQUISITES: prerequisites,
            PAYLOAD_KEY_STEPS: steps,
            PAYLOAD_KEY_CHECKPOINTS: checkpoints,
            PAYLOAD_KEY_RISKS: risks,
            PAYLOAD_KEY_RECOMMENDATION: ROLLBACK_RECOMMENDATION_READY
            if passed
            else ROLLBACK_RECOMMENDATION_FIX_PREREQUISITES,
        }
        if output:
            result[PAYLOAD_KEY_OUTPUT_PATH] = self.save_result(result, output)
        return result

    def save_result(self, result: dict, path: str) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, result)
        return str(target)

    def _build_prerequisites(
        self,
        evidence_manifest: str | None,
        *,
        validation_mode: str | None = None,
    ) -> list[dict]:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        gate = self._container.release_gate.evaluate(strict=False, validation_mode=validation_mode)
        readiness = self._container.release_readiness.build_report(validation_mode=validation_mode)
        evidence = self._verify_evidence(evidence_manifest)
        return [
            {
                PAYLOAD_KEY_NAME: ROLLBACK_PREREQUISITE_RELEASE_GATE_AVAILABLE,
                PAYLOAD_KEY_PASSED: gate.get(PAYLOAD_KEY_DECISION)
                in {
                    RELEASE_PIPELINE_GATE_DECISION_ALLOW,
                    RELEASE_PIPELINE_GATE_DECISION_WARN,
                    RELEASE_PIPELINE_GATE_DECISION_BLOCK,
                },
                PAYLOAD_KEY_DETAIL: {PAYLOAD_KEY_DECISION: gate.get(PAYLOAD_KEY_DECISION)},
            },
            {
                PAYLOAD_KEY_NAME: ROLLBACK_PREREQUISITE_READINESS_REPORT_AVAILABLE,
                PAYLOAD_KEY_PASSED: readiness.get(PAYLOAD_KEY_SCHEMA_VERSION)
                == SCHEMA_RELEASE_READINESS,
                PAYLOAD_KEY_DETAIL: {PAYLOAD_KEY_READY: readiness.get(PAYLOAD_KEY_READY)},
            },
            evidence,
        ]

    def _verify_evidence(self, evidence_manifest: str | None) -> dict:
        if evidence_manifest is None:
            return {
                PAYLOAD_KEY_NAME: ROLLBACK_PREREQUISITE_EVIDENCE_MANIFEST_OPTIONAL,
                PAYLOAD_KEY_PASSED: True,
                PAYLOAD_KEY_DETAIL: {PAYLOAD_KEY_STATUS: ROLLBACK_EVIDENCE_STATUS_NOT_PROVIDED},
            }
        try:
            verification = self._container.evidence_bundle.verify_bundle(evidence_manifest)
            return {
                PAYLOAD_KEY_NAME: ROLLBACK_PREREQUISITE_EVIDENCE_MANIFEST_VERIFIED,
                PAYLOAD_KEY_PASSED: verification.get(PAYLOAD_KEY_VERIFIED, False),
                PAYLOAD_KEY_DETAIL: {
                    PAYLOAD_KEY_FAILED_COUNT: verification.get(PAYLOAD_KEY_FAILED_COUNT, 0)
                },
            }
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
            return {
                PAYLOAD_KEY_NAME: ROLLBACK_PREREQUISITE_EVIDENCE_MANIFEST_VERIFIED,
                PAYLOAD_KEY_PASSED: False,
                PAYLOAD_KEY_DETAIL: {PAYLOAD_KEY_ERROR: str(exc)},
            }

    def _build_steps(self, version: str, reason: str) -> list[dict]:
        return [
            {
                PAYLOAD_KEY_ORDER: 1,
                PAYLOAD_KEY_NAME: ROLLBACK_STEP_ANNOUNCE,
                PAYLOAD_KEY_DRY_RUN: True,
                PAYLOAD_KEY_DESCRIPTION: f"Announce rollback for {version}: {reason}",
            },
            {
                PAYLOAD_KEY_ORDER: 2,
                PAYLOAD_KEY_NAME: ROLLBACK_STEP_FREEZE_DEPLOYMENTS,
                PAYLOAD_KEY_DRY_RUN: True,
                PAYLOAD_KEY_DESCRIPTION: "Block new deployment executions while rollback is active",
            },
            {
                PAYLOAD_KEY_ORDER: 3,
                PAYLOAD_KEY_NAME: ROLLBACK_STEP_RESTORE_ARTIFACT,
                PAYLOAD_KEY_DRY_RUN: True,
                PAYLOAD_KEY_DESCRIPTION: "Restore previous known-good artifact or config snapshot",
            },
            {
                PAYLOAD_KEY_ORDER: 4,
                PAYLOAD_KEY_NAME: ROLLBACK_STEP_RESTART_SERVICES,
                PAYLOAD_KEY_DRY_RUN: True,
                PAYLOAD_KEY_DESCRIPTION: "Restart affected services after rollback restore",
            },
            {
                PAYLOAD_KEY_ORDER: 5,
                PAYLOAD_KEY_NAME: ROLLBACK_STEP_VERIFY_HEALTH,
                PAYLOAD_KEY_DRY_RUN: True,
                PAYLOAD_KEY_DESCRIPTION: "Run readiness, liveness, SLO, and doctor checks",
            },
            {
                PAYLOAD_KEY_ORDER: 6,
                PAYLOAD_KEY_NAME: ROLLBACK_STEP_CAPTURE_EVIDENCE,
                PAYLOAD_KEY_DRY_RUN: True,
                PAYLOAD_KEY_DESCRIPTION: "Generate post-rollback evidence bundle",
            },
        ]

    def _build_checkpoints(self, *, validation_mode: str | None = None) -> list[dict]:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        readiness = self._container.health_check.readiness()
        liveness = self._container.health_check.liveness()
        slo = self._container.slo_service.evaluate()
        doctor = self._container.runbook_engine.doctor(validation_mode=validation_mode)
        return [
            {
                PAYLOAD_KEY_NAME: ROLLBACK_CHECKPOINT_READINESS,
                PAYLOAD_KEY_PASSED: readiness.get(PAYLOAD_KEY_STATUS) == HEALTH_STATUS_READY,
                PAYLOAD_KEY_DETAIL: {PAYLOAD_KEY_STATUS: readiness.get(PAYLOAD_KEY_STATUS)},
            },
            {
                PAYLOAD_KEY_NAME: ROLLBACK_CHECKPOINT_LIVENESS,
                PAYLOAD_KEY_PASSED: liveness.get(PAYLOAD_KEY_STATUS) == HEALTH_STATUS_ALIVE,
                PAYLOAD_KEY_DETAIL: {PAYLOAD_KEY_STATUS: liveness.get(PAYLOAD_KEY_STATUS)},
            },
            {
                PAYLOAD_KEY_NAME: ROLLBACK_CHECKPOINT_SLO,
                PAYLOAD_KEY_PASSED: slo.get(PAYLOAD_KEY_STATUS) == SLO_STATUS_HEALTHY,
                PAYLOAD_KEY_DETAIL: {
                    PAYLOAD_KEY_STATUS: slo.get(PAYLOAD_KEY_STATUS),
                    PAYLOAD_KEY_FAILED_OBJECTIVES: slo.get(PAYLOAD_KEY_FAILED_OBJECTIVES, []),
                },
            },
            {
                PAYLOAD_KEY_NAME: ROLLBACK_CHECKPOINT_DOCTOR,
                PAYLOAD_KEY_PASSED: doctor.get(PAYLOAD_KEY_PASSED, False),
                PAYLOAD_KEY_DETAIL: {PAYLOAD_KEY_STATUS: doctor.get(PAYLOAD_KEY_STATUS)},
            },
        ]

    def _build_risks(self, prerequisites: list[dict], checkpoints: list[dict]) -> list[dict]:
        risks = []
        if any(not p[PAYLOAD_KEY_PASSED] for p in prerequisites):
            risks.append(
                {
                    PAYLOAD_KEY_LEVEL: ROLLBACK_RISK_LEVEL_HIGH,
                    PAYLOAD_KEY_NAME: ROLLBACK_RISK_MISSING_PREREQUISITES,
                    PAYLOAD_KEY_MITIGATION: "Resolve failed prerequisites before rollback",
                }
            )
        failed_checkpoints = [c for c in checkpoints if not c[PAYLOAD_KEY_PASSED]]
        if failed_checkpoints:
            risks.append(
                {
                    PAYLOAD_KEY_LEVEL: ROLLBACK_RISK_LEVEL_HIGH,
                    PAYLOAD_KEY_NAME: ROLLBACK_RISK_CHECKPOINT_FAILURES,
                    PAYLOAD_KEY_MITIGATION: "Investigate failed rollback verification checkpoints",
                }
            )
        if not risks:
            risks.append(
                {
                    PAYLOAD_KEY_LEVEL: ROLLBACK_RISK_LEVEL_LOW,
                    PAYLOAD_KEY_NAME: ROLLBACK_RISK_DRY_RUN_ONLY,
                    PAYLOAD_KEY_MITIGATION: "Real rollback still requires operator confirmation",
                }
            )
        return risks
