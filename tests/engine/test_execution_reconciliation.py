from datetime import datetime

from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.domain.execution_event import ExecutionEvent
from core.contracts.enums import CommunicationMessageType, CommunicationPriority, DispatchStatus
from core.contracts.ids import new_execution_event_id
from core.ledger.schema_versions import SCHEMA_EXECUTION_EVENT
from core.ledger.services.communication_inspection_service import CommunicationInspectionService
from core.ledger.services.communication_record_reader import CommunicationRecordReader
from core.ledger.services.communication_record_writer import CommunicationRecordWriter
from core.ledger.services.execution_event_reader import ExecutionEventReader
from core.ledger.services.execution_event_writer import ExecutionEventWriter
from core.ledger.services.execution_reconciliation_service import ExecutionReconciliationService
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.protocol.schema_versions import SCHEMA_COMMUNICATION_ENVELOPE, SCHEMA_DISPATCH_RESULT


def _envelope(message_id, correlation_id, quantity=0):
    payload = {"quantity": quantity} if quantity else {}
    return CommunicationEnvelope(
        schema_version=SCHEMA_COMMUNICATION_ENVELOPE,
        message_id=message_id,
        correlation_id=correlation_id,
        causation_id=None,
        event_time=datetime(2026, 4, 24, 12, 0, 0),
        producer="test",
        target="exec_bridge",
        message_type=CommunicationMessageType.EXECUTION_DISPATCH,
        priority=CommunicationPriority.NORMAL,
        payload=payload,
    )


def _result(message_id):
    return DispatchResult(
        schema_version=SCHEMA_DISPATCH_RESULT,
        dispatch_id=f"d_{message_id}",
        message_id=message_id,
        status=DispatchStatus.TRANSPORT_DELIVERED,
        recorded_at=datetime(2026, 4, 24, 12, 0, 1),
        target="exec_bridge",
        adapter_name="stub_adapter",
        attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
    )


def _event(message_id, correlation_id, event_type, filled_qty=0):
    qty = {"filled": filled_qty} if filled_qty else {}
    return ExecutionEvent(
        schema_version=SCHEMA_EXECUTION_EVENT,
        event_id=new_execution_event_id(),
        message_id=message_id,
        correlation_id=correlation_id,
        event_type=event_type,
        event_time=datetime(2026, 4, 24, 12, 0, 5),
        recorded_at=datetime(2026, 4, 24, 12, 0, 5),
        venue="test_venue",
        quantity=qty,
    )


