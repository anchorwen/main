"""Tests for unified live_dispatch_policy."""

import json
from pathlib import Path

from scripts.live_dispatch_policy import main


def _disabled_calendar(tmp_path: Path) -> Path:
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
    return cal


def test_policy_blocks_on_journal_quality(tmp_path):
    journal = tmp_path / "live_trade_journal.jsonl"
    records = [
        {"recorded_at": "2026-04-28T10:00:00Z", "ack_status": "rejected", "detail": {}},
        {"recorded_at": "2026-04-28T10:01:00Z", "ack_status": "rejected", "detail": {}},
    ]
    journal.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    flag = tmp_path / "live_dispatch_block.flag"
    cal = _disabled_calendar(tmp_path)

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
            "0",
            "--min-samples",
            "1",
            "--flag-path",
            str(flag),
            "--disable-market-calendar",
        ]
    )
    assert rc == 1
    assert flag.exists()
    payload = json.loads(flag.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "live_dispatch_block.v2"


def test_eval_only_does_not_write_or_clear_flag(tmp_path):
    journal = tmp_path / "live_trade_journal.jsonl"
    journal.write_text("", encoding="utf-8")
    flag = tmp_path / "live_dispatch_block.flag"
    flag.write_text('{"blocked": true}', encoding="utf-8")
    cal = _disabled_calendar(tmp_path)

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
            "--flag-path",
            str(flag),
            "--eval-only",
        ]
    )
    assert rc == 0
    assert flag.exists()
    assert flag.read_text(encoding="utf-8") == '{"blocked": true}'


def test_policy_clears_flag_when_clean(tmp_path):
    journal = tmp_path / "live_trade_journal.jsonl"
    journal.write_text("", encoding="utf-8")
    flag = tmp_path / "live_dispatch_block.flag"
    flag.write_text('{"old": true}', encoding="utf-8")
    cal = _disabled_calendar(tmp_path)

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
            "--flag-path",
            str(flag),
        ]
    )
    assert rc == 0
    assert not flag.exists()
