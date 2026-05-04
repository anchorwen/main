import json

from scripts.mt5_bridge_worker import _coerce_positive_float, main, process_one


def _write_outbox_message(
    outbox_dir, *, date_key: str, target: str, message_id: str, symbol: str = "XAUUSD"
):
    target_dir = outbox_dir / date_key / target
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "request": {"dispatch_id": "dispatch_001", "requested_at": "2026-04-28T10:00:00Z"},
        "envelope": {
            "message_id": message_id,
            "target": target,
            "payload": {"symbol": symbol, "action": "open", "side": "long"},
        },
        "mt5": {"terminal_path": "D:/MetaTrader 5/terminal64.exe"},
    }
    file_path = target_dir / f"{message_id}.mt5.json"
    file_path.write_text(json.dumps(payload), encoding="utf-8")
    return file_path


def test_mt5_bridge_process_one_writes_ack_and_archives_in_dry_run(tmp_path):
    outbox_dir = tmp_path / "mt5_outbox"
    receipt_dir = tmp_path / "receipts"
    archive_dir = tmp_path / "archive"
    journal_path = tmp_path / "live_trade_journal.jsonl"
    protection_flag_path = tmp_path / "live_dispatch_block.flag"
    message_path = _write_outbox_message(
        outbox_dir,
        date_key="2026-04-28",
        target="exec_bridge",
        message_id="message_001",
    )

    result = process_one(
        message_path,
        outbox_dir=outbox_dir,
        receipt_dir=receipt_dir,
        archive_dir=archive_dir,
        journal_path=journal_path,
        protection_flag_path=protection_flag_path,
        default_volume=0.01,
        deviation=20,
        magic=90001,
        dry_run=True,
    )

    assert result["ack_status"] == "acknowledged"
    receipt_path = receipt_dir / "2026-04-28" / "exec_bridge" / "message_001.ack.json"
    assert receipt_path.exists()
    receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt_payload["ack_status"] == "acknowledged"
    assert receipt_payload["detail"]["reason"] == "dry_run"
    assert not message_path.exists()
    assert (archive_dir / "2026-04-28" / "exec_bridge" / "message_001.mt5.json").exists()
    journal_lines = journal_path.read_text(encoding="utf-8").splitlines()
    assert len(journal_lines) == 1
    journal_payload = json.loads(journal_lines[0])
    assert journal_payload["message_id"] == "message_001"
    assert journal_payload["ack_status"] == "acknowledged"


def test_mt5_bridge_main_once_processes_pending_messages(tmp_path):
    outbox_dir = tmp_path / "mt5_outbox"
    receipt_dir = tmp_path / "receipts"
    archive_dir = tmp_path / "archive"
    journal_path = tmp_path / "journal.jsonl"
    _write_outbox_message(
        outbox_dir,
        date_key="2026-04-28",
        target="exec_bridge",
        message_id="message_002",
    )

    rc = main(
        [
            "--outbox-dir",
            str(outbox_dir),
            "--receipt-dir",
            str(receipt_dir),
            "--archive-dir",
            str(archive_dir),
            "--journal-path",
            str(journal_path),
            "--once",
            "--dry-run",
        ]
    )

    assert rc == 0
    assert (receipt_dir / "2026-04-28" / "exec_bridge" / "message_002.ack.json").exists()
    assert journal_path.exists()


def test_coerce_positive_float_handles_invalid_values():
    assert _coerce_positive_float(None) is None
    assert _coerce_positive_float("abc") is None
    assert _coerce_positive_float(0) is None
    assert _coerce_positive_float(-1) is None
    assert _coerce_positive_float("1.25") == 1.25


def test_mt5_bridge_respects_protection_flag(tmp_path):
    outbox_dir = tmp_path / "mt5_outbox"
    receipt_dir = tmp_path / "receipts"
    archive_dir = tmp_path / "archive"
    journal_path = tmp_path / "journal.jsonl"
    protection_flag_path = tmp_path / "live_dispatch_block.flag"
    protection_flag_path.write_text("{}", encoding="utf-8")
    message_path = _write_outbox_message(
        outbox_dir,
        date_key="2026-04-28",
        target="exec_bridge",
        message_id="message_003",
    )
    result = process_one(
        message_path,
        outbox_dir=outbox_dir,
        receipt_dir=receipt_dir,
        archive_dir=archive_dir,
        journal_path=journal_path,
        protection_flag_path=protection_flag_path,
        default_volume=0.01,
        deviation=20,
        magic=90001,
        dry_run=False,
    )
    assert result["ack_status"] == "rejected"
    receipt_payload = json.loads(
        (receipt_dir / "2026-04-28" / "exec_bridge" / "message_003.ack.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt_payload["detail"]["reason"] == "protection_guard_active"
