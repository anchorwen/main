"""Tests for dataset_builder module."""

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

# ── _parse_iso tests ──


def test_parse_iso_with_z():
    from scripts.training.dataset_builder import _parse_iso

    dt = _parse_iso("2026-04-29T05:03:43Z")
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 4
    assert dt.day == 29
    assert dt.hour == 5


def test_parse_iso_with_timezone():
    from scripts.training.dataset_builder import _parse_iso

    dt = _parse_iso("2026-04-29T05:03:43+00:00")
    assert dt is not None
    assert dt.hour == 5


def test_parse_iso_none():
    from scripts.training.dataset_builder import _parse_iso

    assert _parse_iso(None) is None
    assert _parse_iso("") is None


def test_parse_iso_invalid():
    from scripts.training.dataset_builder import _parse_iso

    assert _parse_iso("not-a-date") is None


# ── _normalize_symbol tests ──


def test_normalize_symbol_canonical():
    from scripts.training.dataset_builder import _normalize_symbol

    # XAUUSDc is the canonical form
    assert _normalize_symbol("XAUUSDc") == "XAUUSDc"
    assert _normalize_symbol("XAUUSD") == "XAUUSDc"
    assert _normalize_symbol("EURUSD") == "EURUSD"


# ── temporal_split tests ──


def test_temporal_split_ratios():
    from scripts.training.dataset_builder import temporal_split

    rows = [{"open_recorded_at": f"2026-04-{d:02d}T00:00:00Z", "v": d} for d in range(1, 11)]
    train, val = temporal_split(rows, val_ratio=0.2)
    assert len(train) == 8
    assert len(val) == 2
    assert train[-1]["v"] < val[0]["v"]


def test_temporal_split_no_leakage():
    from scripts.training.dataset_builder import temporal_split

    rows = [{"open_recorded_at": f"2026-04-{d:02d}T00:00:00Z", "v": d} for d in range(1, 31)]
    train, val = temporal_split(rows, val_ratio=0.3)
    max_train_date = max(r["open_recorded_at"] for r in train)
    min_val_date = min(r["open_recorded_at"] for r in val)
    assert max_train_date <= min_val_date


def test_temporal_split_empty():
    from scripts.training.dataset_builder import temporal_split

    train, val = temporal_split([], val_ratio=0.2)
    assert train == []
    assert val == []


def test_temporal_split_single_row():
    from scripts.training.dataset_builder import temporal_split

    rows = [{"open_recorded_at": "2026-04-01T00:00:00Z", "v": 1}]
    train, val = temporal_split(rows, val_ratio=0.2)
    assert len(train) == 1
    assert len(val) == 0


# ── join_labels_to_features tests ──


def _make_label(label_id, open_at, label="win", symbol="XAUUSDc", side="long", pnl=10.0):
    return {
        "schema_version": "training_label.v1",
        "label_id": label_id,
        "position_ticket": 12345,
        "symbol": symbol,
        "side": side,
        "entry_price": 3000.0,
        "exit_price": 3010.0,
        "pnl": pnl,
        "label": label,
        "volume": 0.01,
        "open_message_id": f"msg_{label_id}",
        "open_recorded_at": open_at,
        "close_message_id": f"msg_close_{label_id}",
        "close_recorded_at": open_at.replace("T00:", "T08:"),
        "is_closed": True,
    }


def _make_feature_record(store, event_time_str, symbol="XAUUSDc", **extra_values):
    """Write a single feature record to the store and return it."""
    from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES
    from core.features.store_contracts import FeatureRecord, FeatureSchema

    # Register schema once
    if not store.list_schemas():
        schema = FeatureSchema(
            name="v9_institutional_40",
            version="1.0.0",
            fields=tuple(V9_INSTITUTIONAL_40_FEATURES),
            symbol=symbol,
            timeframe="M5",
            description="Test schema",
        )
        store.register_schema(schema)

    values = {}
    for i, fname in enumerate(V9_INSTITUTIONAL_40_FEATURES):
        values[fname] = extra_values.get(fname, float(i) * 0.01)

    record = FeatureRecord(
        schema_name="v9_institutional_40",
        schema_version="1.0.0",
        symbol=symbol,
        timeframe="M5",
        event_time=datetime.fromisoformat(event_time_str),
        values=values,
        source="test",
    )
    store.write_records([record])
    return record


def test_join_skips_unlabeled(tmp_path: Path):
    from core.features.local_feature_store import LocalFeatureStore
    from scripts.training.dataset_builder import join_labels_to_features

    store = LocalFeatureStore(str(tmp_path))
    labels = [
        _make_label("L1", "2026-04-01T10:00:00Z", label="unlabeled"),
        _make_label("L2", "2026-04-01T11:00:00Z", label="win"),
    ]
    _make_feature_record(store, "2026-04-01T11:00:00+00:00")

    result = join_labels_to_features(labels, store)
    assert result["skipped_unlabeled"] == 1
    assert result["matched"] == 1
    assert result["unmatched"] == 0


