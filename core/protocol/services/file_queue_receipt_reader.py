import json
from datetime import date, datetime, timedelta
from pathlib import Path


class FileQueueReceiptReader:
    def __init__(self, receipt_dir: str):
        self._receipt_dir = Path(receipt_dir)

    def find_by_message_id(self, *, date_key: str, target: str, message_id: str) -> dict | None:
        for lookup_date_key in self._iter_lookup_date_keys(date_key):
            path = self._build_path(date_key=lookup_date_key, target=target, message_id=message_id)
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        return None

    def _iter_lookup_date_keys(self, date_key: str) -> list[str]:
        parsed_date = datetime.fromisoformat(date_key).date()
        return [
            date_key,
            self._format_date_key(parsed_date + timedelta(days=1)),
        ]

    def _format_date_key(self, value: date) -> str:
        return value.isoformat()

    def _build_path(self, *, date_key: str, target: str, message_id: str) -> Path:
        return self._receipt_dir / date_key / target / f"{message_id}.ack.json"


