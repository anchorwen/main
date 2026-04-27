"""Tests for VenueRouter, ConfigHotReload, and Tracing integration."""
import json
from datetime import datetime

from core.deployment.domain_keys import TIMELINE_ACTOR_HOT_RELOAD, TIMELINE_EVENT_ENGINE_CONFIG
from core.observability.metric_names import ENGINE_CONFIG_RELOAD_TOTAL
from core.protocol.services.venue_router import VenueRouter, StubVenueAdapter, VenueAdapter
from core.deployment.config_hot_reload import ConfigHotReload
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.scheduler_service import SchedulerService
from core.deployment.service_container import ServiceContainer
from core.deployment.state_persistence import StatePersistence
from core.contracts.domain.dispatch_request import DispatchRequest
from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.enums import CommunicationMessageType, CommunicationPriority, DispatchStatus


def _env(venue="MT5"):
    return CommunicationEnvelope(
        schema_version="v1", message_id="m1", correlation_id="c1",
        causation_id=None, event_time=datetime(2026, 4, 26, 12, 0, 0),
        producer="test", target="exec_bridge",
        message_type=CommunicationMessageType.DECISION_INTENT,
        priority=CommunicationPriority.NORMAL,
        payload={"action": "open", "symbol": "XAUUSD", "venue": venue},
    )


def _req(env):
    return DispatchRequest(
        schema_version="v1", dispatch_id="d1", envelope=env,
        requested_at=datetime(2026, 4, 26, 12, 0, 1),
    )


class TestVenueRouter:
    def test_route_to_registered_adapter(self):
        mt5 = StubVenueAdapter("MT5")
        router = VenueRouter()
        router.register("MT5", mt5)
        env = _env("MT5")
        result = router.route(_req(env), env)
        assert result.status == DispatchStatus.PROTOCOL_VALIDATED
        assert len(mt5.get_dispatches()) == 1

    def test_route_to_default_adapter(self):
        default = StubVenueAdapter("default")
        router = VenueRouter(default_adapter=default)
        env = _env("unknown_venue")
        result = router.route(_req(env), env)
        assert result.status == DispatchStatus.PROTOCOL_VALIDATED

    def test_no_adapter_returns_failed(self):
        router = VenueRouter()
        env = _env("missing")
        result = router.route(_req(env), env)
        assert result.status == DispatchStatus.FAILED

    def test_multiple_venues(self):
        mt5 = StubVenueAdapter("MT5")
        fix = StubVenueAdapter("FIX")
        router = VenueRouter()
        router.register("MT5", mt5)
        router.register("FIX", fix)

        env_mt5 = _env("MT5")
        env_fix = _env("FIX")
        router.route(_req(env_mt5), env_mt5)
        router.route(_req(env_fix), env_fix)

        assert len(mt5.get_dispatches()) == 1
        assert len(fix.get_dispatches()) == 1

    def test_unregister(self):
        adapter = StubVenueAdapter("X")
        router = VenueRouter()
        router.register("X", adapter)
        router.unregister("X")
        result = router.route(_req(_env("X")), _env("X"))
        assert result.status == DispatchStatus.FAILED

    def test_list_venues(self):
        router = VenueRouter()
        router.register("A", StubVenueAdapter("A"))
        router.register("B", StubVenueAdapter("B"))
        venues = router.list_venues()
        assert len(venues) == 2

    def test_route_log(self):
        mt5 = StubVenueAdapter("MT5")
        router = VenueRouter()
        router.register("MT5", mt5)
        env = _env("MT5")
        router.route(_req(env), env)
        log = router.get_route_log()
        assert len(log) == 1
        assert log[0]["venue"] == "MT5"


