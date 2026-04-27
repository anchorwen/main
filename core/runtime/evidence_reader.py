"""Runtime evidence reader."""
import json
from pathlib import Path

from core.ledger.stream_names import LEDGER_STREAM_RUNTIME_EVIDENCE


class RuntimeEvidenceReader:
    """Reads runtime evidence records from JsonlLedgerStore-compatible layout."""

    def __init__(self, base_dir: str, stream_name: str = LEDGER_STREAM_RUNTIME_EVIDENCE):
        self._base_dir = Path(base_dir)
        self._stream_name = stream_name

    def read_cycle(self, runtime_cycle_id: str) -> list[dict]:
        records = []
        pattern = f"{runtime_cycle_id}.{self._stream_name}.jsonl"
        for path in sorted(self._base_dir.glob(f"*/{pattern}")):
            records.extend(self._read_file(path))
        return records

    def latest_cycle(self, runtime_cycle_id: str) -> dict | None:
        records = self.read_cycle(runtime_cycle_id)
        if not records:
            return None
        return sorted(records, key=lambda item: item.get("generated_at", ""))[-1]

    def list_cycle_ids(self) -> list[str]:
        suffix = f".{self._stream_name}.jsonl"
        ids = set()
        for path in self._base_dir.glob(f"*/*{suffix}"):
            ids.add(path.name.removesuffix(suffix))
        return sorted(ids)

    def _read_file(self, path: Path) -> list[dict]:
        records = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    records.append(json.loads(line))
        return records
