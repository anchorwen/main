import json

from scripts.runtime_protection_guard import evaluate_guard, main


def test_runtime_protection_guard_triggers_on_consecutive_rejections(tmp_path):
    journal = tmp_path / "live_trade_journal.jsonl"
    records = [
        {"recorded_at": "2026-04-28T10:00:00Z", "ack_status": "accepted", "detail": {}},
        {
            "recorded_at": "2026-04-28T10:01:00Z",
            "ack_status": "rejected",
            "detail": {"reason": "x"},
        },
        {
            "recorded_at": "2026-04-28T10:02:00Z",
            "ack_status": "rejected",
            "detail": {"reason": "y"},
        },
    ]
    journal.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    flag = tmp_path / "live_dispatch_block.flag"
    cal = tmp_path / "market_calendar.json"
    cal.write_text(
        json.dumps(
            {
                "schema_version": "market_calendar.v1",
                "weekly_blackout": {"utc_weekend": False},
                "fixed_blackouts": [],
                "session_buffers": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--base-dir",
            str(tmp_path),
            "--journal-path",
            str(journal),
            "--calendar-path",
            str(cal),
            "--date",
            "2026-04-28",
            "--max-consecutive-rejected",
            "1",
            "--min-samples",
            "1",
            "--flag-path",
            str(flag),
        ]
    )

    assert rc == 1
    assert flag.exists()


def test_evaluate_guard_not_triggered_with_clean_data():
    triggered, reasons = evaluate_guard(
        report={
            "total": 5,
            "counts": {"rejected": 0},
            "rejection_rate": 0.0,
            "latest_records": [{"ack_status": "accepted"}],
        },
        max_rejection_rate=0.2,
        max_rejections=1,
        max_consecutive_rejected=1,
        min_samples=3,
    )
    assert triggered is False
    assert reasons == []