class TestConfigHotReload:
    def test_load_config(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"max_open_positions": 5}))
        hr = ConfigHotReload(str(cfg_path))
        data = hr.load()
        assert data["max_open_positions"] == 5

    def test_detect_change(self, tmp_path):
        import time
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"a": 1}))
        hr = ConfigHotReload(str(cfg_path))
        hr.load()
        time.sleep(0.05)
        cfg_path.write_text(json.dumps({"a": 2}))
        changes = hr.check_and_reload()
        assert changes is not None
        assert changes["a"]["old"] == 1
        assert changes["a"]["new"] == 2

    def test_no_change(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"x": 1}))
        hr = ConfigHotReload(str(cfg_path))
        hr.load()
        assert hr.check_and_reload() is None

    def test_listener_called(self, tmp_path):
        import time
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"k": "v1"}))
        hr = ConfigHotReload(str(cfg_path))
        hr.load()
        received = []
        hr.register_listener(lambda changes, new: received.append(changes))
        time.sleep(0.05)
        cfg_path.write_text(json.dumps({"k": "v2"}))
        hr.check_and_reload()
        assert len(received) == 1
        assert received[0]["k"]["new"] == "v2"

    def test_apply_overrides(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        hr = ConfigHotReload()
        applied = hr.apply_overrides(c, {"max_open_positions": 99, "max_drawdown_pct": 3.0})
        assert "max_open_positions" in applied
        assert c.config.max_open_positions == 99
        assert c.config.max_drawdown_pct == 3.0

    def test_apply_overrides_ops_maturity_min_score(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        hr = ConfigHotReload()
        applied = hr.apply_overrides(c, {"ops_maturity_min_score": 48.0})
        assert "ops_maturity_min_score" in applied
        assert c.config.ops_maturity_min_score == 48.0

    def test_engine_config_json_on_build(self, tmp_path):
        base = tmp_path / "data"
        base.mkdir()
        (base / "engine_config.json").write_text(
            json.dumps({"ops_maturity_min_score": 55.5, "max_open_positions": 7}),
            encoding="utf-8",
        )
        cfg = EnvironmentConfig.development(str(base))
        c = ServiceContainer(cfg).build()
        assert c.config.ops_maturity_min_score == 55.5
        assert c.config.max_open_positions == 7

    def test_check_and_reload_applies_and_timeline(self, tmp_path):
        import time
        base = tmp_path / "d"
        base.mkdir()
        f = base / "engine_config.json"
        f.write_text(json.dumps({"ops_maturity_min_score": 40.0}), encoding="utf-8")
        c = ServiceContainer(EnvironmentConfig.development(str(base))).build()
        time.sleep(0.05)
        f.write_text(json.dumps({"ops_maturity_min_score": 51.0}), encoding="utf-8")
        changes = c.config_hot_reload.check_and_reload()
        assert changes is not None
        assert c.config.ops_maturity_min_score == 51.0
        assert c.metrics is not None
        assert c.metrics.get_counter(ENGINE_CONFIG_RELOAD_TOTAL) == 1.0
        evs = c.operations_timeline.list_events(event_type=TIMELINE_EVENT_ENGINE_CONFIG)
        assert len(evs) == 1
        assert evs[0].get("actor") == TIMELINE_ACTOR_HOT_RELOAD
        assert "ops_maturity_min_score" in (evs[0].get("summary") or {}).get("changed_keys", [])

    def test_scheduler_registers_engine_config_poll(self, tmp_path):
        c = ServiceContainer(
            EnvironmentConfig.development(
                str(tmp_path),
                engine_config_poll_interval_seconds=120.0,
            )
        ).build()
        sp = StatePersistence(str(tmp_path / "state"))
        sched = SchedulerService.for_container(c, persistence=sp, alert_service=None)
        names = [t["name"] for t in sched.get_status()["tasks"]]
        assert "engine_config_poll" in names
        t = next(x for x in sched.get_status()["tasks"] if x["name"] == "engine_config_poll")
        assert t["interval_seconds"] == 120.0

    def test_status(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"a": 1}))
        hr = ConfigHotReload(str(cfg_path))
        hr.load()
        s = hr.get_status()
        assert s["reload_count"] == 0
        assert s["listener_count"] == 0

    def test_no_path(self):
        hr = ConfigHotReload()
        assert hr.load() == {}
        assert hr.check_and_reload() is None


class TestTracingIntegration:
    def test_cycle_outcome_has_trace(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path), enable_idempotency=False)
        c = ServiceContainer(cfg).build()
        orch = c.build_orchestrator()
        outcome = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        assert outcome.trace_summary is not None
        assert outcome.trace_summary["trace_id"] is not None
        assert outcome.trace_summary["span_count"] >= 1
        assert outcome.trace_summary["root_span"] == "decision_cycle"
        assert outcome.trace_summary["total_duration_ms"] >= 0

    def test_trace_has_runtime_loop_span(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path), enable_idempotency=False)
        c = ServiceContainer(cfg).build()
        orch = c.build_orchestrator()
        outcome = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        assert outcome.trace_summary["span_count"] >= 2

    def test_error_cycle_has_trace(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path), enable_idempotency=False)
        c = ServiceContainer(cfg).build()

        class Broken:
            def run_decision_cycle(self, t, f):
                raise RuntimeError("boom")

        orch = c.build_orchestrator(Broken())
        outcome = orch.run_cycle({"symbol": "X"}, {})
        assert outcome.trace_summary is not None
        assert outcome.trace_summary["error_count"] >= 1

    def test_throttled_cycle_has_trace(self, tmp_path):
        from core.protocol.services.resilience import RateLimiter
        cfg = EnvironmentConfig.development(str(tmp_path), enable_idempotency=False)
        c = ServiceContainer(cfg).build()
        orch = c.build_orchestrator()
        orch._rate_limiter = RateLimiter(max_rate=0, window_seconds=10)
        outcome = orch.run_cycle({"symbol": "X"}, {})
        assert outcome.trace_summary is not None
        assert outcome.decision_result is None
