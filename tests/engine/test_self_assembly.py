"""Tests that ServiceContainer can self-assemble a complete system
with zero manual wiring."""

from core.deployment.environment_config import EnvironmentConfig, Environment
from core.deployment.service_container import ServiceContainer


class TestSelfAssembly:
    def test_build_runtime_loop_from_container(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        loop = c.build_runtime_loop()

        assert loop is not None
        assert c.runtime_loop is loop

        result = loop.run_decision_cycle(
            trigger={"symbol": "XAUUSD"},
            feature_source={"f": 1.0},
        )

        assert result.feature_snapshot.symbol == "XAUUSD"
        assert result.intent is not None
        assert result.verdict is not None
        assert result.record is not None

    def test_build_orchestrator_auto_creates_loop(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        orch = c.build_orchestrator()

        assert c.runtime_loop is not None
        assert c.orchestrator is not None

        outcome = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        assert outcome.decision_result is not None

    def test_full_zero_config_lifecycle(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        orch = c.build_orchestrator()

        outcome = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})

        if outcome.decision_result and outcome.decision_result.communication_record:
            msg_id = outcome.decision_result.communication_record.message_id
            orch.process_execution_event(
                message_id=msg_id, event_type="ack", venue="test",
            )
            orch.process_execution_event(
                message_id=msg_id, event_type="filled",
                filled_quantity=0.002, price=2000.0, venue="test",
            )

        snap = c.diagnostics.build_snapshot()
        assert snap["metrics"] is not None

        health = c.health_check.readiness()
        assert health["status"] == "ready"

    def test_production_config_self_assembly(self, tmp_path):
        cfg = EnvironmentConfig.production(str(tmp_path))
        c = ServiceContainer(cfg).build()
        orch = c.build_orchestrator()

        assert c.idempotency_store is not None
        assert c.feedback_loop is not None
        assert c.brain_tracker is not None
        assert c.governance_rule_engine is not None

        outcome = orch.run_cycle({"symbol": "EURUSD"}, {"f": 0.5})
        assert outcome is not None

    def test_test_config_minimal(self, tmp_path):
        cfg = EnvironmentConfig.test(str(tmp_path))
        c = ServiceContainer(cfg).build()
        loop = c.build_runtime_loop()

        assert c.metrics is None
        assert c.audit_log is None
        assert c.feedback_loop is None

        result = loop.run_decision_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        assert result.intent is not None

    def test_brain_registration_and_governance(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()

        c.brain_registry.register({
            "brain_id": "test_brain",
            "brain_type": "stub",
            "status": "live",
        })
        c.governance_service.register_brain("test_brain", "live")

        assert "test_brain" in c.governance_service.get_active_brain_ids()
        assert c.brain_registry.get_entry("test_brain") is not None

    def test_all_services_present_in_development(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()

        required = [
            "ledger_store", "communication_writer", "communication_reader",
            "execution_event_writer", "execution_event_reader",
            "reconciliation_service", "inspection_service",
            "replay_service", "replay_gate", "operations_service",
            "dispatcher", "message_builder", "risk_service",
            "metrics", "audit_log", "diagnostics",
            "governance_service", "governance_rule_engine",
            "parliament_service", "position_tracker", "market_context",
            "execution_manager", "health_check",
            "feature_service", "brain_registry", "brain_run_service",
            "override_resolver", "decision_compiler", "decision_record_writer",
            "control_snapshot_service", "feedback_loop", "brain_tracker",
        ]
        for attr in required:
            assert getattr(c, attr) is not None, f"Missing: {attr}"
