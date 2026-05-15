"""Rollback drill service and CLI tests."""

import json
from pathlib import Path

from apps.engine.cli import main
from core.contracts.domain_keys import PAYLOAD_KEY_VALIDATION_MODE
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.schema_versions import SCHEMA_ROLLBACK_DRILL
from core.deployment.service_container import ServiceContainer
from core.observability.metric_names import CYCLES_ERRORS, CYCLES_TOTAL


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


class TestRollbackDrillService:
    def test_run_drill_passes(self, tmp_path):
        c = _container(tmp_path)
        result = c.rollback_drill.run(version="1.0.0", reason="test")  # type: ignore[reportOptionalMemberAccess]
        assert result["schema_version"] == SCHEMA_ROLLBACK_DRILL
        assert result["status"] == "passed"
        assert result["passed"] is True
        assert result["dry_run"] is True
        assert result["version"] == "1.0.0"

    def test_run_drill_accepts_fast_validation_mode(self, tmp_path):
        c = _container(tmp_path)
        result = c.rollback_drill.run(validation_mode="fast")  # type: ignore[reportOptionalMemberAccess]
        assert result["schema_version"] == SCHEMA_ROLLBACK_DRILL
        assert result[PAYLOAD_KEY_VALIDATION_MODE] == "fast"

    def test_drill_steps_are_ordered(self, tmp_path):
        c = _container(tmp_path)
        result = c.rollback_drill.run()  # type: ignore[reportOptionalMemberAccess]
        orders = [s["order"] for s in result["steps"]]
        assert orders == sorted(orders)
        assert result["steps"][0]["name"] == "announce_rollback"

    def test_drill_with_verified_evidence(self, tmp_path):
        c = _container(tmp_path / "data")
        built = c.evidence_bundle.build_bundle(str(tmp_path / "evidence"), label="rb1")  # type: ignore[reportOptionalMemberAccess]
        result = c.rollback_drill.run(evidence_manifest=built["manifest_path"])  # type: ignore[reportOptionalMemberAccess]
        assert result["passed"] is True
        prereq = {p["name"]: p for p in result["prerequisites"]}
        assert prereq["evidence_manifest_verified"]["passed"] is True

    def test_drill_detects_tampered_evidence(self, tmp_path):
        c = _container(tmp_path / "data")
        built = c.evidence_bundle.build_bundle(str(tmp_path / "evidence"), label="rb2")  # type: ignore[reportOptionalMemberAccess]
        (Path(built["bundle_dir"]) / "slo.json").write_text("{}", encoding="utf-8")
        result = c.rollback_drill.run(evidence_manifest=built["manifest_path"])  # type: ignore[reportOptionalMemberAccess]
        assert result["passed"] is False
        assert "evidence_manifest_verified" in result["summary"]["failed_prerequisites"]

    def test_slo_breach_fails_checkpoint(self, tmp_path):
        c = _container(tmp_path)
        c.metrics.inc(CYCLES_TOTAL, 100)  # type: ignore[reportOptionalMemberAccess]
        c.metrics.inc(CYCLES_ERRORS, 20)  # type: ignore[reportOptionalMemberAccess]
        result = c.rollback_drill.run()  # type: ignore[reportOptionalMemberAccess]
        assert result["passed"] is False
        assert "slo" in result["summary"]["failed_checkpoints"]
        assert result["recommendation"] == "fix_prerequisites_before_rollback"

    def test_save_output(self, tmp_path):
        c = _container(tmp_path)
        out = tmp_path / "rollback.json"
        result = c.rollback_drill.run(output=str(out))  # type: ignore[reportOptionalMemberAccess]
        assert result["output_path"] == str(out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_ROLLBACK_DRILL

    def test_risks_low_when_clean(self, tmp_path):
        c = _container(tmp_path)
        result = c.rollback_drill.run()  # type: ignore[reportOptionalMemberAccess]
        assert result["risks"][0]["level"] == "low"
        assert result["recommendation"] == "rollback_ready"

    def test_container_has_rollback_drill(self, tmp_path):
        c = _container(tmp_path)
        assert c.rollback_drill is not None


class TestRollbackDrillCLI:
    def test_cli_rollback_drill(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "rollback-drill"])
        assert rc == 0

    def test_cli_rollback_drill_fast_validation_mode(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "rollback-drill", "--validation-mode", "fast"])
        assert rc in (0, 1)

    def test_cli_rollback_drill_test_env_force_metrics(self, tmp_path, capsys):
        rc = main(
            [
                "--base-dir",
                str(tmp_path),
                "--env",
                "test",
                "--force-metrics",
                "rollback-drill",
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == SCHEMA_ROLLBACK_DRILL
        assert rc == 0

    def test_cli_rollback_drill_output(self, tmp_path):
        out = tmp_path / "rollback.json"
        rc = main(["--base-dir", str(tmp_path), "rollback-drill", "--output", str(out)])
        assert rc == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_ROLLBACK_DRILL

    def test_cli_rollback_drill_with_evidence(self, tmp_path):
        c = _container(tmp_path / "data")
        built = c.evidence_bundle.build_bundle(str(tmp_path / "evidence"), label="cli")  # type: ignore[reportOptionalMemberAccess]
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "rollback-drill",
                "--evidence-manifest",
                built["manifest_path"],
            ]
        )
        assert rc == 0

    def test_cli_rollback_drill_bad_evidence(self, tmp_path):
        rc = main(
            [
                "--base-dir",
                str(tmp_path),
                "rollback-drill",
                "--evidence-manifest",
                str(tmp_path / "missing.json"),
            ]
        )
        assert rc == 1
