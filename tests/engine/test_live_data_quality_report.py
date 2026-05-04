"""Data quality report contract tests."""

from scripts.live_data_quality_report import (
    _latency_check,
    _parse_iso_to_epoch,
    _receipt_format_issues,
    _status_consistency,
)


def test_status_consistency_ok():
    assert _status_consistency("accepted", "accepted") == "ok(accepted)"


def test_status_consistency_mismatch():
    result = _status_consistency("accepted", "rejected")
    assert "mismatch" in result
    assert "accepted" in result
    assert "rejected" in result


def test_status_consistency_missing():
    assert _status_consistency(None, None) == "both_missing"
    assert _status_consistency(None, "accepted") == "journal_missing"
    assert _status_consistency("accepted", None) == "receipt_missing"


def test_parse_iso_to_epoch_valid():
    ts = _parse_iso_to_epoch("2026-05-04T10:00:00Z")
    assert ts is not None
    assert ts > 0


def test_parse_iso_to_epoch_none():
    assert _parse_iso_to_epoch(None) is None
    assert _parse_iso_to_epoch("") is None


def test_latency_check_within_threshold():
    result = _latency_check(
        "2026-05-04T10:00:00Z",
        "2026-05-04T10:00:10Z",
        max_latency_seconds=30.0,
    )
    assert result["latency_seconds"] == 10.0
    assert result["exceeds_threshold"] is False


def test_latency_check_exceeds_threshold():
    result = _latency_check(
        "2026-05-04T10:00:00Z",
        "2026-05-04T10:01:00Z",
        max_latency_seconds=30.0,
    )
    assert result["latency_seconds"] == 60.0
    assert result["exceeds_threshold"] is True


def test_latency_check_parse_error():
    result = _latency_check("bad_date", "2026-05-04T10:00:10Z")
    assert result.get("parse_error") is not None


def test_receipt_format_issues_error_field():
    issues = _receipt_format_issues({"error": "some_error", "ack_status": "rejected"})
    assert any("receipt_has_error_field" in i for i in issues)


def test_receipt_format_issues_missing_ack():
    issues = _receipt_format_issues({"message_id": "test", "received_at": "2026-05-04T10:00:00Z"})
    assert any("receipt_missing_ack_status" in i for i in issues)


def test_receipt_format_issues_none():
    assert _receipt_format_issues(None) == []
