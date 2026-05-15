"""Compliance control matrix service and CLI tests."""

import json
from pathlib import Path

from apps.engine.cli import main
from core.contracts.domain_keys import (
    PAYLOAD_KEY_GOVERNANCE_FOCUS,
    PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT,
    PAYLOAD_KEY_VALIDATION_MODE,
    PAYLOAD_KEY_VALIDATION_MODE_COUNTS,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.schema_versions import SCHEMA_COMPLIANCE_CONTROL_MATRIX
from core.deployment.service_container import ServiceContainer
from core.observability.metric_names import CYCLES_ERRORS, CYCLES_TOTAL
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE_REPORT


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


def _register_clean_release(container, tmp_path, version="1.0.0"):
    return _register_alpha_release(
        container, tmp_path, version=version, warning_count=0, validation_mode="deep"
    )


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


def _register_legacy_release(container, tmp_path, version="1.0.0"):
    pipeline = container.release_pipeline.run(
        version=version, output_dir=str(tmp_path / "pipeline" / version)
    )
    cert = container.release_certification.certify(pipeline_summary=pipeline, approver="qa")
    return container.release_registry.register(cert, actor="qa")


class TestComplianceControlMatrixService:
    def test_empty_matrix_warns(self, tmp_path):
        c = _container(tmp_path)
        report = c.compliance_control_matrix.generate()  # type: ignore[reportOptionalMemberAccess]
        assert report["schema_version"] == SCHEMA_COMPLIANCE_CONTROL_MATRIX
        assert report["status"] == "warn"
        assert report["warning_count"] >= 1
        assert report["control_count"] == 14

    def test_matrix_accepts_fast_validation_mode(self, tmp_path):
        c = _container(tmp_path)
        report = c.compliance_control_matrix.generate(validation_mode="fast")  # type: ignore[reportOptionalMemberAccess]
        assert report["schema_version"] == SCHEMA_COMPLIANCE_CONTROL_MATRIX
        assert report[PAYLOAD_KEY_VALIDATION_MODE] == "fast"

    def test_clean_release_matrix_passes(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_clean_release(c, tmp_path)
        report = c.compliance_control_matrix.generate()  # type: ignore[reportOptionalMemberAccess]
        assert report["status"] == "pass"
        assert report["failed_count"] == 0
        assert all(control["status"] == "pass" for control in report["controls"])
        rel1 = next(cn for cn in report["controls"] if cn["control_id"] == "REL-001")
        assert rel1["evidence"][PAYLOAD_KEY_VALIDATION_MODE_COUNTS]["deep"] >= 1

    def test_governance_controls_pass_when_registry_clean(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_clean_release(c, tmp_path)
        report = c.compliance_control_matrix.generate()  # type: ignore[reportOptionalMemberAccess]
        gov1 = next(cn for cn in report["controls"] if cn["control_id"] == "GOV-001")
        gov2 = next(cn for cn in report["controls"] if cn["control_id"] == "GOV-002")
        gov3 = next(cn for cn in report["controls"] if cn["control_id"] == "GOV-003")
        gov4 = next(cn for cn in report["controls"] if cn["control_id"] == "GOV-004")
        assert gov1["status"] == "pass"
        assert gov2["status"] == "pass"
        assert gov3["status"] == "pass"
        assert gov4["status"] == "pass"
        focus_ids = {item["control_id"] for item in report["summary"][PAYLOAD_KEY_GOVERNANCE_FOCUS]}
        assert focus_ids == {"GOV-003", "GOV-004"}
        assert report["summary"][PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 0

    def test_governance_deep_presence_warns_without_deep_release(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_alpha_release(
            c, tmp_path, version="1.3.0", warning_count=0, validation_mode="fast"
        )
        report = c.compliance_control_matrix.generate()  # type: ignore[reportOptionalMemberAccess]
        gov3 = next(cn for cn in report["controls"] if cn["control_id"] == "GOV-003")
        assert gov3["status"] == "warn"

    def test_governance_deep_coverage_warns_for_mixed_registry(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_alpha_release(
            c, tmp_path, version="1.4.0", warning_count=0, validation_mode="fast"
        )
        _register_alpha_release(
            c, tmp_path, version="1.4.1", warning_count=0, validation_mode="deep"
        )
        _register_alpha_release(
            c, tmp_path, version="1.4.2", warning_count=0, validation_mode="fast"
        )
        report = c.compliance_control_matrix.generate()  # type: ignore[reportOptionalMemberAccess]
        gov4 = next(cn for cn in report["controls"] if cn["control_id"] == "GOV-004")
        assert gov4["status"] == "warn"
        assert report["summary"][PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] >= 1

    def test_governance_ops_maturity_control_warns_low_score(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_clean_release(c, tmp_path)
        reg_path = Path(c.release_registry.path)  # type: ignore[reportOptionalMemberAccess]
        records = json.loads(reg_path.read_text(encoding="utf-8"))
        for r in records:
            r["summary"]["ops_maturity_score"] = 40.0
        reg_path.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
        report = c.compliance_control_matrix.generate()  # type: ignore[reportOptionalMemberAccess]
        gov2 = next(cn for cn in report["controls"] if cn["control_id"] == "GOV-002")
        assert gov2["status"] == "warn"
        assert report["warning_count"] >= 1
        assert gov2["evidence"]["min_score"] == 60.0

    def test_governance_ops_maturity_control_passes_with_lower_config_min(self, tmp_path):
        c = ServiceContainer(
            EnvironmentConfig.development(str(tmp_path / "data"), ops_maturity_min_score=30.0)
        ).build()
        _register_clean_release(c, tmp_path)
        reg_path = Path(c.release_registry.path)  # type: ignore[reportOptionalMemberAccess]
        records = json.loads(reg_path.read_text(encoding="utf-8"))
        for r in records:
            r["summary"]["ops_maturity_score"] = 40.0
        reg_path.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
        report = c.compliance_control_matrix.generate()  # type: ignore[reportOptionalMemberAccess]
        gov2 = next(cn for cn in report["controls"] if cn["control_id"] == "GOV-002")
        assert gov2["status"] == "pass"
        assert gov2["evidence"]["min_score"] == 30.0

    def test_alpha_budget_controls_pass_when_evidence_clean(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_alpha_release(c, tmp_path, version="1.2.0", warning_count=0)
        report = c.compliance_control_matrix.generate()  # type: ignore[reportOptionalMemberAccess]
        alpha1 = next(cn for cn in report["controls"] if cn["control_id"] == "ALPHA-001")
        alpha2 = next(cn for cn in report["controls"] if cn["control_id"] == "ALPHA-002")
        assert alpha1["status"] == "pass"
        assert alpha2["status"] == "pass"
        assert alpha1["evidence"]["evidence_count"] == 1

    def test_alpha_budget_missing_evidence_warns_control(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_legacy_release(c, tmp_path, version="1.2.1")
        report = c.compliance_control_matrix.generate()  # type: ignore[reportOptionalMemberAccess]
        alpha1 = next(cn for cn in report["controls"] if cn["control_id"] == "ALPHA-001")
        assert alpha1["status"] == "warn"
        assert alpha1["evidence"]["missing_evidence_count"] == 1
        assert alpha1["remediation"]

    def test_alpha_budget_warnings_warn_control(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_alpha_release(c, tmp_path, version="1.2.2", warning_count=1)
        report = c.compliance_control_matrix.generate()  # type: ignore[reportOptionalMemberAccess]
        alpha2 = next(cn for cn in report["controls"] if cn["control_id"] == "ALPHA-002")
        assert alpha2["status"] == "warn"
        assert alpha2["evidence"]["warning_total"] == 1
        assert report["warning_count"] >= 1

    def test_control_shape(self, tmp_path):
        c = _container(tmp_path)
        report = c.compliance_control_matrix.generate()  # type: ignore[reportOptionalMemberAccess]
        control = report["controls"][0]
        assert set(control) == {
            "control_id",
            "objective",
            "evidence_source",
            "status",
            "passed",
            "evidence",
            "gap",
            "remediation",
        }

    def test_failed_timeline_event_fails_control(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_clean_release(c, tmp_path)
        c.operations_timeline.record("manual_failure", {"passed": False})  # type: ignore[reportOptionalMemberAccess]
        report = c.compliance_control_matrix.generate()  # type: ignore[reportOptionalMemberAccess]
        assert report["status"] == "fail"
        aud2 = next(cn for cn in report["controls"] if cn["control_id"] == "AUD-002")
        assert aud2["status"] == "fail"
        assert aud2["remediation"]

    def test_slo_breach_fails_control(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_clean_release(c, tmp_path)
        c.metrics.inc(CYCLES_TOTAL, 100)  # type: ignore[reportOptionalMemberAccess]
        c.metrics.inc(CYCLES_ERRORS, 20)  # type: ignore[reportOptionalMemberAccess]
        report = c.compliance_control_matrix.generate()  # type: ignore[reportOptionalMemberAccess]
        obs = next(cn for cn in report["controls"] if cn["control_id"] == "OBS-001")
        assert obs["status"] == "fail"

    def test_uncertified_registry_fails_control(self, tmp_path):
        c = _container(tmp_path / "data")
        pipeline = c.release_pipeline.run(output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        cert = c.release_certification.certify(pipeline_summary=pipeline)  # type: ignore[reportOptionalMemberAccess]
        cert["certified"] = False
        cert["status"] = "rejected"
        c.release_registry.register(cert)  # type: ignore[reportOptionalMemberAccess]
        report = c.compliance_control_matrix.generate()  # type: ignore[reportOptionalMemberAccess]
        rel2 = next(cn for cn in report["controls"] if cn["control_id"] == "REL-002")
        assert rel2["status"] == "fail"

    def test_save_report(self, tmp_path):
        c = _container(tmp_path)
        out = tmp_path / "matrix.json"
        report = c.compliance_control_matrix.generate(output=str(out))  # type: ignore[reportOptionalMemberAccess]
        assert report["output_path"] == str(out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_COMPLIANCE_CONTROL_MATRIX

    def test_container_has_matrix(self, tmp_path):
        c = _container(tmp_path)
        assert c.compliance_control_matrix is not None


class TestComplianceControlMatrixCLI:
    def test_cli_matrix_warn_returns_zero(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "compliance-matrix"])
        assert rc == 0

    def test_cli_matrix_fast_validation_mode(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "compliance-matrix", "--validation-mode", "fast"])
        assert rc in (0, 1)

    def test_cli_matrix_output(self, tmp_path):
        out = tmp_path / "matrix.json"
        rc = main(["--base-dir", str(tmp_path), "compliance-matrix", "--output", str(out)])
        assert rc == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_COMPLIANCE_CONTROL_MATRIX

    def test_cli_matrix_fail_returns_one(self, tmp_path):
        c = _container(tmp_path / "data")
        _register_clean_release(c, tmp_path)
        c.operations_timeline.record("manual_failure", {"passed": False})  # type: ignore[reportOptionalMemberAccess]
        rc = main(["--base-dir", str(tmp_path / "data"), "compliance-matrix"])
        assert rc == 1

    def test_cli_matrix_test_env_force_metrics(self, tmp_path, capsys):
        rc = main(
            [
                "--base-dir",
                str(tmp_path),
                "--env",
                "test",
                "--force-metrics",
                "compliance-matrix",
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == SCHEMA_COMPLIANCE_CONTROL_MATRIX
        assert "summary" in out
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in out["summary"]
        ids = {item["control_id"] for item in out["summary"][PAYLOAD_KEY_GOVERNANCE_FOCUS]}
        assert ids == {"GOV-003", "GOV-004"}
        assert rc in (0, 1)
