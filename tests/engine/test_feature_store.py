"""Feature store MVP tests."""

from datetime import UTC, datetime, timedelta

import pytest

from core.contracts.ids import new_snapshot_id
from core.features.feature_snapshot import StoredFeatureSnapshot
from core.features.local_feature_store import LocalFeatureStore
from core.features.store_contracts import FeatureQuery, FeatureRecord, FeatureSchema
from core.features.update_job import IncrementalFeatureUpdateJob


def _schema():
    return FeatureSchema(
        name="technical_v1",
        version="1.0",
        fields=("ema_bias", "adx_slope"),
        symbol="XAUUSD",
        timeframe="M1",
    )


def _record(t, ema=1.0, adx=0.2):
    return FeatureRecord(
        schema_name="technical_v1",
        schema_version="1.0",
        symbol="XAUUSD",
        timeframe="M1",
        event_time=t,
        values={"ema_bias": ema, "adx_slope": adx},
        source="test",
    )


class TestFeatureStoreContracts:
    def test_feature_record_roundtrip(self):
        record = _record(datetime.now(UTC).replace(tzinfo=None))
        restored = FeatureRecord.from_dict(record.to_dict())
        assert restored.schema_name == record.schema_name
        assert restored.values == record.values

    def test_feature_record_requires_values(self):
        with pytest.raises(ValueError):
            FeatureRecord(
                schema_name="s",
                schema_version="1",
                symbol="XAUUSD",
                timeframe="M1",
                event_time=datetime.now(UTC).replace(tzinfo=None),
                values={},
            )


class TestLocalFeatureStore:
    def test_register_and_list_schema(self, tmp_path):
        store = LocalFeatureStore(str(tmp_path))
        store.register_schema(_schema())
        schemas = store.list_schemas()
        assert len(schemas) == 1
        assert schemas[0].name == "technical_v1"

    def test_write_and_query_records(self, tmp_path):
        store = LocalFeatureStore(str(tmp_path))
        store.register_schema(_schema())
        t0 = datetime.now(UTC).replace(tzinfo=None)
        assert store.write_records([_record(t0, 1.5, 0.3)]) == 1
        records = store.query(FeatureQuery(symbol="XAUUSD", timeframe="M1"))
        assert len(records) == 1
        assert records[0].values["ema_bias"] == 1.5

    def test_write_rejects_unregistered_schema(self, tmp_path):
        store = LocalFeatureStore(str(tmp_path))
        with pytest.raises(ValueError):
            store.write_records([_record(datetime.now(UTC).replace(tzinfo=None))])

    def test_write_rejects_missing_fields(self, tmp_path):
        store = LocalFeatureStore(str(tmp_path))
        store.register_schema(_schema())
        bad = FeatureRecord(
            schema_name="technical_v1",
            schema_version="1.0",
            symbol="XAUUSD",
            timeframe="M1",
            event_time=datetime.now(UTC).replace(tzinfo=None),
            values={"ema_bias": 1.0},
        )
        with pytest.raises(ValueError):
            store.write_records([bad])

    def test_query_time_range_and_limit(self, tmp_path):
        store = LocalFeatureStore(str(tmp_path))
        store.register_schema(_schema())
        base = datetime.now(UTC).replace(tzinfo=None)
        store.write_records(
            [
                _record(base, 1.0, 0.1),
                _record(base + timedelta(minutes=1), 2.0, 0.2),
                _record(base + timedelta(minutes=2), 3.0, 0.3),
            ]
        )
        records = store.query(
            FeatureQuery(
                symbol="XAUUSD",
                timeframe="M1",
                start=base + timedelta(seconds=30),
                limit=1,
            )
        )
        assert len(records) == 1
        assert records[0].values["ema_bias"] == 3.0

    def test_latest(self, tmp_path):
        store = LocalFeatureStore(str(tmp_path))
        store.register_schema(_schema())
        base = datetime.now(UTC).replace(tzinfo=None)
        store.write_records(
            [_record(base, 1.0, 0.1), _record(base + timedelta(minutes=1), 2.0, 0.2)]
        )
        latest = store.latest("XAUUSD", "M1", schema_name="technical_v1")
        assert latest.values["ema_bias"] == 2.0

    def test_query_missing_partition_empty(self, tmp_path):
        store = LocalFeatureStore(str(tmp_path))
        assert store.query(FeatureQuery(symbol="XAUUSD", timeframe="M1")) == []


class TestIncrementalFeatureUpdateJob:
    def test_incremental_job_writes_only_new_records(self, tmp_path):
        store = LocalFeatureStore(str(tmp_path))
        schema = _schema()
        base = datetime.now(UTC).replace(tzinfo=None)
        produced = [
            _record(base, 1.0, 0.1),
            _record(base + timedelta(minutes=1), 2.0, 0.2),
        ]

        def producer(since):
            return produced

        job = IncrementalFeatureUpdateJob(store, schema, producer)
        first = job.run()
        second = job.run()
        assert first.records_written == 2
        assert second.records_written == 0
        assert len(store.query(FeatureQuery(symbol="XAUUSD", timeframe="M1"))) == 2

    def test_incremental_job_passes_since(self, tmp_path):
        store = LocalFeatureStore(str(tmp_path))
        schema = _schema()
        base = datetime.now(UTC).replace(tzinfo=None)
        seen_since = []

        def producer(since):
            seen_since.append(since)
            if since is None:
                return [_record(base, 1.0, 0.1)]
            return [_record(base + timedelta(minutes=1), 2.0, 0.2)]

        job = IncrementalFeatureUpdateJob(store, schema, producer)
        job.run()
        job.run()
        assert seen_since[0] is None
        assert seen_since[1] == base


class TestStoredFeatureSnapshot:
    def test_snapshot_from_record(self):
        record = _record(datetime.now(UTC).replace(tzinfo=None), 1.2, 0.4)
        snapshot = StoredFeatureSnapshot.from_record(new_snapshot_id(), record)
        assert snapshot.symbol == "XAUUSD"
        assert snapshot.get("ema_bias") == 1.2
        assert snapshot.get("missing", 9) == 9
