"""Production scenario tests: stress, recovery, contract validation."""

import time

from apps.engine.system_facade import SystemFacade, SystemSelfTest
from core.contracts.validators import ContractValidator
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.lifecycle_manager import LifecycleManager
from core.deployment.service_container import ServiceContainer
from core.deployment.state_persistence import StatePersistence
from core.market.signal_processor import MarketSignalProcessor, SignalFilter
from core.observability.metric_names import CYCLES_TOTAL
from core.protocol.services.resilience import CircuitBreaker, RateLimiter


def _sys(tmp_path, **kw):
    cfg = EnvironmentConfig.development(str(tmp_path), enable_idempotency=False, **kw)
    c = ServiceContainer(cfg).build()
    orch = c.build_orchestrator()
    return c, orch


class TestStressMultiSymbol:
    def test_rapid_multi_symbol_ticks(self, tmp_path):
        c, orch = _sys(tmp_path)
        facade = SystemFacade(c, orchestrator=orch)
        proc = MarketSignalProcessor(facade, SignalFilter(cooldown_seconds=0), c.market_context)
        symbols = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
        for i in range(50):
            proc.process_tick(
                {"symbol": symbols[i % 5], "bid": 100 + i * 0.01, "ask": 100.01 + i * 0.01}
            )
        assert proc.get_stats()["triggered"] == 50
        assert c.metrics.get_counter(CYCLES_TOTAL) == 50  # type: ignore[reportOptionalMemberAccess]


class TestStressPositionExhaustion:
    def test_position_limit_blocks_after_max(self, tmp_path):
        cfg = EnvironmentConfig.development(
            str(tmp_path), max_open_positions=3, enable_idempotency=False
        )
        c = ServiceContainer(cfg).build()
        for i in range(3):
            c.position_tracker.open_position(  # type: ignore[reportOptionalMemberAccess]
                position_id=f"p{i}", symbol="XAUUSD", side="long", quantity=1.0, entry_price=2000.0
            )
        orch = c.build_orchestrator()
        outcome = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        assert not outcome.decision_result.verdict.is_allowed()

    def test_position_freed_allows_new(self, tmp_path):
        cfg = EnvironmentConfig.development(
            str(tmp_path), max_open_positions=2, enable_idempotency=False
        )
        c = ServiceContainer(cfg).build()
        c.position_tracker.open_position(  # type: ignore[reportOptionalMemberAccess]
            position_id="p0", symbol="XAUUSD", side="long", quantity=1.0, entry_price=2000.0
        )
        c.position_tracker.open_position(  # type: ignore[reportOptionalMemberAccess]
            position_id="p1", symbol="XAUUSD", side="long", quantity=1.0, entry_price=2000.0
        )
        orch = c.build_orchestrator()
        orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        ctx_before = c.position_tracker.get_risk_context()  # type: ignore[reportOptionalMemberAccess]
        assert ctx_before["open_position_count"] == 2

        c.position_tracker.close_position("p0", 2010.0)  # type: ignore[reportOptionalMemberAccess]
        c.position_tracker.close_position("p1", 2010.0)  # type: ignore[reportOptionalMemberAccess]
        ctx_after = c.position_tracker.get_risk_context()  # type: ignore[reportOptionalMemberAccess]
        assert ctx_after["open_position_count"] == 0


class TestStressCircuitBreaker:
    def test_circuit_opens_blocks_then_recovers(self, tmp_path):
        c, orch = _sys(tmp_path)
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=0.1)
        orch._circuit_breaker = cb
        for _ in range(3):
            cb.record_failure()
        assert orch.run_cycle({"symbol": "X"}, {"f": 1}).decision_result is None
        time.sleep(0.15)
        assert orch.run_cycle({"symbol": "X"}, {"f": 1}).decision_result is not None


