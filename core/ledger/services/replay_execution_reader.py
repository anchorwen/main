import json
from pathlib import Path

from core.deployment.domain_keys import PAYLOAD_KEY_REPLAY_ID
from core.ledger.stream_names import LEDGER_STREAM_REPLAYS, stream_jsonl_filename


class ReplayExecutionReader:
    def __init__(self, base_dir: str):
        self._base_dir = Path(base_dir)

    def list_records(self, *, date_key: str, target: str) -> list[dict]:
        path = self._build_path(date_key=date_key, target=target)
        if not path.exists():
            return []
        return self._read_jsonl(path)

    def find_by_replay_id(self, *, date_key: str, target: str, replay_id: str) -> dict | None:
        for item in self.list_records(date_key=date_key, target=target):
            if item.get(PAYLOAD_KEY_REPLAY_ID) == replay_id:
                return item
        return None

    def _build_path(self, *, date_key: str, target: str) -> Path:
        return self._base_dir / date_key / stream_jsonl_filename(target, LEDGER_STREAM_REPLAYS)

    def _read_jsonl(self, path: Path) -> list[dict]:
        records = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
        return records