class TestReconciliationService:
    def _build(self, tmp_path, messages, events):
        store = JsonlLedgerStore(str(tmp_path))
        cw = CommunicationRecordWriter(ledger_store=store)
        ew = ExecutionEventWriter(store)
        for m in messages:
            cw.write_record(_envelope(m["mid"], m["cid"], m.get("qty", 0)), _result(m["mid"]))
        for e in events:
            ew.write_event(_event(e["mid"], e["cid"], e["type"], e.get("fq", 0)))
        return ExecutionReconciliationService(
            CommunicationRecordReader(str(tmp_path)),
            ExecutionEventReader(str(tmp_path)),
        )

    def test_matched_fill(self, tmp_path):
        svc = self._build(
            tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}],
            [
                {"mid": "m1", "cid": "c1", "type": "ack"},
                {"mid": "m1", "cid": "c1", "type": "filled", "fq": 100},
            ],
        )
        r = svc.reconcile_message(
            date_key="2026-04-24", target="exec_bridge", message_id="m1", correlation_id="c1"
        )
        assert r["status"] == "matched"
        assert r["filled_quantity"] == 100
        assert r["mismatches"] == []

    def test_unmatched_no_events(self, tmp_path):
        svc = self._build(tmp_path, [{"mid": "m1", "cid": "c1"}], [])
        r = svc.reconcile_message(
            date_key="2026-04-24", target="exec_bridge", message_id="m1", correlation_id="c1"
        )
        assert r["status"] == "unmatched"

    def test_breached_rejected(self, tmp_path):
        svc = self._build(
            tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}],
            [
                {"mid": "m1", "cid": "c1", "type": "ack"},
                {"mid": "m1", "cid": "c1", "type": "rejected"},
            ],
        )
        r = svc.reconcile_message(
            date_key="2026-04-24", target="exec_bridge", message_id="m1", correlation_id="c1"
        )
        assert r["status"] == "breached"
        assert any(m["type"] == "state_mismatch" for m in r["mismatches"])

    def test_partial_cancelled_with_fills(self, tmp_path):
        svc = self._build(
            tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}],
            [
                {"mid": "m1", "cid": "c1", "type": "partially_filled", "fq": 30},
                {"mid": "m1", "cid": "c1", "type": "cancelled"},
            ],
        )
        r = svc.reconcile_message(
            date_key="2026-04-24", target="exec_bridge", message_id="m1", correlation_id="c1"
        )
        assert r["status"] == "partial"
        assert r["filled_quantity"] == 30

    def test_breached_quantity_mismatch(self, tmp_path):
        svc = self._build(
            tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}],
            [{"mid": "m1", "cid": "c1", "type": "filled", "fq": 80}],
        )
        r = svc.reconcile_message(
            date_key="2026-04-24", target="exec_bridge", message_id="m1", correlation_id="c1"
        )
        assert r["status"] == "breached"
        assert any(m["type"] == "quantity_mismatch" for m in r["mismatches"])

    def test_stale_non_terminal(self, tmp_path):
        svc = self._build(
            tmp_path,
            [{"mid": "m1", "cid": "c1"}],
            [
                {"mid": "m1", "cid": "c1", "type": "ack"},
                {"mid": "m1", "cid": "c1", "type": "accepted"},
            ],
        )
        r = svc.reconcile_message(
            date_key="2026-04-24", target="exec_bridge", message_id="m1", correlation_id="c1"
        )
        assert r["status"] == "stale"

    def test_correlation_reconciliation(self, tmp_path):
        svc = self._build(
            tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}, {"mid": "m2", "cid": "c1", "qty": 50}],
            [
                {"mid": "m1", "cid": "c1", "type": "filled", "fq": 100},
                {"mid": "m2", "cid": "c1", "type": "rejected"},
            ],
        )
        r = svc.reconcile_correlation(
            date_key="2026-04-24",
            target="exec_bridge",
            correlation_id="c1",
            message_ids=["m1", "m2"],
        )
        assert r["status"] == "breached"
        assert r["breached_message_ids"] == ["m2"]
        assert r["total_filled_quantity"] == 100

    def test_correlation_all_matched(self, tmp_path):
        svc = self._build(
            tmp_path,
            [{"mid": "m1", "cid": "c1", "qty": 100}, {"mid": "m2", "cid": "c1", "qty": 50}],
            [
                {"mid": "m1", "cid": "c1", "type": "filled", "fq": 100},
                {"mid": "m2", "cid": "c1", "type": "filled", "fq": 50},
            ],
        )
        r = svc.reconcile_correlation(
            date_key="2026-04-24",
            target="exec_bridge",
            correlation_id="c1",
            message_ids=["m1", "m2"],
        )
        assert r["status"] == "matched"
        assert r["total_intended_quantity"] == 150
        assert r["total_filled_quantity"] == 150


class TestInspectionServiceWithExecutionTimeline:
    def test_trace_includes_execution_timeline(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        cw = CommunicationRecordWriter(ledger_store=store)
        ew = ExecutionEventWriter(store)
        cw.write_record(_envelope("m1", "c1"), _result("m1"))
        ew.write_event(_event("m1", "c1", "ack"))
        ew.write_event(_event("m1", "c1", "filled", filled_qty=100))

        svc = CommunicationInspectionService(
            record_reader=CommunicationRecordReader(str(tmp_path)),
            execution_event_reader=ExecutionEventReader(str(tmp_path)),
        )
        trace = svc.get_message_trace(date_key="2026-04-24", target="exec_bridge", message_id="m1")
        assert (
            trace is not None
        )  # TECH_DEBT-009: get_message_trace 返回 dict|None, 已写入契约下恒非 None
        assert trace["execution_timeline"]["event_count"] == 2
        assert trace["execution_timeline"]["is_terminal"] is True

    def test_trace_without_execution_reader(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        CommunicationRecordWriter(ledger_store=store).write_record(
            _envelope("m1", "c1"), _result("m1")
        )

        svc = CommunicationInspectionService(record_reader=CommunicationRecordReader(str(tmp_path)))
        trace = svc.get_message_trace(date_key="2026-04-24", target="exec_bridge", message_id="m1")
        assert (
            trace is not None
        )  # TECH_DEBT-009: get_message_trace 返回 dict|None, 已写入契约下恒非 None
        assert trace["execution_timeline"] is None

    def test_correlation_delivery_summary_includes_execution_stats(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        cw = CommunicationRecordWriter(ledger_store=store)
        ew = ExecutionEventWriter(store)
        for mid in ["m1", "m2"]:
            cw.write_record(_envelope(mid, "c1"), _result(mid))
        ew.write_event(_event("m1", "c1", "filled", filled_qty=100))
        ew.write_event(_event("m2", "c1", "ack"))

        svc = CommunicationInspectionService(
            record_reader=CommunicationRecordReader(str(tmp_path)),
            execution_event_reader=ExecutionEventReader(str(tmp_path)),
        )
        trace = svc.get_correlation_trace(
            date_key="2026-04-24", target="exec_bridge", correlation_id="c1"
        )
        ds = trace["delivery_summary"]
        assert ds["execution_event_count"] == 2
        assert ds["execution_terminal_count"] == 1
        assert ds["execution_total_filled_quantity"] == 100
