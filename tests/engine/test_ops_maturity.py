import time

from core.deployment.environment_config import EnvironmentConfig
from core.deployment.lifecycle_manager import LifecycleManager
from core.deployment.operational_support import ConfigValidator, RetryPolicy
from core.deployment.service_container import ServiceContainer
from core.deployment.state_persistence import StatePersistence
from core.observability.alert_service import (
    AlertRule,
    AlertService,
    InMemoryAlertChannel,
    LogAlertChannel,
)
from core.observability.audit_log import StructuredAuditLog


class TestAlertRule:
    def test_fires_on_condition(self):
        rule = AlertRule("test", lambda ctx: ctx.get("x") > 5, cooldown_seconds=0)
        assert rule.should_fire({"x": 10})
        assert not rule.should_fire({"x": 1})

    def test_cooldown_prevents_rapid_fire(self):
        rule = AlertRule("test", lambda ctx: True, cooldown_seconds=100)
        assert rule.should_fire({})
        assert not rule.should_fire({})

    def test_cooldown_expires(self):
        rule = AlertRule("test", lambda ctx: True, cooldown_seconds=0.01)
        assert rule.should_fire({})
        time.sleep(0.02)
        assert rule.should_fire({})


class TestAlertService:
    def test_evaluate_fires_matching_rules(self):
        channel = InMemoryAlertChannel()
        svc = AlertService(channels=[channel])
        svc.add_rule(AlertRule("a", lambda ctx: ctx.get("x") > 5, cooldown_seconds=0))
        svc.add_rule(AlertRule("b", lambda ctx: ctx.get("y") > 10, cooldown_seconds=0))

        fired = svc.evaluate({"x": 10, "y": 2})
        assert len(fired) == 1
        assert fired[0]["rule_name"] == "a"
        assert len(channel.get_alerts()) == 1

    def test_default_rules(self):
        svc = AlertService.with_default_rules()
        fired = svc.evaluate({"error_rate": 0.5, "circuit_state": "open"})
        assert len(fired) == 2

    def test_log_channel(self, tmp_path):
        audit = StructuredAuditLog(str(tmp_path / "audit"))
        channel = LogAlertChannel(audit)
        svc = AlertService(channels=[channel])
        svc.add_rule(AlertRule("t", lambda ctx: True, cooldown_seconds=0))
        svc.evaluate({"test": True})

        entries = audit.read_entries()
        assert len(entries) >= 1
        assert entries[-1]["event_type"] == "alert"

    def test_fired_history(self):
        svc = AlertService()
        svc.add_rule(AlertRule("h", lambda ctx: True, cooldown_seconds=0))
        svc.evaluate({})
        svc.evaluate({})
        history = svc.get_fired_history()
        assert len(history) == 2

    def test_no_rules_no_fire(self):
        svc = AlertService()
        assert svc.evaluate({"x": 100}) == []


class TestRetryPolicy:
    def test_succeeds_first_try(self):
        rp = RetryPolicy(max_retries=3)
        assert rp.execute(lambda: 42) == 42

    def test_retries_then_succeeds(self):
        calls = {"count": 0}

        def flaky():
            calls["count"] += 1
            if calls["count"] < 3:
                raise ValueError("fail")
            return "ok"

        rp = RetryPolicy(max_retries=3, base_delay_seconds=0.001)
        assert rp.execute(flaky) == "ok"
        assert calls["count"] == 3

    def test_exhausts_retries(self):
        rp = RetryPolicy(max_retries=2, base_delay_seconds=0.001)
        import pytest

        with pytest.raises(ValueError):
            rp.execute(lambda: (_ for _ in ()).throw(ValueError("always fails")))

    def test_only_retries_specified_exceptions(self):
        rp = RetryPolicy(
            max_retries=3,
            base_delay_seconds=0.001,
            retryable_exceptions=(ConnectionError,),
        )
        import pytest

        with pytest.raises(ValueError):
            rp.execute(lambda: (_ for _ in ()).throw(ValueError("not retryable")))

    def test_config(self):
        rp = RetryPolicy(max_retries=5, base_delay_seconds=0.5)
        cfg = rp.get_config()
        assert cfg["max_retries"] == 5
        assert cfg["base_delay_seconds"] == 0.5


