import json

from apps.engine.diagnostics_cli import DiagnosticsCLI
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer


def _build_container(tmp_path, **overrides):
    cfg = EnvironmentConfig.development(str(tmp_path), **overrides)
    return ServiceContainer(cfg).build()


class TestDiagnosticsCLIHealth:
    def test_health_command(self, tmp_path):
        c = _build_container(tmp_path)
        cli = DiagnosticsCLI(c)
        output = json.loads(cli.run(["health"]))
        assert output["liveness"]["status"] == "alive"
        assert output["readiness"]["status"] == "ready"

    def test_ready_command(self, tmp_path):
        c = _build_container(tmp_path)
        cli = DiagnosticsCLI(c)
        output = json.loads(cli.run(["ready"]))
        assert output["status"] == "ready"


class TestDiagnosticsCLIMetrics:
    def test_metrics_command(self, tmp_path):
        c = _build_container(tmp_path)
        c.metrics.inc("test_counter", 5)
        c.metrics.gauge("test_gauge", 42)
        cli = DiagnosticsCLI(c)
        output = json.loads(cli.run(["metrics"]))
        assert output["counters"]["test_counter"] == 5
        assert output["gauges"]["test_gauge"] == 42

    def test_metrics_disabled(self, tmp_path):
        c = _build_container(tmp_path, enable_metrics=False)
        cli = DiagnosticsCLI(c)
        output = json.loads(cli.run(["metrics"]))
        assert "error" in output


class TestDiagnosticsCLIBrain:
    def test_brain_list(self, tmp_path):
        c = _build_container(tmp_path)
        c.governance_service.register_brain("alpha_v1", "live")
        c.governance_service.register_brain("beta_v1", "candidate")
        cli = DiagnosticsCLI(c)
        output = json.loads(cli.run(["brain"]))
        assert output["count"] == 2

    def test_brain_detail(self, tmp_path):
        c = _build_container(tmp_path)
        c.governance_service.register_brain("alpha_v1", "live")
        cli = DiagnosticsCLI(c)
        output = json.loads(cli.run(["brain", "--brain-id", "alpha_v1"]))
        assert output["governance_state"]["status"] == "live"


class TestDiagnosticsCLIAudit:
    def test_audit_command(self, tmp_path):
        c = _build_container(tmp_path)
        c.audit_log.log(event_type="test", severity="info")
        c.audit_log.log(event_type="test2", severity="warning")
        cli = DiagnosticsCLI(c)
        output = json.loads(cli.run(["audit"]))
        assert output["count"] == 2

    def test_audit_filter_severity(self, tmp_path):
        c = _build_container(tmp_path)
        c.audit_log.log(event_type="t1", severity="info")
        c.audit_log.log(event_type="t2", severity="warning")
        cli = DiagnosticsCLI(c)
        output = json.loads(cli.run(["audit", "--severity", "warning"]))
        assert output["count"] == 1
        assert output["entries"][0]["severity"] == "warning"


class TestDiagnosticsCLISnapshot:
    def test_snapshot_command(self, tmp_path):
        c = _build_container(tmp_path)
        c.metrics.inc("x")
        cli = DiagnosticsCLI(c)
        output = json.loads(cli.run(["snapshot"]))
        assert "generated_at" in output
        assert output["metrics"] is not None


class TestDiagnosticsCLIPositionsOrders:
    def test_positions_empty(self, tmp_path):
        c = _build_container(tmp_path)
        cli = DiagnosticsCLI(c)
        output = json.loads(cli.run(["positions"]))
        assert output["positions"] == []
        assert output["risk_context"]["open_position_count"] == 0

    def test_orders_empty(self, tmp_path):
        c = _build_container(tmp_path)
        cli = DiagnosticsCLI(c)
        output = json.loads(cli.run(["orders"]))
        assert output["orders"] == []


class TestContainerWithOrchestrator:
    def test_build_orchestrator(self, tmp_path):
        c = _build_container(tmp_path)
        from datetime import datetime
        from pathlib import Path

        from apps.engine.runtime_loop import RuntimeLoop
        from core.contracts.domain.decision_intent import DecisionIntent
        from core.contracts.enums import DecisionAction, DecisionSide

        intent = DecisionIntent(
            schema_version="v1",
            intent_id="i1",
            candidate_id="c1",
            snapshot_id="s1",
            event_time=datetime(2026, 4, 24, 12, 0, 0),
            compiled_at=datetime(2026, 4, 24, 12, 0, 1),
            symbol="XAUUSD",
            venue="MT5",
            action=DecisionAction.OPEN,
            side=DecisionSide.LONG,
            conviction=0.85,
            priority="high",
        )
        snap = type(
            "CS",
            (),
            {
                "mode_state": type(
                    "MS", (), {"current_mode": type("M", (), {"value": "normal"})()}
                )(),
                "active_overrides": [],
            },
        )()
        feature = type(
            "FS",
            (),
            {
                "snapshot_id": "s1",
                "event_time": datetime(2026, 4, 24, 12, 0, 0),
                "symbol": "XAUUSD",
                "venue": "MT5",
            },
        )()
        type(
            "DC",
            (),
            {
                "regime_state": {"primary_regime": "trend"},
                "supporting_brains": [],
                "opposing_brains": [],
            },
        )()
        record = type("R", (), {"record_id": "r1"})()

        loop = RuntimeLoop(
            control_snapshot_service=type("CSS", (), {"freeze": lambda self, **kw: snap})(),
            feature_service=type("FS_svc", (), {"build_snapshot": lambda self, trigger: feature})(),
            brain_run_service=type("BRS", (), {"run_active_brains": lambda self, **kw: []})(),
            parliament_adapter=c.parliament_service,
            override_resolver=type("OR", (), {"resolve": lambda self, **kw: []})(),
            decision_compiler=type("DC", (), {"compile_intent": lambda self, **kw: intent})(),
            decision_record_writer=type(
                "DRW",
                (),
                {
                    "seed_record": lambda self, **kw: (record, Path(tmp_path) / "x.jsonl"),
                },
            )(),
            intent_message_builder=c.message_builder,
            communication_dispatcher=c.dispatcher,
            communication_record_writer=c.communication_writer,
            risk_evaluation_service=c.risk_service,
        )

        orch = c.build_orchestrator(loop)
        assert orch is not None
        assert c.orchestrator is orch

        outcome = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        assert outcome.decision_result.verdict.is_allowed()

    def test_governance_rule_engine_built(self, tmp_path):
        c = _build_container(tmp_path)
        assert c.governance_rule_engine is not None
        c.governance_service.register_brain("test_brain", "live")
        fired = c.governance_rule_engine.evaluate(
            {
                "test_brain": {"health_signal": "critical", "sample_count": 20},
            }
        )
        assert len(fired) >= 1
        assert c.governance_service.get_brain_state("test_brain")["status"] == "frozen"

    def test_health_check_built(self, tmp_path):
        c = _build_container(tmp_path)
        assert c.health_check is not None
        assert c.health_check.liveness()["status"] == "alive"
