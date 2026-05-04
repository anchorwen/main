"""Tests for live_monitor module."""

import json
import os
import time
from pathlib import Path

# ── _check_circuit_breaker tests ──


def test_circuit_breaker_no_flag(tmp_path: Path):
    from scripts.live_monitor import _check_circuit_breaker

    flag_path = tmp_path / "no_such_flag.flag"
    result = _check_circuit_breaker(flag_path)
    assert result["blocked"] is False
    assert result["flag_exists"] is False
    assert result["reasons"] == []


def test_circuit_breaker_blocked(tmp_path: Path):
    from scripts.live_monitor import _check_circuit_breaker

    flag_path = tmp_path / "live_dispatch_block.flag"
    flag_path.write_text(
        json.dumps(
            {
                "schema_version": "live_dispatch_block.v2",
                "blocked": True,
                "reasons": ["spread_too_wide", "rejection_rate_high"],
                "sources": {"spread_probe": {"spread_points": 35}},
            }
        ),
        encoding="utf-8",
    )

    result = _check_circuit_breaker(flag_path)
    assert result["blocked"] is True
    assert result["flag_exists"] is True
    assert "spread_too_wide" in result["reasons"]
    assert result["flag_age_seconds"] is not None


def test_circuit_breaker_not_blocked(tmp_path: Path):
    from scripts.live_monitor import _check_circuit_breaker

    flag_path = tmp_path / "live_dispatch_block.flag"
    flag_path.write_text(
        json.dumps({"schema_version": "v2", "blocked": False, "reasons": []}),
        encoding="utf-8",
    )

    result = _check_circuit_breaker(flag_path)
    assert result["blocked"] is False


def test_circuit_breaker_corrupt_flag(tmp_path: Path):
    from scripts.live_monitor import _check_circuit_breaker

    flag_path = tmp_path / "live_dispatch_block.flag"
    flag_path.write_text("not json", encoding="utf-8")

    result = _check_circuit_breaker(flag_path)
    # Flag present but unreadable → blocked
    assert result["blocked"] is True


# ── _check_trade_quality tests ──


def _make_journal_line(ack_status, recorded_at, action="open", side="long"):
    return json.dumps(
        {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": recorded_at,
            "ack_status": ack_status,
            "action": action,
            "side": side,
            "symbol": "XAUUSDc",
            "message_id": f"msg_{ack_status}",
            "position_ticket": 12345,
        }
    )


def test_trade_quality_counts(tmp_path: Path):
    from scripts.live_monitor import _check_trade_quality

    journal = tmp_path / "journal.jsonl"
    now_iso = "2026-05-04T14:00:00Z"
    lines = [
        _make_journal_line("accepted", now_iso),
        _make_journal_line("accepted", now_iso),
        _make_journal_line("rejected", now_iso),
        _make_journal_line("rejected", now_iso),
        _make_journal_line("rejected", now_iso),
        _make_journal_line("accepted", now_iso),
    ]
    journal.write_text("\n".join(lines), encoding="utf-8")

    result = _check_trade_quality(journal, lookback_hours=24.0)
    assert result["recent_total"] == 6
    assert result["accepted"] == 3
    assert result["rejected"] == 3
    assert result["rejection_rate"] == 0.5


def test_trade_quality_empty_journal(tmp_path: Path):
    from scripts.live_monitor import _check_trade_quality

    journal = tmp_path / "nonexistent.jsonl"
    result = _check_trade_quality(journal)
    assert result["recent_total"] == 0


def test_trade_quality_tail_consecutive_rejected(tmp_path: Path):
    from scripts.live_monitor import _check_trade_quality

    journal = tmp_path / "journal.jsonl"
    now = "2026-05-04T14:00:00Z"
    lines = [
        _make_journal_line("accepted", now),
        _make_journal_line("rejected", now),
        _make_journal_line("rejected", now),
        _make_journal_line("rejected", now),
    ]
    journal.write_text("\n".join(lines), encoding="utf-8")

    result = _check_trade_quality(journal, lookback_hours=24.0)
    assert result["tail_consecutive_rejected"] == 3


def test_trade_quality_outside_lookback(tmp_path: Path):
    from scripts.live_monitor import _check_trade_quality

    journal = tmp_path / "journal.jsonl"
    old = "2026-04-01T00:00:00Z"
    journal.write_text(_make_journal_line("accepted", old), encoding="utf-8")

    # lookback 1 hour, entry is days old
    result = _check_trade_quality(journal, lookback_hours=1.0)
    assert result["recent_total"] == 0


