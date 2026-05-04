"""Feature quality report pipeline contract tests."""

import json
from pathlib import Path

from scripts.live_feature_quality_report import (
    _read_feature_vectors,
    build_report,
    main,
)
from scripts.validators.feature_quality_validator import FEATURE_NAMES


def _write_store_data(store_dir: Path, *, symbol: str = "XAUUSD", timeframe: str = "M5") -> Path:
    """Write synthetic feature records to a LocalFeatureStore layout."""
    records_dir = store_dir / "records" / f"symbol={symbol}" / f"timeframe={timeframe}"
    records_dir.mkdir(parents=True, exist_ok=True)
    records_path = records_dir / "features.jsonl"

    records = []
    for i in range(20):
        values = {name: round(i * 0.001 + (hash(name) % 100) * 0.0001, 6) for name in FEATURE_NAMES}
        rec = {
            "schema_name": "v9_institutional_40",
            "schema_version": "1.0.0",
            "symbol": symbol,
            "timeframe": timeframe,
            "event_time": f"2026-05-04T{i:02d}:00:00Z",
            "values": values,
            "source": "v9_live_computer",
            "ingested_at": f"2026-05-04T1{i:02d}:01:00Z",
        }
        records.append(rec)

    records_path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return records_path


def test_read_feature_vectors(tmp_path: Path):
    _write_store_data(tmp_path)
    vectors = _read_feature_vectors(tmp_path, max_samples=10)
    assert len(vectors) == 10
    assert vectors[0].shape[0] == 40
    assert all(v.dtype == np.float64 for v in vectors)


def test_read_feature_vectors_empty_store(tmp_path: Path):
    vectors = _read_feature_vectors(tmp_path)
    assert len(vectors) == 0


def test_read_feature_vectors_date_filter(tmp_path: Path):
    _write_store_data(tmp_path)
    vectors = _read_feature_vectors(tmp_path, date_filter="2026-05-04T10")
    assert len(vectors) == 1


def test_build_report_no_store(tmp_path: Path):
    report = build_report(tmp_path / "nonexistent")
    assert report["sample_size"] == 0
    assert "error" in report


def test_build_report_with_data(tmp_path: Path):
    _write_store_data(tmp_path)
    report = build_report(tmp_path)
    assert report["schema_version"] == "feature_quality_report.v1"
    assert report["sample_size"] == 20
    assert report["quality"]["total_vectors"] == 20
    assert report["quality"]["valid_vectors"] == 20
    assert report["severity"] == "ok"
    assert len(report["per_feature_stats"]) == 40


def test_build_report_with_norm_shift(tmp_path: Path):
    _write_store_data(tmp_path)
    norm_path = tmp_path / "norm.json"
    norm = {
        "schema_version": "brain_normalization.v1",
        "brain_id": "test",
        "feature_schema_id": "v9_institutional_40",
        "mean": [10.0] * 40,
        "std": [0.0001] * 40,
    }
    norm_path.write_text(json.dumps(norm))
    report = build_report(tmp_path, norm_config_path=norm_path, shift_threshold=2.0)
    assert report["distribution_shift"]["shift_detected"] is True


def test_build_report_with_zero_vector(tmp_path: Path):
    """Write a mix of normal and zero vectors."""
    records_dir = tmp_path / "records" / "symbol=XAUUSD" / "timeframe=M5"
    records_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for i in range(10):
        if i < 3:
            values = {name: 0.0 for name in FEATURE_NAMES}
        else:
            values = {name: round(i * 0.01, 6) for name in FEATURE_NAMES}
        records.append(
            {
                "schema_name": "v9_institutional_40",
                "schema_version": "1.0.0",
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "event_time": f"2026-05-04T{i:02d}:00:00Z",
                "values": values,
                "source": "v9_live_computer",
                "ingested_at": f"2026-05-04T{i:02d}:01:00Z",
            }
        )
    (records_dir / "features.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    report = build_report(tmp_path)
    assert report["quality"]["zero_vectors"] == 3
    assert report["quality"]["valid_vectors"] == 7
    assert "zero_vectors" in str(report["issues"])


def test_build_report_json_output(tmp_path: Path):
    _write_store_data(tmp_path)
    out = tmp_path / "report.json"
    ret = main(["--store-dir", str(tmp_path), "--output", str(out)])
    assert ret == 0
    assert out.exists()
    report = json.loads(out.read_text())
    assert report["schema_version"] == "feature_quality_report.v1"


def test_build_report_severity_critical(tmp_path: Path):
    """All-zero vectors produce valid_rate=0.0 → severity=critical."""
    records_dir = tmp_path / "records" / "symbol=XAUUSD" / "timeframe=M5"
    records_dir.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "schema_name": "v9_institutional_40",
            "schema_version": "1.0.0",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "event_time": "2026-05-04T10:00:00Z",
            "values": {name: 0.0 for name in FEATURE_NAMES},
            "source": "fallback",
            "ingested_at": "2026-05-04T10:00:01Z",
        }
    ]
    (records_dir / "features.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    report = build_report(tmp_path)
    assert report["severity"] == "critical"
    assert report["quality"]["valid_rate"] == 0.0


def test_cli_help():
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "scripts/live_feature_quality_report.py", "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "feature_quality_report" in proc.stdout.lower() or "store-dir" in proc.stdout


import numpy as np
