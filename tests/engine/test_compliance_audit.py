"""Compliance audit service and CLI tests."""

import json
from pathlib import Path

from apps.engine.cli import main
from core.deployment.domain_keys import (
    PAYLOAD_KEY_GOVERNANCE_FOCUS,
    PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_VALIDATION_MODE_COUNTS,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.schema_versions import SCHEMA_COMPLIANCE_AUDIT
from core.deployment.service_container import ServiceContainer
from core.observability.metric_names import CYCLES_ERRORS, CYCLES_TOTAL
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE_REPORT


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


def _register_clean_release(container, tmp_path, version="1.0.0"):
    return _register_alpha_release(
        container, tmp_path, version=version, warning_count=0, validation_mode="deep"
    )


def _register_legacy_release(container, tmp_path, version="1.0.0"):
    pipeline = container.release_pipeline.run(
        version=version, output_dir=str(tmp_path / "pipeline" / version)
    )
    cert = container.release_certification.certify(pipeline_summary=pipeline, approver="qa")
    return container.release_registry.register(cert, actor="qa")


def _register_alpha_release(
    container, tmp_path, version="1.0.0", warning_count=0, validation_mode="fast"
):
    warnings = []
    if warning_count:
        warnings = [
            {"alpha_id": "alpha1", "type": "daily_usage_high", "usage_ratio": 0.8, "threshold": 0.8}
        ]
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
        validation_mode=validation_mode,
        alpha_budget_usage_report=alpha_report,
    )
    cert = container.release_certification.certify(pipeline_summary=pipeline, approver="qa")
    return container.release_registry.register(cert, actor="qa")


