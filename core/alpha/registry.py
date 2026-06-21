"""Alpha Registry with JSON file persistence."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from core.alpha.contracts import AlphaRecord
from core.alpha.schema_versions import SCHEMA_ALPHA_REGISTRY


class AlphaRegistry:
    """Stores AlphaRecord entries for the B0 Alpha Factory."""

    def __init__(self):
        self._records: dict[str, AlphaRecord] = {}

    def register(self, record: AlphaRecord) -> AlphaRecord:
        if record.alpha_id in self._records:
            raise ValueError(f"alpha already registered: {record.alpha_id}")
        self._records[record.alpha_id] = record
        return record

    def upsert(self, record: AlphaRecord) -> AlphaRecord:
        self._records[record.alpha_id] = replace(
            record, updated_at=datetime.now(UTC).replace(tzinfo=None)
        )
        return self._records[record.alpha_id]

    def get(self, alpha_id: str) -> AlphaRecord | None:
        return self._records.get(alpha_id)

    def require(self, alpha_id: str) -> AlphaRecord:
        record = self.get(alpha_id)
        if record is None:
            raise ValueError(f"unknown alpha_id: {alpha_id}")
        return record

    def list_records(self, state: str | None = None) -> list[AlphaRecord]:
        records = list(self._records.values())
        if state is not None:
            records = [record for record in records if record.state_value == state]
        return sorted(records, key=lambda r: r.alpha_id)

    def remove(self, alpha_id: str) -> None:
        self._records.pop(alpha_id, None)

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_ALPHA_REGISTRY,
            "alpha_count": len(self._records),
            "records": [record.to_dict() for record in self.list_records()],
        }

    # ── persistence ──

    def save(self, path: str | Path) -> Path:
        # DQAF-046 Plan B: route through StateWriter gate for schema
        # validation + cross-symbol contamination guard + atomic write.
        from core.state.catalog import lookup
        from core.state.writer import StateWriter

        writer = StateWriter.from_state_path(path)
        writer.write_artifact(lookup("ALPHA_REGISTRY"), writer._symbol, self.to_dict())
        return Path(path)

    @classmethod
    def load(cls, path: str | Path) -> "AlphaRegistry":
        src = Path(path)
        if not src.exists():
            raise FileNotFoundError(f"alpha registry state file not found: {src}")
        data = json.loads(src.read_text(encoding="utf-8"))
        registry = cls()
        for rec_data in data.get("records", []):
            record = AlphaRecord(
                alpha_id=rec_data.get("alpha_id", "unknown"),
                name=rec_data.get("name", rec_data.get("alpha_id", "unknown")),
                version=rec_data.get("version", "0.0.0"),
                state=rec_data.get("state", "candidate"),
                strategy_id=rec_data.get("strategy_id"),
                strategy_class=rec_data.get("strategy_class"),
                assets=rec_data.get("assets"),
                tags=tuple(rec_data.get("tags", [])),
                metadata=rec_data.get("metadata", {}),
                performance=rec_data.get("performance", {}),
                risk_profile=rec_data.get("risk_profile", {}),
            )
            registry._records[record.alpha_id] = record
        return registry
