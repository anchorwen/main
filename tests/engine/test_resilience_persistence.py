import time
from typing import Any, cast

from core.deployment.environment_config import EnvironmentConfig
from core.deployment.replay_isolation import (
    NullDispatchAdapter,
    ReplayDispatchAdapter,
    ReplayEnvironment,
)
from core.deployment.service_container import ServiceContainer
from core.deployment.state_persistence import StatePersistence
from core.feedback.brain_performance_tracker import BrainPerformanceTracker
from core.governance.governance_service import GovernanceService
from core.market.position_tracker import PositionTracker
from core.protocol.services.resilience import CircuitBreaker, CircuitState, RateLimiter


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False

    def test_half_open_after_cooldown(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.15)
        assert (
            cast(object, cb.state) == CircuitState.HALF_OPEN
        )  # TECH_DEBT-009: comparison-overlap 绕过 (A3)
        assert cb.allow_request() is True

    def test_half_open_success_resets(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.1)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        assert cb.allow_request() is True
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.1)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        cb.allow_request()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_reset(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert (
            cast(object, cb.state) == CircuitState.CLOSED
        )  # TECH_DEBT-009: comparison-overlap 绕过 (A3)
        assert cb.allow_request() is True

    def test_status(self):
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        cb.record_failure()
        s = cb.get_status()
        assert s["state"] == "closed"
        assert s["failure_count"] == 1
        assert s["failure_threshold"] == 3

    def test_trip_counter(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.01)
        cb.record_failure()
        assert cb.get_status()["total_trips"] == 1
        time.sleep(0.02)
        cb.allow_request()
        cb.record_failure()
        assert cb.get_status()["total_trips"] == 2


class TestRateLimiter:
    def test_allows_within_rate(self):
        rl = RateLimiter(max_rate=5, window_seconds=1.0)
        for _ in range(5):
            assert rl.allow() is True

    def test_throttles_over_rate(self):
        rl = RateLimiter(max_rate=3, window_seconds=10.0)
        for _ in range(3):
            rl.allow()
        assert rl.allow() is False

    def test_refills_after_window(self):
        rl = RateLimiter(max_rate=2, window_seconds=0.05)
        rl.allow()
        rl.allow()
        assert rl.allow() is False
        time.sleep(0.06)
        assert rl.allow() is True

    def test_status(self):
        rl = RateLimiter(max_rate=10)
        rl.allow()
        s = rl.get_status()
        assert s["total_allowed"] == 1
        assert s["total_throttled"] == 0
        assert s["max_rate"] == 10


class TestStatePersistence:
    def test_save_and_restore_governance(self, tmp_path):
        sp = StatePersistence(str(tmp_path / "state"))
        gs = GovernanceService()
        gs.register_brain("alpha", "live")
        gs.register_brain("beta", "candidate")
        gs.transition("beta", "live", "promoted")

        path = sp.save_governance_state(gs, "test_save")
        assert path.exists()

        gs2 = GovernanceService()
        data = sp.restore_governance_state(gs2, "test_save")
        assert data is not None
        assert gs2.get_brain_state("alpha") is not None
        assert gs2.get_brain_state("beta") is not None

    def test_save_brain_tracker(self, tmp_path):
        sp = StatePersistence(str(tmp_path / "state"))
        bt = BrainPerformanceTracker()
        bt.record_outcome("a", {"composite_score": 0.8})
        path = sp.save_brain_tracker(bt)
        assert path.exists()

    def test_save_positions(self, tmp_path):
        sp = StatePersistence(str(tmp_path / "state"))
        pt = PositionTracker()
        pt.open_position(
            position_id="p1", symbol="XAUUSD", side="long", quantity=1.0, entry_price=2000.0
        )
        path = sp.save_positions(pt)
        assert path.exists()

    def test_save_all(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path / "data"))
        c = ServiceContainer(cfg).build()
        assert c.governance_service is not None  # TECH_DEBT-009: 容器构建契约 (L149 register_brain)
        assert c.position_tracker is not None  # TECH_DEBT-009: 容器构建契约 (L150 open_position)
        c.governance_service.register_brain("test", "live")
        c.position_tracker.open_position(
            position_id="p1", symbol="X", side="long", quantity=1, entry_price=100
        )

        sp = StatePersistence(str(tmp_path / "state"))
        result = sp.save_all(c)
        assert "governance" in result["paths"]
        assert "tracker" in result["paths"]
        assert "positions" in result["paths"]

    def test_restore_missing_returns_none(self, tmp_path):
        sp = StatePersistence(str(tmp_path / "state"))
        gs = GovernanceService()
        assert sp.restore_governance_state(gs, "nonexistent") is None


