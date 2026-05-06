from pathlib import Path

from core.contracts.serialization.json_codec import to_json
from core.ledger.stream_names import LEDGER_STREAM_DECISIONS, stream_jsonl_filename


class JsonlLedgerStore:
    def __init__(self, base_dir: str):
        self._base_dir = Path(base_dir)

    def append_record(
        self, date_key: str, symbol: str, record, stream_name: str = LEDGER_STREAM_DECISIONS
    ) -> Path:
        # Only decision records live under the decisions/ subdirectory.
        # Other streams (communications, execution_events, replays, runtime_evidence)
        # are stored directly under base_dir/{date_key}/ to keep read paths backward-compatible.
        if stream_name == LEDGER_STREAM_DECISIONS:
            target_dir = self._base_dir / "decisions" / date_key
        else:
            target_dir = self._base_dir / date_key
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / stream_jsonl_filename(symbol, stream_name)
        with target_file.open("a", encoding="utf-8") as f:
            f.write(to_json(record) + "\n")
        return target_file
