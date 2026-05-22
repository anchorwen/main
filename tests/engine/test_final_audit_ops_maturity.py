"""Final audit and ops maturity services."""

import json

from apps.engine.cli import main
from core.contracts.domain_keys import (
    PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE,
    PAYLOAD_KEY_GOVERNANCE_FOCUS,
    PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_VALIDATION_MODE_COUNTS,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.schema_versions import SCHEMA_FINAL_AUDIT, SCHEMA_OPS_MATURITY
from core.deployment.service_container import ServiceContainer
from core.observability.metric_names import CYCLES_ERRORS, CYCLES_TOTAL


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


class TestFinalAuditService:
    def test_final_audit_schema(self, tmp_path):
        c = _container(tmp_path)
        report = c.final_audit.build_report()
        assert report["schema_version"] == SCHEMA_FINAL_AUDIT
        assert "ready_for_production" in report
        assert PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE in report
        assert "compliance_matrix" in report
        p = tmp_path / "fa.json"
        c.final_audit.save_report(str(p), report=report)
        loaded = json.loads(p.read_text(encoding="utf-8"))
        assert loaded["ready_for_production"] == report["ready_for_production"]

    def test_final_audit_accepts_fast_validation_mode(self, tmp_path):
        c = _container(tmp_path)
        pipeline = c.release_pipeline.run(
            version="1.0.0", output_dir=str(tmp_path / "pipeline"), validation_mode="fast"
        )
        cert = c.release_certification.certify(pipeline_summary=pipeline)
        c.release_registry.register(cert)
        report = c.final_audit.build_report(validation_mode="fast")
        assert report["schema_version"] == SCHEMA_FINAL_AUDIT
        assert report[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        assert report["registry"][PAYLOAD_KEY_VALIDATION_MODE_COUNTS]["fast"] >= 1
        focus_names = {item["name"] for item in report["summary"][PAYLOAD_KEY_GOVERNANCE_FOCUS]}
        assert focus_names == {
            "registry_deep_validation_present",
            "registry_deep_validation_coverage_complete",
        }

    def test_final_audit_flags_when_no_deep_registered(self, tmp_path):
        c = _container(tmp_path)
        c.metrics.inc(CYCLES_TOTAL, 100)
        c.metrics.inc(CYCLES_ERRORS, 20)
        pipeline = c.release_pipeline.run(
            version="2.0.0",
            output_dir=str(tmp_path / "pipeline"),
            validation_mode="fast",
            strict_gate=False,
        )
        cert = c.release_certification.certify(pipeline_summary=pipeline)
        c.release_registry.register(cert)
        report = c.final_audit.build_report(validation_mode="fast")
        assert any(
            "No deep validation-mode releases registered" in item for item in report["findings"]
        )
        assert report["summary"][PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] >= 1

    def test_final_audit_flags_partial_deep_coverage(self, tmp_path):
        c = _container(tmp_path)
        c.metrics.inc(CYCLES_TOTAL, 100)
        c.metrics.inc(CYCLES_ERRORS, 20)
        p_fast = c.release_pipeline.run(
            version="2.1.0",
            output_dir=str(tmp_path / "pipeline"),
            validation_mode="fast",
            strict_gate=False,
        )
        cert_fast = c.release_certification.certify(pipeline_summary=p_fast)
        c.release_registry.register(cert_fast)
        p_deep = c.release_pipeline.run(
            version="2.1.1",
            output_dir=str(tmp_path / "pipeline"),
            validation_mode="deep",
            strict_gate=False,
        )
        cert_deep = c.release_certification.certify(pipeline_summary=p_deep)
        c.release_registry.register(cert_deep)
        p_fast2 = c.release_pipeline.run(
            version="2.1.2",
            output_dir=str(tmp_path / "pipeline"),
            validation_mode="fast",
            strict_gate=False,
        )
        cert_fast2 = c.release_certification.certify(pipeline_summary=p_fast2)
        c.release_registry.register(cert_fast2)
        report = c.final_audit.build_report(validation_mode="fast")
        assert any("Deep validation coverage is partial" in item for item in report["findings"])
        assert report["summary"][PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] >= 1


class TestOpsMaturityService:
    def test_ops_maturity_schema(self, tmp_path):
        c = _container(tmp_path)
        report = c.ops_maturity.evaluate()
        assert report["schema_version"] == SCHEMA_OPS_MATURITY
        assert "maturity_score" in report
        assert report["min_score_threshold"] == 60.0
        assert report["meets_threshold"] == (report["maturity_score"] >= 60.0)
        assert "pillars" in report
        assert PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE in report["pillars"]
        p = tmp_path / "om.json"
        c.ops_maturity.save_report(str(p))
        loaded = json.loads(p.read_text(encoding="utf-8"))
        assert loaded["maturity_score"] == report["maturity_score"]

    def test_ops_maturity_accepts_fast_validation_mode(self, tmp_path):
        c = _container(tmp_path)
        pipeline = c.release_pipeline.run(
            version="1.0.1", output_dir=str(tmp_path / "pipeline"), validation_mode="fast"
        )
        cert = c.release_certification.certify(pipeline_summary=pipeline)
        c.release_registry.register(cert)
        report = c.ops_maturity.evaluate(validation_mode="fast")
        assert report["schema_version"] == SCHEMA_OPS_MATURITY
        assert report[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        assert (
            report[PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE]["registry"][
                PAYLOAD_KEY_VALIDATION_MODE_COUNTS
            ]["fast"]
            >= 1
        )
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in report["summary"]
        assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in report["summary"]

    def test_ops_maturity_normalizes_malformed_compliance_governance_fields(self, tmp_path):
        c = _container(tmp_path)
        original_generate = c.compliance_audit.generate

        def _malformed_generate(*, output=None, validation_mode=None):
            report = original_generate(output=output, validation_mode=validation_mode)
            report["summary"][PAYLOAD_KEY_GOVERNANCE_FOCUS] = "invalid"
            report["summary"][PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] = "7"
            return report

        c.compliance_audit.generate = _malformed_generate
        report = c.ops_maturity.evaluate(validation_mode="fast")
        assert report["summary"][PAYLOAD_KEY_GOVERNANCE_FOCUS] == []
        assert report["summary"][PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 0


def test_cli_final_audit_and_ops_maturity(tmp_path):
    r1 = main(["--base-dir", str(tmp_path), "final-audit"])
    r2 = main(["--base-dir", str(tmp_path), "ops-maturity"])
    assert r1 in (0, 1)
    assert r2 in (0, 1)
    out1 = tmp_path / "fa.json"
    out2 = tmp_path / "om.json"
    assert main(["--base-dir", str(tmp_path), "final-audit", "--output", str(out1)]) in (0, 1)
    assert main(["--base-dir", str(tmp_path), "ops-maturity", "--output", str(out2)]) in (0, 1)
    assert out1.exists() and out2.exists()


def test_cli_final_audit_fast_validation_mode(tmp_path, capsys):
    rc = main(["--base-dir", str(tmp_path), "final-audit", "--validation-mode", "fast"])
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == SCHEMA_FINAL_AUDIT
    assert PAYLOAD_KEY_GOVERNANCE_FOCUS in out["summary"]
    assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in out["summary"]
    assert rc in (0, 1)


def test_cli_ops_maturity_fast_validation_mode(tmp_path, capsys):
    rc = main(["--base-dir", str(tmp_path), "ops-maturity", "--validation-mode", "fast"])
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == SCHEMA_OPS_MATURITY
    assert PAYLOAD_KEY_GOVERNANCE_FOCUS in out["summary"]
    assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in out["summary"]
    assert rc in (0, 1)


def test_cli_final_audit_test_env_force_metrics_still_emits_schema(tmp_path, capsys):
    rc = main(
        [
            "--base-dir",
            str(tmp_path),
            "--env",
            "test",
            "--force-metrics",
            "final-audit",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == SCHEMA_FINAL_AUDIT
    assert rc in (0, 1)


def test_cli_ops_maturity_test_env_force_metrics_still_emits_schema(tmp_path, capsys):
    rc = main(
        [
            "--base-dir",
            str(tmp_path),
            "--env",
            "test",
            "--force-metrics",
            "ops-maturity",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == SCHEMA_OPS_MATURITY
    assert "maturity_score" in out
    assert rc in (0, 1)
