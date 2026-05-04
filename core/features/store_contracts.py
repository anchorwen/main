"""Feature store contracts.

A lightweight A1 Feature Store MVP. The initial implementation is local
JSONL for zero extra dependencies, while keeping partition/schema concepts
compatible with future Parquet/DuckDB/ClickHouse backends.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class FeatureSchema:
    name: str
    version: str
    fields: tuple[str, ...]
    symbol: str
    timeframe: str
    description: str = ""


@dataclass(frozen=True)
class FeatureRecord:
    schema_name: str
    schema_version: str
    symbol: str
    timeframe: str
    event_time: datetime
    values: dict[str, Any]
    source: str = "unknown"
    ingested_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError("FeatureRecord values cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "event_time": self.event_time.isoformat(),
            "values": self.values,
            "source": self.source,
            "ingested_at": self.ingested_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FeatureRecord":
        return cls(
            schema_name=payload["schema_name"],
            schema_version=payload["schema_version"],
            symbol=payload["symbol"],
            timeframe=payload["timeframe"],
            event_time=datetime.fromisoformat(payload["event_time"]),
            values=dict(payload["values"]),
            source=payload.get("source", "unknown"),
            ingested_at=datetime.fromisoformat(payload["ingested_at"]),
        )


@dataclass(frozen=True)
class FeatureQuery:
    symbol: str
    timeframe: str
    schema_name: str | None = None
    schema_version: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    limit: int | None = None


@runtime_checkable
class FeatureStore(Protocol):
    def register_schema(self, schema: FeatureSchema) -> None: ...

    def list_schemas(self) -> list[FeatureSchema]: ...

    def write_records(self, records: list[FeatureRecord]) -> int: ...

    def query(self, query: FeatureQuery) -> list[FeatureRecord]: ...

    def latest(
        self, symbol: str, timeframe: str, *, schema_name: str | None = None
    ) -> FeatureRecord | None: ...
