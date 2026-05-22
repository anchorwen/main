"""Evidence bundle service and CLI tests."""

import json
from pathlib import Path

from apps.engine.cli import main
from core.contracts.domain_keys import (
    ENGINE_CONFIG_KEY_HOT_RELOAD,
    ENGINE_CONFIG_KEY_RUNTIME_METRICS,
    EVIDENCE_SECTION_ALPHA_BUDGET_USAGE,
    EVIDENCE_SECTION_ENGINE_CONFIG,
    EVIDENCE_SECTION_MANIFEST,
    PAYLOAD_KEY_GOVERNANCE_FOCUS,
    PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT,
    PAYLOAD_KEY_VALIDATION_MODE,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.schema_versions import (
    SCHEMA_ENGINE_CONFIG_EVIDENCE,
    SCHEMA_EVIDENCE_BUNDLE,
    SCHEMA_EVIDENCE_MANIFEST,
    SCHEMA_EVIDENCE_VERIFICATION,
)
from core.deployment.service_container import ServiceContainer
from core.observability.metric_names import ENGINE_CONFIG_RELOAD_TOTAL
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE_REPORT


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


class TestEvidenceBundleService:
    def test_build_bundle_creates_files(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.evidence_bundle.build_bundle(str(tmp_path / "evidence"), label="rel1")
        assert result["schema_version"] == SCHEMA_EVIDENCE_BUNDLE
        assert result["label"] == "rel1"
        assert result["file_count"] == 10
        bundle_dir = Path(result["bundle_dir"])
        for name in [
            "readiness",
            "gate",
            "slo",
            "preflight",
            "doctor",
            "diagnostics",
            "final_audit",
            "ops_maturity",
            EVIDENCE_SECTION_ENGINE_CONFIG,
            EVIDENCE_SECTION_MANIFEST,
        ]:
            assert (bundle_dir / f"{name}.json").exists()

    def test_build_bundle_accepts_fast_validation_mode(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.evidence_bundle.build_bundle(
            str(tmp_path / "evidence"),
            label="rel_fast",
            validation_mode="fast",
        )
        assert result["schema_version"] == SCHEMA_EVIDENCE_BUNDLE
        assert result[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        assert manifest[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        assert (Path(result["bundle_dir"]) / "readiness.json").exists()

    def test_manifest_normalizes_malformed_final_audit_governance_fields(self, tmp_path):
        c = _container(tmp_path / "data")
        original_build_report = c.final_audit.build_report

        def _malformed_build_report(*, validation_mode=None):
            report = original_build_report(validation_mode=validation_mode)
            report["summary"]["governance_focus"] = [{"name": "ok"}, "bad", 7]
            report["summary"]["governance_warning_count"] = "5"
            return report

        c.final_audit.build_report = _malformed_build_report
        result = c.evidence_bundle.build_bundle(str(tmp_path / "evidence"), label="rel_norm")
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        assert manifest["summary"][PAYLOAD_KEY_GOVERNANCE_FOCUS] == [{"name": "ok"}]
        assert manifest["summary"][PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 0

    def test_build_manifest_normalizes_malformed_governance_summary_input(self, tmp_path):
        c = _container(tmp_path / "data")
        manifest = c.evidence_bundle._build_manifest(
            label="rel_manifest_norm",
            target_dir=tmp_path / "evidence",
            files=[],
            validation_mode="fast",
            governance_summary={
                PAYLOAD_KEY_GOVERNANCE_FOCUS: "invalid",
                PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT: "-2",
            },
        )
        assert manifest["summary"][PAYLOAD_KEY_GOVERNANCE_FOCUS] == []
        assert manifest["summary"][PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 0

    def test_build_manifest_derives_warning_count_from_focus(self, tmp_path):
        c = _container(tmp_path / "data")
        manifest = c.evidence_bundle._build_manifest(
            label="rel_manifest_warn_derivation",
            target_dir=tmp_path / "evidence",
            files=[],
            validation_mode="fast",
            governance_summary={
                PAYLOAD_KEY_GOVERNANCE_FOCUS: [
                    {"name": "registry_deep_validation_present", "level": "warn"},
                    {"control_id": "GOV-004", "status": "pass"},
                ],
                PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT: 88,
            },
        )
        assert manifest["summary"][PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT] == 1

    def test_manifest_contains_checksums(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.evidence_bundle.build_bundle(str(tmp_path / "evidence"), label="rel2")
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        assert manifest["schema_version"] == SCHEMA_EVIDENCE_MANIFEST
        assert manifest["summary"]["file_count"] == 10
        assert PAYLOAD_KEY_GOVERNANCE_FOCUS in manifest["summary"]
        assert PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT in manifest["summary"]
        for item in manifest["files"]:
            assert len(item["sha256"]) == 64
            assert item["size_bytes"] > 0

    def test_verify_bundle_success(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.evidence_bundle.build_bundle(str(tmp_path / "evidence"), label="rel3")
        verify = c.evidence_bundle.verify_bundle(result["manifest_path"])
        assert verify["schema_version"] == SCHEMA_EVIDENCE_VERIFICATION
        assert verify[PAYLOAD_KEY_VALIDATION_MODE] == "deep"
        assert verify["verified"] is True
        assert verify["failed_count"] == 0

    def test_verify_bundle_detects_tamper(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.evidence_bundle.build_bundle(str(tmp_path / "evidence"), label="rel4")
        readiness_path = Path(result["bundle_dir"]) / "readiness.json"
        readiness_path.write_text('{"tampered": true}', encoding="utf-8")
        verify = c.evidence_bundle.verify_bundle(result["manifest_path"])
        assert verify["verified"] is False
        assert verify["failed_count"] >= 1

    def test_bundle_summary_contains_gate_and_ready(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.evidence_bundle.build_bundle(str(tmp_path / "evidence"), label="rel5")
        assert result["gate_decision"] in {"allow", "warn", "block"}
        assert isinstance(result["ready"], bool)
        assert "manifest_checksum" in result

    def test_build_bundle_includes_alpha_budget_usage_report(self, tmp_path):
        c = _container(tmp_path / "data")
        alpha_report = {
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": "2026-01-01",
            "alpha_count": 1,
            "warning_count": 1,
            "warnings": [
                {
                    "alpha_id": "alpha1",
                    "type": "daily_usage_high",
                    "usage_ratio": 0.8,
                    "threshold": 0.8,
                }
            ],
        }
        result = c.evidence_bundle.build_bundle(
            str(tmp_path / "evidence"),
            label="rel_alpha",
            alpha_budget_usage_report=alpha_report,
        )
        assert result["file_count"] == 11
        assert EVIDENCE_SECTION_ALPHA_BUDGET_USAGE in result["sections"]
        bundle_dir = Path(result["bundle_dir"])
        payload = json.loads((bundle_dir / "alpha_budget_usage.json").read_text(encoding="utf-8"))
        assert payload["warning_count"] == 1
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        assert EVIDENCE_SECTION_ALPHA_BUDGET_USAGE in manifest["summary"]["sections"]

    def test_engine_config_section_shape(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.evidence_bundle.build_bundle(str(tmp_path / "evidence"), label="ecfg1")
        payload = json.loads(
            (Path(result["bundle_dir"]) / "engine_config.json").read_text(encoding="utf-8"),
        )
        assert payload["schema_version"] == SCHEMA_ENGINE_CONFIG_EVIDENCE
        assert payload[ENGINE_CONFIG_KEY_HOT_RELOAD]["config_path"] is not None
        assert "effective" in payload
        assert payload[ENGINE_CONFIG_KEY_RUNTIME_METRICS][ENGINE_CONFIG_RELOAD_TOTAL] >= 0.0

    def test_engine_config_omits_runtime_metrics_without_collector(self, tmp_path):
        c = ServiceContainer(
            EnvironmentConfig.development(str(tmp_path / "data"), enable_metrics=False),
        ).build()
        result = c.evidence_bundle.build_bundle(str(tmp_path / "evidence"), label="ecfg0m")
        payload = json.loads(
            (Path(result["bundle_dir"]) / "engine_config.json").read_text(encoding="utf-8"),
        )
        assert payload["schema_version"] == SCHEMA_ENGINE_CONFIG_EVIDENCE
        assert ENGINE_CONFIG_KEY_RUNTIME_METRICS not in payload
        assert c.metrics is None

    def test_container_has_evidence_bundle(self, tmp_path):
        c = _container(tmp_path)
        assert c.evidence_bundle is not None


class TestEvidenceCLI:
    def test_cli_evidence_build(self, tmp_path):
        out = tmp_path / "evidence"
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "evidence",
                "build",
                "--output-dir",
                str(out),
                "--label",
                "cli1",
            ]
        )
        assert rc == 0
        assert (out / "cli1" / "manifest.json").exists()

    def test_cli_evidence_build_fast_validation_mode(self, tmp_path):
        out = tmp_path / "evidence"
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "evidence",
                "build",
                "--output-dir",
                str(out),
                "--label",
                "cli_fast",
                "--validation-mode",
                "fast",
            ]
        )
        assert rc == 0

    def test_cli_evidence_build_test_env_omits_runtime_metrics(self, tmp_path):
        out = tmp_path / "evidence"
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "--env",
                "test",
                "evidence",
                "build",
                "--output-dir",
                str(out),
                "--label",
                "cli_test_nom",
            ]
        )
        assert rc == 0
        ec = json.loads((out / "cli_test_nom" / "engine_config.json").read_text(encoding="utf-8"))
        assert ec["schema_version"] == SCHEMA_ENGINE_CONFIG_EVIDENCE
        assert ENGINE_CONFIG_KEY_RUNTIME_METRICS not in ec

    def test_cli_evidence_build_test_env_force_metrics_includes_runtime_metrics(self, tmp_path):
        out = tmp_path / "evidence"
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "--env",
                "test",
                "--force-metrics",
                "evidence",
                "build",
                "--output-dir",
                str(out),
                "--label",
                "cli_test_fm",
            ]
        )
        assert rc == 0
        ec = json.loads((out / "cli_test_fm" / "engine_config.json").read_text(encoding="utf-8"))
        assert ENGINE_CONFIG_KEY_RUNTIME_METRICS in ec
        assert ec[ENGINE_CONFIG_KEY_RUNTIME_METRICS][ENGINE_CONFIG_RELOAD_TOTAL] >= 0.0

    def test_cli_evidence_verify(self, tmp_path):
        out = tmp_path / "evidence"
        c = _container(tmp_path / "data")
        built = c.evidence_bundle.build_bundle(str(out), label="cli2")
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "evidence",
                "verify",
                "--manifest",
                built["manifest_path"],
            ]
        )
        assert rc == 0

    def test_cli_evidence_verify_requires_manifest(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path / "data"), "evidence", "verify"])
        assert rc == 1

    def test_cli_evidence_verify_tampered(self, tmp_path):
        out = tmp_path / "evidence"
        c = _container(tmp_path / "data")
        built = c.evidence_bundle.build_bundle(str(out), label="cli3")
        (Path(built["bundle_dir"]) / "slo.json").write_text("{}", encoding="utf-8")
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "evidence",
                "verify",
                "--manifest",
                built["manifest_path"],
            ]
        )
        assert rc == 1


