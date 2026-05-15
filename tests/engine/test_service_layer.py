import time

from apps.engine.system_facade import SystemFacade, SystemSelfTest
from core.contracts.domain_keys import EVIDENCE_SECTION_ENGINE_CONFIG
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.lifecycle_manager import LifecycleManager
from core.deployment.scheduler_service import SchedulerService
from core.deployment.schema_versions import SCHEMA_ENGINE_CONFIG_EVIDENCE
from core.deployment.service_container import ServiceContainer
from core.deployment.state_persistence import StatePersistence
from core.observability.alert_service import AlertService, InMemoryAlertChannel


def _container(tmp_path, **kw):
    cfg = EnvironmentConfig.development(str(tmp_path), **kw)
    return ServiceContainer(cfg).build()


class TestSchedulerService:
    def test_run_once(self):
        calls = []
        sched = SchedulerService()
        sched.add_task("t1", lambda: calls.append("a"), interval_seconds=0)
        sched.add_task("t2", lambda: calls.append("b"), interval_seconds=0)
        results = sched.run_once()
        assert len(results) == 2
        assert calls == ["a", "b"]

    def test_interval_gating(self):
        calls = []
        sched = SchedulerService()
        sched.add_task("t1", lambda: calls.append(1), interval_seconds=1000)
        sched.run_once()
        sched.run_once()
        assert len(calls) == 1

    def test_disabled_task_skipped(self):
        calls = []
        sched = SchedulerService()
        sched.add_task("t1", lambda: calls.append(1), interval_seconds=0, enabled=False)
        sched.run_once()
        assert len(calls) == 0

    def test_error_handling(self):
        sched = SchedulerService()
        sched.add_task("bad", lambda: 1 / 0, interval_seconds=0)
        results = sched.run_once()
        assert results[0]["status"] == "error"

    def test_start_stop(self):
        calls = {"count": 0}
        sched = SchedulerService()
        sched.add_task(
            "t1", lambda: calls.__setitem__("count", calls["count"] + 1), interval_seconds=0.02
        )
        sched.start()
        time.sleep(0.35)
        sched.stop(timeout=1.0)
        assert calls["count"] >= 2
        assert not sched._running

    def test_status(self):
        sched = SchedulerService()
        sched.add_task("t1", lambda: None, interval_seconds=60)
        s = sched.get_status()
        assert s["task_count"] == 1
        assert s["running"] is False

    def test_for_container(self, tmp_path):
        c = _container(tmp_path)
        sp = StatePersistence(str(tmp_path / "state"))
        alert = AlertService.with_default_rules()
        sched = SchedulerService.for_container(c, persistence=sp, alert_service=alert)
        assert sched.get_status()["task_count"] >= 1

    def test_for_container_runs(self, tmp_path):
        c = _container(
            tmp_path,
            daily_ops_enabled=False,
            feature_store_scheduled_update=False,
            ops_monitoring_enabled=False,
        )
        c.governance_service.register_brain("t1", "live")  # type: ignore[reportOptionalMemberAccess]
        c.brain_tracker.record_outcome("t1", {"composite_score": 0.8})  # type: ignore[reportOptionalMemberAccess]
        sp = StatePersistence(str(tmp_path / "state"))
        sched = SchedulerService.for_container(c, persistence=sp)
        results = sched.run_once()
        assert all(r["status"] == "ok" for r in results)


