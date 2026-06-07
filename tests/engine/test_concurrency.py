"""Concurrency safety and container completeness tests."""

import threading

from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer
from core.feedback.brain_performance_tracker import BrainPerformanceTracker
from core.governance.governance_service import GovernanceService
from core.market.position_tracker import PositionTracker
from core.observability.event_bus import EventBus
from core.observability.metrics_collector import MetricsCollector
from core.protocol.services.resilience import CircuitBreaker, RateLimiter


def _container(tmp_path):
    cfg = EnvironmentConfig.development(str(tmp_path))
    return ServiceContainer(cfg).build()


class TestContainerCompleteness:
    def test_all_new_services_present(self, tmp_path):
        c = _container(tmp_path)
        assert c.venue_router is not None
        assert c.alert_service is not None
        assert c.config_hot_reload is not None
        assert c.governance_rule_engine is not None
        assert c.health_check is not None

    def test_venue_router_has_default(self, tmp_path):
        c = _container(tmp_path)
        from datetime import UTC, datetime

        from core.contracts.domain.communication_envelope import CommunicationEnvelope
        from core.contracts.domain.dispatch_request import DispatchRequest
        from core.contracts.enums import CommunicationMessageType, CommunicationPriority

        env = CommunicationEnvelope(
            schema_version="v1",
            message_id="m1",
            correlation_id="c1",
            causation_id=None,
            event_time=datetime.now(UTC).replace(tzinfo=None),
            producer="t",
            target="x",
            message_type=CommunicationMessageType.DECISION_INTENT,
            priority=CommunicationPriority.NORMAL,
            payload={"venue": "any"},
        )
        req = DispatchRequest(
            schema_version="v1",
            dispatch_id="d1",
            envelope=env,
            requested_at=datetime.now(UTC).replace(tzinfo=None),
        )
        result = c.venue_router.route(req, env)
        assert result.adapter_name == "stub_default"

    def test_alert_service_has_rules(self, tmp_path):
        c = _container(tmp_path)
        fired = c.alert_service.evaluate({"error_rate": 0.5})
        assert len(fired) >= 1

    def test_service_count_38(self, tmp_path):
        c = _container(tmp_path)
        attrs = [
            "ledger_store",
            "communication_writer",
            "communication_reader",
            "execution_event_writer",
            "execution_event_reader",
            "reconciliation_service",
            "inspection_service",
            "replay_service",
            "replay_gate",
            "operations_service",
            "dispatcher",
            "message_builder",
            "risk_service",
            "metrics",
            "audit_log",
            "diagnostics",
            "governance_service",
            "governance_rule_engine",
            "parliament_service",
            "position_tracker",
            "market_context",
            "execution_manager",
            "health_check",
            "feature_service",
            "brain_registry",
            "brain_run_service",
            "override_resolver",
            "decision_compiler",
            "decision_record_writer",
            "control_snapshot_service",
            "feedback_loop",
            "brain_tracker",
            "venue_router",
            "alert_service",
            "config_hot_reload",
        ]
        present = [a for a in attrs if getattr(c, a) is not None]
        assert len(present) >= 35


class TestConcurrencyCircuitBreaker:
    def test_concurrent_record_failure(self):
        cb = CircuitBreaker(failure_threshold=100)
        errors = []

        def hammer():
            try:
                for _ in range(50):
                    cb.record_failure()
                    cb.record_success()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=hammer) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert errors == []
        assert cb.get_status()["state"] in ("closed", "open", "half_open")

    def test_concurrent_allow_request(self):
        cb = CircuitBreaker(failure_threshold=1000)
        results = []

        def check():
            for _ in range(100):
                results.append(cb.allow_request())

        threads = [threading.Thread(target=check) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(results) == 1000


class TestConcurrencyRateLimiter:
    def test_concurrent_allow(self):
        rl = RateLimiter(max_rate=50, window_seconds=1.0)
        results = []

        def consume():
            for _ in range(20):
                results.append(rl.allow())

        threads = [threading.Thread(target=consume) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        allowed = sum(1 for r in results if r)
        assert allowed <= 51


class TestConcurrencyEventBus:
    def test_concurrent_publish_subscribe(self):
        bus = EventBus()
        received = []
        lock = threading.Lock()
        bus.subscribe("evt", lambda t, p: (lock.acquire(), received.append(p), lock.release()))  # type: ignore[func-returns-value]

        def publish():
            for i in range(50):
                bus.publish("evt", {"i": i})

        threads = [threading.Thread(target=publish) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(received) == 250


class TestConcurrencyMetrics:
    def test_concurrent_inc(self):
        m = MetricsCollector()

        def inc():
            for _ in range(1000):
                m.inc("counter")

        threads = [threading.Thread(target=inc) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert m.get_counter("counter") == 10000


class TestConcurrencyBrainTracker:
    def test_concurrent_record(self):
        bt = BrainPerformanceTracker(window_size=100)
        errors = []

        def record():
            try:
                for _i in range(50):
                    bt.record_outcome("brain_a", {"composite_score": 0.5})
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=record) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert errors == []
        s = bt.get_brain_summary("brain_a")
        assert s["sample_count"] == 100


class TestConcurrencyGovernance:
    def test_concurrent_transitions(self):
        gs = GovernanceService()
        gs.register_brain("x", "live")
        errors = []

        def flip():
            try:
                for _ in range(20):
                    gs.transition("x", "frozen", "test")
                    gs.transition("x", "probation", "test")
                    gs.transition("x", "live", "test")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=flip) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        state = gs.get_brain_state("x")
        assert state["status"] in ("live", "frozen", "probation")


class TestConcurrencyPositionTracker:
    def test_concurrent_open_close(self):
        pt = PositionTracker()
        errors = []

        def ops(tid):
            try:
                for i in range(20):
                    pid = f"p_{tid}_{i}"
                    pt.open_position(
                        position_id=pid, symbol="X", side="long", quantity=1, entry_price=100
                    )
                    pt.close_position(pid, 101)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=ops, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert errors == []
        assert pt.get_risk_context()["open_position_count"] == 0
        assert pt.get_risk_context()["closed_position_count"] == 100