class TestComplianceAuditService:
    def test_empty_audit_warns(self, tmp_path):
        c = _container(tmp_path)
        report = c.compliance_audit.generate()  # type: ignore[reportOptionalMemberAccess]
        assert report["schema_version"] == SCHEMA_COMPLIANCE_AUDIT
        assert report["status"] == "warn"
        assert report["passed"] is False
        names = {check["name"] for check in report["checks"] if check["level"] == "warn"}
        assert "release_registry_present" in names
        assert "operations_timeline_present" in names

    def test_audit_accepts_fast_validation_mode(self, tmp_path):
        c = _container(tmp_path)
        report = c.compliance_audit.generate(validation_mode="fast")  # type: ignore[reportOptionalMemberAccess]
        assert report["schema_version"] == SCHEMA_COMPLIANCE_AUDIT
        assert report[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        assert report["summary"][PAYLOAD_KEY_VALIDATION_MODE_COUNTS] == {}

    def test_audit_summary_includes_validation_mode_counts(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_alpha_release(
            c, tmp_path, version="2.0.0", warning_count=0, validation_mode="fast"
        )
        report = c.compliance_audit.generate(validation_mode="fast")  # type: ignore[reportOptionalMemberAccess]
        assert report["summary"][PAYLOAD_KEY_VALIDATION_MODE_COUNTS]["fast"] >= 1

    def test_audit_warns_when_registry_has_no_deep_validation(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_alpha_release(
            c, tmp_path, version="2.1.0", warning_count=0, validation_mode="fast"
        )
        report = c.compliance_audit.generate(validation_mode="fast")  # type: ignore[reportOptionalMemberAccess]
        deep_check = next(
            ch for ch in report["checks"] if ch["name"] == "registry_deep_validation_present"
        )
        assert deep_check["level"] == "warn"
        assert report["summary"][PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] >= 1
        assert any("deep validation release" in rec["action"] for rec in report["recommendations"])

    def test_audit_warns_when_deep_validation_coverage_is_partial(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_alpha_release(
            c, tmp_path, version="2.2.0", warning_count=0, validation_mode="fast"
        )
        _register_alpha_release(
            c, tmp_path, version="2.2.1", warning_count=0, validation_mode="deep"
        )
        _register_alpha_release(
            c, tmp_path, version="2.2.2", warning_count=0, validation_mode="fast"
        )
        report = c.compliance_audit.generate(validation_mode="fast")  # type: ignore[reportOptionalMemberAccess]
        coverage_check = next(
            ch
            for ch in report["checks"]
            if ch["name"] == "registry_deep_validation_coverage_complete"
        )
        assert coverage_check["level"] == "warn"
        names = {item["name"] for item in report["summary"][PAYLOAD_KEY_GOVERNANCE_FOCUS]}
        assert names == {
            "registry_deep_validation_coverage_complete",
            "registry_deep_validation_present",
        }
        assert any("deep validation coverage" in rec["action"] for rec in report["recommendations"])

    def test_clean_release_audit_passes(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_clean_release(c, tmp_path)
        report = c.compliance_audit.generate()  # type: ignore[reportOptionalMemberAccess]
        assert report["status"] == "pass"
        assert report["passed"] is True
        assert report["summary"]["registry_record_count"] == 1

    def test_uncertified_record_fails(self, tmp_path):
        c = _container(tmp_path / "data")
        pipeline = c.release_pipeline.run(output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        cert = c.release_certification.certify(pipeline_summary=pipeline)  # type: ignore[reportOptionalMemberAccess]
        cert["certified"] = False
        cert["status"] = "rejected"
        c.release_registry.register(cert)  # type: ignore[reportOptionalMemberAccess]
        report = c.compliance_audit.generate()  # type: ignore[reportOptionalMemberAccess]
        assert report["status"] == "fail"
        assert any(
            ch["name"] == "all_registered_releases_certified" and ch["level"] == "fail"
            for ch in report["checks"]
        )

    def test_failed_timeline_event_fails(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_clean_release(c, tmp_path)
        c.operations_timeline.record("manual_failure", {"passed": False})  # type: ignore[reportOptionalMemberAccess]
        report = c.compliance_audit.generate()  # type: ignore[reportOptionalMemberAccess]
        assert report["status"] == "fail"
        assert any(
            ch["name"] == "no_failed_timeline_events" and ch["level"] == "fail"
            for ch in report["checks"]
        )

    def test_slo_breach_fails(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_clean_release(c, tmp_path)
        c.metrics.inc(CYCLES_TOTAL, 100)  # type: ignore[reportOptionalMemberAccess]
        c.metrics.inc(CYCLES_ERRORS, 20)  # type: ignore[reportOptionalMemberAccess]
        report = c.compliance_audit.generate()  # type: ignore[reportOptionalMemberAccess]
        assert report["status"] == "fail"
        assert any(ch["name"] == "slo_healthy" and ch["level"] == "fail" for ch in report["checks"])

    def test_gate_block_fails(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_clean_release(c, tmp_path)
        c.risk_service = None
        report = c.compliance_audit.generate()  # type: ignore[reportOptionalMemberAccess]
        assert report["status"] == "fail"
        assert any(
            ch["name"] == "release_gate_not_blocking" and ch["level"] == "fail"
            for ch in report["checks"]
        )

    def test_alpha_budget_evidence_registered_passes(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_alpha_release(c, tmp_path, version="1.2.0", warning_count=0)
        report = c.compliance_audit.generate()  # type: ignore[reportOptionalMemberAccess]
        alpha_check = next(
            ch for ch in report["checks"] if ch["name"] == "alpha_budget_evidence_registered"
        )
        warning_check = next(
            ch for ch in report["checks"] if ch["name"] == "alpha_budget_warnings_clear"
        )
        assert alpha_check["level"] == "pass"
        assert warning_check["level"] == "pass"
        assert report["summary"]["alpha_budget_evidence_count"] == 1

    def test_alpha_budget_missing_evidence_warns(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_legacy_release(c, tmp_path, version="1.2.1")
        report = c.compliance_audit.generate()  # type: ignore[reportOptionalMemberAccess]
        alpha_check = next(
            ch for ch in report["checks"] if ch["name"] == "alpha_budget_evidence_registered"
        )
        assert alpha_check["level"] == "warn"
        assert alpha_check["detail"]["missing_evidence_count"] == 1

    def test_alpha_budget_warnings_warn(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_alpha_release(c, tmp_path, version="1.2.2", warning_count=1)
        report = c.compliance_audit.generate()  # type: ignore[reportOptionalMemberAccess]
        warning_check = next(
            ch for ch in report["checks"] if ch["name"] == "alpha_budget_warnings_clear"
        )
        assert warning_check["level"] == "warn"
        assert warning_check["detail"]["warning_total"] == 1
        assert report["summary"]["alpha_budget_warning_total"] == 1

    def test_governance_registry_checks_pass_with_clean_release(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_clean_release(c, tmp_path)
        report = c.compliance_audit.generate()  # type: ignore[reportOptionalMemberAccess]
        fa = next(ch for ch in report["checks"] if ch["name"] == "registry_final_audit_cleared")
        om = next(ch for ch in report["checks"] if ch["name"] == "registry_ops_maturity_threshold")
        assert fa["level"] == "pass"
        assert om["level"] == "pass"

    def test_governance_registry_warns_on_low_ops_maturity(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_clean_release(c, tmp_path)
        reg_path = Path(c.release_registry.path)  # type: ignore[reportOptionalMemberAccess]
        records = json.loads(reg_path.read_text(encoding="utf-8"))
        for r in records:
            r["summary"]["ops_maturity_score"] = 40.0
        reg_path.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
        report = c.compliance_audit.generate()  # type: ignore[reportOptionalMemberAccess]
        om = next(ch for ch in report["checks"] if ch["name"] == "registry_ops_maturity_threshold")
        assert om["level"] == "warn"
        assert om["detail"]["min_score"] == 60.0

    def test_governance_ops_maturity_respects_config_min_score(self, tmp_path):
        c = ServiceContainer(
            EnvironmentConfig.development(str(tmp_path / "data"), ops_maturity_min_score=30.0)
        ).build()
        _register_clean_release(c, tmp_path)
        reg_path = Path(c.release_registry.path)  # type: ignore[reportOptionalMemberAccess]
        records = json.loads(reg_path.read_text(encoding="utf-8"))
        for r in records:
            r["summary"]["ops_maturity_score"] = 40.0
        reg_path.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
        report = c.compliance_audit.generate()  # type: ignore[reportOptionalMemberAccess]
        om = next(ch for ch in report["checks"] if ch["name"] == "registry_ops_maturity_threshold")
        assert om["level"] == "pass"
        assert om["detail"]["min_score"] == 30.0

    def test_governance_ops_maturity_warns_below_config_min(self, tmp_path):
        c = ServiceContainer(
            EnvironmentConfig.development(str(tmp_path / "data"), ops_maturity_min_score=50.0)
        ).build()
        _register_clean_release(c, tmp_path)
        reg_path = Path(c.release_registry.path)  # type: ignore[reportOptionalMemberAccess]
        records = json.loads(reg_path.read_text(encoding="utf-8"))
        for r in records:
            r["summary"]["ops_maturity_score"] = 40.0
        reg_path.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
        report = c.compliance_audit.generate()  # type: ignore[reportOptionalMemberAccess]
        om = next(ch for ch in report["checks"] if ch["name"] == "registry_ops_maturity_threshold")
        assert om["level"] == "warn"
        assert om["detail"]["min_score"] == 50.0

    def test_save_report(self, tmp_path):
        c = _container(tmp_path)
        out = tmp_path / "audit.json"
        report = c.compliance_audit.generate(output=str(out))  # type: ignore[reportOptionalMemberAccess]
        assert report["output_path"] == str(out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_COMPLIANCE_AUDIT

    def test_recommendations_for_warnings(self, tmp_path):
        c = _container(tmp_path)
        report = c.compliance_audit.generate()  # type: ignore[reportOptionalMemberAccess]
        assert report["recommendations"]
        assert any("Register" in rec["action"] for rec in report["recommendations"])

    def test_container_has_compliance_audit(self, tmp_path):
        c = _container(tmp_path)
        assert c.compliance_audit is not None


class TestComplianceAuditCLI:
    def test_cli_compliance_audit_warn_returns_zero(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "compliance-audit"])
        assert rc == 0

    def test_cli_compliance_audit_fast_validation_mode(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "compliance-audit", "--validation-mode", "fast"])
        out = json.loads(capsys.readouterr().out)
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in out["summary"]
        assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in out["summary"]
        assert rc in (0, 1)

    def test_cli_compliance_audit_output(self, tmp_path):
        out = tmp_path / "audit.json"
        rc = main(["--base-dir", str(tmp_path), "compliance-audit", "--output", str(out)])
        assert rc == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_COMPLIANCE_AUDIT

    def test_cli_compliance_audit_fail_returns_one(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_clean_release(c, tmp_path)
        c.operations_timeline.record("manual_failure", {"passed": False})  # type: ignore[reportOptionalMemberAccess]
        rc = main(["--base-dir", str(tmp_path / "data"), "compliance-audit"])
        assert rc == 1

    def test_cli_compliance_audit_test_env_force_metrics(self, tmp_path, capsys):
        rc = main(
            [
                "--base-dir",
                str(tmp_path),
                "--env",
                "test",
                "--force-metrics",
                "compliance-audit",
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == SCHEMA_COMPLIANCE_AUDIT
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in out["summary"]
        assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in out["summary"]
        assert rc in (0, 1)
