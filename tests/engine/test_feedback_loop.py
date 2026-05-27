from datetime import datetime

from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.domain.execution_event import ExecutionEvent
from core.contracts.enums import CommunicationMessageType, CommunicationPriority, DispatchStatus
from core.contracts.ids import new_execution_event_id
from core.feedback.brain_performance_tracker import BrainPerformanceTracker
from core.feedback.decision_scorer import DecisionScorer
from core.feedback.feedback_loop import FeedbackLoop
from core.feedback.outcome_collector import OutcomeCollector
from core.ledger.services.communication_record_reader import CommunicationRecordReader
from core.ledger.services.communication_record_writer import CommunicationRecordWriter
from core.ledger.services.execution_event_reader import ExecutionEventReader
from core.ledger.services.execution_event_writer import ExecutionEventWriter
from core.ledger.services.execution_reconciliation_service import ExecutionReconciliationService
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore


def _env(mid, cid, qty=0):
    return CommunicationEnvelope(
        schema_version="v1",
        message_id=mid,
        correlation_id=cid,
        causation_id=None,
        event_time=datetime(2026, 4, 24, 12, 0, 0),
        producer="t",
        target="exec_bridge",
        message_type=CommunicationMessageType.EXECUTION_DISPATCH,
        priority=CommunicationPriority.NORMAL,
        payload={"quantity": qty} if qty else {},
    )


def _dr(mid):
    return DispatchResult(
        schema_version="v1",
        dispatch_id=f"d_{mid}",
        message_id=mid,
        status=DispatchStatus.TRANSPORT_DELIVERED,
        recorded_at=datetime(2026, 4, 24, 12, 0, 1),
        target="exec_bridge",
        adapter_name="stub",
        attempts=[{"adapter_name": "stub", "status": "succeeded", "reason": None}],
    )


def _ev(mid, cid, etype, fq=0):
    return ExecutionEvent(
        schema_version="v1",
        event_id=new_execution_event_id(),
        message_id=mid,
        correlation_id=cid,
        event_type=etype,
        event_time=datetime(2026, 4, 24, 12, 0, 5),
        recorded_at=datetime(2026, 4, 24, 12, 0, 5),
        venue="test",
        quantity={"filled": fq} if fq else {},
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
        er, recon = _build(
            tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}],
            [
                {"mid": "m1", "cid": "c1", "type": "ack"},
                {"mid": "m1", "cid": "c1", "type": "filled", "fq": 100},
            ],
        )
        oc = OutcomeCollector(er, recon)
        out = oc.collect(
            date_key="2026-04-24",
            target="exec_bridge",
            message_id="m1",
            correlation_id="c1",
            intended_quantity=100,
            intended_side="long",
        )
        assert out["fill_quality"]["grade"] == "clean_fill"
        assert out["fill_quality"]["fill_ratio"] == 1.0
        assert out["execution_outcome"] == "success"

    def test_collect_rejected(self, tmp_path):
        er, recon = _build(
            tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}],
            [
                {"mid": "m1", "cid": "c1", "type": "ack"},
                {"mid": "m1", "cid": "c1", "type": "rejected"},
            ],
        )
        oc = OutcomeCollector(er, recon)
        out = oc.collect(
            date_key="2026-04-24",
            target="exec_bridge",
            message_id="m1",
            correlation_id="c1",
            intended_quantity=100,
        )
        assert out["fill_quality"]["grade"] == "rejected"
        assert out["execution_outcome"] == "breach"

    def test_collect_partial_cancel(self, tmp_path):
        er, recon = _build(
            tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}],
            [
                {"mid": "m1", "cid": "c1", "type": "partially_filled", "fq": 30},
                {"mid": "m1", "cid": "c1", "type": "cancelled"},
            ],
        )
        oc = OutcomeCollector(er, recon)
        out = oc.collect(
            date_key="2026-04-24",
            target="exec_bridge",
            message_id="m1",
            correlation_id="c1",
            intended_quantity=100,
        )
        assert out["fill_quality"]["grade"] == "partial_cancel"
        assert out["execution_outcome"] == "cancelled"

    def test_collect_no_events(self, tmp_path):
        er, recon = _build(tmp_path, [{"mid": "m1", "cid": "c1"}], [])
        oc = OutcomeCollector(er, recon)
        out = oc.collect(
            date_key="2026-04-24", target="exec_bridge", message_id="m1", correlation_id="c1"
        )
        assert out["fill_quality"]["grade"] == "no_execution"
        assert out["execution_outcome"] == "no_execution"


