"""Release readiness service and CLI tests."""
import json
from pathlib import Path

from apps.engine.cli import main
from core.deployment.domain_keys import (
    PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE,
    PAYLOAD_KEY_GOVERNANCE_FOCUS,
    PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT,
    PAYLOAD_KEY_VALIDATION_MODE,
    READINESS_CAP_ALERTS,
    READINESS_CAP_AUDIT_LOG,
    READINESS_CAP_BACKTESTING,
    READINESS_CAP_CLI_OPERATIONS,
    READINESS_CAP_CONFIG_HOT_RELOAD,
    READINESS_CAP_DECISION_CYCLE,
    READINESS_CAP_DIAGNOSTICS,
    READINESS_CAP_EXECUTION_LIFECYCLE,
    READINESS_CAP_FEEDBACK_LOOP,
    READINESS_CAP_GOVERNANCE_RULES,
    READINESS_CAP_LEDGER_PERSISTENCE,
    READINESS_CAP_METRICS,
    READINESS_CAP_RECONCILIATION,
    READINESS_CAP_REPLAY_OPERATIONS,
    READINESS_CAP_RISK_EVALUATION,
    READINESS_CAP_TRACING,
    READINESS_CAP_VENUE_ROUTING,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer
from core.deployment.release_readiness import ReleaseReadinessService
from core.deployment.capability_registry import CapabilityRegistry
from core.deployment.schema_versions import SCHEMA_RELEASE_READINESS
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE_REPORT


def _container(tmp_path, env="development"):
    factory = {
        "development": EnvironmentConfig.development,
        "production": EnvironmentConfig.production,
        "test": EnvironmentConfig.test,
    }
    return ServiceContainer(factory[env](str(tmp_path))).build()


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


class TestReleaseReadinessService:
    def test_build_report_ready(self, tmp_path):
        c = _container(tmp_path)
        report = c.release_readiness.build_report()
        assert report["schema_version"] == SCHEMA_RELEASE_READINESS
        assert report["ready"] is True
        assert report["summary"]["failed_check_count"] == 0
        assert report["summary"]["alpha_budget_missing_evidence_count"] == 0
        assert report["services"]["missing_count"] == 0
        assert report["capabilities"]["available"] == report["capabilities"]["total"]

    def test_report_has_runtime_environment_health(self, tmp_path):
        c = _container(tmp_path)
        report = ReleaseReadinessService(c, version="9.9.9").build_report()
        assert report["version"] == "9.9.9"
        assert report["runtime"]["python_version"]
        assert report["environment"]["base_dir"] == str(tmp_path)
        assert report["health"]["readiness"]["status"] == "ready"
        assert report["health"]["liveness"]["status"] == "alive"

    def test_required_services_are_detailed(self, tmp_path):
        c = _container(tmp_path)
        report = c.release_readiness.build_report()
        details = report["services"]["details"]
        assert details["runtime_loop"]["present"] is True
        assert details["inspection_service"]["present"] is True
        assert details["replay_service"]["present"] is True
        assert details["replay_gate"]["present"] is True
        assert details["operations_service"]["present"] is True
        assert details["venue_router"]["present"] is True
        assert details["alert_service"]["present"] is True
        assert details["config_hot_reload"]["present"] is True

    def test_detects_missing_service(self, tmp_path):
        c = _container(tmp_path)
        c.risk_service = None
        report = ReleaseReadinessService(c).build_report()
        assert report["ready"] is False
        assert "risk_service" in report["services"]["missing"]
        assert "required_services_present" in report["summary"]["failed_checks"]


    def test_report_includes_alpha_budget_governance_empty(self, tmp_path):
        c = _container(tmp_path)
        report = c.release_readiness.build_report()
        assert report[PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE]["record_count"] == 0
        assert report[PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE]["missing_evidence_count"] == 0
        assert report["summary"]["alpha_budget_warning_total"] == 0
        assert report["summary"][PAYLOAD_KEY_GOVERNANCE_FOCUS] == []
        assert report["summary"][PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 0
        assert "alpha_budget_evidence_registered" not in report["summary"]["failed_checks"]

    def test_alpha_budget_clean_keeps_readiness_ready(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_alpha_release(c, tmp_path, warning_count=0)
        report = c.release_readiness.build_report()
        assert report["ready"] is True
        assert report[PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE]["evidence_count"] == 1
        assert report["summary"]["alpha_budget_timeline_event_count"] == 1
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in report["summary"]
        assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in report["summary"]
        checks = {check["name"]: check for check in report["checks"]}
        assert checks["alpha_budget_evidence_registered"]["passed"] is True
        assert checks["alpha_budget_warnings_clear"]["passed"] is True

    def test_readiness_summary_normalizes_malformed_registry_governance_fields(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_alpha_release(c, tmp_path, warning_count=0)
        registry_path = Path(c.release_registry.path)
        records = json.loads(registry_path.read_text(encoding="utf-8"))
        records[0]["summary"]["governance_focus"] = "invalid"
        records[0]["summary"]["governance_warning_count"] = "9"
        registry_path.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
        report = c.release_readiness.build_report()
        assert report["summary"][PAYLOAD_KEY_GOVERNANCE_FOCUS] == []
        assert report["summary"][PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 0

    def test_alpha_budget_missing_evidence_blocks_readiness(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_legacy_release(c, tmp_path)
        report = c.release_readiness.build_report()
        assert report["ready"] is False
        assert "alpha_budget_evidence_registered" in report["summary"]["failed_checks"]
        assert report["summary"]["alpha_budget_missing_evidence_count"] == 1

    def test_alpha_budget_warnings_block_readiness(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_alpha_release(c, tmp_path, warning_count=1)
        report = c.release_readiness.build_report()
        assert report["ready"] is False
        assert "alpha_budget_warnings_clear" in report["summary"]["failed_checks"]
        assert report["summary"]["alpha_budget_warning_total"] == 1

    def test_save_report(self, tmp_path):
        c = _container(tmp_path / "data")
        out = tmp_path / "release" / "readiness.json"
        saved = c.release_readiness.save_report(str(out))
        assert saved == str(out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["ready"] is True

    def test_all_release_environments_ready(self, tmp_path):
        for env in ["development", "production"]:
            c = _container(tmp_path / env, env=env)
            report = c.release_readiness.build_report()
            assert report["ready"] is True

    def test_test_environment_reports_degraded_profile(self, tmp_path):
        c = _container(tmp_path / "test", env="test")
        report = c.release_readiness.build_report()
        assert report["ready"] is False
        assert "metrics" in report["services"]["missing"]
        assert "audit_log" in report["services"]["missing"]

    def test_fast_validation_uses_core_checks_only(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_legacy_release(c, tmp_path)
        report = c.release_readiness.build_report(validation_mode="fast")
        assert report[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        names = [item["name"] for item in report["checks"]]
        assert "alpha_budget_evidence_registered" not in names
        assert "alpha_budget_warnings_clear" not in names

    def test_capability_registry_allows_extension(self, tmp_path):
        c = _container(tmp_path)
        registry = CapabilityRegistry()
        registry.register("custom_capability", ["health_check"])
        report = ReleaseReadinessService(c, capability_registry=registry).build_report()
        assert report["capabilities"]["items"]["custom_capability"] is True


class TestReadinessCLI:
    def test_cli_readiness(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "readiness"])
        assert rc == 0

    def test_cli_readiness_output(self, tmp_path):
        out = tmp_path / "readiness.json"
        rc = main(["--base-dir", str(tmp_path), "readiness", "--output", str(out)])
        assert rc == 0
        assert out.exists()
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["ready"] is True
        assert payload["summary"]["failed_check_count"] == 0

    def test_cli_readiness_fast_mode_skips_deep_checks(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_legacy_release(c, tmp_path)
        out = tmp_path / "readiness_fast.json"
        rc = main([
            "--base-dir",
            str(tmp_path / "data"),
            "readiness",
            "--validation-mode",
            "fast",
            "--output",
            str(out),
        ])
        assert rc == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        names = [item["name"] for item in payload["checks"]]
        assert "alpha_budget_evidence_registered" not in names
        assert "alpha_budget_warnings_clear" not in names

    def test_cli_readiness_global_validation_mode_applies(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_legacy_release(c, tmp_path)
        out = tmp_path / "readiness_global_fast.json"
        rc = main([
            "--base-dir",
            str(tmp_path / "data"),
            "--validation-mode",
            "fast",
            "readiness",
            "--output",
            str(out),
        ])
        assert rc == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        names = [item["name"] for item in payload["checks"]]
        assert "alpha_budget_evidence_registered" not in names

    def test_cli_readiness_subcommand_validation_mode_overrides_global(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_legacy_release(c, tmp_path)
        out = tmp_path / "readiness_override_deep.json"
        rc = main([
            "--base-dir",
            str(tmp_path / "data"),
            "--validation-mode",
            "fast",
            "readiness",
            "--validation-mode",
            "deep",
            "--output",
            str(out),
        ])
        assert rc == 1
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload[PAYLOAD_KEY_VALIDATION_MODE] == "deep"
        names = [item["name"] for item in payload["checks"]]
        assert "alpha_budget_evidence_registered" in names

    def test_cli_readiness_production(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "--env", "production", "readiness"])
        assert rc == 0

    def test_cli_readiness_production_no_metrics_flag_omits_collector(self, tmp_path, capsys):
        rc = main([
            "--base-dir", str(tmp_path), "--env", "production", "--no-metrics", "readiness",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["environment"]["enable_metrics"] is False
        assert "metrics" in out["services"]["missing"]
        assert rc == 1

    def test_cli_readiness_test_env_metrics_service_missing(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "--env", "test", "readiness"])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["environment"]["enable_metrics"] is False
        assert "metrics" in out["services"]["missing"]

    def test_cli_readiness_test_env_force_metrics_restores_metrics_service(self, tmp_path, capsys):
        rc = main([
            "--base-dir", str(tmp_path), "--env", "test", "--force-metrics", "readiness",
        ])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["environment"]["enable_metrics"] is True
        assert "metrics" not in out["services"]["missing"]


class TestReadinessArchitecture:
    def test_capability_names_stable(self, tmp_path):
        c = _container(tmp_path)
        report = c.release_readiness.build_report()
        expected = {
            READINESS_CAP_DECISION_CYCLE, READINESS_CAP_RISK_EVALUATION, READINESS_CAP_LEDGER_PERSISTENCE,
            READINESS_CAP_EXECUTION_LIFECYCLE, READINESS_CAP_RECONCILIATION, READINESS_CAP_FEEDBACK_LOOP,
            READINESS_CAP_REPLAY_OPERATIONS,
            READINESS_CAP_GOVERNANCE_RULES, READINESS_CAP_METRICS, READINESS_CAP_AUDIT_LOG, READINESS_CAP_DIAGNOSTICS,
            READINESS_CAP_TRACING, READINESS_CAP_ALERTS, READINESS_CAP_CONFIG_HOT_RELOAD, READINESS_CAP_VENUE_ROUTING,
            READINESS_CAP_BACKTESTING, READINESS_CAP_CLI_OPERATIONS,
        }
        assert set(report["capabilities"]["items"]) == expected

    def test_checks_are_machine_readable(self, tmp_path):
        c = _container(tmp_path)
        report = c.release_readiness.build_report()
        for check in report["checks"]:
            assert set(check) == {"name", "passed", "detail"}
            assert isinstance(check["passed"], bool)
            assert isinstance(check["detail"], dict)

    def test_summary_matches_checks(self, tmp_path):
        c = _container(tmp_path)
        report = c.release_readiness.build_report()
        failed = [item for item in report["checks"] if not item["passed"]]
        assert report["summary"]["failed_check_count"] == len(failed)
        assert report["ready"] == (len(failed) == 0)
