"""Runtime evidence integration tests."""

import json

from core.execution.paper_gateway import PaperExecutionGateway
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.runtime.evidence_contracts import RuntimeEvidenceRecord
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


def _pipeline(evidence_writer=None, approval_chain=None):
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
        approval_chain=approval_chain,
        evidence_writer=evidence_writer,
    )


class TestRuntimeEvidenceRecord:
    def test_from_pipeline_result(self):
        pipeline = _pipeline()
        result = pipeline.run(
            {"ema_bias": 2.0}, {"price": 2000.0}, {"runtime_cycle_id": "cycle_test"}
        )
        record = RuntimeEvidenceRecord.from_pipeline_result(
            evidence_id="evidence1",
            runtime_cycle_id="cycle_test",
            result=result,
        )
        assert record.schema_version == SCHEMA_RUNTIME_EVIDENCE_RECORD
        assert record.runtime_cycle_id == "cycle_test"
        assert record.signal_count == 1
        assert record.order_count == 1
        assert record.quality_summary["order_count"] == 1
        assert record.to_dict()["payload"]["runtime_cycle_id"] == "cycle_test"


class TestRuntimeEvidenceWriter:
    def test_write_result_appends_ledger_record(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        writer = RuntimeEvidenceWriter(store)
        pipeline = _pipeline()
        result = pipeline.run({"ema_bias": 2.0}, {"price": 2000.0}, {"runtime_cycle_id": "cycle_a"})
        record, path = writer.write_result(runtime_cycle_id="cycle_a", result=result)
        assert record.runtime_cycle_id == "cycle_a"
        assert path.exists()  # type: ignore[reportAttributeAccessIssue]
        payload = json.loads(path.read_text(encoding="utf-8").strip())  # type: ignore[reportAttributeAccessIssue]
        assert payload["schema_version"] == SCHEMA_RUNTIME_EVIDENCE_RECORD
        assert payload["payload"]["quality_report"]["order_count"] == 1

    def test_pipeline_writes_evidence_when_writer_configured(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        writer = RuntimeEvidenceWriter(store)
        chain = RuntimeExecutionApprovalChain(
            [
                RuntimeRiskGate(max_quantity=100, allowed_symbols={"XAUUSD"}, max_notional=50_000),
                RuntimeGovernanceGate(allowed_strategy_ids={"alpha1"}, allowed_venues={"PAPER"}),
            ]
        )
        pipeline = _pipeline(evidence_writer=writer, approval_chain=chain)
        result = pipeline.run({"ema_bias": 2.0}, {"price": 2000.0}, {"runtime_cycle_id": "cycle_b"})
        files = list(tmp_path.rglob("*.jsonl"))
        assert result.runtime_cycle_id == "cycle_b"
        assert len(files) == 1
        payload = json.loads(files[0].read_text(encoding="utf-8").strip())
        assert payload["runtime_cycle_id"] == "cycle_b"
        assert payload["approval_count"] == 2
        assert payload["order_count"] == 1
        assert payload["payload"]["approvals"][0]["approved"] is True

    def test_pipeline_writes_denied_cycle_evidence(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        writer = RuntimeEvidenceWriter(store)
        chain = RuntimeExecutionApprovalChain([RuntimeRiskGate(max_quantity=1)])
        pipeline = _pipeline(evidence_writer=writer, approval_chain=chain)
        result = pipeline.run({"ema_bias": 2.0}, {"price": 2000.0}, {"runtime_cycle_id": "cycle_c"})
        files = list(tmp_path.rglob("*.jsonl"))
        payload = json.loads(files[0].read_text(encoding="utf-8").strip())
        assert result.order_count if hasattr(result, "order_count") else len(result.orders) == 0  # type: ignore[reportAttributeAccessIssue]
        assert payload["runtime_cycle_id"] == "cycle_c"
        assert payload["order_count"] == 0
        assert payload["approval_count"] == 1
        assert payload["skipped_count"] == 1
        assert payload["payload"]["skipped_signals"][0]["reason"] == "execution_denied"
