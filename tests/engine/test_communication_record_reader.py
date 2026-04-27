from datetime import datetime, timedelta

from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import CommunicationMessageType, CommunicationPriority, DispatchStatus
from core.ledger.services.communication_record_reader import CommunicationRecordReader
from core.ledger.services.communication_record_writer import CommunicationRecordWriter
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.protocol.schema_versions import SCHEMA_COMMUNICATION_ENVELOPE, SCHEMA_DISPATCH_RESULT


def build_envelope(message_id: str, correlation_id: str, target: str = "exec_bridge"):
    return CommunicationEnvelope(
        schema_version=SCHEMA_COMMUNICATION_ENVELOPE,
        message_id=message_id,
        correlation_id=correlation_id,
        causation_id=None,
        event_time=datetime(2026, 4, 24, 12, 0, 0),
        producer="decision_engine",
        target=target,
        message_type=CommunicationMessageType.DECISION_INTENT,
        priority=CommunicationPriority.NORMAL,
        payload={"intent_id": message_id},
        deadline_at=datetime(2026, 4, 24, 12, 0, 0) + timedelta(seconds=5),
    )


def build_result(message_id: str):
    return DispatchResult(
        schema_version=SCHEMA_DISPATCH_RESULT,
        dispatch_id=f"dispatch_{message_id}",
        message_id=message_id,
        status=DispatchStatus.PROTOCOL_VALIDATED,
        recorded_at=datetime(2026, 4, 24, 12, 0, 1),
        target="exec_bridge",
        adapter_name="stub_adapter",
        attempts=[{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
    )


def test_communication_record_reader_lists_records_for_target_and_date(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))

    writer.write_record(build_envelope("message_001", "corr_001"), build_result("message_001"))
    writer.write_record(build_envelope("message_002", "corr_002"), build_result("message_002"))

    records = reader.list_records(date_key="2026-04-24", target="exec_bridge")

    assert len(records) == 2
    assert records[0]["message_id"] == "message_001"
    assert records[1]["message_id"] == "message_002"


def test_communication_record_reader_finds_record_by_message_id(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))

    writer.write_record(build_envelope("message_abc", "corr_group"), build_result("message_abc"))

    record = reader.find_by_message_id(
        date_key="2026-04-24",
        target="exec_bridge",
        message_id="message_abc",
    )

    assert record is not None
    assert record["message_id"] == "message_abc"
    assert record["dispatch"]["attempts"][0]["adapter_name"] == "stub_adapter"


def test_communication_record_reader_filters_by_correlation_id(tmp_path):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))

    writer.write_record(build_envelope("message_001", "corr_shared"), build_result("message_001"))
    writer.write_record(build_envelope("message_002", "corr_shared"), build_result("message_002"))
    writer.write_record(build_envelope("message_003", "corr_other"), build_result("message_003"))

    records = reader.find_by_correlation_id(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id="corr_shared",
    )

    assert [item["message_id"] for item in records] == ["message_001", "message_002"]


def test_communication_record_reader_returns_empty_when_stream_missing(tmp_path):
    reader = CommunicationRecordReader(base_dir=str(tmp_path))

    assert reader.list_records(date_key="2026-04-24", target="missing_target") == []
    assert reader.find_by_message_id(
        date_key="2026-04-24",
        target="missing_target",
        message_id="missing_message",
    ) is None
    assert reader.find_by_correlation_id(
        date_key="2026-04-24",
        target="missing_target",
        correlation_id="missing_corr",
    ) == []





