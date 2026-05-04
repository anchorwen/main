"""Journal schema validator contract tests."""

import json
from pathlib import Path

from scripts.validators.journal_validator import (
    validate_journal_file,
    validate_journal_record,
)


def test_valid_record_passes():
    rec = {
        "schema_version": "live_trade_journal.v2",
        "recorded_at": "2026-05-04T10:00:00Z",
        "message_id": "test_001",
        "target": "exec_bridge",
        "ack_status": "accepted",
        "symbol": "XAUUSDc",
        "action": "open",
        "side": "long",
    }
    ok, errs = validate_journal_record(rec)
    assert ok
    assert len(errs) == 0


def test_missing_required_fields():
    rec = {"message_id": "bare"}
    ok, errs = validate_journal_record(rec)
    assert not ok
    assert any("missing required field" in e for e in errs)


def test_invalid_ack_status():
    rec = {
        "schema_version": "live_trade_journal.v2",
        "recorded_at": "2026-05-04T10:00:00Z",
        "message_id": "test_002",
        "target": "exec_bridge",
        "ack_status": "bogus_status",
        "symbol": "XAUUSDc",
        "action": "open",
        "side": "long",
    }
    ok, errs = validate_journal_record(rec)
    assert not ok
    assert any("bogus_status" in e for e in errs)


def test_invalid_side():
    rec = {
        "schema_version": "live_trade_journal.v2",
        "recorded_at": "2026-05-04T10:00:00Z",
        "message_id": "test_003",
        "target": "exec_bridge",
        "ack_status": "accepted",
        "symbol": "XAUUSDc",
        "action": "open",
        "side": "diagonal",
    }
    ok, errs = validate_journal_record(rec)
    assert not ok
    assert any("diagonal" in e for e in errs)


def test_validate_journal_file_basic(tmp_path: Path):
    jl = tmp_path / "journal.jsonl"
    valid = json.dumps(
        {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": "2026-05-04T10:00:00Z",
            "message_id": "ok_001",
            "target": "exec_bridge",
            "ack_status": "accepted",
            "symbol": "XAUUSDc",
            "action": "open",
            "side": "long",
        }
    )
    invalid = json.dumps({"message_id": "bad_001"})
    jl.write_text(valid + "\n" + invalid + "\n", encoding="utf-8")
    report = validate_journal_file(jl)
    assert report["valid"] == 1
    assert report["invalid"] == 1
    assert report["total_records"] == 2


def test_validate_journal_file_missing():
    report = validate_journal_file(Path("/nonexistent/journal.jsonl"))
    assert report["exists"] is False
    assert report["total_records"] == 0
