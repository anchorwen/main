import json
from pathlib import Path

from core.contracts.domain_keys import PAYLOAD_KEY_CORRELATION_ID, PAYLOAD_KEY_MESSAGE_ID
from core.ledger.stream_names import LEDGER_STREAM_COMMUNICATIONS, stream_jsonl_filename


class CommunicationRecordReader:
    def __init__(self, base_dir: str):
        self._base_dir = Path(base_dir)

    def list_records(self, *, date_key: str, target: str) -> list[dict]:
        path = self._build_path(date_key=date_key, target=target)
        if not path.exists():
            return []
        return self._read_jsonl(path)

    def find_by_message_id(self, *, date_key: str, target: str, message_id: str) -> dict | None:
        for item in self.list_records(date_key=date_key, target=target):
            if item.get(PAYLOAD_KEY_MESSAGE_ID) == message_id:
                return item
        return None

    def find_by_correlation_id(
        self, *, date_key: str, target: str, correlation_id: str
    ) -> list[dict]:
        return [
            item
            for item in self.list_records(date_key=date_key, target=target)
            if item.get(PAYLOAD_KEY_CORRELATION_ID) == correlation_id
        ]

    def _build_path(self, *, date_key: str, target: str) -> Path:
        return (
            self._base_dir / date_key / stream_jsonl_filename(target, LEDGER_STREAM_COMMUNICATIONS)
        )

    def _read_jsonl(self, path: Path) -> list[dict]:
        records = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
        return records
