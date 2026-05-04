"""Smoke tests for shadow vs live compare report script."""

import json

from scripts.shadow_live_compare_report import build_report_payload


def test_shadow_live_compare_report_payload(tmp_path):
    journal = tmp_path / "live_trade_journal.jsonl"
    journal.write_text(
        '{"recorded_at": "2026-04-28T12:00:00Z", "ack_status": "accepted", "symbol": "XAUUSDc", "detail": {}}\n',
        encoding="utf-8",
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "scenario": "s1",
                        "symbol": "XAUUSDc",
                        "action": "open",
                        "dispatch_status": "sent",
                        "risk_status": "ok",
                    },
                ],
                "stats": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    payload = build_report_payload(
        date_key="2026-04-28",
        symbol="XAUUSDc",
        journal_path=str(journal),
        shadow_baseline_json=str(baseline),
    )
    assert payload["schema_version"] == "shadow_live_compare_report.v2"
    assert payload["live_execution_summary"]["total"] == 1
    assert payload["shadow_signal_summary"]["matched_rows"] == 1
    assert "parity_notes" in payload