class TestEvidenceContent:
    def test_bundle_sections_are_json_objects(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.evidence_bundle.build_bundle(str(tmp_path / "evidence"), label="rel6")
        bundle_dir = Path(result["bundle_dir"])
        for file_path in bundle_dir.glob("*.json"):
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            assert isinstance(payload, dict)

    def test_manifest_lists_all_sections(self, tmp_path):
        c = _container(tmp_path / "data")
        result = c.evidence_bundle.build_bundle(str(tmp_path / "evidence"), label="rel7")
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        sections = set(manifest["summary"]["sections"])
        assert sections == {
            "readiness",
            "gate",
            "slo",
            "preflight",
            "doctor",
            "diagnostics",
            "final_audit",
            "ops_maturity",
            EVIDENCE_SECTION_ENGINE_CONFIG,
            EVIDENCE_SECTION_MANIFEST,
        }

    def test_cli_evidence_build_with_alpha_budget_usage_report(self, tmp_path):
        out = tmp_path / "evidence"
        report = tmp_path / "alpha_budget_usage_report.json"
        report.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
                    "usage_date": "2026-01-01",
                    "alpha_count": 1,
                    "warning_count": 0,
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        rc = main(
            [
                "--base-dir",
                str(tmp_path / "data"),
                "evidence",
                "build",
                "--output-dir",
                str(out),
                "--label",
                "cli_alpha",
                "--alpha-budget-usage-report",
                str(report),
            ]
        )
        assert rc == 0
        assert (out / "cli_alpha" / "alpha_budget_usage.json").exists()
        manifest = json.loads((out / "cli_alpha" / "manifest.json").read_text(encoding="utf-8"))
        assert EVIDENCE_SECTION_ALPHA_BUDGET_USAGE in manifest["summary"]["sections"]
