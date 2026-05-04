"""Label builder contract tests."""

import json
from pathlib import Path

from scripts.training.label_builder import (
    _classify_label,
    _compute_pnl,
    _extract_entry_price,
    build_basic_stats_report,
    build_trade_records,
)


def test_classify_label_win():
    assert _classify_label(10.5) == "win"


def test_classify_label_loss():
    assert _classify_label(-5.0) == "loss"


def test_classify_label_breakeven():
    assert _classify_label(0.0) == "breakeven"


def test_classify_label_unlabeled():
    assert _classify_label(None) == "unlabeled"


def test_compute_pnl_long():
    pnl = _compute_pnl("long", 100.0, 110.0, 1.0)
    assert pnl == 10.0


def test_compute_pnl_short():
    pnl = _compute_pnl("short", 110.0, 100.0, 1.0)
    assert pnl == 10.0


def test_compute_pnl_with_volume():
    pnl = _compute_pnl("long", 100.0, 105.0, 0.5)
    assert pnl == 2.5


def test_compute_pnl_missing_prices():
    assert _compute_pnl("long", None, 110.0, 1.0) is None
    assert _compute_pnl("long", 100.0, None, 1.0) is None


def test_extract_entry_price_from_order():
    detail = {"order": {"price": 123.45, "price_open": 120.0}}
    assert _extract_entry_price(detail) == 123.45


def test_extract_entry_price_none():
    assert _extract_entry_price(None) is None
    assert _extract_entry_price({}) is None


def test_build_trade_records_basic(tmp_path: Path):
    jp = tmp_path / "journal.jsonl"
    entries = [
        {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": "2026-05-04T10:00:00Z",
            "message_id": "open_001",
            "action": "open",
            "side": "long",
            "symbol": "XAUUSDc",
            "ack_status": "accepted",
            "position_ticket": 1001,
            "sl": 4590.0,
            "tp": 4630.0,
            "volume": 0.1,
            "detail": {"retcode": 10009, "order": {"price": 4610.0}},
        },
        {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": "2026-05-04T11:00:00Z",
            "message_id": "close_001",
            "action": "close",
            "side": "long",
            "symbol": "XAUUSDc",
            "ack_status": "accepted",
            "position_ticket": 1001,
            "volume": 0.1,
            "detail": {"retcode": 10009, "order": {"price": 4620.0}},
        },
    ]
    jp.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries),
        encoding="utf-8",
    )
    records = build_trade_records(jp)
    assert len(records) == 1
    r = records[0]
    assert r["position_ticket"] == 1001
    assert r["is_closed"] is True
    assert r["entry_price"] == 4610.0
    assert r["exit_price"] == 4620.0
    assert r["pnl"] == 1.0  # (4620-4610) * 0.1
    assert r["label"] == "win"


def test_build_trade_records_unlinked(tmp_path: Path):
    jp = tmp_path / "journal.jsonl"
    entries = [
        {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": "2026-05-04T10:00:00Z",
            "message_id": "open_nopos",
            "action": "open",
            "side": "short",
            "symbol": "XAUUSDc",
            "ack_status": "rejected",
            "position_ticket": None,
            "sl": 4600.0,
            "tp": 4560.0,
            "detail": {"reason": "order_send_failed"},
        },
    ]
    jp.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries),
        encoding="utf-8",
    )
    records = build_trade_records(jp)
    assert len(records) == 1
    r = records[0]
    assert r["position_ticket"] is None
    assert r["is_closed"] is False
    assert r["label"] == "unlabeled"
    assert r["open_ack_status"] == "rejected"


def test_build_basic_stats_report():
    records = [
        {"label": "win", "is_closed": True, "pnl": 10.0},
        {"label": "loss", "is_closed": True, "pnl": -5.0},
        {"label": "win", "is_closed": True, "pnl": 3.0},
        {"label": "unlabeled", "is_closed": False, "pnl": None},
    ]
    stats = build_basic_stats_report(records)
    assert stats["total_records"] == 4
    assert stats["closed_trades"] == 3
    assert stats["open_trades"] == 1
    assert stats["labels"]["win"] == 2
    assert stats["labels"]["loss"] == 1
    assert stats["labels"]["unlabeled"] == 1
    assert stats["pnl_summary"]["total_pnl"] == 8.0
    assert stats["pnl_summary"]["max_pnl"] == 10.0
    assert stats["pnl_summary"]["min_pnl"] == -5.0


def test_build_basic_stats_report_empty():
    stats = build_basic_stats_report([])
    assert stats["total_records"] == 0
