"""Release gate service and CLI tests."""

import json

from apps.engine.cli import main
from core.contracts.domain_keys import (
    EVIDENCE_SECTION_ALPHA_BUDGET_USAGE,
    PAYLOAD_KEY_VALIDATION_MODE,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.release_gate import ReleaseGateService
from core.deployment.schema_versions import SCHEMA_RELEASE_GATE
from core.deployment.service_container import ServiceContainer
from core.observability.metric_names import CYCLES_ERRORS, CYCLES_TOTAL
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE_REPORT


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


class TestReleaseGateService:
    def test_gate_allows_clean_system(self, tmp_path):
        c = _container(tmp_path)
        report = c.release_gate.evaluate()
        assert report["schema_version"] == SCHEMA_RELEASE_GATE
        assert report["decision"] == "allow"
        assert report["allowed"] is True
        assert report["summary"]["block_count"] == 0

    def test_gate_accepts_fast_validation_mode(self, tmp_path):
        c = _container(tmp_path)
        report = c.release_gate.evaluate(validation_mode="fast")
        assert report["schema_version"] == SCHEMA_RELEASE_GATE
        assert report[PAYLOAD_KEY_VALIDATION_MODE] == "fast"
        assert report["decision"] in {"allow", "warn", "block"}

    def test_gate_blocks_missing_service(self, tmp_path):
        c = _container(tmp_path)
        c.risk_service = None
        report = c.release_gate.evaluate()
        assert report["decision"] == "block"
        assert report["allowed"] is False
        assert "readiness" in report["summary"]["blocking_signals"]

    def test_gate_warns_non_strict_slo_breach(self, tmp_path):
        c = _container(tmp_path)
        c.metrics.inc(CYCLES_TOTAL, 100)
        c.metrics.inc(CYCLES_ERRORS, 10)
        report = c.release_gate.evaluate(strict=False)
        assert report["decision"] == "warn"
        assert report["allowed"] is False
        assert "slo" in report["summary"]["warning_signals"]

    def test_gate_blocks_strict_slo_breach(self, tmp_path):
        c = _container(tmp_path)
        c.metrics.inc(CYCLES_TOTAL, 100)
        c.metrics.inc(CYCLES_ERRORS, 10)
        report = c.release_gate.evaluate(strict=True)
        assert report["decision"] == "block"
        assert "slo" in report["summary"]["warning_signals"]

    def test_gate_blocks_alpha_budget_warnings_in_strict_mode(self, tmp_path):
        c = _container(tmp_path)
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
        report = c.release_gate.evaluate(strict=True, alpha_budget_usage_report=alpha_report)
        assert report["decision"] == "block"
        assert EVIDENCE_SECTION_ALPHA_BUDGET_USAGE in report["summary"]["warning_signals"]
        assert report["evidence"][EVIDENCE_SECTION_ALPHA_BUDGET_USAGE]["warning_count"] == 1

    def test_gate_warns_alpha_budget_warnings_in_non_strict_mode(self, tmp_path):
        c = _container(tmp_path)
        alpha_report = {
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": "2026-01-01",
            "alpha_count": 1,
            "warning_count": 1,
            "warnings": [
                {
                    "alpha_id": "alpha1",
                    "type": "daily_usage_exhausted",
                    "usage_ratio": 1.0,
                    "threshold": 1.0,
                }
            ],
        }
        report = c.release_gate.evaluate(strict=False, alpha_budget_usage_report=alpha_report)
        assert report["decision"] == "warn"
        assert report["allowed"] is False
        signal = [
            item
            for item in report["signals"]
            if item["name"] == EVIDENCE_SECTION_ALPHA_BUDGET_USAGE
        ][0]
        assert signal["level"] == "warn"
        assert signal["detail"]["warning_count"] == 1

    def test_gate_allows_clean_alpha_budget_report(self, tmp_path):
        c = _container(tmp_path)
        alpha_report = {
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": "2026-01-01",
            "alpha_count": 1,
            "warning_count": 0,
            "warnings": [],
        }
        report = c.release_gate.evaluate(strict=True, alpha_budget_usage_report=alpha_report)
        assert report["decision"] == "allow"
        assert EVIDENCE_SECTION_ALPHA_BUDGET_USAGE not in report["summary"]["warning_signals"]
        assert report["summary"]["signal_count"] == 5

    def test_signal_shape(self, tmp_path):
        c = _container(tmp_path)
        report = c.release_gate.evaluate()
        names = {s["name"] for s in report["signals"]}
        assert names == {"readiness", "preflight", "slo", "config"}
        for signal in report["signals"]:
            assert set(signal) == {"name", "level", "passed", "detail"}
            assert signal["level"] in {"allow", "warn", "block"}

    def test_evidence_compact(self, tmp_path):
        c = _container(tmp_path)
        report = c.release_gate.evaluate()
        assert "readiness" in report["evidence"]
        assert "preflight" in report["evidence"]
        assert "slo" in report["evidence"]
        assert "config" in report["evidence"]

    def test_save_report(self, tmp_path):
        c = _container(tmp_path)
        out = tmp_path / "gate.json"
        saved = c.release_gate.save_report(str(out))
        assert saved == str(out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_RELEASE_GATE

    def test_safe_exception_blocks(self, tmp_path):
        c = _container(tmp_path)

        class BrokenReadiness:
            def build_report(self):
                raise RuntimeError("boom")

        c.release_readiness = BrokenReadiness()
        report = ReleaseGateService(c).evaluate()
        assert report["decision"] == "block"
        assert "readiness" in report["summary"]["blocking_signals"]


class TestReleaseGateCLI:
    def test_cli_gate(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "gate"])
        assert rc == 0

    def test_cli_gate_output(self, tmp_path):
        out = tmp_path / "gate.json"
        rc = main(["--base-dir", str(tmp_path), "gate", "--output", str(out)])
        assert rc == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_RELEASE_GATE

    def test_cli_gate_fast_validation_mode(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "gate", "--validation-mode", "fast"])
        assert rc in (0, 1)

    def test_cli_gate_blocks_on_slo_strict(self, tmp_path):
        c = _container(tmp_path)
        c.metrics.inc(CYCLES_TOTAL, 100)
        c.metrics.inc(CYCLES_ERRORS, 50)
        report = c.release_gate.evaluate(strict=True)
        assert report["decision"] == "block"

    def test_container_has_gate(self, tmp_path):
        c = _container(tmp_path)
        assert c.release_gate is not None

    def test_cli_gate_blocks_with_alpha_budget_usage_report(self, tmp_path, capsys):
        report_path = tmp_path / "alpha_budget_usage_report.json"
        report_path.write_text(
            json.dumps(
                {
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
            ),
            encoding="utf-8",
        )
        rc = main(
            ["--base-dir", str(tmp_path), "gate", "--alpha-budget-usage-report", str(report_path)]
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert payload["decision"] == "block"
        assert EVIDENCE_SECTION_ALPHA_BUDGET_USAGE in payload["summary"]["warning_signals"]

    def test_cli_gate_non_strict_warns_with_alpha_budget_usage_report(self, tmp_path, capsys):
        report_path = tmp_path / "alpha_budget_usage_report.json"
        report_path.write_text(
            json.dumps(
                {
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
            ),
            encoding="utf-8",
        )
        rc = main(
            [
                "--base-dir",
                str(tmp_path),
                "gate",
                "--alpha-budget-usage-report",
                str(report_path),
                "--non-strict",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["decision"] == "warn"

    def test_cli_gate_test_env_honors_base_dir_and_exits(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "--env", "test", "gate"])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema_version"] == SCHEMA_RELEASE_GATE
        assert payload["decision"] == "block"

    def test_cli_gate_production_no_metrics_valid_schema(self, tmp_path, capsys):
        main(
            [
                "--base-dir",
                str(tmp_path),
                "--env",
                "production",
                "--no-metrics",
                "gate",
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == SCHEMA_RELEASE_GATE
        assert out["decision"] in {"allow", "warn", "block"}
