from core.deployment.environment_config import Environment, EnvironmentConfig
from core.deployment.health_check import HealthCheckService
from core.deployment.service_container import ServiceContainer


class TestEnvironmentConfig:
    def test_development_factory(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        assert cfg.environment == Environment.DEVELOPMENT
        assert cfg.adapter_name == "stub"
        assert cfg.enable_feedback_loop is True

    def test_test_factory(self, tmp_path):
        cfg = EnvironmentConfig.test(str(tmp_path))
        assert cfg.environment == Environment.TEST
        assert cfg.enable_audit_log is False
        assert cfg.enable_metrics is False

    def test_production_factory(self, tmp_path):
        cfg = EnvironmentConfig.production(str(tmp_path))
        assert cfg.environment == Environment.PRODUCTION
        assert cfg.is_live() is True
        assert cfg.allows_real_dispatch() is True
        assert cfg.max_drawdown_pct == 3.0

    def test_overrides(self, tmp_path):
        cfg = EnvironmentConfig.production(str(tmp_path), max_open_positions=20)
        assert cfg.max_open_positions == 20

    def test_simulation_dispatch(self, tmp_path):
        cfg = EnvironmentConfig(
            environment=Environment.SIMULATION,
            base_dir=str(tmp_path),
        )
        assert cfg.is_simulation() is True
        assert cfg.allows_real_dispatch() is True

    def test_replay_no_dispatch(self, tmp_path):
        cfg = EnvironmentConfig(
            environment=Environment.REPLAY,
            base_dir=str(tmp_path),
        )
        assert cfg.is_replay() is True
        assert cfg.allows_real_dispatch() is False


class TestServiceContainer:
    def test_build_development(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        assert c.ledger_store is not None
        assert c.communication_writer is not None
        assert c.communication_reader is not None
        assert c.execution_event_writer is not None
        assert c.execution_event_reader is not None
        assert c.reconciliation_service is not None
        assert c.inspection_service is not None
        assert c.replay_service is not None
        assert c.replay_gate is not None
        assert c.operations_service is not None
        assert c.dispatcher is not None
        assert c.message_builder is not None
        assert c.risk_service is not None
        assert c.feedback_loop is not None
        assert c.brain_tracker is not None
        assert c.metrics is not None
        assert c.audit_log is not None
        assert c.diagnostics is not None
        assert c.governance_service is not None
        assert c.parliament_service is not None
        assert c.position_tracker is not None
        assert c.market_context is not None
        assert c.execution_manager is not None

    def test_build_test_env(self, tmp_path):
        cfg = EnvironmentConfig.test(str(tmp_path))
        c = ServiceContainer(cfg).build()
        assert c.metrics is None
        assert c.audit_log is None
        assert c.idempotency_store is None
        assert c.feedback_loop is None

    def test_build_production(self, tmp_path):
        cfg = EnvironmentConfig.production(str(tmp_path))
        c = ServiceContainer(cfg).build()
        assert c.idempotency_store is not None
        assert c.feedback_loop is not None
        assert c.metrics is not None
        assert c.audit_log is not None

    def test_idempotent_build(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg)
        c1 = c.build()
        c2 = c.build()
        assert c1 is c2

    def test_diagnostics_snapshot(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        snap = c.diagnostics.build_snapshot()  # type: ignore[reportOptionalMemberAccess]
        assert "generated_at" in snap
        assert snap["metrics"] is not None
        assert snap["brain_health"] is not None


class TestHealthCheck:
    def test_liveness(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        health = HealthCheckService(c)
        result = health.liveness()
        assert result["status"] == "alive"
        assert result["environment"] == "development"

    def test_readiness_all_ok(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        health = HealthCheckService(c)
        result = health.readiness()
        assert result["status"] == "ready"
        all_checks = {ch["name"]: ch["status"] for ch in result["checks"]}
        assert all_checks["ledger_store"] == "ok"
        assert all_checks["risk_service"] == "ok"
        assert all_checks["dispatcher"] == "ok"

    def test_readiness_test_env(self, tmp_path):
        cfg = EnvironmentConfig.test(str(tmp_path))
        c = ServiceContainer(cfg).build()
        health = HealthCheckService(c)
        result = health.readiness()
        assert result["status"] == "ready"
        check_names = [ch["name"] for ch in result["checks"]]
        assert "metrics" not in check_names
        assert "feedback_loop" not in check_names
