from datetime import datetime

from core.contracts.domain.execution_event import ExecutionEvent
from core.contracts.ids import new_execution_event_id
from core.ledger.services.execution_event_writer import ExecutionEventWriter
from core.ledger.services.execution_event_reader import ExecutionEventReader
from core.ledger.services.execution_reconciliation_service import ExecutionReconciliationService
from core.ledger.services.communication_record_writer import CommunicationRecordWriter
from core.ledger.services.communication_record_reader import CommunicationRecordReader
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import DispatchStatus, CommunicationMessageType, CommunicationPriority
from core.feedback.outcome_collector import OutcomeCollector
from core.feedback.decision_scorer import DecisionScorer
from core.feedback.brain_performance_tracker import BrainPerformanceTracker
from core.feedback.feedback_loop import FeedbackLoop


def _env(mid, cid, qty=0):
    return CommunicationEnvelope(
        schema_version="v1", message_id=mid, correlation_id=cid, causation_id=None,
        event_time=datetime(2026, 4, 24, 12, 0, 0), producer="t", target="exec_bridge",
        message_type=CommunicationMessageType.EXECUTION_DISPATCH,
        priority=CommunicationPriority.NORMAL,
        payload={"quantity": qty} if qty else {},
    )


def _dr(mid):
    return DispatchResult(
        schema_version="v1", dispatch_id=f"d_{mid}", message_id=mid,
        status=DispatchStatus.TRANSPORT_DELIVERED,
        recorded_at=datetime(2026, 4, 24, 12, 0, 1),
        target="exec_bridge", adapter_name="stub",
        attempts=[{"adapter_name": "stub", "status": "succeeded", "reason": None}],
    )


def _ev(mid, cid, etype, fq=0):
    return ExecutionEvent(
        schema_version="v1", event_id=new_execution_event_id(),
        message_id=mid, correlation_id=cid, event_type=etype,
        event_time=datetime(2026, 4, 24, 12, 0, 5),
        recorded_at=datetime(2026, 4, 24, 12, 0, 5),
        venue="test", quantity={"filled": fq} if fq else {},
    )


def _build(tmp_path, msgs, evts):
    store = JsonlLedgerStore(str(tmp_path))
    cw = CommunicationRecordWriter(ledger_store=store)
    ew = ExecutionEventWriter(store)
    for m in msgs:
        cw.write_record(_env(m["mid"], m["cid"], m.get("qty", 0)), _dr(m["mid"]))
    for e in evts:
        ew.write_event(_ev(e["mid"], e["cid"], e["type"], e.get("fq", 0)))
    cr = CommunicationRecordReader(str(tmp_path))
    er = ExecutionEventReader(str(tmp_path))
    recon = ExecutionReconciliationService(cr, er)
    return er, recon


class TestOutcomeCollector:
    def test_collect_clean_fill(self, tmp_path):
        er, recon = _build(tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}],
            [{"mid": "m1", "cid": "c1", "type": "ack"},
             {"mid": "m1", "cid": "c1", "type": "filled", "fq": 100}],
        )
        oc = OutcomeCollector(er, recon)
        out = oc.collect(date_key="2026-04-24", target="exec_bridge",
                         message_id="m1", correlation_id="c1",
                         intended_quantity=100, intended_side="long")
        assert out["fill_quality"]["grade"] == "clean_fill"
        assert out["fill_quality"]["fill_ratio"] == 1.0
        assert out["execution_outcome"] == "success"

    def test_collect_rejected(self, tmp_path):
        er, recon = _build(tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}],
            [{"mid": "m1", "cid": "c1", "type": "ack"},
             {"mid": "m1", "cid": "c1", "type": "rejected"}],
        )
        oc = OutcomeCollector(er, recon)
        out = oc.collect(date_key="2026-04-24", target="exec_bridge",
                         message_id="m1", correlation_id="c1", intended_quantity=100)
        assert out["fill_quality"]["grade"] == "rejected"
        assert out["execution_outcome"] == "breach"

    def test_collect_partial_cancel(self, tmp_path):
        er, recon = _build(tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}],
            [{"mid": "m1", "cid": "c1", "type": "partially_filled", "fq": 30},
             {"mid": "m1", "cid": "c1", "type": "cancelled"}],
        )
        oc = OutcomeCollector(er, recon)
        out = oc.collect(date_key="2026-04-24", target="exec_bridge",
                         message_id="m1", correlation_id="c1", intended_quantity=100)
        assert out["fill_quality"]["grade"] == "partial_cancel"
        assert out["execution_outcome"] == "cancelled"

    def test_collect_no_events(self, tmp_path):
        er, recon = _build(tmp_path, [{"mid": "m1", "cid": "c1"}], [])
        oc = OutcomeCollector(er, recon)
        out = oc.collect(date_key="2026-04-24", target="exec_bridge",
                         message_id="m1", correlation_id="c1")
        assert out["fill_quality"]["grade"] == "no_execution"
        assert out["execution_outcome"] == "no_execution"


