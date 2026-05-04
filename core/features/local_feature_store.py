"""Local JSONL feature store implementation."""

import json
from pathlib import Path

from core.features.store_contracts import FeatureQuery, FeatureRecord, FeatureSchema


class LocalFeatureStore:
    """Dependency-free local feature store using partitioned JSONL files."""

    def __init__(self, base_dir: str):
        self._base_dir = Path(base_dir)
        self._schema_path = self._base_dir / "schemas.json"
        self._records_dir = self._base_dir / "records"

    def register_schema(self, schema: FeatureSchema) -> None:
        schemas = self._load_schemas()
        key = self._schema_key(schema.name, schema.version, schema.symbol, schema.timeframe)
        schemas[key] = {
            "name": schema.name,
            "version": schema.version,
            "fields": list(schema.fields),
            "symbol": schema.symbol,
            "timeframe": schema.timeframe,
            "description": schema.description,
        }
        self._write_schemas(schemas)

    def list_schemas(self) -> list[FeatureSchema]:
        return [
            FeatureSchema(
                name=item["name"],
                version=item["version"],
                fields=tuple(item["fields"]),
                symbol=item["symbol"],
                timeframe=item["timeframe"],
                description=item.get("description", ""),
            )
            for item in self._load_schemas().values()
        ]

    def write_records(self, records: list[FeatureRecord]) -> int:
        count = 0
        for record in records:
            self._validate_record(record)
            path = self._record_path(record.symbol, record.timeframe)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(), default=str) + "\n")
            count += 1
        return count

    def query(self, query: FeatureQuery) -> list[FeatureRecord]:
        path = self._record_path(query.symbol, query.timeframe)
        if not path.exists():
            return []
        records = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                record = FeatureRecord.from_dict(json.loads(line))
                if self._matches(record, query):
                    records.append(record)
        records.sort(key=lambda r: r.event_time)
        if query.limit is not None:
            records = records[-query.limit :]
        return records

    def latest(
        self, symbol: str, timeframe: str, *, schema_name: str | None = None
    ) -> FeatureRecord | None:
        records = self.query(
            FeatureQuery(symbol=symbol, timeframe=timeframe, schema_name=schema_name, limit=1)
        )
        return records[-1] if records else None

    def _validate_record(self, record: FeatureRecord) -> None:
        schemas = self._load_schemas()
        key = self._schema_key(
            record.schema_name, record.schema_version, record.symbol, record.timeframe
        )
        if key not in schemas:
            raise ValueError(f"Feature schema not registered: {key}")
        required_fields = set(schemas[key]["fields"])
        missing = required_fields - set(record.values)
        if missing:
            raise ValueError(f"Feature record missing fields: {sorted(missing)}")

    def _matches(self, record: FeatureRecord, query: FeatureQuery) -> bool:
        if query.schema_name and record.schema_name != query.schema_name:
            return False
        if query.schema_version and record.schema_version != query.schema_version:
            return False
        if query.start and record.event_time < query.start:
            return False
        if query.end and record.event_time > query.end:
            return False
        return True

    def _record_path(self, symbol: str, timeframe: str) -> Path:
        return self._records_dir / f"symbol={symbol}" / f"timeframe={timeframe}" / "features.jsonl"

    def _load_schemas(self) -> dict:
        if not self._schema_path.exists():
            return {}
        return json.loads(self._schema_path.read_text(encoding="utf-8"))

    def _write_schemas(self, schemas: dict) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._schema_path.write_text(json.dumps(schemas, indent=2), encoding="utf-8")

    def _schema_key(self, name: str, version: str, symbol: str, timeframe: str) -> str:
        return f"{name}:{version}:{symbol}:{timeframe}"