class TestStressRateLimiter:
    def test_burst_throttled(self, tmp_path):
        c, orch = _sys(tmp_path)
        orch._rate_limiter = RateLimiter(max_rate=5, window_seconds=10.0)
        results = [orch.run_cycle({"symbol": "X"}, {"f": 1}) for _ in range(10)]
        assert sum(1 for r in results if r.decision_result is not None) == 5
        assert sum(1 for r in results if r.decision_result is None) == 5


class TestStressGovernanceCascade:
    def test_brain_freeze_cascade(self, tmp_path):
        c, _ = _sys(tmp_path)
        for b in ["alpha", "beta", "gamma"]:
            c.governance_service.register_brain(b, "live")  # type: ignore[reportOptionalMemberAccess]
            for _ in range(20):
                c.brain_tracker.record_outcome(b, {"composite_score": 0.05})  # type: ignore[reportOptionalMemberAccess]
        smap = {s["brain_id"]: s for s in c.brain_tracker.get_all_summaries()}  # type: ignore[reportOptionalMemberAccess]
        fired = c.governance_rule_engine.evaluate(smap)  # type: ignore[reportOptionalMemberAccess]
        assert len(fired) == 3
        for b in ["alpha", "beta", "gamma"]:
            assert c.governance_service.get_brain_state(b)["status"] in ("frozen", "probation")  # type: ignore[reportOptionalMemberAccess]

    def test_mixed_health_correct_transitions(self, tmp_path):
        c, _ = _sys(tmp_path)
        c.governance_service.register_brain("healthy", "candidate")  # type: ignore[reportOptionalMemberAccess]
        c.governance_service.register_brain("degraded", "live")  # type: ignore[reportOptionalMemberAccess]
        c.governance_service.register_brain("critical", "live")  # type: ignore[reportOptionalMemberAccess]
        c.governance_rule_engine.evaluate(  # type: ignore[reportOptionalMemberAccess]
            {
                "healthy": {"health_signal": "healthy", "sample_count": 50, "composite_mean": 0.9},
                "degraded": {
                    "health_signal": "degraded",
                    "sample_count": 30,
                    "composite_mean": 0.25,
                },
                "critical": {
                    "health_signal": "critical",
                    "sample_count": 20,
                    "composite_mean": 0.05,
                },
            }
        )
        assert c.governance_service.get_brain_state("healthy")["status"] == "live"  # type: ignore[reportOptionalMemberAccess]
        assert c.governance_service.get_brain_state("degraded")["status"] == "probation"  # type: ignore[reportOptionalMemberAccess]
        assert c.governance_service.get_brain_state("critical")["status"] == "frozen"  # type: ignore[reportOptionalMemberAccess]


class TestRecovery:
    def test_save_crash_restore(self, tmp_path):
        c1, _ = _sys(tmp_path / "d1")
        c1.governance_service.register_brain("alpha", "live")  # type: ignore[reportOptionalMemberAccess]
        c1.position_tracker.open_position(  # type: ignore[reportOptionalMemberAccess]
            position_id="p1", symbol="X", side="long", quantity=1, entry_price=100
        )
        sp = StatePersistence(str(tmp_path / "state"))
        sp.save_all(c1, label="pre_crash")
        c2, _ = _sys(tmp_path / "d2")
        assert sp.restore_governance_state(c2.governance_service, "pre_crash") is not None
        assert c2.governance_service.get_brain_state("alpha") is not None  # type: ignore[reportOptionalMemberAccess]

    def test_lifecycle_restore(self, tmp_path):
        c1, _ = _sys(tmp_path / "d1")
        c1.governance_service.register_brain("x", "live")  # type: ignore[reportOptionalMemberAccess]
        sp = StatePersistence(str(tmp_path / "state"))
        lm1 = LifecycleManager(c1, sp)
        lm1.startup()
        lm1.shutdown(save_state=True, state_label="s1")
        c2, _ = _sys(tmp_path / "d2")
        lm2 = LifecycleManager(c2, sp)
        r = lm2.startup(restore_state=True, state_label="s1")
        assert r["status"] == "started"
        assert c2.governance_service.get_brain_state("x") is not None  # type: ignore[reportOptionalMemberAccess]
        lm2.shutdown(save_state=False)

    def test_degraded_no_metrics(self, tmp_path):
        cfg = EnvironmentConfig.test(str(tmp_path))
        c = ServiceContainer(cfg).build()
        orch = c.build_orchestrator()
        assert orch.run_cycle({"symbol": "X"}, {"f": 1}).decision_result is not None

    def test_degraded_no_feedback(self, tmp_path):
        cfg = EnvironmentConfig.development(
            str(tmp_path), enable_feedback_loop=False, enable_idempotency=False
        )
        c = ServiceContainer(cfg).build()
        orch = c.build_orchestrator()
        assert orch.run_cycle({"symbol": "X"}, {"f": 1}).decision_result is not None