# ── _check_bridge tests ──


def test_bridge_empty(tmp_path: Path):
    from scripts.live_monitor import _check_bridge

    result = _check_bridge(tmp_path / "no", tmp_path / "no", tmp_path / "no.log")
    assert result["outbox_pending"] == 0
    assert result["receipt_total"] == 0
    assert result["bridge_alive"] is False


def test_bridge_outbox_pending(tmp_path: Path):
    from scripts.live_monitor import _check_bridge

    outbox = tmp_path / "outbox"
    outbox.mkdir()
    for i in range(3):
        (outbox / f"intent_{i}.mt5.json").write_text("{}", encoding="utf-8")

    result = _check_bridge(outbox, tmp_path / "receipts", tmp_path / "bridge.log")
    assert result["outbox_pending"] == 3
    assert result["outbox_stale_count"] == 0


def test_bridge_outbox_stale(tmp_path: Path):
    from scripts.live_monitor import _check_bridge

    outbox = tmp_path / "outbox"
    outbox.mkdir()
    p = outbox / "old.mt5.json"
    p.write_text("{}", encoding="utf-8")
    # Set mtime to 20 minutes ago
    stale_time = time.time() - 1200
    os.utime(str(p), (stale_time, stale_time))

    result = _check_bridge(outbox, tmp_path / "receipts", tmp_path / "bridge.log")
    assert result["outbox_stale_count"] == 1


def test_bridge_log_freshness(tmp_path: Path):
    from scripts.live_monitor import _check_bridge

    log = tmp_path / "bridge.log"
    log.write_text("running", encoding="utf-8")

    result = _check_bridge(tmp_path / "outbox", tmp_path / "receipts", log)
    assert result["bridge_alive"] is True
    assert result["bridge_log_age_seconds"] is not None


# ── _check_brains tests ──


def test_brains_empty(tmp_path: Path):
    from scripts.live_monitor import _check_brains

    result = _check_brains(tmp_path / "nonexistent")
    assert result["active_brains"] == []
    assert result["recent_decision_count"] == 0


