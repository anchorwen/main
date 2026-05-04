import json

from scripts.trade_quality_report import build_report


def test_trade_quality_report_aggregates_counts(tmp_path):
    journal = tmp_path / "live_trade_journal.jsonl"
    records = [
        {"recorded_at": "2026-04-28T10:00:00Z", "ack_status": "accepted", "detail": {}},
        {
            "recorded_at": "2026-04-28T10:01:00Z",
            "ack_status": "rejected",
            "detail": {"reason": "symbol_not_found"},
        },
        {"recorded_at": "2026-04-28T10:02:00Z", "ack_status": "acknowledged", "detail": {}},
    ]
    journal.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    report = build_report(journal_path=str(journal), date_key="2026-04-28")

    assert report["total"] == 3
    assert report["counts"]["accepted"] == 1
    assert report["counts"]["rejected"] == 1
    assert report["counts"]["acknowledged"] == 1
    assert report["rejected_reasons"]["symbol_not_found"] == 1
    assert report["live_consecutive_rejected_tail"] == 0


def test_trade_quality_report_symbol_filter(tmp_path):
    journal = tmp_path / "live_trade_journal.jsonl"
    records = [
        {
            "recorded_at": "2026-04-28T10:00:00Z",
            "ack_status": "accepted",
            "symbol": "XAUUSDc",
            "detail": {},
        },
        {
            "recorded_at": "2026-04-28T10:01:00Z",
            "ack_status": "rejected",
            "symbol": "EURUSD",
            "detail": {},
        },
    ]
    journal.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    report = build_report(journal_path=str(journal), date_key="2026-04-28", symbol="XAUUSDc")

    assert report["total"] == 1
    assert report["counts"]["accepted"] == 1


def test_trade_quality_report_consecutive_rejected_tail(tmp_path):
    journal = tmp_path / "live_trade_journal.jsonl"
    records = [
        {"recorded_at": "2026-04-28T10:00:00Z", "ack_status": "accepted", "detail": {}},
        {"recorded_at": "2026-04-28T10:01:00Z", "ack_status": "rejected", "detail": {}},
        {"recorded_at": "2026-04-28T10:02:00Z", "ack_status": "rejected", "detail": {}},
        {"recorded_at": "2026-04-28T10:03:00Z", "ack_status": "rejected", "detail": {}},
    ]
    journal.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    report = build_report(journal_path=str(journal), date_key="2026-04-28")
    assert report["live_consecutive_rejected_tail"] == 3
