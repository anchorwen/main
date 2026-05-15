"""Postmortem report service and CLI tests."""

import json

from apps.engine.cli import main
from core.contracts.domain_keys import (
    EVIDENCE_SECTION_ENGINE_CONFIG,
    PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE,
    PAYLOAD_KEY_GOVERNANCE_FOCUS,
    PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT,
    PAYLOAD_KEY_VALIDATION_MODE,
    RELEASE_PIPELINE_SOURCE,
    TIMELINE_ACTOR_HOT_RELOAD,
    TIMELINE_EVENT_ENGINE_CONFIG,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.schema_versions import (
    SCHEMA_ALPHA_BUDGET_GOVERNANCE_EVENT,
    SCHEMA_ENGINE_CONFIG_EVIDENCE,
    SCHEMA_ENGINE_CONFIG_RELOAD_EVENT,
    SCHEMA_POSTMORTEM_REPORT,
)
from core.deployment.service_container import ServiceContainer
from core.observability.metric_names import CYCLES_ERRORS, CYCLES_TOTAL
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE_REPORT


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


class TestPostmortemReportService:
    def test_generate_clean_report(self, tmp_path):
        c = _container(tmp_path)
        report = c.postmortem_report.generate(incident_id="inc-1")  # type: ignore[reportOptionalMemberAccess]
        assert report["schema_version"] == SCHEMA_POSTMORTEM_REPORT
        assert report["incident"]["id"] == "inc-1"
        assert report["incident"]["status"] == "closed"
        assert report["findings"][0]["id"] == "no_material_findings"

    def test_generate_accepts_fast_validation_mode(self, tmp_path):
        c = _container(tmp_path)
        report = c.postmortem_report.generate(incident_id="inc-fast", validation_mode="fast")  # type: ignore[reportOptionalMemberAccess]
        assert report["schema_version"] == SCHEMA_POSTMORTEM_REPORT
        assert report[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in report["summary"]
        assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in report["summary"]

    def test_generate_normalizes_malformed_compliance_governance_fields(self, tmp_path):
        c = _container(tmp_path)
        original_generate = c.compliance_audit.generate  # type: ignore[reportOptionalMemberAccess]

        def _malformed_generate(*, output=None, validation_mode=None):
            report = original_generate(output=output, validation_mode=validation_mode)
            report["summary"][PAYLOAD_KEY_GOVERNANCE_FOCUS] = "invalid"
            report["summary"][PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] = "8"
            return report

        c.compliance_audit.generate = _malformed_generate  # type: ignore[reportOptionalMemberAccess]
        report = c.postmortem_report.generate(incident_id="inc-malformed", validation_mode="fast")  # type: ignore[reportOptionalMemberAccess]
        assert report["summary"][PAYLOAD_KEY_GOVERNANCE_FOCUS] == []
        assert report["summary"][PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 0

    def test_report_includes_timeline_events(self, tmp_path):
        c = _container(tmp_path)
        gate = c.release_gate.evaluate()  # type: ignore[reportOptionalMemberAccess]
        c.operations_timeline.record_release_gate(gate, actor="ci")  # type: ignore[reportOptionalMemberAccess]
        report = c.postmortem_report.generate(incident_id="inc-2")  # type: ignore[reportOptionalMemberAccess]
        assert report["timeline_summary"]["event_count"] == 1
        assert report["timeline"][0]["actor"] == "ci"

    def test_failed_timeline_event_creates_finding(self, tmp_path):
        c = _container(tmp_path)
        c.operations_timeline.record("custom_failure", {"passed": False})  # type: ignore[reportOptionalMemberAccess]
        report = c.postmortem_report.generate(incident_id="inc-3")  # type: ignore[reportOptionalMemberAccess]
        ids = {f["id"] for f in report["findings"]}
        assert "timeline_failures" in ids
        assert report["incident"]["status"] == "action_required"

    def test_slo_breach_creates_finding(self, tmp_path):
        c = _container(tmp_path)
        c.metrics.inc(CYCLES_TOTAL, 100)  # type: ignore[reportOptionalMemberAccess]
        c.metrics.inc(CYCLES_ERRORS, 20)  # type: ignore[reportOptionalMemberAccess]
        report = c.postmortem_report.generate(incident_id="inc-4")  # type: ignore[reportOptionalMemberAccess]
        ids = {f["id"] for f in report["findings"]}
        assert "slo_breach" in ids
        assert report["impact"]["slo_breaching"] is True

    def test_gate_block_creates_critical_status(self, tmp_path):
        c = _container(tmp_path)
        c.risk_service = None
        report = c.postmortem_report.generate(incident_id="inc-5")  # type: ignore[reportOptionalMemberAccess]
        ids = {f["id"] for f in report["findings"]}
        assert "release_gate_blocked" in ids
        assert report["incident"]["status"] == "critical"

    def test_corrective_actions_generated(self, tmp_path):
        c = _container(tmp_path)
        c.operations_timeline.record("custom_failure", {"passed": False})  # type: ignore[reportOptionalMemberAccess]
        report = c.postmortem_report.generate(incident_id="inc-6")  # type: ignore[reportOptionalMemberAccess]
        assert report["corrective_actions"]
        assert report["corrective_actions"][0]["owner"] == "operations"

    def test_engine_config_in_postmortem_evidence(self, tmp_path):
        c = _container(tmp_path / "data")
        c.operations_timeline.record(  # type: ignore[reportOptionalMemberAccess]
            TIMELINE_EVENT_ENGINE_CONFIG,
            {
                "schema_version": SCHEMA_ENGINE_CONFIG_RELOAD_EVENT,
                "reloaded": True,
                "changes": {"ops_maturity_min_score": {"old": 60.0, "new": 55.0}},
                "ops_maturity_min_score": 55.0,
            },
            actor=TIMELINE_ACTOR_HOT_RELOAD,
        )
        report = c.postmortem_report.generate(incident_id="inc-ecfg")  # type: ignore[reportOptionalMemberAccess]
        ec = report["evidence"][EVIDENCE_SECTION_ENGINE_CONFIG]
        assert ec["timeline"]["event_count"] == 1
        assert ec["current"]["schema_version"] == SCHEMA_ENGINE_CONFIG_EVIDENCE
        assert report["impact"]["engine_config_timeline_events"] == 1

    def test_alpha_budget_governance_clean_in_evidence(self, tmp_path):
        c = _container(tmp_path / "data")
        c.operations_timeline.record_alpha_budget_governance(  # type: ignore[reportOptionalMemberAccess]
            {
                "schema_version": SCHEMA_ALPHA_BUDGET_GOVERNANCE_EVENT,
                "source": "test",
                "status": "passed",
                "record_count": 1,
                "evidence_count": 1,
                "missing_evidence_count": 0,
                "warning_total": 0,
                "warning_release_count": 0,
            }
        )
        report = c.postmortem_report.generate(incident_id="inc-alpha-clean")  # type: ignore[reportOptionalMemberAccess]
        alpha = report["evidence"][PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE]
        assert alpha["event_count"] == 1
        assert alpha["evidence_count"] == 1
        assert alpha["warning_total"] == 0
        assert report["impact"]["alpha_budget_warnings"] == 0
        assert report["findings"][0]["id"] == "no_material_findings"

    def test_alpha_budget_missing_evidence_creates_finding(self, tmp_path):
        c = _container(tmp_path / "data")
        c.operations_timeline.record_alpha_budget_governance(  # type: ignore[reportOptionalMemberAccess]
            {
                "schema_version": SCHEMA_ALPHA_BUDGET_GOVERNANCE_EVENT,
                "source": "test",
                "status": "warning",
                "record_count": 1,
                "evidence_count": 0,
                "missing_evidence_count": 1,
                "warning_total": 0,
                "warning_release_count": 0,
            }
        )
        report = c.postmortem_report.generate(incident_id="inc-alpha-missing")  # type: ignore[reportOptionalMemberAccess]
        ids = {finding["id"] for finding in report["findings"]}
        assert "alpha_budget_evidence_missing" in ids
        assert report["impact"]["alpha_budget_missing_evidence"] == 1
        assert any(action["owner"] == "release" for action in report["corrective_actions"])

    def test_alpha_budget_warnings_create_finding(self, tmp_path):
        c = _container(tmp_path / "data")
        c.operations_timeline.record_alpha_budget_governance(  # type: ignore[reportOptionalMemberAccess]
            {
                "schema_version": SCHEMA_ALPHA_BUDGET_GOVERNANCE_EVENT,
                "source": "test",
                "status": "warning",
                "record_count": 1,
                "evidence_count": 1,
                "missing_evidence_count": 0,
                "warning_total": 2,
                "warning_release_count": 1,
            }
        )
        report = c.postmortem_report.generate(incident_id="inc-alpha-warn")  # type: ignore[reportOptionalMemberAccess]
        ids = {finding["id"] for finding in report["findings"]}
        assert "alpha_budget_warnings_present" in ids
        assert report["evidence"][PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE]["warning_total"] == 2
        assert report["impact"]["alpha_budget_warnings"] == 2
        assert any(action["owner"] == "risk" for action in report["corrective_actions"])

    def test_pipeline_alpha_budget_timeline_appears_in_postmortem(self, tmp_path):
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
        c.release_pipeline.run(  # type: ignore[reportOptionalMemberAccess]
            output_dir=str(tmp_path / "pipeline"),
            strict_gate=False,
            alpha_budget_usage_report=alpha_report,
        )
        report = c.postmortem_report.generate(incident_id="inc-alpha-pipeline")  # type: ignore[reportOptionalMemberAccess]
        alpha = report["evidence"][PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE]
        assert alpha["event_count"] == 1
        assert alpha["warning_total"] == 1
        assert alpha["events"][0]["summary"]["source"] == RELEASE_PIPELINE_SOURCE

    def test_save_report_output(self, tmp_path):
        c = _container(tmp_path)
        out = tmp_path / "postmortem.json"
        report = c.postmortem_report.generate(incident_id="inc-7", output=str(out))  # type: ignore[reportOptionalMemberAccess]
        assert report["output_path"] == str(out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_POSTMORTEM_REPORT

    def test_container_has_postmortem_report(self, tmp_path):
        c = _container(tmp_path)
        assert c.postmortem_report is not None


class TestPostmortemReportCLI:
    def test_cli_postmortem_report(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "postmortem-report", "--incident-id", "inc-cli"])
        assert rc == 0

    def test_cli_postmortem_report_fast_validation_mode(self, tmp_path, capsys):
        rc = main(
            [
                "--base-dir",
                str(tmp_path),
                "postmortem-report",
                "--incident-id",
                "inc-cli-fast",
                "--validation-mode",
                "fast",
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in out["summary"]
        assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in out["summary"]
        assert rc in (0, 1)

    def test_cli_postmortem_report_output(self, tmp_path):
        out = tmp_path / "pm.json"
        rc = main(
            [
                "--base-dir",
                str(tmp_path),
                "postmortem-report",
                "--incident-id",
                "inc-cli-2",
                "--title",
                "Release PM",
                "--severity",
                "medium",
                "--output",
                str(out),
            ]
        )
        assert rc == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["incident"]["title"] == "Release PM"
        assert payload["incident"]["severity"] == "medium"

    def test_cli_postmortem_report_critical_returns_one(self, tmp_path):
        c = _container(tmp_path)
        c.risk_service = None
        c.operations_timeline.record("custom_failure", {"passed": False})  # type: ignore[reportOptionalMemberAccess]
        rc = main(["--base-dir", str(tmp_path), "postmortem-report", "--incident-id", "inc-cli-3"])
        assert rc == 0

    def test_cli_postmortem_report_test_env_force_metrics(self, tmp_path, capsys):
        rc = main(
            [
                "--base-dir",
                str(tmp_path),
                "--env",
                "test",
                "--force-metrics",
                "postmortem-report",
                "--incident-id",
                "inc-tfm",
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == SCHEMA_POSTMORTEM_REPORT
        assert out["incident"]["id"] == "inc-tfm"
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in out["summary"]
        assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in out["summary"]
        assert rc in (0, 1)
