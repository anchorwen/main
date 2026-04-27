"""Feature snapshot helpers for strategy plugins."""
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.features.store_contracts import FeatureRecord


@dataclass(frozen=True)
class StoredFeatureSnapshot:
    snapshot_id: str
    symbol: str
    timeframe: str
    event_time: datetime
    values: dict[str, Any]
    schema_name: str
    schema_version: str

    @classmethod
    def from_record(cls, snapshot_id: str, record: FeatureRecord) -> "StoredFeatureSnapshot":
        return cls(
            snapshot_id=snapshot_id,
            symbol=record.symbol,
            timeframe=record.timeframe,
            event_time=record.event_time,
            values=dict(record.values),
            schema_name=record.schema_name,
            schema_version=record.schema_version,
        )

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)
