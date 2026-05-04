"""In-memory Alpha Registry MVP."""

from dataclasses import replace
from datetime import UTC, datetime

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
