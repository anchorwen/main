from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.engine.batch_processor import BatchProcessor
from apps.engine.runtime_loop import RuntimeLoop
from core.contracts.domain.decision_intent import DecisionIntent
from core.contracts.enums import DecisionAction, DecisionSide
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.replay_isolation import ReplayEnvironment
from core.deployment.service_container import ServiceContainer
from core.deployment.state_persistence import StatePersistence
from core.observability.event_bus import EventBus
from core.observability.metric_names import (
    BATCH_TOTAL_TRIGGERS,
    CYCLES_ERRORS,
    CYCLES_TOTAL,
    execution_event_metric,
)


def _intent(symbol="XAUUSD"):
    return DecisionIntent(
        schema_version="v1",
        intent_id="i_e2e",
        candidate_id="c1",
        snapshot_id="s1",
        event_time=datetime(2026, 4, 24, 12, 0, 0),
        compiled_at=datetime(2026, 4, 24, 12, 0, 1),
        symbol=symbol,
        venue="MT5",
        action=DecisionAction.OPEN,
        side=DecisionSide.LONG,
        conviction=0.85,
        priority="high",
        suggested_risk_fraction=0.002,
    )


def _build_loop(container, tmp_path, intent_fn=None):
    snap = type(
        "CS",
        (),
        {
            "mode_state": type("MS", (), {"current_mode": type("M", (), {"value": "normal"})()})(),
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
            "supporting_brains": ["alpha"],
            "opposing_brains": [],
        },
    )()
    record = type("R", (), {"record_id": "r_e2e"})()

    return RuntimeLoop(
        control_snapshot_service=type("CSS", (), {"freeze": lambda self, **kw: snap})(),
        feature_service=type("FS_svc", (), {"build_snapshot": lambda self, trigger: feature})(),
        brain_run_service=type("BRS", (), {"run_active_brains": lambda self, **kw: []})(),
        parliament_adapter=container.parliament_service,
        override_resolver=type("OR", (), {"resolve": lambda self, **kw: []})(),
        decision_compiler=type(
            "DC",
            (),
            {
                "compile_intent": lambda self, **kw: (intent_fn or _intent)(),
            },
        )(),
        decision_record_writer=type(
            "DRW",
            (),
            {
                "seed_record": lambda self, **kw: (record, Path(tmp_path) / "x.jsonl"),
            },
        )(),
        intent_message_builder=container.message_builder,
        communication_dispatcher=container.dispatcher,
        communication_record_writer=container.communication_writer,
        risk_evaluation_service=container.risk_service,
    )


class TestEventBus:
    def test_pub_sub(self):
        bus = EventBus()
        received = []
        bus.subscribe("test.event", lambda t, p: received.append(p))
        bus.publish("test.event", {"key": "val"})
        assert len(received) == 1
        assert received[0]["key"] == "val"

    def test_multiple_subscribers(self):
        bus = EventBus()
        a, b = [], []
        bus.subscribe("x", lambda t, p: a.append(1))
        bus.subscribe("x", lambda t, p: b.append(1))
        count = bus.publish("x")
        assert count == 2

    def test_unsubscribe(self):
        bus = EventBus()

        def handler(t, p):
            return None

        bus.subscribe("x", handler)
        assert bus.get_subscriber_count("x") == 1
        bus.unsubscribe("x", handler)
        assert bus.get_subscriber_count("x") == 0

    def test_event_log(self):
        bus = EventBus()
        bus.publish("a")
        bus.publish("b")
        log = bus.get_event_log()
        assert len(log) == 2

    def test_no_subscribers_no_error(self):
        bus = EventBus()
        assert bus.publish("orphan") == 0

    def test_handler_error_doesnt_crash(self):
        bus = EventBus()
        bus.subscribe("x", lambda t, p: 1 / 0)
        bus.subscribe("x", lambda t, p: None)
        assert bus.publish("x") == 1


