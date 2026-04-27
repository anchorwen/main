"""Deployment plan service and CLI tests."""
import json
from pathlib import Path

from apps.engine.cli import main
from core.deployment.domain_keys import EVIDENCE_SECTION_ALPHA_BUDGET_USAGE, PAYLOAD_KEY_VALIDATION_MODE
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer
from core.deployment.schema_versions import SCHEMA_DEPLOYMENT_PLAN
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE_REPORT


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


class TestDeploymentPlanService:
    def test_standard_plan_ready(self, tmp_path):
        c = _container(tmp_path)
        plan = c.deployment_plan.build_plan(version="1.2.3", strategy="standard")
        assert plan["schema_version"] == SCHEMA_DEPLOYMENT_PLAN
        assert plan["status"] == "ready"
        assert plan["executable"] is True
        assert plan["version"] == "1.2.3"
        assert plan["strategy"] == "standard"

    def test_plan_accepts_fast_validation_mode(self, tmp_path):
        c = _container(tmp_path)
        plan = c.deployment_plan.build_plan(validation_mode="fast")
        assert plan["schema_version"] == SCHEMA_DEPLOYMENT_PLAN
        assert plan[PAYLOAD_KEY_VALIDATION_MODE] == "fast"

    def test_canary_plan_phases(self, tmp_path):
        c = _container(tmp_path)
        plan = c.deployment_plan.build_plan(strategy="canary")
        names = [p["name"] for p in plan["phases"]]
        assert "deploy_canary_10pct" in names
        assert "promote_50pct" in names
        assert "promote_100pct" in names

    def test_shadow_plan_phases(self, tmp_path):
        c = _container(tmp_path)
        plan = c.deployment_plan.build_plan(strategy="shadow")
        names = [p["name"] for p in plan["phases"]]
        assert "shadow_deploy" in names
        assert "shadow_compare" in names

    def test_invalid_strategy(self, tmp_path):
        c = _container(tmp_path)
        plan = c.deployment_plan.build_plan(strategy="bluegreen")
        assert plan["status"] == "invalid"
        assert plan[PAYLOAD_KEY_VALIDATION_MODE] == "deep"
        assert "canary" in plan["available_strategies"]

    def test_plan_blocks_when_gate_blocks(self, tmp_path):
        c = _container(tmp_path)
        c.risk_service = None
        plan = c.deployment_plan.build_plan()
        assert plan["status"] == "blocked"
        assert plan["executable"] is False
        assert plan["gate"]["decision"] == "block"


    def test_plan_blocks_with_alpha_budget_usage_report_warnings(self, tmp_path):
        c = _container(tmp_path)
        alpha_report = {
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": "2026-01-01",
            "alpha_count": 1,
            "warning_count": 1,
            "warnings": [{"alpha_id": "alpha1", "type": "daily_usage_high", "usage_ratio": 0.8, "threshold": 0.8}],
        }
        plan = c.deployment_plan.build_plan(alpha_budget_usage_report=alpha_report)
        assert plan["status"] == "blocked"
        assert plan["executable"] is False
        assert plan["gate"]["decision"] == "block"
        assert EVIDENCE_SECTION_ALPHA_BUDGET_USAGE in plan["gate"]["summary"]["warning_signals"]

    def test_plan_warns_with_alpha_budget_usage_report_non_strict(self, tmp_path):
        c = _container(tmp_path)
        alpha_report = {
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": "2026-01-01",
            "alpha_count": 1,
            "warning_count": 1,
            "warnings": [{"alpha_id": "alpha1", "type": "daily_usage_high", "usage_ratio": 0.8, "threshold": 0.8}],
        }
        plan = c.deployment_plan.build_plan(alpha_budget_usage_report=alpha_report, strict_gate=False)
        assert plan["status"] == "ready"
        assert plan["executable"] is True
        assert plan["gate"]["decision"] == "warn"

    def test_plan_with_evidence_bundle(self, tmp_path):
        c = _container(tmp_path / "data")
        plan = c.deployment_plan.build_plan(version="2.0.0", strategy="canary",
                                            evidence_dir=str(tmp_path / "evidence"))
        assert plan["evidence"] is not None
        assert Path(plan["evidence"]["manifest_path"]).exists()


    def test_plan_evidence_bundle_includes_alpha_budget_usage_report(self, tmp_path):
        c = _container(tmp_path / "data")
        alpha_report = {
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": "2026-01-01",
            "alpha_count": 1,
            "warning_count": 0,
            "warnings": [],
        }
        plan = c.deployment_plan.build_plan(
            version="5.0.0",
            strategy="standard",
            evidence_dir=str(tmp_path / "evidence"),
            alpha_budget_usage_report=alpha_report,
        )
        assert plan["evidence"] is not None
        bundle_dir = Path(plan["evidence"]["bundle_dir"])
        assert (bundle_dir / "alpha_budget_usage.json").exists()
        manifest = json.loads(Path(plan["evidence"]["manifest_path"]).read_text(encoding="utf-8"))
        assert EVIDENCE_SECTION_ALPHA_BUDGET_USAGE in manifest["summary"]["sections"]

    def test_save_plan(self, tmp_path):
        c = _container(tmp_path)
        out = tmp_path / "deploy_plan.json"
        saved = c.deployment_plan.save_plan(str(out), version="3.0.0", strategy="standard")
        assert saved == str(out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["version"] == "3.0.0"

    def test_checkpoints_present(self, tmp_path):
        c = _container(tmp_path)
        plan = c.deployment_plan.build_plan(strategy="canary")
        checkpoint_names = {c["name"] for c in plan["checkpoints"]}
        assert {"readiness", "gate", "slo", "doctor"}.issubset(checkpoint_names)
        assert "canary_10pct_slo" in checkpoint_names

    def test_rollback_triggers_present(self, tmp_path):
        c = _container(tmp_path)
        plan = c.deployment_plan.build_plan(strategy="canary")
        triggers = {t["name"] for t in plan["rollback"]}
        assert "release_gate_block" in triggers
        assert "slo_breach" in triggers
        assert "canary_error_budget_exhausted" in triggers

    def test_commands_include_version(self, tmp_path):
        c = _container(tmp_path)
        plan = c.deployment_plan.build_plan(version="4.5.6")
        assert any("4.5.6" in cmd for cmd in plan["commands"])

    def test_commands_include_effective_validation_mode(self, tmp_path):
        c = _container(tmp_path)
        plan = c.deployment_plan.build_plan(version="4.5.7", validation_mode="fast")
        assert any("--validation-mode fast" in cmd for cmd in plan["commands"])

    def test_container_has_deployment_plan(self, tmp_path):
        c = _container(tmp_path)
        assert c.deployment_plan is not None


class TestDeploymentPlanCLI:
    def test_cli_deploy_plan(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "deploy-plan"])
        assert rc == 0

    def test_cli_deploy_plan_fast_validation_mode(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "deploy-plan", "--validation-mode", "fast"])
        assert rc in (0, 1)

    def test_cli_deploy_plan_test_env_force_metrics(self, tmp_path, capsys):
        rc = main([
            "--base-dir", str(tmp_path), "--env", "test", "--force-metrics", "deploy-plan",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == SCHEMA_DEPLOYMENT_PLAN
        assert rc in (0, 1)

    def test_cli_deploy_plan_canary_output(self, tmp_path):
        out = tmp_path / "plan.json"
        rc = main(["--base-dir", str(tmp_path), "deploy-plan",
                   "--version", "1.0.1", "--strategy", "canary",
                   "--output", str(out)])
        assert rc == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["strategy"] == "canary"
        assert payload["version"] == "1.0.1"

    def test_cli_deploy_plan_with_evidence(self, tmp_path):
        out = tmp_path / "plan.json"
        ev = tmp_path / "evidence"
        rc = main(["--base-dir", str(tmp_path / "data"), "deploy-plan",
                   "--strategy", "shadow", "--evidence-dir", str(ev),
                   "--output", str(out)])
        assert rc == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["evidence"] is not None


    def test_cli_deploy_plan_blocks_with_alpha_budget_usage_report(self, tmp_path, capsys):
        report_path = tmp_path / "alpha_budget_usage_report.json"
        report_path.write_text(json.dumps({
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": "2026-01-01",
            "alpha_count": 1,
            "warning_count": 1,
            "warnings": [{"alpha_id": "alpha1", "type": "daily_usage_high", "usage_ratio": 0.8, "threshold": 0.8}],
        }), encoding="utf-8")
        rc = main(["--base-dir", str(tmp_path), "deploy-plan", "--alpha-budget-usage-report", str(report_path)])
        payload = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert payload["status"] == "blocked"
        assert payload["gate"]["decision"] == "block"
        assert EVIDENCE_SECTION_ALPHA_BUDGET_USAGE in payload["gate"]["summary"]["warning_signals"]

    def test_cli_deploy_plan_non_strict_with_alpha_budget_usage_report(self, tmp_path, capsys):
        report_path = tmp_path / "alpha_budget_usage_report.json"
        report_path.write_text(json.dumps({
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": "2026-01-01",
            "alpha_count": 1,
            "warning_count": 1,
            "warnings": [{"alpha_id": "alpha1", "type": "daily_usage_high", "usage_ratio": 0.8, "threshold": 0.8}],
        }), encoding="utf-8")
        rc = main(["--base-dir", str(tmp_path), "deploy-plan", "--alpha-budget-usage-report", str(report_path), "--non-strict"])
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["status"] == "ready"
        assert payload["gate"]["decision"] == "warn"
