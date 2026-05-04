from datetime import UTC, datetime

from core.contracts.domain.replay_execution_record import ReplayExecutionRecord
from core.contracts.ids import new_replay_execution_id
from core.ledger.stream_names import LEDGER_STREAM_REPLAYS


class ReplayExecutionWriter:
    def __init__(self, ledger_store):
        self._ledger_store = ledger_store

    def write_record(
        self, execution_result: dict, *, date_key: str, symbol: str
    ) -> tuple[ReplayExecutionRecord, object]:
        record = ReplayExecutionRecord.from_execution_result(
            replay_id=new_replay_execution_id(),
            executed_at=datetime.now(UTC).replace(tzinfo=None),
            execution_result=execution_result,
        )
        ledger_path = self._ledger_store.append_record(
            date_key=date_key,
            symbol=symbol,
            record=record,
            stream_name=LEDGER_STREAM_REPLAYS,
        )
        return record, ledger_path