class TestSystemFacade:
    def test_decide(self, tmp_path):
        c = _container(tmp_path, enable_idempotency=False)
        orch = c.build_orchestrator()
        facade = SystemFacade(c, orchestrator=orch)
        r = facade.decide("XAUUSD")
        assert "cycle_id" in r
        assert "allowed" in r

    def test_health(self, tmp_path):
        c = _container(tmp_path)
        facade = SystemFacade(c)
        h = facade.health()
        assert h["liveness"]["status"] == "alive"
        assert h["readiness"]["status"] == "ready"

    def test_metrics(self, tmp_path):
        c = _container(tmp_path)
        c.metrics.inc("test_x", 3)  # type: ignore[reportOptionalMemberAccess]
        facade = SystemFacade(c)
        m = facade.metrics()
        assert m["counters"]["test_x"] == 3

    def test_snapshot(self, tmp_path):
        c = _container(tmp_path)
        facade = SystemFacade(c)
        s = facade.snapshot()
        assert "generated_at" in s
        assert s[EVIDENCE_SECTION_ENGINE_CONFIG]["schema_version"] == SCHEMA_ENGINE_CONFIG_EVIDENCE

    def test_list_brains(self, tmp_path):
        c = _container(tmp_path)
        c.governance_service.register_brain("a", "live")  # type: ignore[reportOptionalMemberAccess]
        c.governance_service.register_brain("b", "candidate")  # type: ignore[reportOptionalMemberAccess]
        facade = SystemFacade(c)
        brains = facade.list_brains()
        assert len(brains) == 2

    def test_freeze_unfreeze(self, tmp_path):
        c = _container(tmp_path)
        c.governance_service.register_brain("x", "live")  # type: ignore[reportOptionalMemberAccess]
        facade = SystemFacade(c)
        r = facade.freeze_brain("x", "test")
        assert r["status"] == "frozen"
        r = facade.unfreeze_brain("x", "test")
        assert r["status"] == "probation"

    def test_positions_orders(self, tmp_path):
        c = _container(tmp_path)
        facade = SystemFacade(c)
        p = facade.positions()
        assert p["open"] == []
        o = facade.orders()
        assert o["count"] == 0

    def test_audit_recent(self, tmp_path):
        c = _container(tmp_path)
        c.audit_log.log(event_type="test", severity="info")  # type: ignore[reportOptionalMemberAccess]
        facade = SystemFacade(c)
        entries = facade.audit_recent()
        assert len(entries) >= 1

    def test_alerts_recent(self, tmp_path):
        c = _container(tmp_path)
        channel = InMemoryAlertChannel()
        alert_svc = AlertService.with_default_rules(channels=[channel])
        alert_svc.evaluate({"error_rate": 0.5})
        facade = SystemFacade(c, alert_service=alert_svc)
        alerts = facade.alerts_recent()
        assert len(alerts) >= 1

    def test_decide_without_orchestrator(self, tmp_path):
        c = _container(tmp_path)
        facade = SystemFacade(c)
        r = facade.decide("XAUUSD")
        assert r["error"] == "orchestrator not built"

    def test_full_workflow(self, tmp_path):
        c = _container(tmp_path, enable_idempotency=False)
        c.governance_service.register_brain("alpha", "live")  # type: ignore[reportOptionalMemberAccess]
        orch = c.build_orchestrator()
        facade = SystemFacade(c, orchestrator=orch)

        r = facade.decide("XAUUSD")
        assert r["allowed"] is True or r["allowed"] is False

        h = facade.health()
        assert h["readiness"]["status"] == "ready"

        brains = facade.list_brains()
        assert len(brains) == 1

        s = facade.snapshot()
        assert s["metrics"] is not None


class TestSystemSelfTest:
    def test_all_pass(self, tmp_path):
        c = _container(tmp_path)
        st = SystemSelfTest(c)
        result = st.run()
        assert result["all_passed"] is True
        assert result["passed"] == result["total"]
        assert result["failed"] == 0

    def test_reports_failures(self, tmp_path):
        c = _container(tmp_path)
        c.health_check = None
        st = SystemSelfTest(c)
        result = st.run()
        assert result["failed"] >= 1
        assert result["all_passed"] is False

    def test_feature_service_check(self, tmp_path):
        c = _container(tmp_path)
        st = SystemSelfTest(c)
        result = st.run()
        feature_result = [r for r in result["results"] if r["name"] == "feature_service"]
        assert feature_result[0]["status"] == "pass"

    def test_engine_config_check(self, tmp_path):
        c = _container(tmp_path)
        st = SystemSelfTest(c)
        result = st.run()
        row = [r for r in result["results"] if r["name"] == EVIDENCE_SECTION_ENGINE_CONFIG]
        assert len(row) == 1
        assert row[0]["status"] == "pass"

    def test_engine_config_check_with_metrics_disabled(self, tmp_path):
        c = ServiceContainer(
            EnvironmentConfig.development(str(tmp_path), enable_metrics=False),
        ).build()
        st = SystemSelfTest(c)
        result = st.run()
        assert result["all_passed"]
        row = [r for r in result["results"] if r["name"] == EVIDENCE_SECTION_ENGINE_CONFIG]
        assert len(row) == 1
        assert row[0]["status"] == "pass"


class TestFullServiceStack:
    def test_scheduler_facade_lifecycle_integration(self, tmp_path):
        c = _container(tmp_path, enable_idempotency=False)
        c.governance_service.register_brain("alpha", "live")  # type: ignore[reportOptionalMemberAccess]
        c.brain_tracker.record_outcome("alpha", {"composite_score": 0.7})  # type: ignore[reportOptionalMemberAccess]

        orch = c.build_orchestrator()
        sp = StatePersistence(str(tmp_path / "state"))
        lm = LifecycleManager(c, state_persistence=sp)
        alert_svc = AlertService.with_default_rules(channels=[InMemoryAlertChannel()])
        sched = SchedulerService.for_container(c, persistence=sp, alert_service=alert_svc)
        facade = SystemFacade(
            c, orchestrator=orch, lifecycle=lm, scheduler=sched, alert_service=alert_svc
        )

        startup = lm.startup()
        assert startup["status"] == "started"

        r = facade.decide("XAUUSD")
        assert "cycle_id" in r

        sched.run_once()

        h = facade.health()
        assert h["lifecycle"]["running"] is True
        assert h["scheduler"]["task_count"] >= 1

        st = SystemSelfTest(c)
        self_test = st.run()
        assert self_test["all_passed"]

        shutdown = lm.shutdown()
        assert shutdown["status"] == "stopped"
        assert len(shutdown["phases"]) >= 1