def test_join_no_feature_match(tmp_path: Path):
    from core.features.local_feature_store import LocalFeatureStore
    from scripts.training.dataset_builder import join_labels_to_features

    store = LocalFeatureStore(str(tmp_path))
    labels = [_make_label("L1", "2026-04-01T10:00:00Z", label="win")]
    # No feature records

    result = join_labels_to_features(labels, store)
    assert result["matched"] == 0
    assert result["unmatched"] == 1


def test_join_full_pipeline(tmp_path: Path):
    from core.features.local_feature_store import LocalFeatureStore
    from scripts.training.dataset_builder import join_labels_to_features

    store = LocalFeatureStore(str(tmp_path))
    labels = [
        _make_label("L1", "2026-04-01T10:00:00Z", label="win", pnl=15.0),
        _make_label("L2", "2026-04-02T14:00:00Z", label="loss", pnl=-5.0),
        _make_label("L3", "2026-04-03T09:00:00Z", label="unlabeled"),
    ]
    _make_feature_record(store, "2026-04-01T09:59:55+00:00")
    _make_feature_record(store, "2026-04-02T13:59:55+00:00")
    # No feature for L3 (and it's unlabeled anyway)

    result = join_labels_to_features(labels, store)
    assert result["matched"] == 2
    assert result["unmatched"] == 0
    assert result["skipped_unlabeled"] == 1

    joined = result["joined"]
    assert len(joined) == 2

    # First row: win
    assert joined[0]["label"] == "win"
    assert joined[0]["pnl"] == 15.0
    assert joined[0]["symbol"] == "XAUUSDc"
    assert abs(joined[0]["time_delta_seconds"]) == 5.0
    assert "f_0" in joined[0]
    assert "f_39" in joined[0]
    assert joined[0]["f_0"] == 0.0  # V9_INSTITUTIONAL_40_FEATURES[0] = "M5_Ret_1", value is 0*0.01

    # Second row: loss
    assert joined[1]["label"] == "loss"
    assert joined[1]["pnl"] == -5.0


def test_join_outside_time_window(tmp_path: Path):
    from core.features.local_feature_store import LocalFeatureStore
    from scripts.training.dataset_builder import join_labels_to_features

    store = LocalFeatureStore(str(tmp_path))
    labels = [_make_label("L1", "2026-04-01T10:00:00Z", label="win")]
    _make_feature_record(store, "2026-04-01T12:00:00+00:00")  # 2 hours later

    result = join_labels_to_features(labels, store, max_time_delta_seconds=300.0)
    assert result["matched"] == 0
    assert result["unmatched"] == 1


def test_join_feature_within_window_chosen(tmp_path: Path):
    from core.features.local_feature_store import LocalFeatureStore
    from scripts.training.dataset_builder import join_labels_to_features

    store = LocalFeatureStore(str(tmp_path))
    labels = [_make_label("L1", "2026-04-01T10:00:00Z", label="win")]
    _make_feature_record(store, "2026-04-01T09:57:00+00:00")  # 3 min before

    result = join_labels_to_features(labels, store, max_time_delta_seconds=300.0)
    assert result["matched"] == 1


# ── export_parquet tests ──


def test_export_parquet_roundtrip(tmp_path: Path):
    pytest.importorskip("pyarrow")
    from scripts.training.dataset_builder import export_parquet

    joined = [
        {
            "label_id": f"L{i}",
            "symbol": "XAUUSD",
            "side": "long",
            "label": "win" if i % 2 == 0 else "loss",
            "pnl": float(i * 5),
            "entry_price": 3000.0,
            "exit_price": 3005.0,
            "volume": 0.01,
            "open_recorded_at": "2026-04-01T10:00:00Z",
            "feature_event_time": "2026-04-01T09:59:55+00:00",
            "feature_source": "test",
            "time_delta_seconds": 5.0,
            **{f"f_{j}": float(j) * 0.1 for j in range(40)},
        }
        for i in range(5)
    ]

    out = tmp_path / "test.parquet"
    result = export_parquet(joined, out)
    assert result == out
    assert out.exists()

    import pandas as pd

    df = pd.read_parquet(out)
    assert len(df) == 5
    assert "f_0" in df.columns
    assert "f_39" in df.columns
    assert "label" in df.columns
    assert df["label"].iloc[0] == "win"
    assert df["f_0"].iloc[0] == 0.0


# ── export_npz tests ──