class TestReplayIsolation:
    def test_replay_adapter_captures(self):
        adapter = ReplayDispatchAdapter()
        from datetime import datetime

        from core.contracts.domain.communication_envelope import CommunicationEnvelope
        from core.contracts.domain.dispatch_request import DispatchRequest
        from core.contracts.enums import CommunicationMessageType, CommunicationPriority

        env = CommunicationEnvelope(
            schema_version="v1",
            message_id="m1",
            correlation_id="c1",
            causation_id=None,
            event_time=datetime(2026, 4, 24, 12, 0, 0),
            producer="t",
            target="exec_bridge",
            message_type=CommunicationMessageType.DECISION_INTENT,
            priority=CommunicationPriority.NORMAL,
            payload={"action": "open", "symbol": "XAUUSD", "side": "long"},
        )
        req = DispatchRequest(
            schema_version="v1",
            dispatch_id="d1",
            envelope=env,
            requested_at=datetime(2026, 4, 24, 12, 0, 1),
        )
        result = adapter.dispatch(req, env)
        assert (
            cast(Any, result.status).value == "protocol_validated"
        )  # TECH_DEBT-009: DispatchResult.status 静态 str, 运行时为 DispatchStatus 枚举
        assert result.trace["replay_mode"] is True
        assert adapter.get_captured_count() == 1

    def test_null_adapter(self):
        adapter = NullDispatchAdapter()
        from datetime import datetime

        from core.contracts.domain.communication_envelope import CommunicationEnvelope
        from core.contracts.domain.dispatch_request import DispatchRequest
        from core.contracts.enums import CommunicationMessageType, CommunicationPriority

        env = CommunicationEnvelope(
            schema_version="v1",
            message_id="m1",
            correlation_id="c1",
            causation_id=None,
            event_time=datetime(2026, 4, 24, 12, 0, 0),
            producer="t",
            target="exec_bridge",
            message_type=CommunicationMessageType.DECISION_INTENT,
            priority=CommunicationPriority.NORMAL,
        )
        req = DispatchRequest(
            schema_version="v1",
            dispatch_id="d1",
            envelope=env,
            requested_at=datetime(2026, 4, 24, 12, 0, 1),
        )
        result = adapter.dispatch(req, env)
        assert result.trace["null_mode"] is True

    def test_replay_environment(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        original_dispatcher = c.dispatcher

        replay = ReplayEnvironment(c)
        replay.activate()
        assert c.dispatcher is not original_dispatcher

        replay.deactivate()
        assert c.dispatcher is original_dispatcher

    def test_replay_summary(self):
        adapter = ReplayDispatchAdapter()
        from datetime import datetime

        from core.contracts.domain.communication_envelope import CommunicationEnvelope
        from core.contracts.domain.dispatch_request import DispatchRequest
        from core.contracts.enums import CommunicationMessageType, CommunicationPriority

        for i, (action, symbol) in enumerate(
            [("open", "XAUUSD"), ("open", "EURUSD"), ("close", "XAUUSD")]
        ):
            env = CommunicationEnvelope(
                schema_version="v1",
                message_id=f"m{i}",
                correlation_id=f"c{i}",
                causation_id=None,
                event_time=datetime(2026, 4, 24, 12, 0, 0),
                producer="t",
                target="x",
                message_type=CommunicationMessageType.DECISION_INTENT,
                priority=CommunicationPriority.NORMAL,
                payload={"action": action, "symbol": symbol},
            )
            req = DispatchRequest(
                schema_version="v1",
                dispatch_id=f"d{i}",
                envelope=env,
                requested_at=datetime(2026, 4, 24, 12, 0, 1),
            )
            adapter.dispatch(req, env)

        from core.deployment.replay_isolation import ReplayEnvironment

        re = ReplayEnvironment.__new__(ReplayEnvironment)
        re._replay_adapter = adapter
        summary = re.get_replay_summary()
        assert summary["total_dispatches"] == 3
        assert summary["action_counts"]["open"] == 2
        assert summary["symbol_counts"]["XAUUSD"] == 2
