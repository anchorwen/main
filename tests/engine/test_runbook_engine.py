"""Runbook engine and CLI tests."""
import json

from apps.engine.cli import main
from core.deployment.domain_keys import (
    COMPLIANCE_CHECK_ALPHA_BUDGET_EVIDENCE_REGISTERED,
    PAYLOAD_KEY_GOVERNANCE_FOCUS,
    PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT,
    PAYLOAD_KEY_VALIDATION_MODE,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer
from core.deployment.state_persistence import StatePersistence
from core.deployment.runbook_engine import RunbookEngine
from core.deployment.schema_versions import SCHEMA_RUNBOOK_RESULT
from core.observability.metric_names import CYCLES_ERRORS
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE_REPORT


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


def _register_alpha_release(container, tmp_path, version="1.0.0", warning_count=0):
    warnings = []
    if warning_count:
        warnings = [{"alpha_id": "alpha1", "type": "daily_usage_high", "usage_ratio": 0.8, "threshold": 0.8}]
    alpha_report = {
        "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
        "usage_date": "2026-01-01",
        "alpha_count": 1,
        "warning_count": warning_count,
        "warnings": warnings,
    }
    pipeline = container.release_pipeline.run(
        version=version,
        output_dir=str(tmp_path / "pipeline" / version),
        strict_gate=False,
        alpha_budget_usage_report=alpha_report,
    )
    cert = container.release_certification.certify(pipeline_summary=pipeline, approver="qa")
    return container.release_registry.register(cert, actor="qa")


def _register_legacy_release(container, tmp_path, version="1.0.0"):
    pipeline = container.release_pipeline.run(version=version, output_dir=str(tmp_path / "pipeline" / version))
    cert = container.release_certification.certify(pipeline_summary=pipeline, approver="qa")
    return container.release_registry.register(cert, actor="qa")


class TestRunbookEngine:
    def test_preflight_passes(self, tmp_path):
        c = _container(tmp_path)
        result = c.runbook_engine.preflight()
        assert result["schema_version"] == SCHEMA_RUNBOOK_RESULT
        assert result["runbook"] == "preflight"
        assert result["passed"] is True
        assert result["summary"]["failed_check_count"] == 0
        assert "alpha_budget" in result["payload"]
        assert "readiness_gaps" in result["payload"]
        assert result["payload"]["readiness_gaps"]["missing"] == []
        assert result["payload"]["readiness_gaps"]["recommendations"] == []
        assert result["payload"]["readiness_gaps"]["execution_plan_items"] == []
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in result["payload"]["readiness_summary"]
        assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in result["payload"]["readiness_summary"]

    def test_doctor_passes_healthy(self, tmp_path):
        c = _container(tmp_path)
        result = c.runbook_engine.doctor()
        assert result["runbook"] == "doctor"
        assert result["passed"] is True
        assert result["payload"]["recommendations"][0]["action"] == "no_action"

    def test_postmortem_without_persistence(self, tmp_path):
        c = _container(tmp_path)
        result = c.runbook_engine.postmortem(label="x")
        assert result["runbook"] == "postmortem"
        assert result["passed"] is True
        assert result["payload"]["state_snapshot"]["status"] == "not_configured"

    def test_postmortem_with_persistence(self, tmp_path):
        c = _container(tmp_path / "data")
        sp = StatePersistence(str(tmp_path / "state"))
        engine = RunbookEngine(c, persistence=sp)
        result = engine.postmortem(label="incident1")
        assert result["passed"] is True
        assert result["payload"]["state_snapshot"]["status"] == "saved"

    def test_postmortem_output(self, tmp_path):
        c = _container(tmp_path)
        out = tmp_path / "postmortem.json"
        result = c.runbook_engine.postmortem(label="x", output=str(out))
        assert result["output_path"] == str(out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["runbook"] == "postmortem"

    def test_unknown_runbook(self, tmp_path):
        c = _container(tmp_path)
        result = c.runbook_engine.run("missing")
        assert result["status"] == "unknown"
        assert "preflight" in result["available"]

    def test_preflight_detects_missing_service(self, tmp_path):
        c = _container(tmp_path)
        c.risk_service = None
        result = c.runbook_engine.preflight()
        assert result["passed"] is False
        assert "release_ready" in result["summary"]["failed_checks"]

    def test_doctor_recommends_on_alerts(self, tmp_path):
        c = _container(tmp_path)
        c.metrics.inc(CYCLES_ERRORS, 1.0)
        result = c.runbook_engine.doctor()
        assert any(r["action"] == "inspect_alerts" for r in result["payload"]["recommendations"])

    def test_doctor_recommends_inspect_readiness_when_replay_services_missing(self, tmp_path):
        c = _container(tmp_path)
        c.replay_service = None
        result = c.runbook_engine.doctor()
        readiness_recommendations = [
            r
            for r in result["payload"]["recommendations"]
            if r["action"] == "inspect_readiness"
        ]
        assert readiness_recommendations
        assert "replay_service" in readiness_recommendations[0]["reason"]
        assert "replay_service" in readiness_recommendations[0]["details"]["missing"]
        assert "replay_operations" in readiness_recommendations[0]["details"]["capabilities"]
        assert "inspect_readiness" in readiness_recommendations[0]["details"]["recommendations"]
        assert "restore_required_services" in readiness_recommendations[0]["details"]["recommendations"]
        assert "restore_capability_gaps" in readiness_recommendations[0]["details"]["recommendations"]
        assert "restore_replay_services" in readiness_recommendations[0]["details"]["recommendations"]
        assert "action_plan" in readiness_recommendations[0]["details"]
        assert "restore_replay_services" in readiness_recommendations[0]["details"]["action_plan"]
        assert "readiness_gaps" in result["payload"]
        assert "replay_service" in result["payload"]["readiness_gaps"]["missing"]
        assert "inspect_readiness" in result["payload"]["readiness_gaps"]["recommendations"]
        assert "restore_required_services" in result["payload"]["readiness_gaps"]["recommendations"]
        assert "restore_capability_gaps" in result["payload"]["readiness_gaps"]["recommendations"]
        assert "restore_replay_services" in result["payload"]["readiness_gaps"]["recommendations"]
        assert "action_plan" in result["payload"]["readiness_gaps"]
        assert "execution_plan" in result["payload"]["readiness_gaps"]
        assert "execution_plan_items" in result["payload"]["readiness_gaps"]
        assert result["payload"]["readiness_gaps"]["execution_plan"][0] == "restore_replay_services"
        assert result["payload"]["readiness_gaps"]["execution_plan_items"][0]["action"] == "restore_replay_services"
        assert result["payload"]["readiness_gaps"]["execution_plan_items"][0]["order"] == 1
        assert "replay_service" in result["payload"]["readiness_gaps"]["execution_plan_items"][0]["missing"]
        assert "replay_operations" in result["payload"]["readiness_gaps"]["execution_plan_items"][0]["capabilities"]
        assert "restore_replay_services" in result["payload"]["readiness_gaps"]["action_plan"]
        assert result["payload"]["readiness_gaps"]["action_plan"]["restore_replay_services"]["order"] == 1
        assert result["payload"]["readiness_gaps"]["action_plan"]["restore_required_services"]["capabilities"] == []
        assert result["payload"]["readiness_gaps"]["action_plan"]["restore_capability_gaps"]["missing"] == []
        assert "replay_service" in result["payload"]["readiness_gaps"]["action_plan"]["restore_replay_services"]["missing"]
        assert "replay_operations" in result["payload"]["readiness_gaps"]["action_plan"]["restore_replay_services"]["capabilities"]
        assert result["payload"]["readiness_gaps"]["action_plan"]["restore_replay_services"]["priority"] == "high"
        assert "reason" in result["payload"]["readiness_gaps"]["action_plan"]["restore_required_services"]


    def test_preflight_reports_alpha_budget_clean(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_alpha_release(c, tmp_path, warning_count=0)
        result = c.runbook_engine.preflight()
        assert result["passed"] is True
        assert result["payload"]["alpha_budget"]["evidence_count"] == 1
        checks = {check["name"]: check for check in result["checks"]}
        assert checks["alpha_budget_evidence_registered"]["passed"] is True
        assert checks["alpha_budget_warnings_clear"]["passed"] is True

    def test_preflight_fails_on_missing_alpha_budget_evidence(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_legacy_release(c, tmp_path)
        result = c.runbook_engine.preflight()
        assert result["passed"] is False
        assert "alpha_budget_evidence_registered" in result["summary"]["failed_checks"]
        assert result["payload"]["alpha_budget"]["missing_evidence_count"] == 1

    def test_doctor_recommends_alpha_budget_evidence_attachment(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_legacy_release(c, tmp_path)
        result = c.runbook_engine.doctor()
        assert "alpha_budget_evidence_registered" in result["summary"]["failed_checks"]
        assert any(r["action"] == "attach_alpha_budget_evidence" for r in result["payload"]["recommendations"])

    def test_preflight_fast_skips_alpha_budget_checks(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_legacy_release(c, tmp_path)
        result = c.runbook_engine.preflight(validation_mode="fast")
        assert result[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        failed_checks = set(result["summary"]["failed_checks"])
        assert "alpha_budget_evidence_registered" not in failed_checks
        assert "alpha_budget_warnings_clear" not in failed_checks

    def test_doctor_fast_skips_alpha_budget_recommendations(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_legacy_release(c, tmp_path)
        result = c.runbook_engine.doctor(validation_mode="fast")
        assert result[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        assert "alpha_budget_evidence_registered" not in result["summary"]["failed_checks"]
        assert not any(r["action"] == "attach_alpha_budget_evidence" for r in result["payload"]["recommendations"])

    def test_doctor_recommends_alpha_budget_warning_review(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_alpha_release(c, tmp_path, warning_count=1)
        result = c.runbook_engine.doctor()
        assert "alpha_budget_warnings_clear" in result["summary"]["failed_checks"]
        assert any(r["action"] == "review_alpha_budget_warnings" for r in result["payload"]["recommendations"])
        assert result["payload"]["alpha_budget"]["warning_total"] == 1

    def test_result_shape(self, tmp_path):
        c = _container(tmp_path)
        result = c.runbook_engine.preflight()
        assert set(result) == {
            "schema_version", "runbook", "started_at", "finished_at",
            "status", "passed", "summary", "checks", "payload", "validation_mode",
        }
        for check in result["checks"]:
            assert set(check) == {"name", "passed", "detail"}


class TestRunbookCLI:
    def test_cli_preflight(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "runbook", "preflight"])
        assert rc == 0

    def test_cli_doctor(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "runbook", "doctor"])
        assert rc == 0

    def test_cli_runbook_fast_validation_mode(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "runbook", "preflight", "--validation-mode", "fast"])
        assert rc == 0

    def test_cli_runbook_global_validation_mode_applies(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "--validation-mode", "fast", "runbook", "preflight"])
        out = json.loads(capsys.readouterr().out)
        assert out[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        assert COMPLIANCE_CHECK_ALPHA_BUDGET_EVIDENCE_REGISTERED not in out["summary"]["failed_checks"]
        assert rc in (0, 1)

    def test_cli_runbook_subcommand_validation_mode_overrides_global(self, tmp_path, capsys):
        rc = main([
            "--base-dir",
            str(tmp_path),
            "--validation-mode",
            "fast",
            "runbook",
            "preflight",
            "--validation-mode",
            "deep",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out[PAYLOAD_KEY_VALIDATION_MODE] == "deep"
        assert rc in (0, 1)

    def test_cli_postmortem_output(self, tmp_path):
        out = tmp_path / "pm.json"
        rc = main(["--base-dir", str(tmp_path), "runbook", "postmortem",
                   "--label", "incident", "--output", str(out)])
        assert rc == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["runbook"] == "postmortem"

    def test_cli_preflight_test_env_force_metrics_emits_runbook_result(self, tmp_path, capsys):
        rc = main([
            "--base-dir", str(tmp_path), "--env", "test", "--force-metrics",
            "runbook", "preflight",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == SCHEMA_RUNBOOK_RESULT
        assert out["runbook"] == "preflight"
        assert rc in (0, 1)

    def test_cli_doctor_test_env_force_metrics_emits_runbook_result(self, tmp_path, capsys):
        rc = main([
            "--base-dir", str(tmp_path), "--env", "test", "--force-metrics", "runbook", "doctor",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == SCHEMA_RUNBOOK_RESULT
        assert out["runbook"] == "doctor"
        assert rc in (0, 1)


class TestRunbookContainer:
    def test_container_has_runbook_engine(self, tmp_path):
        c = _container(tmp_path)
        assert c.runbook_engine is not None

    def test_run_dispatcher(self, tmp_path):
        c = _container(tmp_path)
        for name in ["preflight", "doctor", "postmortem"]:
            result = c.runbook_engine.run(name)
            assert result["runbook"] == name