def test_export_npz_roundtrip(tmp_path: Path):
    from scripts.training.dataset_builder import export_npz

    joined = [
        {
            "label_id": f"L{i}",
            "symbol": "XAUUSD",
            "side": "long",
            "label": "win" if i % 2 == 0 else "loss",
            "pnl": float(i * 3),
            "entry_price": 3000.0,
            "exit_price": 3003.0,
            "volume": 0.01,
            "open_recorded_at": "2026-04-01T10:00:00Z",
            "feature_event_time": "2026-04-01T09:59:55+00:00",
            "feature_source": "test",
            "time_delta_seconds": 5.0,
            **{f"f_{j}": float(j) * 0.1 for j in range(40)},
        }
        for i in range(3)
    ]

    out = tmp_path / "test.npz"
    result = export_npz(joined, out)
    assert result == out
    assert out.exists()

    data = np.load(out)
    assert data["X"].shape == (3, 40)
    assert data["y"].shape == (3,)
    assert data["pnl"].shape == (3,)
    assert len(data["feature_names"]) == 40
    assert data["y"][0] == 1  # win → TP
    assert data["y"][1] == -1  # loss → SL


# ── build_dataset end-to-end tests ──


def test_build_dataset_e2e(tmp_path: Path):
    pytest.importorskip("pyarrow")
    from scripts.training.dataset_builder import build_dataset

    # Create labels jsonl
    labels_content = "\n".join(
        json.dumps(
            {
                "schema_version": "training_label.v1",
                "label_id": f"label_{i}",
                "position_ticket": 1000 + i,
                "symbol": "XAUUSDc",
                "side": "long",
                "entry_price": 3000.0,
                "exit_price": 3005.0,
                "pnl": 5.0,
                "label": "win",
                "volume": 0.01,
                "open_message_id": f"msg_{i}",
                "open_recorded_at": f"2026-05-0{i}T10:00:00Z",
                "close_message_id": f"msg_close_{i}",
                "close_recorded_at": f"2026-05-0{i}T18:00:00Z",
                "is_closed": True,
            }
        )
        for i in range(1, 6)
    )
    labels_path = tmp_path / "labels.jsonl"
    labels_path.write_text(labels_content, encoding="utf-8")

    # Create feature store with matching records
    from core.features.local_feature_store import LocalFeatureStore
    from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES
    from core.features.store_contracts import FeatureRecord, FeatureSchema

    store_dir = tmp_path / "feature_store"
    store = LocalFeatureStore(str(store_dir))
    store.register_schema(
        FeatureSchema(
            name="v9_institutional_40",
            version="1.0.0",
            fields=tuple(V9_INSTITUTIONAL_40_FEATURES),
            symbol="XAUUSD",
            timeframe="M5",
            description="Test schema",
        )
    )

    for i in range(1, 6):
        values = {}
        for j, fname in enumerate(V9_INSTITUTIONAL_40_FEATURES):
            values[fname] = float(i * 100 + j) * 0.01
        record = FeatureRecord(
            schema_name="v9_institutional_40",
            schema_version="1.0.0",
            symbol="XAUUSD",
            timeframe="M5",
            event_time=datetime.fromisoformat(f"2026-05-0{i}T09:59:55+00:00"),
            values=values,
            source="test",
        )
        store.write_records([record])

    output_dir = tmp_path / "output"
    result = build_dataset(
        labels_path=labels_path,
        feature_store_dir=store_dir,
        output_dir=output_dir,
        symbol="XAUUSD",
    )

    assert result["labels_loaded"] == 5
    assert result["matched"] == 5
    assert result["unmatched"] == 0
    assert result["skipped_unlabeled"] == 0
    assert result["train_samples"] >= 1
    assert result["val_samples"] >= 1
    assert result["train_path"] is not None
    assert result["val_path"] is not None

    # Verify parquet files
    import pandas as pd

    train_df = pd.read_parquet(result["train_path"])
    assert len(train_df) == result["train_samples"]
    assert "f_0" in train_df.columns


def test_build_dataset_all_unlabeled(tmp_path: Path):
    from scripts.training.dataset_builder import build_dataset

    labels_content = "\n".join(
        json.dumps(
            {
                "schema_version": "training_label.v1",
                "label_id": f"L{i}",
                "symbol": "XAUUSDc",
                "side": "long",
                "label": "unlabeled",
                "pnl": None,
                "open_recorded_at": f"2026-05-0{i}T10:00:00Z",
            }
        )
        for i in range(1, 4)
    )
    labels_path = tmp_path / "labels.jsonl"
    labels_path.write_text(labels_content, encoding="utf-8")

    result = build_dataset(
        labels_path=labels_path,
        feature_store_dir=tmp_path / "empty_store",
        output_dir=tmp_path / "output",
    )

    assert result["skipped_unlabeled"] == 3
    assert result["matched"] == 0
    assert result["train_path"] is None


def test_build_dataset_empty_labels(tmp_path: Path):
    from scripts.training.dataset_builder import build_dataset

    labels_path = tmp_path / "empty.jsonl"
    labels_path.write_text("", encoding="utf-8")

    result = build_dataset(
        labels_path=labels_path,
        feature_store_dir=tmp_path / "empty_store",
        output_dir=tmp_path / "output",
    )

    assert "error" in result
    assert result["error"] == "no_labels_found"
