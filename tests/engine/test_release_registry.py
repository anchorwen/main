"""Release registry service and CLI tests."""

import json

from apps.engine.cli import main
from core.contracts.domain_keys import (
    PAYLOAD_KEY_GOVERNANCE_FOCUS,
    PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT,
    PAYLOAD_KEY_VALIDATION_MODE_COUNTS,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.release_registry import ReleaseRegistryService
from core.deployment.schema_versions import (
    SCHEMA_RELEASE_REGISTRY_EXPORT,
    SCHEMA_RELEASE_REGISTRY_SUMMARY,
    SCHEMA_RELEASE_REGISTRY_VERIFICATION,
)
from core.deployment.service_container import ServiceContainer
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE_REPORT


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


def _certificate(container, tmp_path, version="1.0.0"):
    pipeline = container.release_pipeline.run(
        version=version,
        output_dir=str(tmp_path / "pipeline" / version),
        validation_mode="fast",
    )
    return container.release_certification.certify(pipeline_summary=pipeline, approver="qa")


def _certificate_with_alpha_budget(container, tmp_path, version="1.0.0", warning_count=0):
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
        validation_mode="fast",
        alpha_budget_usage_report=alpha_report,
    )
    return container.release_certification.certify(pipeline_summary=pipeline, approver="qa")