class TestDecisionScorer:
    def test_score_clean_fill_profitable(self):
        scorer = DecisionScorer()
        outcome = {
            "fill_quality": {"grade": "clean_fill", "fill_ratio": 1.0},
            "timeline": {"event_count": 2, "event_types": ["ack", "filled"], "terminal_event_type": "filled"},
            "reconciliation": {"status": "matched"},
            "execution_outcome": "success",
            "intended_side": "long",
        }
        result = scorer.score(outcome, market_context={"realized_pnl": 200})
        assert result["composite_score"] > 0.7
        assert result["dimensions"]["fill_quality"]["score"] == 1.0
        assert result["dimensions"]["risk_compliance"]["score"] == 1.0

    def test_score_rejected(self):
        scorer = DecisionScorer()
        outcome = {
            "fill_quality": {"grade": "rejected", "fill_ratio": 0.0},
            "timeline": {"event_count": 2, "event_types": ["ack", "rejected"], "terminal_event_type": "rejected"},
            "reconciliation": {"status": "breached"},
            "execution_outcome": "rejected",
        }
        result = scorer.score(outcome)
        assert result["composite_score"] < 0.35
        assert result["dimensions"]["fill_quality"]["score"] == 0.0
        assert result["dimensions"]["risk_compliance"]["score"] == 0.0

    def test_score_partial_with_direction(self):
        scorer = DecisionScorer()
        outcome = {
            "fill_quality": {"grade": "partial_open", "fill_ratio": 0.3},
            "timeline": {"event_count": 2, "event_types": ["ack", "partially_filled"]},
            "reconciliation": {"status": "partial"},
            "execution_outcome": "partial",
            "intended_side": "long",
        }
        result = scorer.score(outcome, market_context={"price_move_pct": 0.5})
        assert result["dimensions"]["directional_accuracy"]["reason"] == "direction_correct"


class TestBrainPerformanceTracker:
    def test_insufficient_data(self):
        tracker = BrainPerformanceTracker()
        s = tracker.get_brain_summary("brain_001")
        assert s["health_signal"] == "insufficient_data"
        assert s["recommendation"] == "observe"

    def test_healthy_brain(self):
        tracker = BrainPerformanceTracker()
        for _ in range(20):
            tracker.record_outcome("brain_001", {
                "composite_score": 0.85,
                "execution_outcome": "success",
                "fill_grade": "clean_fill",
            })
        s = tracker.get_brain_summary("brain_001")
        assert s["health_signal"] == "healthy"
        assert s["recommendation"] == "eligible_for_promotion"

    def test_degraded_brain(self):
        tracker = BrainPerformanceTracker()
        for _ in range(15):
            tracker.record_outcome("brain_bad", {
                "composite_score": 0.2,
                "execution_outcome": "pending",
                "fill_grade": "pending",
            })
        s = tracker.get_brain_summary("brain_bad")
        assert s["health_signal"] == "degraded"
        assert s["recommendation"] == "demote_to_probation"

    def test_critical_brain(self):
        tracker = BrainPerformanceTracker()
        for _ in range(10):
            tracker.record_outcome("brain_crit", {
                "composite_score": 0.1,
                "execution_outcome": "breach",
                "fill_grade": "rejected",
            })
        s = tracker.get_brain_summary("brain_crit")
        assert s["health_signal"] == "critical"
        assert s["recommendation"] == "freeze"

    def test_window_eviction(self):
        tracker = BrainPerformanceTracker(window_size=5)
        for i in range(10):
            tracker.record_outcome("brain_win", {"composite_score": float(i) / 10})
        s = tracker.get_brain_summary("brain_win")
        assert s["sample_count"] == 5

    def test_get_all_summaries(self):
        tracker = BrainPerformanceTracker()
        tracker.record_outcome("a", {"composite_score": 0.5})
        tracker.record_outcome("b", {"composite_score": 0.8})
        summaries = tracker.get_all_summaries()
        assert len(summaries) == 2
        assert summaries[0]["brain_id"] == "a"


