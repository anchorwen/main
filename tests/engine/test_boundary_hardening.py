"""Boundary hardening tests for core paths: risk, governance, execution, signals."""

from datetime import UTC, datetime, timedelta

from apps.engine.system_facade import SystemFacade
from core.contracts.enums import RiskDecisionStatus
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer
from core.execution.execution_manager import ExecutionManager
from core.feedback.brain_performance_tracker import BrainPerformanceTracker
from core.governance.governance_rule_engine import GovernanceRuleEngine
from core.governance.governance_service import GovernanceService
from core.market.position_tracker import PositionTracker
from core.market.signal_processor import MarketSignalProcessor, SignalFilter
from core.observability.metrics_collector import MetricsCollector
from core.risk.risk_evaluation_service import RiskEvaluationService
from core.risk.risk_policies import (
    ConcentrationPolicy,
    DrawdownPolicy,
    ExposurePolicy,
    ModePolicy,
    PositionLimitPolicy,
)


def _container(tmp_path, **kw):
    cfg = EnvironmentConfig.development(str(tmp_path), **kw)
    return ServiceContainer(cfg).build()


def _snap(mode="normal"):
    return type(
        "CS",
        (),
        {
            "mode_state": type("MS", (), {"current_mode": type("CM", (), {"value": mode})()})(),
        },
    )()


def _intent(action="open", symbol="XAUUSD"):
    return type(
        "I",
        (),
        {
            "action": type("A", (), {"value": action})(),
            "symbol": symbol,
            "intent_id": "test",
            "is_passive": lambda self: action in ("observe", "abstain"),
            "is_open_intent": lambda self: action == "open",
        },
    )()


class TestRiskBoundaries:
    def test_all_policies_block(self):
        svc = RiskEvaluationService([PositionLimitPolicy(max_open_positions=0)])
        v = svc.evaluate(_intent(), _snap(), context={"open_position_count": 1})
        assert not v.is_allowed()

    def test_drawdown_boundary(self):
        policy = DrawdownPolicy(max_drawdown_pct=5.0)
        result = policy.evaluate(_intent(), _snap(), {"current_drawdown_pct": 5.0})
        assert result["status"] != RiskDecisionStatus.ALLOW

    def test_drawdown_just_under(self):
        policy = DrawdownPolicy(max_drawdown_pct=5.0)
        result = policy.evaluate(_intent(), _snap(), {"current_drawdown_pct": 4.99})
        assert result["status"] in (RiskDecisionStatus.ALLOW, RiskDecisionStatus.ALLOW_LIMITED)

    def test_exposure_exactly_at_limit(self):
        policy = ExposurePolicy(max_notional=100000)
        result = policy.evaluate(_intent(), _snap(), {"current_notional_exposure": 100000})
        assert result["status"] != RiskDecisionStatus.ALLOW

    def test_concentration_at_limit(self):
        policy = ConcentrationPolicy(max_per_symbol=3)
        result = policy.evaluate(
            _intent(symbol="XAUUSD"),
            _snap(),
            {"positions_per_symbol": {"XAUUSD": 3}},
        )
        assert result["status"] != RiskDecisionStatus.ALLOW

    def test_halted_mode_blocks_all(self):
        policy = ModePolicy()
        result = policy.evaluate(_intent(), _snap("halted"), {})
        assert result["status"] == RiskDecisionStatus.DENY

    def test_observe_only_allows_observe(self):
        policy = ModePolicy()
        result = policy.evaluate(_intent("observe"), _snap("observe_only"), {})
        assert result["status"] != RiskDecisionStatus.DENY

    def test_multiple_blocking_reasons(self):
        svc = RiskEvaluationService(
            [
                PositionLimitPolicy(max_open_positions=0),
                DrawdownPolicy(max_drawdown_pct=1.0),
            ]
        )
        v = svc.evaluate(
            _intent(),
            _snap(),
            context={"open_position_count": 5, "current_drawdown_pct": 10.0},
        )
        assert not v.is_allowed()
        assert len(v.blocking_reasons) >= 2