class TestReleaseRegistryService:
    def test_register_certificate(self, tmp_path):
        c = _container(tmp_path / "data")
        cert = _certificate(c, tmp_path, version="1.0.0")
        record = c.release_registry.register(cert, actor="qa")
        assert record["id"] == "rel_000001"
        assert record["actor"] == "qa"
        assert record["version"] == "1.0.0"
        assert record["validation_mode"] == "fast"
        assert record["certified"] is True

    def test_register_from_file(self, tmp_path):
        c = _container(tmp_path / "data")
        cert = _certificate(c, tmp_path, version="1.0.1")
        cert_path = tmp_path / "cert.json"
        cert_path.write_text(json.dumps(cert), encoding="utf-8")
        record = c.release_registry.register(str(cert_path))
        assert record["version"] == "1.0.1"

    def test_list_filter_latest(self, tmp_path):
        c = _container(tmp_path / "data")
        c.release_registry.register(_certificate(c, tmp_path, version="1.0.0"))
        c.release_registry.register(_certificate(c, tmp_path, version="2.0.0"))
        assert len(c.release_registry.list_records()) == 2
        assert len(c.release_registry.list_records(version="1.0.0")) == 1
        assert c.release_registry.latest()["version"] == "2.0.0"
        assert c.release_registry.latest(version="1.0.0")["version"] == "1.0.0"

    def test_summary(self, tmp_path):
        c = _container(tmp_path / "data")
        c.release_registry.register(_certificate(c, tmp_path, version="1.0.0"))
        summary = c.release_registry.summarize()
        assert summary["schema_version"] == SCHEMA_RELEASE_REGISTRY_SUMMARY
        assert summary["record_count"] == 1
        assert summary["validation_mode"] == "fast"
        assert summary["certified_count"] == 1
        assert "final_audit" in summary
        assert "ops_maturity" in summary
        assert summary["final_audit"]["evidence_count"] == 1
        assert summary["final_audit"]["ready_count"] == 1
        assert summary["ops_maturity"]["record_count_with_score"] == 1
        assert summary["ops_maturity"]["score_avg"] is not None
        assert summary[PAYLOAD_KEY_VALIDATION_MODE_COUNTS]["fast"] == 1
        assert "governance_focus" in summary
        assert "governance_warning_count" in summary
        assert summary["governance_warning_count"] >= 0

    def test_register_records_governance_summary_fields(self, tmp_path):
        c = _container(tmp_path / "data")
        cert = _certificate(c, tmp_path, version="1.0.0")
        record = c.release_registry.register(cert, actor="qa")
        assert record["summary"]["final_audit_present"] is True
        assert record["summary"]["final_audit_ready_for_production"] is True
        assert record["summary"]["ops_maturity_present"] is True
        assert record["summary"]["ops_maturity_score"] is not None
        assert record["summary"]["validation_mode"] == "fast"
        assert "governance_focus" in record["summary"]
        assert "governance_warning_count" in record["summary"]

    def test_register_normalizes_malformed_governance_fields(self, tmp_path):
        c = _container(tmp_path / "data")
        cert = _certificate(c, tmp_path, version="1.0.2")
        cert["governance_focus"] = "invalid"
        cert["governance_warning_count"] = "3"
        record = c.release_registry.register(cert, actor="qa")
        assert record["summary"]["governance_focus"] == []
        assert record["summary"]["governance_warning_count"] == 0
        summary = c.release_registry.summarize()
        assert summary["governance_focus"] == []
        assert summary["governance_warning_count"] == 0

    def test_register_filters_non_dict_governance_focus_items(self, tmp_path):
        c = _container(tmp_path / "data")
        cert = _certificate(c, tmp_path, version="1.0.3")
        cert["governance_focus"] = [{"name": "ok"}, "invalid", 1, {"level": "warn"}]
        cert["governance_warning_count"] = 2
        record = c.release_registry.register(cert, actor="qa")
        assert record["summary"]["governance_focus"] == [{"name": "ok"}, {"level": "warn"}]
        summary = c.release_registry.summarize()
        assert summary["governance_focus"] == [{"name": "ok"}, {"level": "warn"}]

    def test_register_derives_warning_count_from_focus(self, tmp_path):
        c = _container(tmp_path / "data")
        cert = _certificate(c, tmp_path, version="1.0.4")
        cert["governance_focus"] = [{"name": "a", "level": "pass"}, {"name": "b", "status": "warn"}]
        cert["governance_warning_count"] = 99
        record = c.release_registry.register(cert, actor="qa")
        assert record["summary"]["governance_warning_count"] == 1
        summary = c.release_registry.summarize()
        assert summary["governance_warning_count"] == 1

    def test_register_certificate_records_alpha_budget_summary(self, tmp_path):
        c = _container(tmp_path / "data")
        cert = _certificate_with_alpha_budget(c, tmp_path, version="1.2.0", warning_count=1)
        record = c.release_registry.register(cert, actor="qa")
        assert record["summary"]["alpha_budget_evidence_present"] is True
        assert record["summary"]["alpha_budget_warning_count"] == 1

    def test_summary_includes_alpha_budget_counts(self, tmp_path):
        c = _container(tmp_path / "data")
        c.release_registry.register(
            _certificate_with_alpha_budget(c, tmp_path, version="1.2.1", warning_count=0)
        )
        c.release_registry.register(
            _certificate_with_alpha_budget(c, tmp_path, version="1.2.2", warning_count=1)
        )
        c.release_registry.register(_certificate(c, tmp_path, version="1.2.3"))
        summary = c.release_registry.summarize()
        assert summary["alpha_budget"]["evidence_count"] == 2
        assert summary["alpha_budget"]["missing_evidence_count"] == 1
        assert summary["alpha_budget"]["warning_release_count"] == 1
        assert summary["alpha_budget"]["warning_total"] == 1
        assert summary[PAYLOAD_KEY_VALIDATION_MODE_COUNTS]["fast"] == 3

    def test_verify_record_success(self, tmp_path):
        c = _container(tmp_path / "data")
        cert = _certificate(c, tmp_path, version="1.0.0")
        record = c.release_registry.register(cert)
        verify = c.release_registry.verify_record(record["id"], cert)
        assert verify["schema_version"] == SCHEMA_RELEASE_REGISTRY_VERIFICATION
        assert verify["verified"] is True

    def test_verify_record_detects_tamper(self, tmp_path):
        c = _container(tmp_path / "data")
        cert = _certificate(c, tmp_path, version="1.0.0")
        record = c.release_registry.register(cert)
        tampered = dict(cert)
        tampered["approver"] = "evil"
        verify = c.release_registry.verify_record(record["id"], tampered)
        assert verify["verified"] is False

    def test_verify_missing_record(self, tmp_path):
        registry = ReleaseRegistryService(str(tmp_path))
        verify = registry.verify_record("missing", {"certificate_fingerprint": "x"})
        assert verify["verified"] is False
        assert verify["error"] == "record not found"

    def test_export_and_clear(self, tmp_path):
        c = _container(tmp_path / "data")
        c.release_registry.register(_certificate(c, tmp_path, version="1.0.0"))
        out = tmp_path / "registry_export.json"
        saved = c.release_registry.export(str(out))
        assert saved == str(out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_RELEASE_REGISTRY_EXPORT
        cleared = c.release_registry.clear()
        assert cleared["cleared"] == 1
        assert c.release_registry.list_records() == []

    def test_container_has_registry(self, tmp_path):
        c = _container(tmp_path)
        assert c.release_registry is not None


class TestReleaseRegistryCLI:
    def test_cli_register_list_summary_latest_verify(self, tmp_path):
        c = _container(tmp_path / "data")
        cert = _certificate(c, tmp_path, version="3.0.0")
        cert_path = tmp_path / "cert.json"
        cert_path.write_text(json.dumps(cert), encoding="utf-8")
        rc_register = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "release-registry",
                "register",
                "--certificate",
                str(cert_path),
                "--actor",
                "qa",
            ]
        )
        records = c.release_registry.list_records()
        rc_list = main(["--base-dir", str(tmp_path / "data"), "release-registry", "list"])
        rc_summary = main(["--base-dir", str(tmp_path / "data"), "release-registry", "summary"])
        rc_latest = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "release-registry",
                "latest",
                "--version",
                "3.0.0",
            ]
        )
        rc_verify = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "release-registry",
                "verify",
                "--record-id",
                records[0]["id"],
                "--certificate",
                str(cert_path),
            ]
        )
        assert rc_register == 0
        assert rc_list == 0
        assert rc_summary == 0
        assert rc_latest == 0
        assert rc_verify == 0

    def test_cli_register_requires_certificate(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "release-registry", "register"])
        assert rc == 1

    def test_cli_summary_test_env_force_metrics(self, tmp_path, capsys):
        rc = main(
            [
                "--base-dir",
                str(tmp_path),
                "--env",
                "test",
                "--force-metrics",
                "release-registry",
                "summary",
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == SCHEMA_RELEASE_REGISTRY_SUMMARY
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in out
        assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in out
        assert rc == 0

    def test_cli_latest_exposes_governance_summary_fields(self, tmp_path, capsys):
        c = _container(tmp_path / "data")
        cert = _certificate(c, tmp_path, version="3.1.0")
        cert_path = tmp_path / "cert_latest.json"
        cert_path.write_text(json.dumps(cert), encoding="utf-8")
        assert (
            main(
                [
                    "--base-dir",
                    str(tmp_path / "data"),
                    "release-registry",
                    "register",
                    "--certificate",
                    str(cert_path),
                    "--actor",
                    "qa",
                ]
            )
            == 0
        )
        capsys.readouterr()
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "release-registry",
                "latest",
                "--version",
                "3.1.0",
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in out["summary"]
        assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in out["summary"]
        assert rc == 0

    def test_cli_list_exposes_governance_summary_fields(self, tmp_path, capsys):
        c = _container(tmp_path / "data")
        cert = _certificate(c, tmp_path, version="3.2.0")
        cert_path = tmp_path / "cert_list.json"
        cert_path.write_text(json.dumps(cert), encoding="utf-8")
        assert (
            main(
                [
                    "--base-dir",
                    str(tmp_path / "data"),
                    "release-registry",
                    "register",
                    "--certificate",
                    str(cert_path),
                    "--actor",
                    "qa",
                ]
            )
            == 0
        )
        capsys.readouterr()
        rc = main(["--base-dir", str(tmp_path / "data"), "release-registry", "list"])
        out = json.loads(capsys.readouterr().out)
        assert out["records"]
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in out["records"][0]["summary"]
        assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in out["records"][0]["summary"]
        assert rc == 0

    def test_cli_verify_requires_args(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "release-registry", "verify"])
        assert rc == 1

    def test_cli_export_requires_output(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "release-registry", "export"])
        assert rc == 1

    def test_cli_export_and_clear(self, tmp_path):
        c = _container(tmp_path / "data")
        c.release_registry.register(_certificate(c, tmp_path, version="4.0.0"))
        out = tmp_path / "export.json"
        rc_export = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "release-registry",
                "export",
                "--output",
                str(out),
            ]
        )
        rc_clear = main(["--base-dir", str(tmp_path / "data"), "release-registry", "clear"])
        assert rc_export == 0
        assert rc_clear == 0
        assert out.exists()
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in payload["summary"]
        assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in payload["summary"]
