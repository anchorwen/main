"""Feature store incremental update jobs."""
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from core.features.store_contracts import FeatureRecord, FeatureSchema


@dataclass(frozen=True)
class FeatureUpdateResult:
    schema_name: str
    schema_version: str
    symbol: str
    timeframe: str
    records_written: int
    started_at: datetime
    finished_at: datetime

    def to_dict(self) -> dict:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "records_written": self.records_written,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
        }


class IncrementalFeatureUpdateJob:
    """Runs a user-provided producer and writes records into a feature store."""

    def __init__(self, feature_store, schema: FeatureSchema,
                 producer: Callable[[datetime | None], Iterable[FeatureRecord]]):
        self._store = feature_store
        self._schema = schema
        self._producer = producer

    def run(self) -> FeatureUpdateResult:
        started = datetime.utcnow()
        self._store.register_schema(self._schema)
        latest = self._store.latest(self._schema.symbol, self._schema.timeframe,
                                    schema_name=self._schema.name)
        since = latest.event_time if latest else None
        records = list(self._producer(since))
        records = [record for record in records if since is None or record.event_time > since]
        written = self._store.write_records(records) if records else 0
        return FeatureUpdateResult(
            schema_name=self._schema.name,
            schema_version=self._schema.version,
            symbol=self._schema.symbol,
            timeframe=self._schema.timeframe,
            records_written=written,
            started_at=started,
            finished_at=datetime.utcnow(),
        )