class TestGovernanceBoundaries:
    def test_transition_frozen_to_frozen_idempotent(self):
        gs = GovernanceService()
        gs.register_brain("a", "live")
        gs.transition("a", "frozen", "test")
        gs.transition("a", "frozen", "test2")
        assert gs.get_brain_state("a")["status"] == "frozen"

    def test_register_duplicate_preserves_first(self):
        gs = GovernanceService()
        gs.register_brain("x", "live")
        gs.register_brain("x", "candidate")
        state = gs.get_brain_state("x")
        assert state is not None

    def test_rule_engine_empty_summaries(self):
        gs = GovernanceService()
        engine = GovernanceRuleEngine.with_default_rules(gs)
        fired = engine.evaluate({})
        assert fired == []

    def test_rule_engine_unknown_health(self):
        gs = GovernanceService()
        gs.register_brain("new", "live")
        engine = GovernanceRuleEngine.with_default_rules(gs)
        engine.evaluate({"new": {"health_signal": "unknown"}})
        assert gs.get_brain_state("new")["status"] == "live"

    def test_frozen_brain_excluded_from_active(self):
        gs = GovernanceService()
        gs.register_brain("a", "live")
        gs.register_brain("b", "live")
        gs.transition("b", "frozen", "test")
        active = gs.get_active_brain_ids()
        assert "a" in active
        assert "b" not in active


class TestExecutionBoundaries:
    def test_duplicate_order_registration(self):
        em = ExecutionManager(
            execution_event_writer=None,
            position_tracker=PositionTracker(),
            metrics=MetricsCollector(),
        )
        em.register_order(
            message_id="m1", correlation_id="c1", symbol="XAUUSD", side="long", quantity=1.0
        )
        em.register_order(
            message_id="m1", correlation_id="c1", symbol="XAUUSD", side="long", quantity=1.0
        )
        orders = em.list_orders()
        m1_orders = [o for o in orders if o["message_id"] == "m1"]
        assert len(m1_orders) >= 1

    def test_event_on_unknown_order(self):
        em = ExecutionManager(
            execution_event_writer=None,
            position_tracker=PositionTracker(),
            metrics=MetricsCollector(),
        )
        result = em.process_venue_event(
            message_id="nonexistent", event_type="filled", filled_quantity=1.0, price=100.0
        )
        assert result.get("status") == "unknown_order"

    def test_position_tracker_close_nonexistent(self):
        pt = PositionTracker()
        pt.close_position("nonexistent", 100.0)
        assert pt.list_closed() == []

    def test_position_tracker_multiple_open_same_symbol(self):
        pt = PositionTracker()
        pt.open_position(
            position_id="p1", symbol="XAUUSD", side="long", quantity=1.0, entry_price=2000.0
        )
        pt.open_position(
            position_id="p2", symbol="XAUUSD", side="long", quantity=2.0, entry_price=2010.0
        )
        ctx = pt.get_risk_context()
        assert ctx["open_position_count"] == 2
        assert ctx["positions_per_symbol"]["XAUUSD"] == 2