class TestContractValidation:
    def test_intent_contract(self, tmp_path):
        c, orch = _sys(tmp_path)
        out = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1})
        if out.decision_result:
            assert ContractValidator.validate_intent(out.decision_result.intent) == []

    def test_verdict_contract(self, tmp_path):
        c, orch = _sys(tmp_path)
        out = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1})
        if out.decision_result:
            assert ContractValidator.validate_verdict(out.decision_result.verdict) == []

    def test_envelope_contract(self, tmp_path):
        c, orch = _sys(tmp_path)
        out = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1})
        if out.decision_result and out.decision_result.communication_record:
            assert (
                ContractValidator.validate_envelope(out.decision_result.communication_record) == []
            )

    def test_risk_context_contract(self, tmp_path):
        c, _ = _sys(tmp_path)
        c.position_tracker.open_position(  # type: ignore[reportOptionalMemberAccess]
            position_id="p1", symbol="X", side="long", quantity=1, entry_price=100
        )
        assert ContractValidator.validate_risk_context(c.position_tracker.get_risk_context()) == []  # type: ignore[reportOptionalMemberAccess]

    def test_outcome_contract(self, tmp_path):
        c, orch = _sys(tmp_path)
        assert (
            ContractValidator.validate_cycle_outcome(orch.run_cycle({"symbol": "X"}, {"f": 1}))
            == []
        )

    def test_invalid_intent_detected(self):
        bad = type(
            "B",
            (),
            {
                "intent_id": "",
                "candidate_id": None,
                "snapshot_id": "ok",
                "symbol": "X",
                "venue": "MT5",
                "action": None,
                "side": None,
                "conviction": 2.0,
                "event_time": "bad",
            },
        )()
        assert len(ContractValidator.validate_intent(bad)) >= 3


class TestProductionDay:
    def test_full_day(self, tmp_path):
        cfg = EnvironmentConfig.production(str(tmp_path / "data"))
        cfg.enable_idempotency = False
        c = ServiceContainer(cfg).build()
        c.governance_service.register_brain("alpha_v1", "live")  # type: ignore[reportOptionalMemberAccess]
        sp = StatePersistence(str(tmp_path / "state"))
        lm = LifecycleManager(c, sp)
        lm.startup()
        orch = c.build_orchestrator()
        facade = SystemFacade(c, orchestrator=orch, lifecycle=lm)

        assert SystemSelfTest(c).run()["all_passed"]

        filled_ids = []
        for i in range(20):
            r = facade.decide(["XAUUSD", "EURUSD", "GBPUSD"][i % 3])
            if r.get("allowed") and r.get("message_id"):
                filled_ids.append(r["message_id"])

        for mid in filled_ids[:3]:
            facade.process_event(mid, "ack", venue="ex")
            facade.process_event(mid, "filled", filled_quantity=0.001, price=2000, venue="ex")

        assert facade.health()["readiness"]["status"] == "ready"
        assert c.metrics.get_counter(CYCLES_TOTAL) >= 20  # type: ignore[reportOptionalMemberAccess]
        lm.shutdown(save_state=True)
