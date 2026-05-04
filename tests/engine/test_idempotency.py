from datetime import datetime

from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import CommunicationMessageType, CommunicationPriority, DispatchStatus
from core.ledger.services.communication_record_reader import CommunicationRecordReader
from core.ledger.services.communication_record_writer import CommunicationRecordWriter
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.protocol.services.communication_dispatcher import CommunicationDispatcher
from core.protocol.services.idempotency import DuplicateDetector, IdempotencyStore
from core.protocol.services.stub_communication_adapter import StubCommunicationAdapter


def _envelope(mid, cid, idem_key=None):
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
        idempotency_key=idem_key,
    )


class TestIdempotencyStore:
    def test_first_claim_succeeds(self, tmp_path):
        store = IdempotencyStore(str(tmp_path))
        result = store.check_and_claim(
            idempotency_key="key_001",
            message_id="m1",
            date_key="2026-04-24",
        )
        assert result["status"] == "claimed"
        assert result["duplicate"] is False
        assert result["claimed"] is True

    def test_second_claim_detects_duplicate(self, tmp_path):
        store = IdempotencyStore(str(tmp_path))
        store.check_and_claim(idempotency_key="key_001", message_id="m1", date_key="2026-04-24")
        result = store.check_and_claim(
            idempotency_key="key_001",
            message_id="m2",
            date_key="2026-04-24",
        )
        assert result["status"] == "duplicate"
        assert result["duplicate"] is True
        assert result["original_message_id"] == "m1"

    def test_no_key_bypasses(self, tmp_path):
        store = IdempotencyStore(str(tmp_path))
        result = store.check_and_claim(idempotency_key="", message_id="m1", date_key="2026-04-24")
        assert result["status"] == "no_key"
        assert result["duplicate"] is False

    def test_cross_day_lookup(self, tmp_path):
        store = IdempotencyStore(str(tmp_path))
        store.check_and_claim(idempotency_key="key_x", message_id="m1", date_key="2026-04-23")
        assert store.is_duplicate(idempotency_key="key_x", date_key="2026-04-24")

    def test_different_keys_independent(self, tmp_path):
        store = IdempotencyStore(str(tmp_path))
        store.check_and_claim(idempotency_key="key_a", message_id="m1", date_key="2026-04-24")
        result = store.check_and_claim(
            idempotency_key="key_b", message_id="m2", date_key="2026-04-24"
        )
        assert result["duplicate"] is False


class TestDispatcherIdempotency:
    def test_dispatcher_blocks_duplicate_key(self, tmp_path):
        idem_store = IdempotencyStore(str(tmp_path / "idem"))
        dispatcher = CommunicationDispatcher(
            adapter=StubCommunicationAdapter(),
            clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
            idempotency_store=idem_store,
        )

        env1 = _envelope("m1", "c1", idem_key="order_key_001")
        r1 = dispatcher.dispatch(env1)
        assert r1.status == DispatchStatus.PROTOCOL_VALIDATED

        env2 = _envelope("m2", "c1", idem_key="order_key_001")
        r2 = dispatcher.dispatch(env2)
        assert r2.status == DispatchStatus.FAILED
        assert "duplicate" in r2.failure_reason
        assert r2.trace.get("original_message_id") == "m1"

    def test_dispatcher_allows_without_idem_key(self, tmp_path):
        idem_store = IdempotencyStore(str(tmp_path / "idem"))
        dispatcher = CommunicationDispatcher(
            adapter=StubCommunicationAdapter(),
            clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
            idempotency_store=idem_store,
        )
        env = _envelope("m1", "c1")
        r = dispatcher.dispatch(env)
        assert r.status == DispatchStatus.PROTOCOL_VALIDATED

    def test_dispatcher_works_without_idem_store(self):
        dispatcher = CommunicationDispatcher(
            adapter=StubCommunicationAdapter(),
            clock=lambda: datetime(2026, 4, 24, 12, 0, 2),
        )
        env = _envelope("m1", "c1", idem_key="key_001")
        r = dispatcher.dispatch(env)
        assert r.status == DispatchStatus.PROTOCOL_VALIDATED


class TestDuplicateDetector:
    def test_finds_duplicates(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        writer = CommunicationRecordWriter(ledger_store=store)
        reader = CommunicationRecordReader(str(tmp_path))

        env1 = _envelope("m1", "c1", idem_key="dup_key")
        writer.write_record(
            env1,
            DispatchResult(
                schema_version="v1",
                dispatch_id="d1",
                message_id="m1",
                status=DispatchStatus.TRANSPORT_DELIVERED,
                recorded_at=datetime(2026, 4, 24, 12, 0, 1),
                target="exec_bridge",
                adapter_name="stub",
            ),
        )

        env2 = _envelope("m2", "c1", idem_key="dup_key")
        writer.write_record(
            env2,
            DispatchResult(
                schema_version="v1",
                dispatch_id="d2",
                message_id="m2",
                status=DispatchStatus.TRANSPORT_DELIVERED,
                recorded_at=datetime(2026, 4, 24, 12, 0, 2),
                target="exec_bridge",
                adapter_name="stub",
            ),
        )

        detector = DuplicateDetector(reader)
        dups = detector.find_duplicates(date_key="2026-04-24", target="exec_bridge")
        assert len(dups) == 1
        assert dups[0]["idempotency_key"] == "dup_key"
        assert dups[0]["original_message_id"] == "m1"
        assert dups[0]["duplicate_message_id"] == "m2"

    def test_no_duplicates(self, tmp_path):
        store = JsonlLedgerStore(str(tmp_path))
        writer = CommunicationRecordWriter(ledger_store=store)
        reader = CommunicationRecordReader(str(tmp_path))

        writer.write_record(
            _envelope("m1", "c1", idem_key="key_a"),
            DispatchResult(
                schema_version="v1",
                dispatch_id="d1",
                message_id="m1",
                status=DispatchStatus.TRANSPORT_DELIVERED,
                recorded_at=datetime(2026, 4, 24, 12, 0, 1),
                target="exec_bridge",
                adapter_name="stub",
            ),
        )
        writer.write_record(
            _envelope("m2", "c1", idem_key="key_b"),
            DispatchResult(
                schema_version="v1",
                dispatch_id="d2",
                message_id="m2",
                status=DispatchStatus.TRANSPORT_DELIVERED,
                recorded_at=datetime(2026, 4, 24, 12, 0, 2),
                target="exec_bridge",
                adapter_name="stub",
            ),
        )

        detector = DuplicateDetector(reader)
        assert detector.find_duplicates(date_key="2026-04-24", target="exec_bridge") == []
