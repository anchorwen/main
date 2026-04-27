from datetime import datetime

from core.contracts.domain.communication_record import CommunicationRecord
from core.contracts.ids import new_communication_record_id
from core.ledger.stream_names import LEDGER_STREAM_COMMUNICATIONS


class CommunicationRecordWriter:
    def __init__(self, ledger_store):
        self._ledger_store = ledger_store

    def write_record(self, envelope, dispatch_result) -> tuple[CommunicationRecord, object]:
        record = CommunicationRecord.from_dispatch(
            record_id=new_communication_record_id(),
            envelope=envelope,
            dispatch_result=dispatch_result,
        )
        ledger_path = self._ledger_store.append_record(
            date_key=envelope.event_time.strftime("%Y-%m-%d"),
            symbol=envelope.target,
            record=record,
            stream_name=LEDGER_STREAM_COMMUNICATIONS,
        )
        return record, ledger_path

