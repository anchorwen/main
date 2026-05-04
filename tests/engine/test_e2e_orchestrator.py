from datetime import datetime
from pathlib import Path

from apps.engine.orchestrator import DecisionCycleOrchestrator
from apps.engine.runtime_loop import RuntimeLoop
from core.contracts.domain.decision_intent import DecisionIntent
from core.contracts.enums import DecisionAction, DecisionSide
from core.execution.execution_manager import ExecutionManager
from core.feedback.brain_performance_tracker import BrainPerformanceTracker
from core.feedback.decision_scorer import DecisionScorer
from core.feedback.feedback_loop import FeedbackLoop
from core.feedback.outcome_collector import OutcomeCollector
from core.governance.governance_rule_engine import GovernanceRule, GovernanceRuleEngine
from core.governance.governance_service import GovernanceService
from core.ledger.services.communication_record_reader import CommunicationRecordReader
from core.ledger.services.communication_record_writer import CommunicationRecordWriter
from core.ledger.services.execution_event_reader import ExecutionEventReader
from core.ledger.services.execution_event_writer import ExecutionEventWriter
from core.ledger.services.execution_reconciliation_service import ExecutionReconciliationService
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.market.position_tracker import MarketContextProvider, PositionTracker
from core.observability.audit_log import StructuredAuditLog
from core.observability.metric_names import (
    CYCLES_ALLOWED,
    CYCLES_BLOCKED,
    CYCLES_CIRCUIT_OPEN,
    CYCLES_THROTTLED,
    CYCLES_TOTAL,
)
from core.observability.metrics_collector import MetricsCollector
from core.protocol.services.communication_dispatcher import CommunicationDispatcher
from core.protocol.services.intent_message_builder import IntentMessageBuilder
from core.protocol.services.stub_communication_adapter import StubCommunicationAdapter
from core.risk.risk_evaluation_service import RiskEvaluationService
from core.risk.risk_policies import ModePolicy, PositionLimitPolicy


def _intent(action=DecisionAction.OPEN, symbol="XAUUSD"):
    side = (
        DecisionSide.FLAT
        if action in {DecisionAction.ABSTAIN, DecisionAction.OBSERVE}
        else DecisionSide.LONG
    )
    return DecisionIntent(
        schema_version="v1",
        intent_id="intent_e2e",
        candidate_id="c1",
        snapshot_id="s1",
        event_time=datetime(2026, 4, 24, 12, 0, 0),
        compiled_at=datetime(2026, 4, 24, 12, 0, 1),
        symbol=symbol,
        venue="MT5",
        action=action,
        side=side,
        conviction=0.85,
        priority="high",
        suggested_risk_fraction=0.002,
    )