class TestDecisionScorer:
    def test_score_clean_fill_profitable(self):
        scorer = DecisionScorer()
        outcome = {
            "fill_quality": {"grade": "clean_fill", "fill_ratio": 1.0},
            "timeline": {
                "event_count": 2,
                "event_types": ["ack", "filled"],
                "terminal_event_type": "filled",
            },
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
            "timeline": {
                "event_count": 2,
                "event_types": ["ack", "rejected"],
                "terminal_event_type": "rejected",
            },
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
            tracker.record_outcome(
                "brain_001",
                {
                    "composite_score": 0.85,
                    "execution_outcome": "success",
                    "fill_grade": "clean_fill",
                },
            )
        s = tracker.get_brain_summary("brain_001")
        assert s["health_signal"] == "healthy"
        assert s["recommendation"] == "eligible_for_promotion"

    def test_degraded_brain(self):
        tracker = BrainPerformanceTracker()
        for _ in range(15):
            tracker.record_outcome(
                "brain_bad",
                {
                    "composite_score": 0.2,
                    "execution_outcome": "pending",
                    "fill_grade": "pending",
                },
            )
        s = tracker.get_brain_summary("brain_bad")
        assert s["health_signal"] == "degraded"
        assert s["recommendation"] == "demote_to_probation"

    def test_critical_brain(self):
        tracker = BrainPerformanceTracker()
        for _ in range(10):
            tracker.record_outcome(
                "brain_crit",
                {
                    "composite_score": 0.1,
                    "execution_outcome": "breach",
                    "fill_grade": "rejected",
                },
            )
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
        er, recon = _build(
            tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}],
            [
                {"mid": "m1", "cid": "c1", "type": "ack"},
                {"mid": "m1", "cid": "c1", "type": "filled", "fq": 100},
            ],
        )
        loop = FeedbackLoop(
            outcome_collector=OutcomeCollector(er, recon),
            decision_scorer=DecisionScorer(),
            brain_performance_tracker=BrainPerformanceTracker(),
        )
        result = loop.process_decision_outcome(
            date_key="2026-04-24",
            target="exec_bridge",
            message_id="m1",
            correlation_id="c1",
            intended_quantity=100,
            intended_side="long",
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
        er, recon = _build(
            tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}],
            [{"mid": "m1", "cid": "c1", "type": "rejected"}],
        )
        tracker = BrainPerformanceTracker()
        for _ in range(15):
            tracker.record_outcome(
                "bad_brain",
                {
                    "composite_score": 0.1,
                    "execution_outcome": "breach",
                },
            )

        loop = FeedbackLoop(
            outcome_collector=OutcomeCollector(er, recon),
            decision_scorer=DecisionScorer(),
            brain_performance_tracker=tracker,
        )
        result = loop.process_decision_outcome(
            date_key="2026-04-24",
            target="exec_bridge",
            message_id="m1",
            correlation_id="c1",
            intended_quantity=100,
            supporting_brain_ids=["bad_brain"],
        )
        signals = result["governance_signals"]
        assert len(signals) > 0
        assert signals[0]["brain_id"] == "bad_brain"
        assert signals[0]["signal_type"] == "governance_action_required"

    def test_opposing_brain_gets_inverted_score(self, tmp_path):
        er, recon = _build(
            tmp_path,
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
            date_key="2026-04-24",
            target="exec_bridge",
            message_id="m1",
            correlation_id="c1",
            intended_quantity=100,
            intended_side="long",
            supporting_brain_ids=["supporter"],
            opposing_brain_ids=["opposer"],
            market_context={"realized_pnl": 500},
        )
        sup = tracker.get_brain_summary("supporter")
        opp = tracker.get_brain_summary("opposer")
        assert sup["composite_mean"] > opp["composite_mean"]


# ── scripts/feedback_loop: journal → tracker bridge ──

import json
from pathlib import Path

from scripts.feedback_loop import (
    _build_label_index,
    _outcome_from_label,
    _read_decision_records,
    _read_journal,
    ingest_journal_to_tracker,
)