def test_brains_from_decisions(tmp_path: Path):
    from datetime import UTC, datetime

    from scripts.live_monitor import _check_brains

    today = datetime.now(UTC).replace(tzinfo=None).date().isoformat()
    decisions_dir = tmp_path
    date_dir = decisions_dir / today
    date_dir.mkdir()
    decision_file = date_dir / "XAUUSD.decisions.jsonl"
    decision_file.write_text(
        json.dumps(
            {
                "schema_version": "decision_record.v1",
                "record_id": "r1",
                "attribution": {
                    "supporting_brains": ["V9", "XGB"],
                    "opposing_brains": ["OU"],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = _check_brains(decisions_dir)
    assert result["recent_decision_count"] == 1
    assert set(result["active_brains"]) == {"V9", "XGB", "OU"}


# ── _check_shadow_alignment tests ──


def test_shadow_both_silent(tmp_path: Path):
    from scripts.live_monitor import _check_shadow_alignment

    result = _check_shadow_alignment(tmp_path / "shadow_outbox", tmp_path / "live_outbox")
    assert result["alignment"] == "both_silent"


def test_shadow_active_live_silent(tmp_path: Path):
    from scripts.live_monitor import _check_shadow_alignment

    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "intent.mt5.json").write_text("{}", encoding="utf-8")

    result = _check_shadow_alignment(shadow, tmp_path / "live")
    assert result["alignment"] == "shadow_active_live_silent"
    assert result["shadow_intent_count"] == 1


def test_shadow_both_active(tmp_path: Path):
    from scripts.live_monitor import _check_shadow_alignment

    shadow = tmp_path / "shadow"
    live = tmp_path / "live"
    shadow.mkdir()
    live.mkdir()
    (shadow / "s.mt5.json").write_text("{}", encoding="utf-8")
    (live / "l.mt5.json").write_text("{}", encoding="utf-8")

    result = _check_shadow_alignment(shadow, live)
    assert result["alignment"] == "both_active"


# ── _derive_alerts tests ──


def test_derive_alerts_critical_breaker():
    from scripts.live_monitor import _derive_alerts

    breaker = {"blocked": True, "flag_age_seconds": 300, "reasons": ["test"]}
    trade = {"recent_total": 0, "rejection_rate": 0.0, "tail_consecutive_rejected": 0}
    bridge = {"outbox_stale_count": 0, "bridge_alive": True, "bridge_log_age_seconds": 5.0}
    positions = {"available": False}
    brains = {"active_brains": ["V9", "XGB"]}
    shadow = {"alignment": "both_active"}

    level, alerts = _derive_alerts(breaker, trade, bridge, positions, brains, shadow)
    assert level == "CRITICAL"
    assert any(a["component"] == "circuit_breaker" and a["level"] == "CRITICAL" for a in alerts)


def test_derive_alerts_ok():
    from scripts.live_monitor import _derive_alerts

    breaker = {"blocked": False}
    trade = {"recent_total": 10, "rejection_rate": 0.1, "tail_consecutive_rejected": 0}
    bridge = {"outbox_stale_count": 0, "bridge_alive": True, "bridge_log_age_seconds": 10.0}
    positions = {"available": False}
    brains = {"active_brains": ["V9", "XGB", "OU"]}
    shadow = {"alignment": "both_active"}

    level, alerts = _derive_alerts(breaker, trade, bridge, positions, brains, shadow)
    assert level == "OK"
    assert alerts == []


def test_derive_alerts_high_rejection():
    from scripts.live_monitor import _derive_alerts

    breaker = {"blocked": False}
    trade = {"recent_total": 6, "rejection_rate": 0.6, "tail_consecutive_rejected": 4}
    bridge = {"outbox_stale_count": 0, "bridge_alive": True, "bridge_log_age_seconds": 5.0}
    positions = {"available": False}
    brains = {"active_brains": ["V9"]}
    shadow = {"alignment": "both_silent"}

    level, alerts = _derive_alerts(breaker, trade, bridge, positions, brains, shadow)
    assert level == "CRITICAL"
    assert any(a["component"] == "trade_quality" for a in alerts)


def test_derive_alerts_bridge_stale():
    from scripts.live_monitor import _derive_alerts

    breaker = {"blocked": False}
    trade = {"recent_total": 5, "rejection_rate": 0.1, "tail_consecutive_rejected": 0}
    bridge = {"outbox_stale_count": 6, "bridge_alive": False}
    positions = {"available": False}
    brains = {"active_brains": ["V9", "XGB"]}
    shadow = {"alignment": "both_active"}

    level, alerts = _derive_alerts(breaker, trade, bridge, positions, brains, shadow)
    assert level == "CRITICAL"
    assert any(a["component"] == "bridge" for a in alerts)


# ── build_snapshot integration test ──


def test_build_snapshot_integration(tmp_path: Path):
    from scripts.live_monitor import build_snapshot

    base = tmp_path
    # Create a journal with recent entries
    journal = base / "live_trade_journal.jsonl"
    journal.write_text(
        json.dumps(
            {
                "schema_version": "v2",
                "recorded_at": "2026-05-04T14:00:00Z",
                "ack_status": "accepted",
                "action": "open",
                "side": "long",
                "symbol": "XAUUSDc",
                "message_id": "msg_1",
                "position_ticket": 100,
            }
        )
        + "\n"
        + json.dumps(
            {
                "schema_version": "v2",
                "recorded_at": "2026-05-04T14:01:00Z",
                "ack_status": "rejected",
                "action": "open",
                "side": "short",
                "symbol": "XAUUSDc",
                "message_id": "msg_2",
                "position_ticket": 101,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = build_snapshot(base, symbol="XAUUSDc", lookback_hours=24000.0)

    assert snapshot["event"] == "monitor_snapshot"
    assert snapshot["schema_version"] == "live_monitor.v1"
    assert snapshot["symbol"] == "XAUUSDc"
    assert snapshot["alert_level"] in ("OK", "WARNING", "CRITICAL")
    assert "circuit_breaker" in snapshot["components"]
    assert "trade_quality" in snapshot["components"]
    assert "bridge" in snapshot["components"]
    assert "brains" in snapshot["components"]
    assert "shadow_alignment" in snapshot["components"]
    assert snapshot["components"]["trade_quality"]["recent_total"] == 2
    assert snapshot["components"]["trade_quality"]["accepted"] == 1
    assert snapshot["components"]["trade_quality"]["rejected"] == 1
    assert snapshot["components"]["circuit_breaker"]["blocked"] is False