class TestFeedbackLoop:
    def test_full_feedback_loop(self, tmp_path):
        er, recon = _build(tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}],
            [{"mid": "m1", "cid": "c1", "type": "ack"},
             {"mid": "m1", "cid": "c1", "type": "filled", "fq": 100}],
        )
        loop = FeedbackLoop(
            outcome_collector=OutcomeCollector(er, recon),
            decision_scorer=DecisionScorer(),
            brain_performance_tracker=BrainPerformanceTracker(),
        )
        result = loop.process_decision_outcome(
            date_key="2026-04-24", target="exec_bridge",
            message_id="m1", correlation_id="c1",
            intended_quantity=100, intended_side="long",
            supporting_brain_ids=["alpha_v1", "regime_v2"],
            opposing_brain_ids=["risk_v1"],
            market_context={"realized_pnl": 150},
        )
        assert result["scored"]["composite_score"] > 0.5
        assert "alpha_v1" in result["brain_summaries"]
        assert "regime_v2" in result["brain_summaries"]
        assert "risk_v1" in result["brain_summaries"]
        assert result["outcome"]["execution_outcome"] == "success"

    def test_feedback_loop_governance_signals(self, tmp_path):
        er, recon = _build(tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}],
            [{"mid": "m1", "cid": "c1", "type": "rejected"}],
        )
        tracker = BrainPerformanceTracker()
        for _ in range(15):
            tracker.record_outcome("bad_brain", {
                "composite_score": 0.1,
                "execution_outcome": "breach",
            })

        loop = FeedbackLoop(
            outcome_collector=OutcomeCollector(er, recon),
            decision_scorer=DecisionScorer(),
            brain_performance_tracker=tracker,
        )
        result = loop.process_decision_outcome(
            date_key="2026-04-24", target="exec_bridge",
            message_id="m1", correlation_id="c1",
            intended_quantity=100,
            supporting_brain_ids=["bad_brain"],
        )
        signals = result["governance_signals"]
        assert len(signals) > 0
        assert signals[0]["brain_id"] == "bad_brain"
        assert signals[0]["signal_type"] == "governance_action_required"

    def test_opposing_brain_gets_inverted_score(self, tmp_path):
        er, recon = _build(tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}],
            [{"mid": "m1", "cid": "c1", "type": "filled", "fq": 100}],
        )
        tracker = BrainPerformanceTracker()
        loop = FeedbackLoop(
            outcome_collector=OutcomeCollector(er, recon),
            decision_scorer=DecisionScorer(),
            brain_performance_tracker=tracker,
        )
        loop.process_decision_outcome(
            date_key="2026-04-24", target="exec_bridge",
            message_id="m1", correlation_id="c1",
            intended_quantity=100, intended_side="long",
            supporting_brain_ids=["supporter"],
            opposing_brain_ids=["opposer"],
            market_context={"realized_pnl": 500},
        )
        sup = tracker.get_brain_summary("supporter")
        opp = tracker.get_brain_summary("opposer")
        assert sup["composite_mean"] > opp["composite_mean"]