class TestConfigValidator:
    def test_valid_development_config(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        validator = ConfigValidator()
        result = validator.validate(cfg)
        assert result["valid"] is True
        assert result["rules_checked"] >= 11

    def test_valid_production_config(self, tmp_path):
        cfg = EnvironmentConfig.production(str(tmp_path))
        result = ConfigValidator().validate(cfg)
        assert result["valid"] is True
        assert result["warnings"] == []

    def test_production_warnings(self, tmp_path):
        cfg = EnvironmentConfig.production(str(tmp_path))
        cfg.enable_audit_log = False
        cfg.enable_metrics = False
        result = ConfigValidator().validate(cfg)
        assert len(result["warnings"]) >= 2

    def test_invalid_config(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        cfg.max_open_positions = 0
        result = ConfigValidator().validate(cfg)
        assert result["valid"] is False

    def test_invalid_ops_maturity_min_score(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        cfg.ops_maturity_min_score = 101.0
        result = ConfigValidator().validate(cfg)
        assert result["valid"] is False
        assert any(e["rule"] == "ops_maturity_min_score_in_range" for e in result["errors"])

    def test_custom_rule(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        validator = ConfigValidator()
        validator.add_rule("custom", lambda c: c.max_open_positions < 100)
        result = validator.validate(cfg)
        assert result["valid"] is True

    def test_test_config_valid(self, tmp_path):
        cfg = EnvironmentConfig.test(str(tmp_path))
        result = ConfigValidator().validate(cfg)
        assert result["valid"] is True


class TestLifecycleManager:
    def test_startup_shutdown(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path / "data"))
        c = ServiceContainer(cfg).build()
        sp = StatePersistence(str(tmp_path / "state"))
        lm = LifecycleManager(c, state_persistence=sp)

        result = lm.startup()
        assert result["status"] == "started"
        assert lm.is_running()
        assert lm.get_uptime() >= 0

        result = lm.shutdown()
        assert result["status"] == "stopped"
        assert not lm.is_running()
        assert result["uptime_seconds"] >= 0

    def test_double_startup_idempotent(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        lm = LifecycleManager(c)
        lm.startup()
        result = lm.startup()
        assert result["status"] == "already_started"

    def test_shutdown_not_started(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        lm = LifecycleManager(c)
        result = lm.shutdown()
        assert result["status"] == "not_started"

    def test_state_save_on_shutdown(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path / "data"))
        c = ServiceContainer(cfg).build()
        c.governance_service.register_brain("test", "live")  # type: ignore[reportOptionalMemberAccess]
        sp = StatePersistence(str(tmp_path / "state"))
        lm = LifecycleManager(c, state_persistence=sp)

        lm.startup()
        result = lm.shutdown(save_state=True)
        save_phase = [p for p in result["phases"] if p["phase"] == "state_save"]
        assert len(save_phase) == 1
        assert len(save_phase[0]["paths"]) >= 1

    def test_state_restore_on_startup(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path / "data"))
        c1 = ServiceContainer(cfg).build()
        c1.governance_service.register_brain("alpha", "live")  # type: ignore[reportOptionalMemberAccess]
        sp = StatePersistence(str(tmp_path / "state"))
        sp.save_governance_state(c1.governance_service, "restore_test")

        c2 = ServiceContainer(cfg).build()
        lm = LifecycleManager(c2, state_persistence=sp)
        result = lm.startup(restore_state=True, state_label="restore_test")

        restore_phase = [p for p in result["phases"] if p["phase"] == "state_restore"]
        assert restore_phase[0]["restored"] is True
        assert c2.governance_service.get_brain_state("alpha") is not None  # type: ignore[reportOptionalMemberAccess]

    def test_shutdown_hooks(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        lm = LifecycleManager(c)
        called = []
        lm.register_shutdown_hook(lambda: called.append("hook1"))
        lm.register_shutdown_hook(lambda: called.append("hook2"))
        lm.startup()
        lm.shutdown(save_state=False)
        assert called == ["hook1", "hook2"]

    def test_get_status(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        lm = LifecycleManager(c)
        s = lm.get_status()
        assert s["running"] is False

        lm.startup()
        s = lm.get_status()
        assert s["running"] is True
        assert s["started_at"] is not None