class TestOutcomeFromLabel:
    def test_win(self):
        result = _outcome_from_label({"label": "win", "pnl": 5.0}, "accepted")
        assert result["execution_outcome"] == "win"
        assert 0.75 < result["composite_score"] <= 0.95

    def test_loss(self):
        result = _outcome_from_label({"label": "loss", "pnl": -3.0}, "accepted")
        assert result["execution_outcome"] == "loss"
        assert 0.10 <= result["composite_score"] < 0.35

    def test_breakeven(self):
        result = _outcome_from_label({"label": "breakeven", "pnl": 0.0}, "accepted")
        assert result["execution_outcome"] == "breakeven"
        assert result["composite_score"] == 0.50

    def test_fallback_accepted(self):
        result = _outcome_from_label(None, "accepted")
        assert result["execution_outcome"] == "filled"
        assert result["composite_score"] == 0.55

    def test_fallback_rejected(self):
        result = _outcome_from_label(None, "rejected")
        assert result["execution_outcome"] == "rejected"
        assert result["composite_score"] == 0.15

    def test_fallback_unknown(self):
        result = _outcome_from_label(None, "timeout")
        assert result["execution_outcome"] == "timeout"
        assert result["composite_score"] == 0.30


class TestBuildLabelIndex:
    def test_maps_by_ticket(self):
        labels = [
            {"position_ticket": 101, "label": "win", "pnl": 2.0},
            {"position_ticket": 102, "label": "loss", "pnl": -1.0},
        ]
        idx = _build_label_index(labels)
        assert idx[101]["label"] == "win"
        assert idx[102]["label"] == "loss"
        assert 999 not in idx

    def test_empty(self):
        assert _build_label_index([]) == {}