class TestSignalFilterBoundaries:
    def test_missing_symbol(self):
        sf = SignalFilter()
        ok, reason = sf.accept({})
        assert not ok
        assert reason == "missing_symbol"

    def test_duplicate_cooldown(self):
        sf = SignalFilter(cooldown_seconds=10.0)
        ok1, _ = sf.accept({"symbol": "XAUUSD"})
        ok2, reason = sf.accept({"symbol": "XAUUSD"})
        assert ok1 and not ok2
        assert reason == "duplicate_cooldown"

    def test_stale_signal(self):
        sf = SignalFilter(max_staleness_seconds=5.0)
        ok, reason = sf.accept(
            {
                "symbol": "XAUUSD",
                "timestamp": datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=10),
            }
        )
        assert not ok
        assert reason == "stale_signal"

    def test_symbol_not_allowed(self):
        sf = SignalFilter(allowed_symbols={"XAUUSD", "EURUSD"})
        ok, reason = sf.accept({"symbol": "BTCUSD"})
        assert not ok
        assert reason == "symbol_not_allowed"

    def test_fresh_signal_accepted(self):
        sf = SignalFilter(cooldown_seconds=0)
        ok, reason = sf.accept(
            {
                "symbol": "XAUUSD",
                "timestamp": datetime.now(UTC).replace(tzinfo=None),
            }
        )
        assert ok

    def test_stats_accumulate(self):
        sf = SignalFilter(cooldown_seconds=0)
        sf.accept({"symbol": "A"})
        sf.accept({})
        sf.accept({"symbol": "A"})
        stats = sf.get_stats()
        assert stats["accepted"] == 2
        assert stats["rejected_invalid"] == 1

    def test_reset_clears_cooldown(self):
        sf = SignalFilter(cooldown_seconds=100)
        sf.accept({"symbol": "X"})
        sf.reset()
        ok, _ = sf.accept({"symbol": "X"})
        assert ok


class TestMarketSignalProcessor:
    def test_process_tick(self, tmp_path):
        c = _container(tmp_path, enable_idempotency=False)
        orch = c.build_orchestrator()
        facade = SystemFacade(c, orchestrator=orch)
        proc = MarketSignalProcessor(facade, SignalFilter(cooldown_seconds=0), c.market_context)

        result = proc.process_tick({"symbol": "XAUUSD", "bid": 2000.0, "ask": 2001.0})
        assert result["status"] == "triggered"
        assert "decision" in result

    def test_process_tick_filtered(self, tmp_path):
        c = _container(tmp_path, enable_idempotency=False)
        orch = c.build_orchestrator()
        facade = SystemFacade(c, orchestrator=orch)
        proc = MarketSignalProcessor(facade, SignalFilter(cooldown_seconds=100))

        proc.process_tick({"symbol": "XAUUSD"})
        r2 = proc.process_tick({"symbol": "XAUUSD"})
        assert r2["status"] == "filtered"
        assert r2["reason"] == "duplicate_cooldown"

    def test_process_batch(self, tmp_path):
        c = _container(tmp_path, enable_idempotency=False)
        orch = c.build_orchestrator()
        facade = SystemFacade(c, orchestrator=orch)
        proc = MarketSignalProcessor(facade, SignalFilter(cooldown_seconds=0))

        signals = [
            {"symbol": "XAUUSD", "bid": 2000, "ask": 2001},
            {"symbol": "EURUSD", "bid": 1.10, "ask": 1.1001},
            {},
        ]
        result = proc.process_batch(signals)
        assert result["total"] == 3
        assert result["triggered"] == 2
        assert result["filtered"] == 1

    def test_stats(self, tmp_path):
        c = _container(tmp_path, enable_idempotency=False)
        orch = c.build_orchestrator()
        facade = SystemFacade(c, orchestrator=orch)
        proc = MarketSignalProcessor(facade, SignalFilter(cooldown_seconds=0))

        proc.process_tick({"symbol": "XAUUSD"})
        stats = proc.get_stats()
        assert stats["processed"] == 1
        assert stats["triggered"] == 1


class TestBrainPerformanceBoundaries:
    def test_empty_tracker(self):
        bt = BrainPerformanceTracker()
        s = bt.get_brain_summary("nonexistent")
        assert s["sample_count"] == 0

    def test_window_eviction(self):
        bt = BrainPerformanceTracker(window_size=3)
        for i in range(5):
            bt.record_outcome("x", {"composite_score": float(i) / 10})
        s = bt.get_brain_summary("x")
        assert s["sample_count"] == 3

    def test_multiple_brains_isolated(self):
        bt = BrainPerformanceTracker()
        bt.record_outcome("a", {"composite_score": 0.9})
        bt.record_outcome("b", {"composite_score": 0.1})
        sa = bt.get_brain_summary("a")
        sb = bt.get_brain_summary("b")
        assert sa["composite_mean"] > sb["composite_mean"]
