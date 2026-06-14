"""Local JSONL feature store implementation."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from core.features.store_contracts import FeatureQuery, FeatureRecord, FeatureSchema


class FeatureValidationError(ValueError):
    """Raised when a FeatureRecord fails schema validation at write time.

    Institutional Data SLA (Column 1 — Schema Dictatorship):
    Dirty data is rejected at the boundary.  The error message includes
    the exact mismatch so operators can diagnose the root cause without
    grep-ing through log files.
    """

    def __init__(self, message: str, schema_name: str = "", details: dict | None = None):
        super().__init__(message)
        self.schema_name = schema_name
        self.details = details or {}


def _normalize_dt(dt):
    """Return a naive UTC datetime regardless of input timezone."""
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None) - dt.utcoffset()
    return dt


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
        records.sort(key=lambda r: _normalize_dt(r.event_time))
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
        """Validate a FeatureRecord against its registered schema at write time.

        Column 1 — Schema Dictatorship (Institutional Data SLA):
        This is the DATA BOUNDARY.  Every record must pass these checks
        before it enters the Feature Store.  Dirty data is rejected here —
        it never reaches downstream consumers.

        Checks (fail-fast, in order):
          1. Schema registration — schema must exist in schemas.json
          2. Exact field count — |record| == |schema| (no extra, no missing)
          3. Field name match — every schema field must be present
          4. NaN rejection — no NaN, None, or Inf values permitted
        """
        schemas = self._load_schemas()
        key = self._schema_key(
            record.schema_name, record.schema_version, record.symbol, record.timeframe
        )
        if key not in schemas:
            raise FeatureValidationError(
                f"Schema not registered: {key} — register it in schemas.json first",
                schema_name=record.schema_name,
                details={"key": key},
            )

        schema_fields: list[str] = list(schemas[key]["fields"])
        expected_count = len(schema_fields)
        actual_count = len(record.values)

        # ── Check 1: Exact field count ──
        if actual_count != expected_count:
            extra = set(record.values) - set(schema_fields)
            missing = set(schema_fields) - set(record.values)
            raise FeatureValidationError(
                f"Feature vector dimension mismatch for schema '{record.schema_name}': "
                f"expected {expected_count} fields, got {actual_count}. "
                f"Extra fields: {sorted(extra) if extra else 'none'}. "
                f"Missing fields: {sorted(missing) if missing else 'none'}.",
                schema_name=record.schema_name,
                details={
                    "expected_count": expected_count,
                    "actual_count": actual_count,
                    "extra_fields": sorted(extra),
                    "missing_fields": sorted(missing),
                },
            )

        # ── Check 2: Field name exact match ──
        required_fields = set(schema_fields)
        actual_fields = set(record.values)
        missing_names = required_fields - actual_fields
        if missing_names:
            raise FeatureValidationError(
                f"Feature record missing required fields for schema "
                f"'{record.schema_name}': {sorted(missing_names)}",
                schema_name=record.schema_name,
                details={"missing_fields": sorted(missing_names)},
            )

        # ── Check 3: NaN / None / Inf rejection ──
        # Institutional Data SLA: NaN in feature store poisons every
        # downstream computation.  Reject at the boundary.
        nan_fields: list[str] = []
        inf_fields: list[str] = []
        none_fields: list[str] = []
        for name, value in record.values.items():
            if value is None:
                none_fields.append(name)
            elif isinstance(value, float):
                import math
                if math.isnan(value):
                    nan_fields.append(name)
                elif math.isinf(value):
                    inf_fields.append(name)

        if nan_fields or inf_fields or none_fields:
            parts: list[str] = []
            if none_fields:
                parts.append(f"None values: {none_fields}")
            if nan_fields:
                parts.append(f"NaN values: {nan_fields}")
            if inf_fields:
                parts.append(f"Inf values: {inf_fields}")
            raise FeatureValidationError(
                f"Feature record contains invalid values for schema "
                f"'{record.schema_name}': {'; '.join(parts)}. "
                f"NaN/None/Inf are forbidden in the Feature Store.",
                schema_name=record.schema_name,
                details={
                    "none_fields": none_fields,
                    "nan_fields": nan_fields,
                    "inf_fields": inf_fields,
                },
            )

    def _matches(self, record: FeatureRecord, query: FeatureQuery) -> bool:
        if query.schema_name and record.schema_name != query.schema_name:
            return False
        if query.schema_version and record.schema_version != query.schema_version:
            return False
        rec_time = _normalize_dt(record.event_time)
        if query.start:
            if rec_time < _normalize_dt(query.start):
                return False
        if query.end:
            if rec_time > _normalize_dt(query.end):
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

    def count(self, symbol: str | None = None, timeframe: str | None = None) -> int:
        """Count total records, optionally filtered by symbol/timeframe."""
        if not self._records_dir.exists():
            return 0
        total = 0
        for rec_path in self._iter_record_paths(symbol, timeframe):
            if rec_path.exists():
                with rec_path.open("r", encoding="utf-8") as f:
                    total += sum(1 for line in f if line.strip())
        return total

    def stats(self, symbol: str | None = None, timeframe: str | None = None) -> dict:
        """Return per-partition stats: record count, date range, file size."""
        result: dict[str, dict] = {}
        if not self._records_dir.exists():
            return result
        for rec_path in self._iter_record_paths(symbol, timeframe):
            if not rec_path.exists():
                continue
            key = str(rec_path.relative_to(self._records_dir))
            line_count = 0
            first_ts: str | None = None
            last_ts: str | None = None
            with rec_path.open("r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    line_count += 1
                    try:
                        rec = json.loads(line)
                        et = rec.get("event_time", "")
                        if et:
                            if first_ts is None or et < first_ts:
                                first_ts = et
                            if last_ts is None or et > last_ts:
                                last_ts = et
                    except json.JSONDecodeError:
                        continue
            result[key] = {
                "record_count": line_count,
                "first_event": first_ts,
                "last_event": last_ts,
                "file_size_bytes": rec_path.stat().st_size if rec_path.exists() else 0,
            }
        return result

    def compact(
        self,
        symbol: str | None = None,
        timeframe: str | None = None,
        *,
        retention_days: int | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Dedup by event_time (keep last) and optionally trim old records.

        Returns a dict with per-partition compaction stats.
        """
        results: dict[str, dict] = {}
        cutoff = None
        if retention_days is not None:
            cutoff = datetime.utcnow() - timedelta(days=retention_days)
        for rec_path in self._iter_record_paths(symbol, timeframe):
            if not rec_path.exists():
                continue
            key = str(rec_path.relative_to(self._records_dir))
            before_count = 0
            kept: dict[str, dict] = {}  # event_time -> record dict
            duplicates = 0
            trimmed = 0
            with rec_path.open("r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    before_count += 1
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    et = rec.get("event_time", "")
                    if not et:
                        kept[f"__no_ts_{before_count}"] = rec
                        continue
                    if cutoff is not None:
                        try:
                            rec_dt = datetime.fromisoformat(et)
                            if rec_dt < cutoff:
                                trimmed += 1
                                continue
                        except (ValueError, TypeError):
                            pass
                    if et in kept:
                        duplicates += 1
                    kept[et] = rec
            after_count = len(kept)
            if not dry_run and (duplicates > 0 or trimmed > 0 or before_count != after_count):
                tmp_path = rec_path.with_suffix(".jsonl.tmp")
                with tmp_path.open("w", encoding="utf-8") as f:
                    for rec in kept.values():
                        f.write(json.dumps(rec, default=str) + "\n")
                tmp_path.replace(rec_path)
            results[key] = {
                "before": before_count,
                "after": after_count,
                "duplicates_removed": duplicates,
                "trimmed_by_retention": trimmed,
                "dry_run": dry_run,
            }
        return results

    def _iter_record_paths(self, symbol: str | None, timeframe: str | None):
        """Yield Path objects for matching record files."""
        if not self._records_dir.exists():
            return
        sym_pattern = symbol if symbol else "*"
        tf_pattern = timeframe if timeframe else "*"
        for sym_dir in sorted(self._records_dir.glob(f"symbol={sym_pattern}")):
            if not sym_dir.is_dir():
                continue
            for tf_dir in sorted(sym_dir.glob(f"timeframe={tf_pattern}")):
                if not tf_dir.is_dir():
                    continue
                rec_path = tf_dir / "features.jsonl"
                yield rec_path

    def resolve_version(self, schema_name: str, symbol: str, timeframe: str) -> str | None:
        """Return the registered version for a schema name + symbol + timeframe.

        Returns None when no matching schema is registered — callers should
        either skip the operation or fall back to a safe default.
        """
        schemas = self._load_schemas()
        for info in schemas.values():
            if (
                info.get("name") == schema_name
                and info.get("symbol") == symbol
                and info.get("timeframe") == timeframe
            ):
                return info["version"]
        return None

    def _schema_key(self, name: str, version: str, symbol: str, timeframe: str) -> str:
        return f"{name}:{version}:{symbol}:{timeframe}"
