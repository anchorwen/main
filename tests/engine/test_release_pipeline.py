"""Release pipeline service and CLI tests."""

import json
from pathlib import Path

from apps.engine.cli import main
from core.contracts.domain_keys import (
    ARTIFACT_DEPLOYMENT_EXECUTION,
    ARTIFACT_DEPLOYMENT_PLAN,
    ARTIFACT_EVIDENCE_MANIFEST,
    ARTIFACT_EVIDENCE_SUMMARY,
    ARTIFACT_FINAL_AUDIT,
    ARTIFACT_GATE,
    ARTIFACT_OPS_MATURITY,
    ARTIFACT_POSTMORTEM_REPORT,
    ARTIFACT_RELEASE_PIPELINE,
    ARTIFACT_ROLLBACK_DRILL,
    EVIDENCE_SECTION_ALPHA_BUDGET_USAGE,
    PAYLOAD_KEY_GOVERNANCE_FOCUS,
    PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT,
    PAYLOAD_KEY_VALIDATION_MODE,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.schema_versions import SCHEMA_RELEASE_PIPELINE
from core.deployment.service_container import ServiceContainer
from core.observability.metric_names import CYCLES_ERRORS, CYCLES_TOTAL
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE_REPORT


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


class TestReleasePipelineService:
    def test_run_standard_pipeline_passes(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.release_pipeline.run(version="1.0.0", output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        assert result["schema_version"] == SCHEMA_RELEASE_PIPELINE
        assert result["status"] == "passed"
        assert result["passed"] is True
        assert result["version"] == "1.0.0"
        assert Path(result["artifacts"][ARTIFACT_RELEASE_PIPELINE]).exists()

    def test_run_pipeline_accepts_fast_validation_mode(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.release_pipeline.run(  # type: ignore[reportOptionalMemberAccess]
            version="1.0.0",
            output_dir=str(tmp_path / "pipeline"),
            validation_mode="fast",
        )
        assert result["schema_version"] == SCHEMA_RELEASE_PIPELINE
        assert result[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        gate = json.loads(Path(result["artifacts"][ARTIFACT_GATE]).read_text(encoding="utf-8"))
        plan = json.loads(
            Path(result["artifacts"][ARTIFACT_DEPLOYMENT_PLAN]).read_text(encoding="utf-8")
        )
        execution = json.loads(
            Path(result["artifacts"][ARTIFACT_DEPLOYMENT_EXECUTION]).read_text(encoding="utf-8")
        )
        final_audit = json.loads(
            Path(result["artifacts"][ARTIFACT_FINAL_AUDIT]).read_text(encoding="utf-8")
        )
        assert gate[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        assert plan[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        assert execution[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        assert final_audit[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        assert result["alpha_budget_governance"][PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in result["summary"]
        assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in result["summary"]

    def test_pipeline_summary_normalizes_malformed_final_audit_governance_fields(self, tmp_path):
        c = _container(tmp_path / "data")
        original_build_report = c.final_audit.build_report  # type: ignore[reportOptionalMemberAccess]

        def _malformed_build_report(*, validation_mode=None):
            report = original_build_report(validation_mode=validation_mode)
            report["summary"]["governance_focus"] = [{"name": "ok"}, {"status": "warn"}, "bad", 9]
            report["summary"]["governance_warning_count"] = "6"
            return report

        c.final_audit.build_report = _malformed_build_report  # type: ignore[reportOptionalMemberAccess]
        result = c.release_pipeline.run(version="1.0.3", output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        assert result["summary"][PAYLOAD_KEY_GOVERNANCE_FOCUS] == [
            {"name": "ok"},
            {"status": "warn"},
        ]
        assert result["summary"][PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 1

    def test_run_canary_pipeline_passes(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.release_pipeline.run(strategy="canary", output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        assert result["strategy"] == "canary"
        assert result["passed"] is True
        assert result["summary"]["execution_status"] == "succeeded"

    def test_run_shadow_pipeline_passes(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.release_pipeline.run(strategy="shadow", output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        assert result["strategy"] == "shadow"
        assert result["passed"] is True

    def test_pipeline_creates_artifacts(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.release_pipeline.run(output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        for key in [
            ARTIFACT_GATE,
            ARTIFACT_EVIDENCE_SUMMARY,
            ARTIFACT_DEPLOYMENT_PLAN,
            ARTIFACT_DEPLOYMENT_EXECUTION,
            ARTIFACT_ROLLBACK_DRILL,
            ARTIFACT_POSTMORTEM_REPORT,
            ARTIFACT_FINAL_AUDIT,
            ARTIFACT_OPS_MATURITY,
        ]:
            assert Path(result["artifacts"][key]).exists()
        assert Path(result["artifacts"][ARTIFACT_EVIDENCE_MANIFEST]).exists()
        assert "final_audit_ready_for_production" in result["summary"]
        assert "ops_maturity_score" in result["summary"]

    def test_pipeline_records_timeline_events(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.release_pipeline.run(output_dir=str(tmp_path / "pipeline"), actor="ci")  # type: ignore[reportOptionalMemberAccess]
        summary = c.operations_timeline.summarize()  # type: ignore[reportOptionalMemberAccess]
        assert summary["event_count"] >= 4
        assert result["summary"]["timeline_event_count"] >= 4
        assert c.operations_timeline.list_events()[0]["actor"] == "ci"  # type: ignore[reportOptionalMemberAccess]

    def test_pipeline_blocks_on_gate(self, tmp_path):
        c = _container(tmp_path / "data")
        c.risk_service = None
        result = c.release_pipeline.run(output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        assert result["status"] == "blocked"
        assert result["passed"] is False
        assert result["summary"]["gate_decision"] == "block"

    def test_pipeline_fails_on_slo_breach_non_strict(self, tmp_path):
        c = _container(tmp_path / "data")
        c.metrics.inc(CYCLES_TOTAL, 100)  # type: ignore[reportOptionalMemberAccess]
        c.metrics.inc(CYCLES_ERRORS, 20)  # type: ignore[reportOptionalMemberAccess]
        result = c.release_pipeline.run(output_dir=str(tmp_path / "pipeline"), strict_gate=False)  # type: ignore[reportOptionalMemberAccess]
        assert result["status"] == "failed"
        assert result["passed"] is False
        assert result["summary"]["rollback_status"] == "failed"

    def test_pipeline_blocks_with_alpha_budget_usage_report_warnings(self, tmp_path):
        c = _container(tmp_path / "data")
        alpha_report = {
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": "2026-01-01",
            "alpha_count": 1,
            "warning_count": 1,
            "warnings": [
                {
                    "alpha_id": "alpha1",
                    "type": "daily_usage_high",
                    "usage_ratio": 0.8,
                    "threshold": 0.8,
                }
            ],
        }
        result = c.release_pipeline.run(  # type: ignore[reportOptionalMemberAccess]
            output_dir=str(tmp_path / "pipeline"),
            alpha_budget_usage_report=alpha_report,
        )
        assert result["status"] == "blocked"
        assert result["passed"] is False
        assert result["summary"]["gate_decision"] == "block"
        gate = json.loads(Path(result["artifacts"][ARTIFACT_GATE]).read_text(encoding="utf-8"))
        assert EVIDENCE_SECTION_ALPHA_BUDGET_USAGE in gate["summary"]["warning_signals"]
        manifest = json.loads(
            Path(result["artifacts"][ARTIFACT_EVIDENCE_MANIFEST]).read_text(encoding="utf-8")
        )
        assert EVIDENCE_SECTION_ALPHA_BUDGET_USAGE in manifest["summary"]["sections"]

    def test_pipeline_non_strict_carries_alpha_budget_usage_report(self, tmp_path):
        c = _container(tmp_path / "data")
        alpha_report = {
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": "2026-01-01",
            "alpha_count": 1,
            "warning_count": 1,
            "warnings": [
                {
                    "alpha_id": "alpha1",
                    "type": "daily_usage_high",
                    "usage_ratio": 0.8,
                    "threshold": 0.8,
                }
            ],
        }
        result = c.release_pipeline.run(  # type: ignore[reportOptionalMemberAccess]
            output_dir=str(tmp_path / "pipeline"),
            strict_gate=False,
            alpha_budget_usage_report=alpha_report,
        )
        assert result["summary"]["gate_decision"] == "warn"
        assert Path(result["artifacts"][ARTIFACT_DEPLOYMENT_PLAN]).exists()
        plan = json.loads(
            Path(result["artifacts"][ARTIFACT_DEPLOYMENT_PLAN]).read_text(encoding="utf-8")
        )
        assert plan["gate"]["decision"] == "warn"
        assert EVIDENCE_SECTION_ALPHA_BUDGET_USAGE in plan["gate"]["summary"]["warning_signals"]
        assert (
            Path(result["artifacts"][ARTIFACT_EVIDENCE_MANIFEST]).parent / "alpha_budget_usage.json"
        ).exists()

    def test_save_result(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.release_pipeline.run(output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        out = tmp_path / "summary.json"
        saved = c.release_pipeline.save_result(result, str(out))  # type: ignore[reportOptionalMemberAccess]
        assert saved == str(out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_RELEASE_PIPELINE

    def test_container_has_release_pipeline(self, tmp_path):
        c = _container(tmp_path)
        assert c.release_pipeline is not None


class TestReleasePipelineCLI:
    def test_cli_release_pipeline(self, tmp_path):
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "release-pipeline",
                "--output-dir",
                str(tmp_path / "pipeline"),
            ]
        )
        assert rc == 0

    def test_cli_release_pipeline_output(self, tmp_path):
        out = tmp_path / "pipeline_summary.json"
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "release-pipeline",
                "--version",
                "2.0.0",
                "--strategy",
                "canary",
                "--output-dir",
                str(tmp_path / "pipeline"),
                "--output",
                str(out),
            ]
        )
        assert rc == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["version"] == "2.0.0"
        assert payload["strategy"] == "canary"

    def test_cli_release_pipeline_shadow(self, tmp_path):
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "release-pipeline",
                "--strategy",
                "shadow",
                "--output-dir",
                str(tmp_path / "pipeline"),
            ]
        )
        assert rc == 0

    def test_cli_release_pipeline_fast_validation_mode(self, tmp_path):
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "release-pipeline",
                "--output-dir",
                str(tmp_path / "pipeline"),
                "--validation-mode",
                "fast",
            ]
        )
        assert rc in (0, 1)

    def test_cli_release_pipeline_test_env_force_metrics(self, tmp_path, capsys):
        out_dir = str(tmp_path / "pipeline")
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "--env",
                "test",
                "--force-metrics",
                "release-pipeline",
                "--output-dir",
                out_dir,
            ]
        )
        result = json.loads(capsys.readouterr().out)
        assert result["schema_version"] == SCHEMA_RELEASE_PIPELINE
        assert rc in (0, 1)

    def test_cli_release_pipeline_blocks_with_alpha_budget_usage_report(self, tmp_path, capsys):
        report_path = tmp_path / "alpha_budget_usage_report.json"
        report_path.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
                    "usage_date": "2026-01-01",
                    "alpha_count": 1,
                    "warning_count": 1,
                    "warnings": [
                        {
                            "alpha_id": "alpha1",
                            "type": "daily_usage_high",
                            "usage_ratio": 0.8,
                            "threshold": 0.8,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "release-pipeline",
                "--output-dir",
                str(tmp_path / "pipeline"),
                "--alpha-budget-usage-report",
                str(report_path),
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert payload["status"] == "blocked"
        assert payload["summary"]["gate_decision"] == "block"

    def test_cli_release_pipeline_non_strict_with_alpha_budget_usage_report(self, tmp_path, capsys):
        report_path = tmp_path / "alpha_budget_usage_report.json"
        report_path.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
                    "usage_date": "2026-01-01",
                    "alpha_count": 1,
                    "warning_count": 1,
                    "warnings": [
                        {
                            "alpha_id": "alpha1",
                            "type": "daily_usage_high",
                            "usage_ratio": 0.8,
                            "threshold": 0.8,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "release-pipeline",
                "--output-dir",
                str(tmp_path / "pipeline"),
                "--alpha-budget-usage-report",
                str(report_path),
                "--non-strict",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["summary"]["gate_decision"] == "warn"
        manifest = json.loads(
            Path(payload["artifacts"][ARTIFACT_EVIDENCE_MANIFEST]).read_text(encoding="utf-8")
        )
        assert EVIDENCE_SECTION_ALPHA_BUDGET_USAGE in manifest["summary"]["sections"]
