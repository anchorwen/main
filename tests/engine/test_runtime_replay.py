"""Runtime replay readiness tests."""

import json

from core.execution.paper_gateway import PaperExecutionGateway
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.ledger.stream_names import LEDGER_STREAM_RUNTIME_EVIDENCE, stream_jsonl_filename
from core.runtime.cycle_replay import RuntimeCycleReplay
from core.runtime.evidence_reader import RuntimeEvidenceReader
from core.runtime.evidence_writer import RuntimeEvidenceWriter
from core.runtime.execution_gates import (
    RuntimeExecutionApprovalChain,
    RuntimeGovernanceGate,
    RuntimeRiskGate,
)
from core.runtime.execution_gateway_router import ExecutionGatewayRouter
from core.runtime.execution_pipeline import RuntimeExecutionPipeline
from core.runtime.integration_contracts import OrderSizingPolicy
from core.runtime.schema_versions import SCHEMA_RUNTIME_EVIDENCE_RECORD
from core.runtime.signal_order_builder import SignalOrderRequestBuilder
from core.strategies.examples import ThresholdAlphaAgent
from core.strategies.registry import StrategyPluginRegistry, StrategyPluginRunner


def _pipeline(writer=None, chain=None):
    registry = StrategyPluginRegistry()
    agent = ThresholdAlphaAgent("alpha1", "ema_bias", 1.0, -1.0)
    registry.register(agent)
    runner = StrategyPluginRunner(registry)
    runner.warmup_all({})
    router = ExecutionGatewayRouter()
    router.register("PAPER", PaperExecutionGateway())
    return RuntimeExecutionPipeline(
        strategy_runner=runner,
        order_builder=SignalOrderRequestBuilder(
            OrderSizingPolicy(base_quantity=10), default_venue="PAPER"
        ),
        gateway_router=router,
        approval_chain=chain,
        evidence_writer=writer,
    )


class TestRuntimeEvidenceReader:
    def test_reads_cycle_and_lists_ids(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        writer = RuntimeEvidenceWriter(store)
        pipeline = _pipeline(writer)
        pipeline.run({"ema_bias": 2.0}, {"price": 2000.0}, {"runtime_cycle_id": "cycle_a"})
        reader = RuntimeEvidenceReader(str(tmp_path))
        records = reader.read_cycle("cycle_a")
        assert len(records) == 1
        assert reader.latest_cycle("cycle_a")["runtime_cycle_id"] == "cycle_a"
        assert reader.list_cycle_ids() == ["cycle_a"]

    def test_missing_cycle_returns_empty(self, tmp_path):
        reader = RuntimeEvidenceReader(str(tmp_path))
        assert reader.read_cycle("missing") == []
        assert reader.latest_cycle("missing") is None


class TestRuntimeCycleReplay:
    def test_replay_successful_cycle(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        writer = RuntimeEvidenceWriter(store)
        chain = RuntimeExecutionApprovalChain(
            [
                RuntimeRiskGate(max_quantity=100, allowed_symbols={"XAUUSD"}, max_notional=50_000),
                RuntimeGovernanceGate(allowed_strategy_ids={"alpha1"}, allowed_venues={"PAPER"}),
            ]
        )
        pipeline = _pipeline(writer, chain)
        pipeline.run({"ema_bias": 2.0}, {"price": 2000.0}, {"runtime_cycle_id": "cycle_ok"})
        report = RuntimeCycleReplay(RuntimeEvidenceReader(str(tmp_path))).replay("cycle_ok")
        assert report.evidence_found is True
        assert report.replayable is True
        assert report.counts_match is True
        assert report.approvals_present is True
        assert report.quality_present is True
        assert report.signal_count == 1
        assert report.order_count == 1
        assert report.approval_count == 2
        assert report.summary["filled_order_count"] == 1
        assert report.to_dict()["runtime_cycle_id"] == "cycle_ok"

    def test_replay_denied_cycle_is_replayable(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        writer = RuntimeEvidenceWriter(store)
        pipeline = _pipeline(
            writer, RuntimeExecutionApprovalChain([RuntimeRiskGate(max_quantity=1)])
        )
        pipeline.run({"ema_bias": 2.0}, {"price": 2000.0}, {"runtime_cycle_id": "cycle_denied"})
        report = RuntimeCycleReplay(RuntimeEvidenceReader(str(tmp_path))).replay("cycle_denied")
        assert report.replayable is True
        assert report.order_count == 0
        assert report.skipped_count == 1
        assert report.summary["denied_count"] == 1

    def test_replay_missing_cycle(self, tmp_path):
        report = RuntimeCycleReplay(RuntimeEvidenceReader(str(tmp_path))).replay("missing")
        assert report.evidence_found is False
        assert report.replayable is False
        assert "evidence_not_found" in report.issues

    def test_replay_detects_count_mismatch(self, tmp_path):
        date_dir = tmp_path / "2026-01-01"
        date_dir.mkdir()
        path = date_dir / stream_jsonl_filename("cycle_bad", LEDGER_STREAM_RUNTIME_EVIDENCE)
        payload = {
            "schema_version": SCHEMA_RUNTIME_EVIDENCE_RECORD,
            "evidence_id": "e1",
            "runtime_cycle_id": "cycle_bad",
            "generated_at": "2026-01-01T00:00:00",
            "signal_count": 9,
            "order_count": 0,
            "approval_count": 0,
            "skipped_count": 0,
            "quality_summary": {},
            "payload": {
                "runtime_cycle_id": "cycle_bad",
                "signals": [],
                "orders": [],
                "approvals": [],
                "skipped_signals": [],
                "quality_report": {"order_count": 0},
            },
        }
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        report = RuntimeCycleReplay(RuntimeEvidenceReader(str(tmp_path))).replay("cycle_bad")
        assert report.replayable is False
        assert report.counts_match is False
        assert "signal_count_mismatch(9!=0)" in report.issues

    def test_replay_detects_missing_quality(self, tmp_path):
        date_dir = tmp_path / "2026-01-01"
        date_dir.mkdir()
        path = date_dir / stream_jsonl_filename("cycle_no_quality", LEDGER_STREAM_RUNTIME_EVIDENCE)
        payload = {
            "schema_version": SCHEMA_RUNTIME_EVIDENCE_RECORD,
            "evidence_id": "e1",
            "runtime_cycle_id": "cycle_no_quality",
            "generated_at": "2026-01-01T00:00:00",
            "signal_count": 0,
            "order_count": 0,
            "approval_count": 0,
            "skipped_count": 0,
            "quality_summary": {},
            "payload": {"runtime_cycle_id": "cycle_no_quality", "signals": [], "orders": []},
        }
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        report = RuntimeCycleReplay(RuntimeEvidenceReader(str(tmp_path))).replay("cycle_no_quality")
        assert report.replayable is False
        assert "quality_report_missing" in report.issues
