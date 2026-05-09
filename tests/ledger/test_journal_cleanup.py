import json
from pathlib import Path

from core.ledger.services.journal_cleanup import cleanup_orphan_opens


def test_cleanup_orphan_rejected(tmp_path: Path):
    journal = tmp_path / "journal.jsonl"
    entries = [
        {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": "2026-04-29T05:03:43Z",
            "message_id": "open_001",
            "target": "exec_bridge",
            "ack_status": "rejected",
            "detail": {"reason": "order_send_failed"},
            "symbol": "XAUUSDc",
            "action": "open",
            "side": "long",
            "volume": 0.01,
            "position_ticket": None,
            "sl": 4500.0,
            "tp": 4600.0,
        },
    ]
    journal.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )

    count = cleanup_orphan_opens(journal, max_age_hours=24)
    assert count == 1

    lines = journal.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    close_entry = json.loads(lines[1])
    assert close_entry["action"] == "close"
    assert close_entry["open_message_id"] == "open_001"
    assert close_entry["label"] == "auto_orphan_rejected"
    assert close_entry["pnl"] == 0.0


def test_cleanup_stale_no_ticket(tmp_path: Path):
    journal = tmp_path / "journal.jsonl"
    entries = [
        {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": "2026-04-20T12:00:00Z",
            "message_id": "open_002",
            "target": "exec_bridge",
            "ack_status": "accepted",
            "symbol": "XAUUSDc",
            "action": "open",
            "side": "short",
            "volume": 0.01,
            "position_ticket": None,
            "sl": 4600.0,
            "tp": 4500.0,
        },
    ]
    journal.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )

    count = cleanup_orphan_opens(journal, max_age_hours=24)
    assert count == 1

    lines = journal.read_text(encoding="utf-8").strip().split("\n")
    close_entry = json.loads(lines[1])
    assert close_entry["label"] == "auto_orphan_stale"


def test_leaves_real_position_alone(tmp_path: Path):
    """Entries with a real position_ticket must NOT be auto-closed."""
    journal = tmp_path / "journal.jsonl"
    entries = [
        {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": "2026-05-06T15:04:06",
            "message_id": "open_real",
            "target": "exec_bridge",
            "ack_status": "accepted",
            "symbol": "XAUUSDc",
            "action": "open",
            "side": "long",
            "volume": 0.01,
            "position_ticket": 3355347361,
            "sl": 4691.0,
            "tp": 4724.0,
        },
    ]
    journal.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )

    count = cleanup_orphan_opens(journal, max_age_hours=0)
    assert count == 0


def test_idempotent(tmp_path: Path):
    """Running cleanup twice must not create duplicate close entries."""
    journal = tmp_path / "journal.jsonl"
    entries = [
        {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": "2026-04-29T05:03:43Z",
            "message_id": "open_003",
            "target": "exec_bridge",
            "ack_status": "rejected",
            "symbol": "XAUUSDc",
            "action": "open",
            "side": "long",
            "volume": 0.01,
            "position_ticket": None,
            "sl": 4500.0,
            "tp": 4600.0,
        },
    ]
    journal.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )

    count1 = cleanup_orphan_opens(journal, max_age_hours=24)
    assert count1 == 1
    count2 = cleanup_orphan_opens(journal, max_age_hours=24)
    assert count2 == 0

    lines = journal.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2


def test_cleanup_skips_already_closed(tmp_path: Path):
    """Entries that already have a close must not be double-closed."""
    journal = tmp_path / "journal.jsonl"
    entries = [
        {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": "2026-05-05T12:00:00",
            "message_id": "open_004",
            "target": "exec_bridge",
            "ack_status": "accepted",
            "symbol": "XAUUSDc",
            "action": "open",
            "side": "long",
            "volume": 0.01,
            "position_ticket": 123456,
            "sl": 4500.0,
            "tp": 4600.0,
        },
        {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": "2026-05-05T13:00:00",
            "message_id": "close_004",
            "target": "exec_bridge",
            "ack_status": "closed",
            "detail": {"reason": "tp_hit", "close_price": 4600.0, "pnl": 0.10},
            "symbol": "XAUUSDc",
            "action": "close",
            "side": "long",
            "volume": 0.01,
            "position_ticket": 123456,
            "open_message_id": "open_004",
        },
    ]
    journal.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )

    count = cleanup_orphan_opens(journal, max_age_hours=0)
    assert count == 0
