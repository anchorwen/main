import json

from scripts.mt5_bridge_healthcheck import build_report, main


def _write_receipt(
    receipt_dir, *, date_key: str, target: str, message_id: str, ack_status: str, received_at: str
):
    target_dir = receipt_dir / date_key / target
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "message_id": message_id,
        "ack_status": ack_status,
        "received_at": received_at,
        "detail": {},
    }
    (target_dir / f"{message_id}.ack.json").write_text(json.dumps(payload), encoding="utf-8")


def test_mt5_bridge_healthcheck_report_ready_when_limits_ok(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.mt5_bridge_healthcheck._today_key", lambda: "2026-04-28")
    receipt_dir = tmp_path / "receipts"
    outbox_dir = tmp_path / "mt5_outbox"
    _write_receipt(
        receipt_dir,
        date_key="2026-04-28",
        target="exec_bridge",
        message_id="msg_001",
        ack_status="accepted",
        received_at="2026-04-28T10:30:00Z",
    )

    report = build_report(
        outbox_dir=str(outbox_dir),
        receipt_dir=str(receipt_dir),
        max_pending=10,
        max_rejected=0,
    )

    assert report["go_live_ready"] is True
    assert report["counts"]["accepted"] == 1
    assert report["counts"]["pending"] == 0


def test_mt5_bridge_healthcheck_fails_when_rejected_exceeds_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.mt5_bridge_healthcheck._today_key", lambda: "2026-04-28")
    receipt_dir = tmp_path / "receipts"
    outbox_dir = tmp_path / "mt5_outbox"
    _write_receipt(
        receipt_dir,
        date_key="2026-04-28",
        target="exec_bridge",
        message_id="msg_002",
        ack_status="rejected",
        received_at="2026-04-28T10:31:00Z",
    )

    report = build_report(
        outbox_dir=str(outbox_dir),
        receipt_dir=str(receipt_dir),
        max_pending=10,
        max_rejected=0,
    )

    assert report["go_live_ready"] is False
    assert report["checks"]["rejected_within_limit"] is False
    assert report["counts"]["rejected"] == 1
    assert len(report["rejected_samples"]) == 1


def test_mt5_bridge_healthcheck_main_writes_output(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.mt5_bridge_healthcheck._today_key", lambda: "2026-04-28")
    receipt_dir = tmp_path / "receipts"
    _write_receipt(
        receipt_dir,
        date_key="2026-04-28",
        target="exec_bridge",
        message_id="msg_003",
        ack_status="acknowledged",
        received_at="2026-04-28T10:32:00Z",
    )
    output = tmp_path / "reports" / "mt5_health.json"

    rc = main(
        [
            "--outbox-dir",
            str(tmp_path / "mt5_outbox"),
            "--receipt-dir",
            str(receipt_dir),
            "--output",
            str(output),
        ]
    )
    assert rc == 0
    assert output.exists()
