"""Deployment executor service and CLI tests."""
import json

from apps.engine.cli import main
from core.deployment.domain_keys import PAYLOAD_KEY_VALIDATION_MODE
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer
from core.deployment.schema_versions import SCHEMA_DEPLOYMENT_EXECUTION
from core.observability.metric_names import CYCLES_ERRORS, CYCLES_TOTAL


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


class TestDeploymentExecutor:
    def test_execute_standard_plan_success(self, tmp_path):
        c = _container(tmp_path)
        plan = c.deployment_plan.build_plan(version="1.0.0", strategy="standard")
        result = c.deployment_executor.execute(plan)
        assert result["schema_version"] == SCHEMA_DEPLOYMENT_EXECUTION
        assert result["status"] == "succeeded"
        assert result["passed"] is True
        assert result["dry_run"] is True
        assert result["version"] == "1.0.0"

    def test_execute_accepts_fast_validation_mode(self, tmp_path):
        c = _container(tmp_path)
        plan = c.deployment_plan.build_plan(validation_mode="fast")
        result = c.deployment_executor.execute(plan, validation_mode="fast")
        assert result["schema_version"] == SCHEMA_DEPLOYMENT_EXECUTION
        assert result[PAYLOAD_KEY_VALIDATION_MODE] == "fast"

    def test_execute_canary_plan_success(self, tmp_path):
        c = _container(tmp_path)
        plan = c.deployment_plan.build_plan(strategy="canary")
        result = c.deployment_executor.execute(plan)
        assert result["status"] == "succeeded"
        assert result["summary"]["phase_count"] >= 5
        assert result["summary"]["checkpoint_count"] >= 6

    def test_execute_shadow_plan_success(self, tmp_path):
        c = _container(tmp_path)
        plan = c.deployment_plan.build_plan(strategy="shadow")
        result = c.deployment_executor.execute(plan)
        assert result["status"] == "succeeded"
        phases = [p["phase"] for p in result["phase_results"]]
        assert "shadow_deploy" in phases

    def test_invalid_plan_fails(self, tmp_path):
        c = _container(tmp_path)
        result = c.deployment_executor.execute({"status": "invalid", "version": "x"})
        assert result["status"] == "failed"
        assert "invalid_plan" in result["summary"]["failures"]

    def test_blocked_plan_blocks(self, tmp_path):
        c = _container(tmp_path)
        plan = c.deployment_plan.build_plan()
        plan["executable"] = False
        result = c.deployment_executor.execute(plan)
        assert result["status"] == "blocked"
        assert "plan_not_executable" in result["summary"]["failures"]

    def test_slo_breach_fails_execution(self, tmp_path):
        c = _container(tmp_path)
        c.metrics.inc(CYCLES_TOTAL, 100)
        c.metrics.inc(CYCLES_ERRORS, 20)
        plan = c.deployment_plan.build_plan(strict_gate=False)
        result = c.deployment_executor.execute(plan)
        assert result["passed"] is False
        assert result["rollback"]["fired_count"] >= 1
        assert result["rollback"]["recommendation"] == "rollback"

    def test_execute_from_file(self, tmp_path):
        c = _container(tmp_path)
        plan = c.deployment_plan.build_plan(version="2.0.0")
        pf = tmp_path / "plan.json"
        pf.write_text(json.dumps(plan), encoding="utf-8")
        result = c.deployment_executor.execute_from_file(str(pf))
        assert result["version"] == "2.0.0"
        assert result["passed"] is True

    def test_save_result(self, tmp_path):
        c = _container(tmp_path)
        result = c.deployment_executor.execute()
        out = tmp_path / "exec.json"
        saved = c.deployment_executor.save_result(result, str(out))
        assert saved == str(out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_DEPLOYMENT_EXECUTION

    def test_container_has_executor(self, tmp_path):
        c = _container(tmp_path)
        assert c.deployment_executor is not None


class TestDeploymentExecutorCLI:
    def test_cli_deploy_exec(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "deploy-exec"])
        assert rc == 0

    def test_cli_deploy_exec_fast_validation_mode(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "deploy-exec", "--validation-mode", "fast"])
        assert rc in (0, 1)

    def test_cli_deploy_exec_test_env_force_metrics(self, tmp_path, capsys):
        rc = main([
            "--base-dir", str(tmp_path), "--env", "test", "--force-metrics", "deploy-exec",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == SCHEMA_DEPLOYMENT_EXECUTION
        assert rc in (0, 1)

    def test_cli_deploy_exec_output(self, tmp_path):
        out = tmp_path / "exec.json"
        rc = main(["--base-dir", str(tmp_path), "deploy-exec", "--output", str(out)])
        assert rc == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_DEPLOYMENT_EXECUTION

    def test_cli_deploy_exec_from_plan(self, tmp_path):
        c = _container(tmp_path / "data")
        plan = c.deployment_plan.build_plan(version="3.0.0", strategy="shadow")
        pf = tmp_path / "plan.json"
        pf.write_text(json.dumps(plan), encoding="utf-8")
        rc = main(["--base-dir", str(tmp_path / "data"), "deploy-exec", "--plan", str(pf)])
        assert rc == 0

    def test_cli_deploy_exec_canary(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "deploy-exec", "--strategy", "canary"])
        assert rc == 0
