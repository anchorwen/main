"""Release certification service and CLI tests."""

import json
from pathlib import Path

from apps.engine.cli import main
from core.contracts.domain_keys import (
    ARTIFACT_EVIDENCE_MANIFEST,
    ARTIFACT_GATE,
    PAYLOAD_KEY_GOVERNANCE_FOCUS,
    PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.schema_versions import (
    SCHEMA_FINAL_AUDIT,
    SCHEMA_OPS_MATURITY,
    SCHEMA_RELEASE_CERTIFICATE,
    SCHEMA_RELEASE_CERTIFICATE_VERIFICATION,
)
from core.deployment.service_container import ServiceContainer
from core.observability.metric_names import CYCLES_ERRORS, CYCLES_TOTAL
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE_REPORT


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


class TestReleaseCertificationService:
    def test_certify_passed_pipeline(self, tmp_path):
        c = _container(tmp_path / "data")
        pipeline = c.release_pipeline.run(  # type: ignore[reportOptionalMemberAccess]
            version="1.0.0",
            output_dir=str(tmp_path / "pipeline"),
            validation_mode="fast",
        )
        cert = c.release_certification.certify(pipeline_summary=pipeline, approver="qa")  # type: ignore[reportOptionalMemberAccess]
        assert cert["schema_version"] == SCHEMA_RELEASE_CERTIFICATE
        assert cert["status"] == "certified"
        assert cert["certified"] is True
        assert cert["approver"] == "qa"
        assert cert["validation_mode"] == "fast"
        assert cert["pipeline"]["validation_mode"] == "fast"
        assert "governance_focus" in cert
        assert "governance_warning_count" in cert
        assert len(cert["certificate_fingerprint"]) == 64

    def test_certify_from_file(self, tmp_path):
        c = _container(tmp_path / "data")
        pipeline = c.release_pipeline.run(version="1.0.1", output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        pipeline_path = tmp_path / "pipeline.json"
        pipeline_path.write_text(json.dumps(pipeline), encoding="utf-8")
        cert = c.release_certification.certify(pipeline_summary=str(pipeline_path))  # type: ignore[reportOptionalMemberAccess]
        assert cert["version"] == "1.0.1"
        assert cert["certified"] is True

    def test_certify_normalizes_malformed_pipeline_governance_fields(self, tmp_path):
        c = _container(tmp_path / "data")
        pipeline = c.release_pipeline.run(version="1.0.2", output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        pipeline["summary"]["governance_focus"] = [{"name": "ok"}, {"level": "warn"}, "bad", 3]
        pipeline["summary"]["governance_warning_count"] = "4"
        cert = c.release_certification.certify(pipeline_summary=pipeline)  # type: ignore[reportOptionalMemberAccess]
        assert cert[PAYLOAD_KEY_GOVERNANCE_FOCUS] == [{"name": "ok"}, {"level": "warn"}]
        assert cert[PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 1

    def test_certify_rejects_failed_pipeline(self, tmp_path):
        c = _container(tmp_path / "data")
        c.metrics.inc(CYCLES_TOTAL, 100)  # type: ignore[reportOptionalMemberAccess]
        c.metrics.inc(CYCLES_ERRORS, 20)  # type: ignore[reportOptionalMemberAccess]
        pipeline = c.release_pipeline.run(output_dir=str(tmp_path / "pipeline"), strict_gate=False)  # type: ignore[reportOptionalMemberAccess]
        cert = c.release_certification.certify(pipeline_summary=pipeline)  # type: ignore[reportOptionalMemberAccess]
        assert cert["status"] == "rejected"
        assert cert["certified"] is False

    def test_certify_rejects_missing_artifact(self, tmp_path):
        c = _container(tmp_path / "data")
        pipeline = c.release_pipeline.run(output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        Path(pipeline["artifacts"][ARTIFACT_GATE]).unlink()
        cert = c.release_certification.certify(pipeline_summary=pipeline)  # type: ignore[reportOptionalMemberAccess]
        assert cert["certified"] is False
        gate_check = next(item for item in cert["artifact_checks"] if item["name"] == ARTIFACT_GATE)
        assert gate_check["valid"] is False

    def test_certify_rejects_tampered_evidence(self, tmp_path):
        c = _container(tmp_path / "data")
        pipeline = c.release_pipeline.run(output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        slo_path = Path(pipeline["artifacts"][ARTIFACT_EVIDENCE_MANIFEST]).parent / "slo.json"
        slo_path.write_text("{}", encoding="utf-8")
        cert = c.release_certification.certify(pipeline_summary=pipeline)  # type: ignore[reportOptionalMemberAccess]
        assert cert["certified"] is False
        assert cert["evidence_verification"]["verified"] is False

    def test_certify_includes_alpha_budget_evidence(self, tmp_path):
        c = _container(tmp_path / "data")
        alpha_report = {
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": "2026-01-01",
            "alpha_count": 1,
            "warning_count": 0,
            "warnings": [],
        }
        pipeline = c.release_pipeline.run(  # type: ignore[reportOptionalMemberAccess]
            version="1.1.0",
            output_dir=str(tmp_path / "pipeline"),
            alpha_budget_usage_report=alpha_report,
        )
        cert = c.release_certification.certify(pipeline_summary=pipeline)  # type: ignore[reportOptionalMemberAccess]
        assert cert["certified"] is True
        assert cert["alpha_budget_evidence"]["present"] is True
        artifact = cert["alpha_budget_evidence"]["artifact"]
        assert artifact["schema_version"] == SCHEMA_ALPHA_BUDGET_USAGE_REPORT
        assert artifact["warning_count"] == 0
        assert len(artifact["sha256"]) == 64
        assert cert["final_audit_evidence"]["present"] is True
        assert cert["final_audit_evidence"]["artifact"]["schema_version"] == SCHEMA_FINAL_AUDIT
        assert cert["ops_maturity_evidence"]["present"] is True
        assert cert["ops_maturity_evidence"]["artifact"]["schema_version"] == SCHEMA_OPS_MATURITY

    def test_certify_without_alpha_budget_evidence_marks_absent(self, tmp_path):
        c = _container(tmp_path / "data")
        pipeline = c.release_pipeline.run(version="1.1.1", output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        cert = c.release_certification.certify(pipeline_summary=pipeline)  # type: ignore[reportOptionalMemberAccess]
        assert cert["alpha_budget_evidence"]["present"] is False
        assert cert["alpha_budget_evidence"]["artifact"] is None
        assert cert["final_audit_evidence"]["present"] is True
        assert cert["ops_maturity_evidence"]["present"] is True

    def test_verify_certificate_with_alpha_budget_evidence(self, tmp_path):
        c = _container(tmp_path / "data")
        alpha_report = {
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": "2026-01-01",
            "alpha_count": 1,
            "warning_count": 0,
            "warnings": [],
        }
        pipeline = c.release_pipeline.run(  # type: ignore[reportOptionalMemberAccess]
            version="1.1.2",
            output_dir=str(tmp_path / "pipeline"),
            alpha_budget_usage_report=alpha_report,
        )
        out = tmp_path / "cert_alpha.json"
        c.release_certification.certify(pipeline_summary=pipeline, output=str(out))  # type: ignore[reportOptionalMemberAccess]
        verification = c.release_certification.verify_certificate(str(out))  # type: ignore[reportOptionalMemberAccess]
        assert verification["verified"] is True

    def test_verify_certificate_includes_validation_mode(self, tmp_path):
        c = _container(tmp_path / "data")
        pipeline = c.release_pipeline.run(  # type: ignore[reportOptionalMemberAccess]
            version="1.1.3",
            output_dir=str(tmp_path / "pipeline"),
            validation_mode="fast",
        )
        out = tmp_path / "cert_mode.json"
        c.release_certification.certify(pipeline_summary=pipeline, output=str(out))  # type: ignore[reportOptionalMemberAccess]
        verification = c.release_certification.verify_certificate(str(out))  # type: ignore[reportOptionalMemberAccess]
        assert verification["verified"] is True
        assert verification["validation_mode"] == "fast"

    def test_save_and_verify_certificate(self, tmp_path):
        c = _container(tmp_path / "data")
        pipeline = c.release_pipeline.run(output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        out = tmp_path / "cert.json"
        cert = c.release_certification.certify(pipeline_summary=pipeline, output=str(out))  # type: ignore[reportOptionalMemberAccess]
        assert cert["output_path"] == str(out)
        verification = c.release_certification.verify_certificate(str(out))  # type: ignore[reportOptionalMemberAccess]
        assert verification["schema_version"] == SCHEMA_RELEASE_CERTIFICATE_VERIFICATION
        assert verification["verified"] is True

    def test_verify_detects_tampered_certificate(self, tmp_path):
        c = _container(tmp_path / "data")
        pipeline = c.release_pipeline.run(output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        out = tmp_path / "cert.json"
        c.release_certification.certify(pipeline_summary=pipeline, output=str(out))  # type: ignore[reportOptionalMemberAccess]
        payload = json.loads(out.read_text(encoding="utf-8"))
        payload["approver"] = "tampered"
        out.write_text(json.dumps(payload), encoding="utf-8")
        verification = c.release_certification.verify_certificate(str(out))  # type: ignore[reportOptionalMemberAccess]
        assert verification["verified"] is False

    def test_container_has_release_certification(self, tmp_path):
        c = _container(tmp_path)
        assert c.release_certification is not None


class TestReleaseCertificationCLI:
    def test_cli_certify_and_verify(self, tmp_path):
        c = _container(tmp_path / "data")
        pipeline = c.release_pipeline.run(output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        pipeline_path = tmp_path / "pipeline.json"
        cert_path = tmp_path / "cert.json"
        pipeline_path.write_text(json.dumps(pipeline), encoding="utf-8")
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "release-cert",
                "certify",
                "--pipeline",
                str(pipeline_path),
                "--approver",
                "qa",
                "--output",
                str(cert_path),
            ]
        )
        assert rc == 0
        rc_verify = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "release-cert",
                "verify",
                "--certificate",
                str(cert_path),
            ]
        )
        assert rc_verify == 0

    def test_cli_certify_requires_pipeline(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "release-cert", "certify"])
        assert rc == 1

    def test_cli_verify_requires_certificate(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "release-cert", "verify"])
        assert rc == 1

    def test_cli_verify_tampered_certificate(self, tmp_path):
        c = _container(tmp_path / "data")
        pipeline = c.release_pipeline.run(output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        cert_path = tmp_path / "cert.json"
        c.release_certification.certify(pipeline_summary=pipeline, output=str(cert_path))  # type: ignore[reportOptionalMemberAccess]
        payload = json.loads(cert_path.read_text(encoding="utf-8"))
        payload["status"] = "tampered"
        cert_path.write_text(json.dumps(payload), encoding="utf-8")
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "release-cert",
                "verify",
                "--certificate",
                str(cert_path),
            ]
        )
        assert rc == 1

    def test_cli_certify_includes_alpha_budget_evidence(self, tmp_path):
        c = _container(tmp_path / "data")
        alpha_report = {
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": "2026-01-01",
            "alpha_count": 1,
            "warning_count": 0,
            "warnings": [],
        }
        pipeline = c.release_pipeline.run(  # type: ignore[reportOptionalMemberAccess]
            output_dir=str(tmp_path / "pipeline"),
            alpha_budget_usage_report=alpha_report,
        )
        pipeline_path = tmp_path / "pipeline.json"
        cert_path = tmp_path / "cert_alpha.json"
        pipeline_path.write_text(json.dumps(pipeline), encoding="utf-8")
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "release-cert",
                "certify",
                "--pipeline",
                str(pipeline_path),
                "--output",
                str(cert_path),
            ]
        )
        assert rc == 0
        cert = json.loads(cert_path.read_text(encoding="utf-8"))
        assert cert["alpha_budget_evidence"]["present"] is True
        assert cert["alpha_budget_evidence"]["artifact"]["warning_count"] == 0
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in cert
        assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in cert

    def test_cli_certify_and_verify_test_env_force_metrics(self, tmp_path):
        c = _container(tmp_path / "data")
        pipeline = c.release_pipeline.run(output_dir=str(tmp_path / "pipeline"))  # type: ignore[reportOptionalMemberAccess]
        pipeline_path = tmp_path / "pipeline_tfm.json"
        cert_path = tmp_path / "cert_tfm.json"
        pipeline_path.write_text(json.dumps(pipeline), encoding="utf-8")
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "--env",
                "test",
                "--force-metrics",
                "release-cert",
                "certify",
                "--pipeline",
                str(pipeline_path),
                "--approver",
                "qa",
                "--output",
                str(cert_path),
            ]
        )
        assert rc == 0
        rc_v = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "--env",
                "test",
                "--force-metrics",
                "release-cert",
                "verify",
                "--certificate",
                str(cert_path),
            ]
        )
        assert rc_v == 0
        cert = json.loads(cert_path.read_text(encoding="utf-8"))
        assert cert.get("schema_version") == SCHEMA_RELEASE_CERTIFICATE