class TestBatchProcessor:
    def test_batch_decision_cycles(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        assert c.metrics is not None  # TECH_DEBT-009: 容器构建契约 (L156 metrics.get_counter)
        loop = _build_loop(c, tmp_path)
        orch = c.build_orchestrator(loop)

        batch = BatchProcessor(orch, metrics=c.metrics)
        triggers = [{"symbol": "XAUUSD"} for _ in range(5)]
        result = batch.run_batch(triggers, {"f": 1.0})

        assert result["total"] == 5
        assert result["completed"] == 5
        assert result["errors"] == 0
        assert c.metrics.get_counter(BATCH_TOTAL_TRIGGERS) == 5

    def test_batch_with_event_bus(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        loop = _build_loop(c, tmp_path)
        orch = c.build_orchestrator(loop)

        bus = EventBus()
        completed_events = []
        bus.subscribe("batch.completed", lambda t, p: completed_events.append(p))

        batch = BatchProcessor(orch, metrics=c.metrics, event_bus=bus)
        batch.run_batch([{"symbol": "XAUUSD"}] * 3, {"f": 1.0})

        assert len(completed_events) == 1
        assert completed_events[0]["total"] == 3

    def test_batch_error_handling(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()

        class FailingLoop:
            def run_decision_cycle(self, trigger, feature_source):
                if trigger.get("fail"):
                    raise RuntimeError("simulated failure")
                loop = _build_loop(c, tmp_path)
                return loop.run_decision_cycle(trigger, feature_source)

        orch = c.build_orchestrator(FailingLoop())
        batch = BatchProcessor(orch)
        triggers: list[dict[Any, Any]] = [
            {"symbol": "XAUUSD"},
            {"symbol": "XAUUSD", "fail": True},
            {"symbol": "XAUUSD"},
        ]
        result = batch.run_batch(triggers, {"f": 1.0})

        assert result["total"] == 3
        assert result["errors"] == 0

    def test_batch_venue_events(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        loop = _build_loop(c, tmp_path)
        orch = c.build_orchestrator(loop)

        outcome = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        msg_id = outcome.decision_result.communication_record.message_id

        batch = BatchProcessor(orch)
        events = [
            {"message_id": msg_id, "event_type": "ack", "venue": "v1"},
            {
                "message_id": msg_id,
                "event_type": "filled",
                "filled_quantity": 0.002,
                "price": 2000.0,
                "venue": "v1",
            },
        ]
        result = batch.process_venue_events_batch(events)
        assert result["processed"] == 2


class TestOrchestratorErrorRecovery:
    def test_decision_cycle_error_returns_graceful_outcome(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        assert c.metrics is not None  # TECH_DEBT-009: 容器构建契约 (L231 metrics.get_counter)

        class BrokenLoop:
            def run_decision_cycle(self, trigger, feature_source):
                raise RuntimeError("brain inference failed")

        orch = c.build_orchestrator(BrokenLoop())
        outcome = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})

        assert outcome.decision_result is None
        assert any("cycle_error" in str(e) for e in outcome.audit_entries)
        assert c.metrics.get_counter(CYCLES_ERRORS) == 1


class TestFullSystemIntegration:
    def test_complete_lifecycle_from_container(self, tmp_path):
        """One-shot integration: build → cycle → venue events → feedback → governance → persist."""
        cfg = EnvironmentConfig.development(str(tmp_path))
        c = ServiceContainer(cfg).build()
        assert c.governance_service is not None  # TECH_DEBT-009: 容器构建契约
        assert c.position_tracker is not None  # TECH_DEBT-009: 容器构建契约
        assert c.metrics is not None  # TECH_DEBT-009: 容器构建契约
        assert c.diagnostics is not None  # TECH_DEBT-009: 容器构建契约
        assert c.health_check is not None  # TECH_DEBT-009: 容器构建契约
        c.governance_service.register_brain("alpha", "live")

        loop = _build_loop(c, tmp_path)
        orch = c.build_orchestrator(loop)

        outcome = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        assert outcome.decision_result is not None
        assert outcome.decision_result.verdict.is_allowed()

        msg_id = outcome.decision_result.communication_record.message_id

        orch.process_execution_event(message_id=msg_id, event_type="ack", venue="exchange")
        result = orch.process_execution_event(
            message_id=msg_id,
            event_type="filled",
            filled_quantity=0.002,
            price=2000.5,
            venue="exchange",
        )
        assert result["execution"]["new_status"] == "filled"
        assert result.get("feedback") is not None

        assert c.position_tracker.get_risk_context()["open_position_count"] == 1
        assert c.metrics.get_counter(CYCLES_TOTAL) >= 1
        assert c.metrics.get_counter(execution_event_metric("filled")) >= 1

        snap = c.diagnostics.build_snapshot()
        assert snap["metrics"] is not None

        sp = StatePersistence(str(tmp_path / "state"))
        save_result = sp.save_all(c)
        assert len(save_result["paths"]) == 3

        health = c.health_check.readiness()
        assert health["status"] == "ready"

    def test_replay_environment_isolation(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path), enable_idempotency=False)
        c = ServiceContainer(cfg).build()

        replay = ReplayEnvironment(c)
        replay.activate()

        now = datetime.now(UTC).replace(tzinfo=None)

        def _replay_intent():
            return DecisionIntent(
                schema_version="v1",
                intent_id=f"i_{now.timestamp()}",
                candidate_id="c1",
                snapshot_id="s1",
                event_time=now,
                compiled_at=now,
                symbol="XAUUSD",
                venue="MT5",
                action=DecisionAction.OPEN,
                side=DecisionSide.LONG,
                conviction=0.85,
                priority="high",
                suggested_risk_fraction=0.002,
            )

        loop = _build_loop(c, tmp_path, intent_fn=_replay_intent)
        orch = c.build_orchestrator(loop)

        orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        orch.run_cycle({"symbol": "EURUSD"}, {"f": 0.5})

        captured = replay.get_captured_dispatches()
        assert len(captured) == 2

        summary = replay.get_replay_summary()
        assert summary["total_dispatches"] == 2

        replay.deactivate()
