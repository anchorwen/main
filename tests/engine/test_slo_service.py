"""SLO service and CLI tests."""
import json

from apps.engine.cli import main
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer
from core.observability.metrics_collector import MetricsCollector
from core.observability.metric_names import (
    CYCLES_CIRCUIT_OPEN,
    CYCLES_ERRORS,
    CYCLES_THROTTLED,
    CYCLES_TOTAL,
    DISPATCH_FAILED,
    DISPATCH_PROTOCOL_VALIDATED,
    RECONCILIATION_BREACHED,
    RECONCILIATION_MATCHED,
)
from core.observability.slo_service import SloService
from core.observability.schema_versions import SCHEMA_SLO_REPORT


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


class TestSloService:
    def test_empty_metrics_healthy(self):
        report = SloService(MetricsCollector()).evaluate()
        assert report["schema_version"] == SCHEMA_SLO_REPORT
        assert report["status"] == "healthy"
        assert report["failed_objectives"] == []

    def test_decision_success_breach(self):
        m = MetricsCollector()
        m.inc(CYCLES_TOTAL, 100)
        m.inc(CYCLES_ERRORS, 5)
        report = SloService(m).evaluate()
        assert report["status"] == "breaching"
        assert "decision_success_rate" in report["failed_objectives"]
        assert report["objectives"]["decision_success_rate"]["value"] == 0.95

    def test_throttle_breach(self):
        m = MetricsCollector()
        m.inc(CYCLES_TOTAL, 100)
        m.inc(CYCLES_THROTTLED, 10)
        report = SloService(m).evaluate()
        assert "throttle_rate" in report["failed_objectives"]
        assert report["objectives"]["throttle_rate"]["direction"] == "below"

    def test_circuit_open_breach(self):
        m = MetricsCollector()
        m.inc(CYCLES_TOTAL, 100)
        m.inc(CYCLES_CIRCUIT_OPEN, 5)
        report = SloService(m).evaluate()
        assert "circuit_open_rate" in report["failed_objectives"]

    def test_dispatch_success_rate(self):
        m = MetricsCollector()
        m.inc(DISPATCH_PROTOCOL_VALIDATED, 98)
        m.inc(DISPATCH_FAILED, 2)
        report = SloService(m).evaluate()
        assert report["objectives"]["dispatch_success_rate"]["met"] is True

    def test_dispatch_breach(self):
        m = MetricsCollector()
        m.inc(DISPATCH_PROTOCOL_VALIDATED, 90)
        m.inc(DISPATCH_FAILED, 10)
        report = SloService(m).evaluate()
        assert "dispatch_success_rate" in report["failed_objectives"]

    def test_reconciliation_match_rate(self):
        m = MetricsCollector()
        m.inc(RECONCILIATION_MATCHED, 95)
        m.inc(RECONCILIATION_BREACHED, 5)
        report = SloService(m).evaluate()
        assert report["objectives"]["reconciliation_match_rate"]["met"] is True

    def test_error_budget_exhausted(self):
        m = MetricsCollector()
        m.inc(CYCLES_TOTAL, 100)
        m.inc(CYCLES_ERRORS, 20)
        report = SloService(m).evaluate()
        assert report["error_budget"]["exhausted_count"] >= 1

    def test_custom_objectives(self):
        m = MetricsCollector()
        m.inc(CYCLES_TOTAL, 10)
        m.inc(CYCLES_ERRORS, 1)
        service = SloService(m, objectives={
            "decision_success_rate": {"target": 0.8},
        })
        report = service.evaluate()
        assert report["status"] == "healthy"
        assert report["objective_count"] == 1

    def test_save_report(self, tmp_path):
        m = MetricsCollector()
        out = tmp_path / "slo.json"
        saved = SloService(m).save_report(str(out))
        assert saved == str(out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_SLO_REPORT


class TestSloContainerCLI:
    def test_container_has_slo_service(self, tmp_path):
        c = _container(tmp_path)
        assert c.slo_service is not None
        report = c.slo_service.evaluate()
        assert report["status"] == "healthy"

    def test_cli_slo(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "slo"])
        assert rc == 0

    def test_cli_slo_output(self, tmp_path):
        out = tmp_path / "slo.json"
        rc = main(["--base-dir", str(tmp_path), "slo", "--output", str(out)])
        assert rc == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_SLO_REPORT

    def test_cli_slo_test_env_no_metrics_collector(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "--env", "test", "slo"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == SCHEMA_SLO_REPORT
        assert out["raw_counters"] == {}
        assert out["status"] == "healthy"

    def test_cli_slo_production_with_no_metrics_flag(self, tmp_path, capsys):
        rc = main([
            "--base-dir", str(tmp_path), "--env", "production", "--no-metrics", "slo",
        ])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["raw_counters"] == {}
        assert out["status"] == "healthy"

    def test_slo_after_cycles(self, tmp_path):
        c = _container(tmp_path)
        orch = c.build_orchestrator()
        for _ in range(10):
            orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        report = c.slo_service.evaluate()
        assert report["raw_counters"][CYCLES_TOTAL] == 10
        assert "decision_success_rate" in report["objectives"]