class TestReadJournal:
    def test_parses_valid_jsonl(self, tmp_path: Path):
        journal = tmp_path / "journal.jsonl"
        journal.write_text(
            json.dumps(
                {
                    "recorded_at": "2026-05-04T10:00:00Z",
                    "ack_status": "accepted",
                    "position_ticket": 1,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "recorded_at": "2026-05-04T11:00:00Z",
                    "ack_status": "rejected",
                    "position_ticket": 2,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        entries = _read_journal(journal)
        assert len(entries) == 2

    def test_date_filter(self, tmp_path: Path):
        journal = tmp_path / "journal.jsonl"
        journal.write_text(
            json.dumps({"recorded_at": "2026-05-04T10:00:00Z", "ack_status": "accepted"})
            + "\n"
            + json.dumps({"recorded_at": "2026-05-03T10:00:00Z", "ack_status": "accepted"})
            + "\n",
            encoding="utf-8",
        )
        entries = _read_journal(journal, date_filter="2026-05-04")
        assert len(entries) == 1

    def test_skips_empty_lines(self, tmp_path: Path):
        journal = tmp_path / "journal.jsonl"
        journal.write_text(
            "\n"
            + json.dumps({"recorded_at": "2026-05-04T10:00:00Z", "ack_status": "accepted"})
            + "\n\n",
            encoding="utf-8",
        )
        entries = _read_journal(journal)
        assert len(entries) == 1

    def test_skips_invalid_json(self, tmp_path: Path):
        journal = tmp_path / "journal.jsonl"
        journal.write_text(
            "not valid json\n"
            + json.dumps({"recorded_at": "2026-05-04T10:00:00Z", "ack_status": "accepted"})
            + "\n",
            encoding="utf-8",
        )
        entries = _read_journal(journal)
        assert len(entries) == 1

    def test_file_not_found(self, tmp_path: Path):
        entries = _read_journal(tmp_path / "nonexistent.jsonl")
        assert entries == []


class TestReadDecisionRecords:
    def test_parses_decisions(self, tmp_path: Path):
        date = "2026-05-04"
        decisions_dir = tmp_path / "decisions" / date
        decisions_dir.mkdir(parents=True)
        record_path = decisions_dir / "XAUUSDc.decisions.jsonl"
        record_path.write_text(
            json.dumps(
                {
                    "labels": {"decision_side": "LONG"},
                    "attribution": {"supporting_brains": ["V9", "XGB"], "opposing_brains": ["OU"]},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        records = _read_decision_records(tmp_path / "decisions", date_filter=date)
        assert len(records) == 1
        assert "V9" in records[0]["attribution"]["supporting_brains"]

    def test_no_file(self, tmp_path: Path):
        records = _read_decision_records(tmp_path / "decisions", date_filter="2026-05-04")
        assert records == []


class TestIngestJournalToTracker:
    def test_single_brain_with_labels(self, tmp_path: Path):
        from core.feedback.brain_performance_tracker import BrainPerformanceTracker

        base = tmp_path / "data"
        base.mkdir()

        journal = base / "live_trade_journal.jsonl"
        journal.write_text(
            json.dumps(
                {
                    "recorded_at": "2026-05-04T10:00:00Z",
                    "ack_status": "accepted",
                    "position_ticket": 1,
                    "symbol": "XAUUSD",
                    "side": "BUY",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "recorded_at": "2026-05-04T10:01:00Z",
                    "ack_status": "rejected",
                    "position_ticket": 2,
                    "symbol": "XAUUSD",
                    "side": "SELL",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        reports = base / "reports"
        reports.mkdir(parents=True)
        labels = reports / "live_labels.jsonl"
        labels.write_text(
            json.dumps({"position_ticket": 1, "label": "win", "pnl": 3.0}) + "\n",
            encoding="utf-8",
        )

        tracker = BrainPerformanceTracker(window_size=100)
        report = ingest_journal_to_tracker(
            tracker, base_dir=str(base), brain_id="V9", date_filter="2026-05-04"
        )

        assert report["mode"] == "single_brain"
        assert report["journal_entries"] == 2
        assert report["accepted_trades"] == 1
        assert report["updates_applied"] == 2  # 1 accepted + 1 rejected
        assert "V9" in report["brain_ids_updated"]

        summary = tracker.get_brain_summary("V9")
        assert summary["sample_count"] == 2

    def test_single_brain_no_labels(self, tmp_path: Path):
        from core.feedback.brain_performance_tracker import BrainPerformanceTracker

        base = tmp_path / "data"
        base.mkdir()

        journal = base / "live_trade_journal.jsonl"
        journal.write_text(
            json.dumps(
                {
                    "recorded_at": "2026-05-04T10:00:00Z",
                    "ack_status": "accepted",
                    "position_ticket": 1,
                    "symbol": "XAUUSD",
                    "side": "BUY",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        tracker = BrainPerformanceTracker(window_size=100)
        report = ingest_journal_to_tracker(
            tracker, base_dir=str(base), brain_id="V9", date_filter="2026-05-04"
        )

        assert report["updates_applied"] == 1
        summary = tracker.get_brain_summary("V9")
        assert summary["sample_count"] == 1
        assert summary["composite_mean"] == 0.55  # fallback to ack_status

    def test_dry_run(self, tmp_path: Path):
        from core.feedback.brain_performance_tracker import BrainPerformanceTracker

        base = tmp_path / "data"
        base.mkdir()

        journal = base / "live_trade_journal.jsonl"
        journal.write_text(
            json.dumps(
                {
                    "recorded_at": "2026-05-04T10:00:00Z",
                    "ack_status": "accepted",
                    "position_ticket": 1,
                    "symbol": "XAUUSD",
                    "side": "BUY",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        tracker = BrainPerformanceTracker(window_size=100)
        report = ingest_journal_to_tracker(
            tracker, base_dir=str(base), brain_id="V9", date_filter="2026-05-04", dry_run=True
        )

        assert report["updates_applied"] == 0
        assert report["updates_would_apply"] == 1
        assert tracker.get_brain_summary("V9")["sample_count"] == 0

    def test_no_journal(self, tmp_path: Path):
        from core.feedback.brain_performance_tracker import BrainPerformanceTracker

        base = tmp_path / "data"
        base.mkdir()

        tracker = BrainPerformanceTracker(window_size=100)
        report = ingest_journal_to_tracker(
            tracker, base_dir=str(base), brain_id="V9", date_filter="2026-05-04"
        )

        assert report["journal_entries"] == 0
        assert report["updates_applied"] == 0

    def test_multi_brain_with_decisions(self, tmp_path: Path):
        from core.feedback.brain_performance_tracker import BrainPerformanceTracker

        base = tmp_path / "data"
        base.mkdir()

        # New feedback_loop resolves brain attribution from the open journal
        # entry's brain_ids field (per-strategy dispatch), NOT from decision
        # records.  The open entry must include brain_ids.
        journal = base / "live_trade_journal.jsonl"
        journal.write_text(
            json.dumps(
                {
                    "recorded_at": "2026-05-04T09:00:00Z",
                    "action": "open",
                    "ack_status": "accepted",
                    "position_ticket": 1,
                    "symbol": "XAUUSDc",
                    "side": "long",
                    "volume": 0.01,
                    "brain_ids": ["V9", "XGB"],
                    "detail": {"request": {"price": 2600.0}},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "recorded_at": "2026-05-04T10:00:00Z",
                    "action": "close",
                    "position_ticket": 1,
                    "symbol": "XAUUSDc",
                    "side": "short",
                    "detail": {"close_price": 2610.0},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        tracker = BrainPerformanceTracker(window_size=100)
        report = ingest_journal_to_tracker(tracker, base_dir=str(base), date_filter="2026-05-04")

        assert report["mode"] == "multi_brain"
        # Only brains in the open entry's brain_ids list get outcomes.
        # OU is not in brain_ids → no outcome recorded.
        assert report["updates_applied"] == 2
        assert set(report["brain_ids_updated"]) == {"V9", "XGB"}
        assert tracker.get_brain_summary("V9")["sample_count"] == 1
        assert tracker.get_brain_summary("XGB")["sample_count"] == 1
        # OU was not dispatched for this trade → no tracker entry
        assert tracker.get_brain_summary("OU")["sample_count"] == 0

    def test_multi_brain_no_decision_match(self, tmp_path: Path):
        from core.feedback.brain_performance_tracker import BrainPerformanceTracker

        base = tmp_path / "data"
        base.mkdir()

        journal = base / "live_trade_journal.jsonl"
        journal.write_text(
            json.dumps(
                {
                    "recorded_at": "2026-05-04T10:00:00Z",
                    "ack_status": "accepted",
                    "position_ticket": 1,
                    "symbol": "XAUUSD",
                    "side": "BUY",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        dec_dir = base / "decisions" / "2026-05-04"
        dec_dir.mkdir(parents=True)
        decisions = dec_dir / "XAUUSDc.decisions.jsonl"
        decisions.write_text(
            json.dumps(
                {
                    "labels": {"decision_side": "SELL"},
                    "attribution": {"supporting_brains": ["V9"]},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        tracker = BrainPerformanceTracker(window_size=100)
        report = ingest_journal_to_tracker(tracker, base_dir=str(base), date_filter="2026-05-04")

        assert report["mode"] == "multi_brain"
        assert report["updates_applied"] == 0


class TestFeedbackLoopCLI:
    def test_single_brain_dry_run(self, tmp_path: Path, monkeypatch):
        import io
        import sys

        from scripts.feedback_loop import main

        base = tmp_path / "data"
        base.mkdir()

        journal = base / "live_trade_journal.jsonl"
        journal.write_text(
            json.dumps(
                {
                    "recorded_at": "2026-05-04T10:00:00Z",
                    "ack_status": "accepted",
                    "position_ticket": 1,
                    "symbol": "XAUUSD",
                    "side": "BUY",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            exit_code = main(
                [
                    "--brain-id",
                    "V9",
                    "--base-dir",
                    str(base),
                    "--date",
                    "2026-05-04",
                    "--dry-run",
                ]
            )
        finally:
            sys.stdout = old_stdout

        assert exit_code == 0

    def test_output_file(self, tmp_path: Path, monkeypatch):
        import io
        import sys

        from scripts.feedback_loop import main

        base = tmp_path / "data"
        base.mkdir()

        journal = base / "live_trade_journal.jsonl"
        journal.write_text(
            json.dumps(
                {
                    "recorded_at": "2026-05-04T10:00:00Z",
                    "ack_status": "accepted",
                    "position_ticket": 1,
                    "symbol": "XAUUSD",
                    "side": "BUY",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        out = tmp_path / "feedback_report.json"
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            exit_code = main(
                [
                    "--brain-id",
                    "V9",
                    "--base-dir",
                    str(base),
                    "--date",
                    "2026-05-04",
                    "--dry-run",
                    "--output",
                    str(out),
                ]
            )
        finally:
            sys.stdout = old_stdout

        assert exit_code == 0
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["mode"] == "single_brain"
        assert data["brain_id"] == "V9"