def _build_full_stack(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    comm_writer = CommunicationRecordWriter(ledger_store=store)
    comm_reader = CommunicationRecordReader(str(tmp_path))
    event_writer = ExecutionEventWriter(store)
    event_reader = ExecutionEventReader(str(tmp_path))
    recon = ExecutionReconciliationService(comm_reader, event_reader)
    metrics = MetricsCollector()
    audit = StructuredAuditLog(str(tmp_path / "audit"))
    position_tracker = PositionTracker()
    market_ctx = MarketContextProvider()
    market_ctx.update("XAUUSD", bid=2000.0, ask=2001.0)

    governance = GovernanceService(audit_log=audit)
    governance.register_brain("alpha_v1", "live")
    governance.register_brain("regime_v2", "candidate")

    brain_tracker = BrainPerformanceTracker(window_size=50)
    feedback = FeedbackLoop(
        outcome_collector=OutcomeCollector(event_reader, recon),
        decision_scorer=DecisionScorer(),
        brain_performance_tracker=brain_tracker,
    )

    execution_manager = ExecutionManager(
        execution_event_writer=event_writer,
        position_tracker=position_tracker,
        metrics=metrics,
    )

    risk_svc = RiskEvaluationService(
        [
            ModePolicy(),
            PositionLimitPolicy(max_open_positions=10),
        ]
    )

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
    candidate = type(
        "DC",
        (),
        {
            "regime_state": {"primary_regime": "trend"},
            "candidate_id": "c1",
            "supporting_brains": ["alpha_v1"],
            "opposing_brains": ["regime_v2"],
        },
    )()
    record = type("R", (), {"record_id": "record_e2e"})()

    runtime_loop = RuntimeLoop(
        control_snapshot_service=type("CSS", (), {"freeze": lambda self, **kw: snap})(),
        feature_service=type("FS_svc", (), {"build_snapshot": lambda self, trigger: feature})(),
        brain_run_service=type("BRS", (), {"run_active_brains": lambda self, **kw: []})(),
        parliament_adapter=type("PA", (), {"build_candidate": lambda self, **kw: candidate})(),
        override_resolver=type("OR", (), {"resolve": lambda self, **kw: []})(),
        decision_compiler=type("DC_c", (), {"compile_intent": lambda self, **kw: _intent()})(),
        decision_record_writer=type(
            "DRW",
            (),
            {
                "seed_record": lambda self, **kw: (record, Path(tmp_path) / "x.jsonl"),
            },
        )(),
        intent_message_builder=IntentMessageBuilder(producer="engine", target="exec_bridge"),
        communication_dispatcher=CommunicationDispatcher(
            adapter=StubCommunicationAdapter(),
            clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
        ),
        communication_record_writer=comm_writer,
        risk_evaluation_service=risk_svc,
    )

    orchestrator = DecisionCycleOrchestrator(
        runtime_loop,
        execution_manager=execution_manager,
        position_tracker=position_tracker,
        market_context=market_ctx,
        feedback_loop=feedback,
        governance_service=governance,
        audit_log=audit,
        metrics=metrics,
    )

    return orchestrator, execution_manager, governance, metrics, brain_tracker, audit


class TestEndToEndCycleOrchestrator:
    def test_full_cycle_trigger_to_feedback(self, tmp_path):
        orch, em, gov, metrics, tracker, audit = _build_full_stack(tmp_path)

        outcome = orch.run_cycle(
            trigger={"symbol": "XAUUSD"},
            feature_source={"f1": 1.0},
        )

        assert outcome.decision_result.verdict.is_allowed()
        assert outcome.execution_result is not None
        assert outcome.execution_result["status"] == "pending"
        assert metrics.get_counter(CYCLES_TOTAL) == 1
        assert metrics.get_counter(CYCLES_ALLOWED) == 1

        msg_id = outcome.decision_result.communication_record.message_id

        orch.process_execution_event(
            message_id=msg_id,
            event_type="ack",
            venue="exchange_a",
        )
        order = em.get_order(msg_id)
        assert order["status"] == "sent"

        result = orch.process_execution_event(
            message_id=msg_id,
            event_type="filled",
            filled_quantity=0.002,
            price=2000.5,
            venue="exchange_a",
        )

        assert result["execution"]["new_status"] == "filled"
        assert result.get("feedback") is not None
        assert result["feedback"]["scored"]["composite_score"] > 0

        alpha_summary = tracker.get_brain_summary("alpha_v1")
        assert alpha_summary["sample_count"] == 1

    def test_blocked_verdict_skips_execution(self, tmp_path):
        JsonlLedgerStore(str(tmp_path))
        metrics = MetricsCollector()

        snap = type(
            "CS",
            (),
            {
                "mode_state": type(
                    "MS", (), {"current_mode": type("M", (), {"value": "normal"})()}
                )(),
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
        candidate = type(
            "DC",
            (),
            {
                "regime_state": {"primary_regime": "trend"},
                "supporting_brains": [],
                "opposing_brains": [],
            },
        )()
        record = type("R", (), {"record_id": "r1"})()

        risk_svc = RiskEvaluationService([PositionLimitPolicy(max_open_positions=0)])

        loop = RuntimeLoop(
            control_snapshot_service=type("CSS", (), {"freeze": lambda self, **kw: snap})(),
            feature_service=type("FS", (), {"build_snapshot": lambda self, trigger: feature})(),
            brain_run_service=type("BRS", (), {"run_active_brains": lambda self, **kw: []})(),
            parliament_adapter=type("PA", (), {"build_candidate": lambda self, **kw: candidate})(),
            override_resolver=type("OR", (), {"resolve": lambda self, **kw: []})(),
            decision_compiler=type("DC", (), {"compile_intent": lambda self, **kw: _intent()})(),
            decision_record_writer=type(
                "DRW",
                (),
                {
                    "seed_record": lambda self, **kw: (record, Path(tmp_path) / "x.jsonl"),
                },
            )(),
            risk_evaluation_service=risk_svc,
        )

        orch = DecisionCycleOrchestrator(loop, metrics=metrics)
        outcome = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1})

        assert not outcome.decision_result.verdict.is_allowed()
        assert outcome.execution_result is None
        assert metrics.get_counter(CYCLES_BLOCKED) == 1


class TestGovernanceRuleEngine:
    def test_auto_freeze_critical(self):
        gs = GovernanceService()
        gs.register_brain("bad", "live")
        engine = GovernanceRuleEngine.with_default_rules(gs)
        fired = engine.evaluate(
            {
                "bad": {"health_signal": "critical", "sample_count": 20, "composite_mean": 0.1},
            }
        )
        assert len(fired) >= 1
        assert any(r["transition_to"] == "frozen" for r in fired)
        assert gs.get_brain_state("bad")["status"] == "frozen"

    def test_auto_promote_healthy(self):
        gs = GovernanceService()
        gs.register_brain("good", "candidate")
        engine = GovernanceRuleEngine.with_default_rules(gs)
        fired = engine.evaluate(
            {
                "good": {"health_signal": "healthy", "sample_count": 50, "composite_mean": 0.85},
            }
        )
        assert any(r["transition_to"] == "live" for r in fired)
        assert gs.get_brain_state("good")["status"] == "live"

    def test_auto_demote_degraded_live(self):
        gs = GovernanceService()
        gs.register_brain("degraded", "live")
        engine = GovernanceRuleEngine.with_default_rules(gs)
        fired = engine.evaluate(
            {
                "degraded": {
                    "health_signal": "degraded",
                    "sample_count": 20,
                    "composite_mean": 0.25,
                },
            }
        )
        assert any(r.get("transition_to") == "probation" for r in fired)
        assert gs.get_brain_state("degraded")["status"] == "probation"

    def test_custom_rule(self):
        gs = GovernanceService()
        gs.register_brain("custom", "live")
        engine = GovernanceRuleEngine(gs)
        engine.add_rule(
            GovernanceRule(
                name="high_reject_rate",
                condition_fn=lambda ctx: ctx.get("reject_rate", 0) > 0.5,
                action_fn=lambda ctx: {"transition_to": "frozen", "reason": "high_reject_rate"},
                priority=80,
            )
        )
        fired = engine.evaluate({"custom": {"reject_rate": 0.7}})
        assert len(fired) == 1
        assert gs.get_brain_state("custom")["status"] == "frozen"

    def test_no_matching_rules(self):
        gs = GovernanceService()
        gs.register_brain("stable", "live")
        engine = GovernanceRuleEngine.with_default_rules(gs)
        engine.evaluate(
            {
                "stable": {"health_signal": "stable", "sample_count": 5, "composite_mean": 0.6},
            }
        )
        assert gs.get_brain_state("stable")["status"] == "live"

    def test_with_audit_log(self, tmp_path):
        gs = GovernanceService()
        gs.register_brain("audited", "live")
        audit = StructuredAuditLog(str(tmp_path / "audit"))
        engine = GovernanceRuleEngine.with_default_rules(gs, audit_log=audit)
        engine.evaluate(
            {
                "audited": {"health_signal": "critical", "sample_count": 15},
            }
        )
        entries = audit.read_entries()
        assert len(entries) >= 1
        assert any(e["event_type"] == "governance_signal" for e in entries)


class TestOrchestratorResilience:
    def test_rate_limiter_throttles(self, tmp_path):
        from core.protocol.services.resilience import RateLimiter

        orch, _, _, metrics, _, _ = _build_full_stack(tmp_path)
        rl = RateLimiter(max_rate=1, window_seconds=10.0)
        orch._rate_limiter = rl

        r1 = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        assert r1.decision_result is not None

        r2 = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        assert r2.decision_result is None
        assert metrics.get_counter(CYCLES_THROTTLED) == 1

    def test_circuit_breaker_blocks(self, tmp_path):
        from core.protocol.services.resilience import CircuitBreaker

        orch, _, _, metrics, _, _ = _build_full_stack(tmp_path)
        cb = CircuitBreaker(failure_threshold=1)
        orch._circuit_breaker = cb
        cb.record_failure()

        r = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        assert r.decision_result is None
        assert metrics.get_counter(CYCLES_CIRCUIT_OPEN) == 1
