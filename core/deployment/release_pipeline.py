"""End-to-end release pipeline orchestration.

ReleasePipelineService runs a safe dry-run release pipeline that combines
release gate, evidence bundle, deployment plan, deployment execution,
rollback drill, operations timeline recording, and postmortem summary.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from core.contracts.domain_keys import (
    ARTIFACT_BASE_DIR,
    ARTIFACT_DEPLOYMENT_EXECUTION,
    ARTIFACT_DEPLOYMENT_PLAN,
    ARTIFACT_EVIDENCE_MANIFEST,
    ARTIFACT_EVIDENCE_SUMMARY,
    ARTIFACT_FINAL_AUDIT,
    ARTIFACT_GATE,
    ARTIFACT_OPERATIONS_TIMELINE,
    ARTIFACT_OPS_MATURITY,
    ARTIFACT_POSTMORTEM_REPORT,
    ARTIFACT_RELEASE_PIPELINE,
    ARTIFACT_ROLLBACK_DRILL,
    EVIDENCE_SECTION_ALPHA_BUDGET_USAGE,
    PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE,
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_EVENT_COUNT,
    PAYLOAD_KEY_EVIDENCE_COUNT,
    PAYLOAD_KEY_EVIDENCE_MANIFEST,
    PAYLOAD_KEY_EXECUTABLE,
    PAYLOAD_KEY_GATE_DECISION,
    PAYLOAD_KEY_MANIFEST_PATH,
    PAYLOAD_KEY_MATURITY_SCORE,
    PAYLOAD_KEY_MISSING_EVIDENCE_COUNT,
    PAYLOAD_KEY_READY_FOR_PRODUCTION,
    PAYLOAD_KEY_RECORD_COUNT,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_SECTIONS,
    PAYLOAD_KEY_SOURCE,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_SUMMARY,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_WARNING_COUNT,
    PAYLOAD_KEY_WARNING_RELEASE_COUNT,
    PAYLOAD_KEY_WARNING_TOTAL,
    PIPELINE_STAGE_DEPLOYMENT_EXECUTION,
    PIPELINE_STAGE_DEPLOYMENT_PLAN,
    PIPELINE_STAGE_EVIDENCE,
    PIPELINE_STAGE_FINAL_AUDIT,
    PIPELINE_STAGE_GATE,
    PIPELINE_STAGE_OPS_MATURITY,
    PIPELINE_STAGE_POSTMORTEM_REPORT,
    PIPELINE_STAGE_ROLLBACK_DRILL,
    RELEASE_PIPELINE_DEFAULT_ACTOR,
    RELEASE_PIPELINE_DEFAULT_STRATEGY,
    RELEASE_PIPELINE_DEFAULT_VERSION,
    RELEASE_PIPELINE_EVIDENCE_DIR,
    RELEASE_PIPELINE_FILE_DEPLOYMENT_EXECUTION,
    RELEASE_PIPELINE_FILE_DEPLOYMENT_PLAN,
    RELEASE_PIPELINE_FILE_EVIDENCE_SUMMARY,
    RELEASE_PIPELINE_FILE_FINAL_AUDIT,
    RELEASE_PIPELINE_FILE_GATE,
    RELEASE_PIPELINE_FILE_OPS_MATURITY,
    RELEASE_PIPELINE_FILE_POSTMORTEM_REPORT,
    RELEASE_PIPELINE_FILE_RESULT,
    RELEASE_PIPELINE_FILE_ROLLBACK_DRILL,
    RELEASE_PIPELINE_GATE_DECISION_ALLOW,
    RELEASE_PIPELINE_GATE_DECISION_BLOCK,
    RELEASE_PIPELINE_GATE_DECISION_WARN,
    RELEASE_PIPELINE_INCIDENT_KEY_INCIDENT,
    RELEASE_PIPELINE_INCIDENT_STATUS_CRITICAL,
    RELEASE_PIPELINE_KEY_ARTIFACTS,
    RELEASE_PIPELINE_KEY_DRY_RUN,
    RELEASE_PIPELINE_KEY_FINISHED_AT,
    RELEASE_PIPELINE_KEY_PASSED,
    RELEASE_PIPELINE_KEY_STAGES,
    RELEASE_PIPELINE_KEY_STARTED_AT,
    RELEASE_PIPELINE_KEY_STRATEGY,
    RELEASE_PIPELINE_KEY_SUMMARY,
    RELEASE_PIPELINE_KEY_VERSION,
    RELEASE_PIPELINE_OUTPUT_DIR,
    RELEASE_PIPELINE_POSTMORTEM_SEVERITY,
    RELEASE_PIPELINE_ROLLBACK_REASON,
    RELEASE_PIPELINE_SOURCE,
    RELEASE_PIPELINE_STAGE_KEY_DETAIL,
    RELEASE_PIPELINE_STAGE_KEY_NAME,
    RELEASE_PIPELINE_STAGE_KEY_PASSED,
    RELEASE_PIPELINE_STATUS_BLOCKED,
    RELEASE_PIPELINE_STATUS_FAILED,
    RELEASE_PIPELINE_STATUS_PASSED,
    RELEASE_PIPELINE_STATUS_WARNING,
    RELEASE_PIPELINE_SUMMARY_ALPHA_BUDGET_STATUS,
    RELEASE_PIPELINE_SUMMARY_ALPHA_BUDGET_WARNING_TOTAL,
    RELEASE_PIPELINE_SUMMARY_EXECUTION_STATUS,
    RELEASE_PIPELINE_SUMMARY_FINAL_AUDIT_READY,
    RELEASE_PIPELINE_SUMMARY_GATE_DECISION,
    RELEASE_PIPELINE_SUMMARY_OPS_MATURITY_SCORE,
    RELEASE_PIPELINE_SUMMARY_PLAN_STATUS,
    RELEASE_PIPELINE_SUMMARY_POSTMORTEM_STATUS,
    RELEASE_PIPELINE_SUMMARY_ROLLBACK_STATUS,
    RELEASE_PIPELINE_SUMMARY_TIMELINE_EVENT_COUNT,
)
from core.deployment.governance_summary import extract_governance_summary
from core.deployment.schema_versions import (
    SCHEMA_ALPHA_BUDGET_GOVERNANCE_EVENT,
    SCHEMA_RELEASE_PIPELINE,
)
from core.deployment.validation_mode import resolve_validation_mode


class ReleasePipelineService:
    """Runs the full dry-run release workflow."""

    def __init__(self, container):
        self._container = container

    def run(
        self,
        *,
        version: str = RELEASE_PIPELINE_DEFAULT_VERSION,
        strategy: str = RELEASE_PIPELINE_DEFAULT_STRATEGY,
        output_dir: str | None = None,
        strict_gate: bool = True,
        actor: str = RELEASE_PIPELINE_DEFAULT_ACTOR,
        alpha_budget_usage_report: dict | None = None,
        validation_mode: str | None = None,
    ) -> dict:
        validation_mode = resolve_validation_mode(self._container, validation_mode)
        started_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
        base = (
            Path(output_dir)
            if output_dir
            else Path(self._container.config.base_dir) / RELEASE_PIPELINE_OUTPUT_DIR
        )
        base.mkdir(parents=True, exist_ok=True)

        gate = self._container.release_gate.evaluate(
            strict=strict_gate,
            alpha_budget_usage_report=alpha_budget_usage_report,
            validation_mode=validation_mode,
        )
        gate_path = self._write_json(base / RELEASE_PIPELINE_FILE_GATE, gate)
        self._container.operations_timeline.record_release_gate(gate, actor=actor)

        evidence = self._container.evidence_bundle.build_bundle(
            str(base / RELEASE_PIPELINE_EVIDENCE_DIR),
            label=f"release_{version}_{strategy}",
            alpha_budget_usage_report=alpha_budget_usage_report,
            validation_mode=validation_mode,
        )
        evidence_path = self._write_json(base / RELEASE_PIPELINE_FILE_EVIDENCE_SUMMARY, evidence)
        self._container.operations_timeline.record_evidence_bundle(evidence, actor=actor)
        alpha_budget_event = self._alpha_budget_governance_event(
            source=RELEASE_PIPELINE_SOURCE,
            alpha_budget_usage_report=alpha_budget_usage_report,
            gate=gate,
            evidence=evidence,
            validation_mode=validation_mode,
        )
        self._container.operations_timeline.record_alpha_budget_governance(
            alpha_budget_event, actor=actor
        )

        plan = self._container.deployment_plan.build_plan(
            version=version,
            strategy=strategy,
            evidence_dir=None,
            strict_gate=strict_gate,
            alpha_budget_usage_report=alpha_budget_usage_report,
            validation_mode=validation_mode,
        )
        plan_path = self._write_json(base / RELEASE_PIPELINE_FILE_DEPLOYMENT_PLAN, plan)

        execution = self._container.deployment_executor.execute(
            plan,
            dry_run=True,
            validation_mode=validation_mode,
        )
        execution_path = self._write_json(
            base / RELEASE_PIPELINE_FILE_DEPLOYMENT_EXECUTION, execution
        )
        self._container.operations_timeline.record_deployment_execution(execution, actor=actor)

        rollback = self._container.rollback_drill.run(
            version=version,
            reason=RELEASE_PIPELINE_ROLLBACK_REASON,
            evidence_manifest=evidence[PAYLOAD_KEY_MANIFEST_PATH],
            validation_mode=validation_mode,
        )
        rollback_path = self._write_json(base / RELEASE_PIPELINE_FILE_ROLLBACK_DRILL, rollback)
        self._container.operations_timeline.record_rollback_drill(rollback, actor=actor)

        postmortem = self._container.postmortem_report.generate(
            incident_id=f"release-{version}-{strategy}",
            title=f"Release Pipeline {version} {strategy}",
            severity=RELEASE_PIPELINE_POSTMORTEM_SEVERITY,
            validation_mode=validation_mode,
        )
        postmortem_path = self._write_json(
            base / RELEASE_PIPELINE_FILE_POSTMORTEM_REPORT, postmortem
        )
        final_audit = self._container.final_audit.build_report(validation_mode=validation_mode)
        final_audit_path = self._write_json(base / RELEASE_PIPELINE_FILE_FINAL_AUDIT, final_audit)
        ops_maturity = self._container.ops_maturity.evaluate(validation_mode=validation_mode)
        ops_maturity_path = self._write_json(
            base / RELEASE_PIPELINE_FILE_OPS_MATURITY, ops_maturity
        )

        status = self._status(gate, plan, execution, rollback, postmortem)
        result = {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_RELEASE_PIPELINE,
            PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
            RELEASE_PIPELINE_KEY_STARTED_AT: started_at,
            RELEASE_PIPELINE_KEY_FINISHED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            RELEASE_PIPELINE_KEY_VERSION: version,
            RELEASE_PIPELINE_KEY_STRATEGY: strategy,
            RELEASE_PIPELINE_KEY_DRY_RUN: True,
            PAYLOAD_KEY_STATUS: status,
            RELEASE_PIPELINE_KEY_PASSED: status == RELEASE_PIPELINE_STATUS_PASSED,
            RELEASE_PIPELINE_KEY_SUMMARY: {
                RELEASE_PIPELINE_SUMMARY_GATE_DECISION: gate.get(PAYLOAD_KEY_DECISION),
                RELEASE_PIPELINE_SUMMARY_PLAN_STATUS: plan.get(PAYLOAD_KEY_STATUS),
                RELEASE_PIPELINE_SUMMARY_EXECUTION_STATUS: execution.get(PAYLOAD_KEY_STATUS),
                RELEASE_PIPELINE_SUMMARY_ROLLBACK_STATUS: rollback.get(PAYLOAD_KEY_STATUS),
                RELEASE_PIPELINE_SUMMARY_POSTMORTEM_STATUS: postmortem.get(
                    RELEASE_PIPELINE_INCIDENT_KEY_INCIDENT, {}
                ).get(PAYLOAD_KEY_STATUS),
                RELEASE_PIPELINE_SUMMARY_FINAL_AUDIT_READY: final_audit.get(
                    PAYLOAD_KEY_READY_FOR_PRODUCTION, False
                ),
                RELEASE_PIPELINE_SUMMARY_OPS_MATURITY_SCORE: ops_maturity.get(
                    PAYLOAD_KEY_MATURITY_SCORE, 0
                ),
                RELEASE_PIPELINE_SUMMARY_TIMELINE_EVENT_COUNT: (
                    self._container.operations_timeline.summarize().get(PAYLOAD_KEY_EVENT_COUNT)
                ),
                RELEASE_PIPELINE_SUMMARY_ALPHA_BUDGET_STATUS: alpha_budget_event.get(
                    PAYLOAD_KEY_STATUS
                ),
                RELEASE_PIPELINE_SUMMARY_ALPHA_BUDGET_WARNING_TOTAL: alpha_budget_event.get(
                    PAYLOAD_KEY_WARNING_TOTAL, 0
                ),
                **extract_governance_summary(final_audit.get(PAYLOAD_KEY_SUMMARY, {})),
            },
            RELEASE_PIPELINE_KEY_ARTIFACTS: {
                ARTIFACT_BASE_DIR: str(base),
                ARTIFACT_GATE: gate_path,
                ARTIFACT_EVIDENCE_SUMMARY: evidence_path,
                ARTIFACT_EVIDENCE_MANIFEST: evidence.get(PAYLOAD_KEY_MANIFEST_PATH),
                ARTIFACT_DEPLOYMENT_PLAN: plan_path,
                ARTIFACT_DEPLOYMENT_EXECUTION: execution_path,
                ARTIFACT_ROLLBACK_DRILL: rollback_path,
                ARTIFACT_POSTMORTEM_REPORT: postmortem_path,
                ARTIFACT_FINAL_AUDIT: final_audit_path,
                ARTIFACT_OPS_MATURITY: ops_maturity_path,
                ARTIFACT_OPERATIONS_TIMELINE: self._container.operations_timeline.path,
            },
            PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE: alpha_budget_event,
            RELEASE_PIPELINE_KEY_STAGES: [
                self._stage(
                    PIPELINE_STAGE_GATE,
                    gate.get(PAYLOAD_KEY_DECISION)
                    in {RELEASE_PIPELINE_GATE_DECISION_ALLOW, RELEASE_PIPELINE_GATE_DECISION_WARN},
                    gate.get(PAYLOAD_KEY_DECISION),
                ),
                self._stage(
                    PIPELINE_STAGE_EVIDENCE,
                    bool(evidence.get(PAYLOAD_KEY_MANIFEST_PATH)),
                    evidence.get(PAYLOAD_KEY_MANIFEST_PATH),
                ),
                self._stage(
                    PIPELINE_STAGE_DEPLOYMENT_PLAN,
                    plan.get(PAYLOAD_KEY_EXECUTABLE, False),
                    plan.get(PAYLOAD_KEY_STATUS),
                ),
                self._stage(
                    PIPELINE_STAGE_DEPLOYMENT_EXECUTION,
                    execution.get(RELEASE_PIPELINE_KEY_PASSED, False),
                    execution.get(PAYLOAD_KEY_STATUS),
                ),
                self._stage(
                    PIPELINE_STAGE_ROLLBACK_DRILL,
                    rollback.get(RELEASE_PIPELINE_KEY_PASSED, False),
                    rollback.get(PAYLOAD_KEY_STATUS),
                ),
                self._stage(
                    PIPELINE_STAGE_POSTMORTEM_REPORT,
                    postmortem.get(RELEASE_PIPELINE_INCIDENT_KEY_INCIDENT, {}).get(
                        PAYLOAD_KEY_STATUS
                    )
                    != RELEASE_PIPELINE_INCIDENT_STATUS_CRITICAL,
                    postmortem.get(RELEASE_PIPELINE_INCIDENT_KEY_INCIDENT, {}).get(
                        PAYLOAD_KEY_STATUS
                    ),
                ),
                self._stage(
                    PIPELINE_STAGE_FINAL_AUDIT,
                    final_audit.get(PAYLOAD_KEY_READY_FOR_PRODUCTION, False),
                    final_audit.get(PAYLOAD_KEY_READY_FOR_PRODUCTION, False),
                ),
                self._stage(
                    PIPELINE_STAGE_OPS_MATURITY,
                    ops_maturity.get(PAYLOAD_KEY_MATURITY_SCORE, 0) >= 60.0,
                    ops_maturity.get(PAYLOAD_KEY_MATURITY_SCORE, 0),
                ),
            ],
        }
        self._write_json(base / RELEASE_PIPELINE_FILE_RESULT, result)
        result[RELEASE_PIPELINE_KEY_ARTIFACTS][ARTIFACT_RELEASE_PIPELINE] = str(
            base / RELEASE_PIPELINE_FILE_RESULT
        )
        return result

    def _alpha_budget_governance_event(
        self,
        *,
        source: str,
        alpha_budget_usage_report: dict | None,
        gate: dict,
        evidence: dict,
        validation_mode: str,
    ) -> dict:
        warning_count = int((alpha_budget_usage_report or {}).get(PAYLOAD_KEY_WARNING_COUNT, 0))
        evidence_sections = evidence.get(PAYLOAD_KEY_SECTIONS, []) or []
        evidence_present = EVIDENCE_SECTION_ALPHA_BUDGET_USAGE in evidence_sections
        status = RELEASE_PIPELINE_STATUS_PASSED
        if alpha_budget_usage_report is None or not evidence_present:
            status = RELEASE_PIPELINE_STATUS_WARNING
        if warning_count > 0:
            status = RELEASE_PIPELINE_STATUS_WARNING
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_ALPHA_BUDGET_GOVERNANCE_EVENT,
            PAYLOAD_KEY_VALIDATION_MODE: validation_mode,
            PAYLOAD_KEY_SOURCE: source,
            PAYLOAD_KEY_STATUS: status,
            PAYLOAD_KEY_RECORD_COUNT: 1 if alpha_budget_usage_report is not None else 0,
            PAYLOAD_KEY_EVIDENCE_COUNT: 1 if evidence_present else 0,
            PAYLOAD_KEY_MISSING_EVIDENCE_COUNT: 0 if evidence_present else 1,
            PAYLOAD_KEY_WARNING_TOTAL: warning_count,
            PAYLOAD_KEY_WARNING_RELEASE_COUNT: 1 if warning_count > 0 else 0,
            PAYLOAD_KEY_GATE_DECISION: gate.get(PAYLOAD_KEY_DECISION),
            PAYLOAD_KEY_EVIDENCE_MANIFEST: evidence.get(PAYLOAD_KEY_MANIFEST_PATH),
        }

    def save_result(self, result: dict, path: str) -> str:
        return self._write_json(Path(path), result)

    def _status(
        self, gate: dict, plan: dict, execution: dict, rollback: dict, postmortem: dict
    ) -> str:
        if gate.get(PAYLOAD_KEY_DECISION) == RELEASE_PIPELINE_GATE_DECISION_BLOCK or not plan.get(
            PAYLOAD_KEY_EXECUTABLE, False
        ):
            return RELEASE_PIPELINE_STATUS_BLOCKED
        if not execution.get(RELEASE_PIPELINE_KEY_PASSED, False) or not rollback.get(
            RELEASE_PIPELINE_KEY_PASSED, False
        ):
            return RELEASE_PIPELINE_STATUS_FAILED
        if (
            postmortem.get(RELEASE_PIPELINE_INCIDENT_KEY_INCIDENT, {}).get(PAYLOAD_KEY_STATUS)
            == RELEASE_PIPELINE_INCIDENT_STATUS_CRITICAL
        ):
            return RELEASE_PIPELINE_STATUS_FAILED
        return RELEASE_PIPELINE_STATUS_PASSED

    def _stage(self, name: str, passed: bool, detail) -> dict:
        return {
            RELEASE_PIPELINE_STAGE_KEY_NAME: name,
            RELEASE_PIPELINE_STAGE_KEY_PASSED: bool(passed),
            RELEASE_PIPELINE_STAGE_KEY_DETAIL: detail,
        }

    def _write_json(self, path: Path, payload: dict) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return str(path)
