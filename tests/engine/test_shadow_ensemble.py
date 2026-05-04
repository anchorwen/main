"""Shadow ensemble contract tests."""

import json
from pathlib import Path

from scripts.live_shadow_ensemble import (
    _compare_directions,
    _discover_brain_entries,
)


def test_discover_brain_entries_basic(tmp_path: Path):
    # Write a brain entry (not normalization)
    entry = {
        "schema_version": "brain_registry_entry.v1",
        "brain_id": "TestBrain_01",
        "brain_type": "onnx_v9",
        "brain_role": "alpha_brain",
        "model_version": "v9.0",
        "status": "shadow",
        "artifact_path": str(tmp_path / "model.onnx"),
        "feature_schema_id": "v9_institutional_40",
        "normalization_config_path": str(tmp_path / "norm.json"),
    }
    (tmp_path / "TestBrain_01.json").write_text(json.dumps(entry), encoding="utf-8")
    # Write a normalization config (should be skipped)
    (tmp_path / "norm.normalization.json").write_text('{"mean": {}, "std": {}}', encoding="utf-8")
    entries = _discover_brain_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["brain_id"] == "TestBrain_01"


def test_discover_brain_entries_filter_by_id(tmp_path: Path):
    for bid in ["Brain_A", "Brain_B", "Brain_C"]:
        entry = {
            "schema_version": "brain_registry_entry.v1",
            "brain_id": bid,
            "brain_type": "onnx_v9",
            "brain_role": "alpha_brain",
            "model_version": "v9.0",
            "status": "shadow",
            "artifact_path": str(tmp_path / f"{bid}.onnx"),
        }
        (tmp_path / f"{bid}.json").write_text(json.dumps(entry), encoding="utf-8")
    entries = _discover_brain_entries(tmp_path, brain_ids=["Brain_A", "Brain_C"])
    assert len(entries) == 2
    ids = {e["brain_id"] for e in entries}
    assert ids == {"Brain_A", "Brain_C"}


def test_compare_directions_consensus_long():
    results = [
        {
            "brain_id": "A",
            "status": "ok",
            "direction_bias": "long",
            "up_probability": 0.7,
            "down_probability": 0.3,
            "confidence": 0.8,
        },
        {
            "brain_id": "B",
            "status": "ok",
            "direction_bias": "long",
            "up_probability": 0.6,
            "down_probability": 0.4,
            "confidence": 0.6,
        },
        {
            "brain_id": "C",
            "status": "ok",
            "direction_bias": "neutral",
            "up_probability": 0.5,
            "down_probability": 0.5,
            "confidence": 0.2,
        },
    ]
    c = _compare_directions(results)
    assert c["consensus"] == "long"
    assert c["long_count"] == 2
    assert c["neutral_count"] == 1
    assert abs(c["agreement_score"] - 2 / 3) < 0.001


def test_compare_directions_split():
    results = [
        {
            "brain_id": "A",
            "status": "ok",
            "direction_bias": "long",
            "up_probability": 0.7,
            "down_probability": 0.3,
            "confidence": 0.8,
        },
        {
            "brain_id": "B",
            "status": "ok",
            "direction_bias": "short",
            "up_probability": 0.3,
            "down_probability": 0.7,
            "confidence": 0.8,
        },
    ]
    c = _compare_directions(results)
    assert c["consensus"] == "split"
    assert c["agreement_score"] == 0.5


def test_compare_directions_all_neutral():
    results = [
        {
            "brain_id": "A",
            "status": "ok",
            "direction_bias": "neutral",
            "up_probability": 0.5,
            "down_probability": 0.5,
            "confidence": 0.2,
        },
        {
            "brain_id": "B",
            "status": "ok",
            "direction_bias": "neutral",
            "up_probability": 0.5,
            "down_probability": 0.5,
            "confidence": 0.1,
        },
    ]
    c = _compare_directions(results)
    assert c["consensus"] == "neutral"


def test_compare_directions_no_results():
    c = _compare_directions([])
    assert c["consensus"] == "no_results"
