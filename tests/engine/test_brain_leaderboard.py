"""Brain leaderboard contract tests."""

import json
from pathlib import Path

from scripts.training.brain_leaderboard import (
    _link_decision_to_label,
    aggregate_by_brain,
    build_report,
    load_decisions,
    load_labels,
)


def _write_decisions(decisions_dir: Path, date_key: str, records: list[dict]) -> Path:
    d = decisions_dir / date_key
    d.mkdir(parents=True, exist_ok=True)
    p = d / "XAUUSD.decisions.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def test_load_decisions(tmp_path: Path):
    _write_decisions(
        tmp_path,
        "2026-04-26",
        [
            {
                "schema_version": "decision_record.v1",
                "record_id": "r1",
                "event_time": "2026-04-26T01:00:00Z",
                "attribution": {
                    "supporting_brains": ["V9_Institutional_01"],
                    "opposing_brains": [],
                },
                "labels": {"decision_action": "open", "decision_side": "long"},
            },
            {
                "schema_version": "decision_record.v1",
                "record_id": "r2",
                "event_time": "2026-04-26T02:00:00Z",
                "attribution": {
                    "supporting_brains": ["V9_Institutional_01", "XGBoost_V4.5"],
                    "opposing_brains": [],
                },
                "labels": {"decision_action": "open", "decision_side": "short"},
            },
        ],
    )
    records = load_decisions(tmp_path)
    assert len(records) == 2


def test_load_decisions_empty(tmp_path: Path):
    assert load_decisions(tmp_path / "nonexistent") == []


def test_load_decisions_date_filter(tmp_path: Path):
    _write_decisions(
        tmp_path,
        "2026-04-26",
        [
            {
                "schema_version": "decision_record.v1",
                "record_id": "r1",
                "event_time": "2026-04-26T01:00:00Z",
                "attribution": {"supporting_brains": ["A"], "opposing_brains": []},
                "labels": {"decision_side": "long"},
            },
        ],
    )
    _write_decisions(
        tmp_path,
        "2026-04-27",
        [
            {
                "schema_version": "decision_record.v1",
                "record_id": "r2",
                "event_time": "2026-04-27T01:00:00Z",
                "attribution": {"supporting_brains": ["B"], "opposing_brains": []},
                "labels": {"decision_side": "short"},
            },
        ],
    )
    records = load_decisions(tmp_path, date_filter="2026-04-26")
    assert len(records) == 1
    assert records[0]["record_id"] == "r1"


def test_aggregate_by_brain_basic():
    decisions = [
        {
            "event_time": "2026-04-26T01:00:00Z",
            "attribution": {"supporting_brains": ["Brain_A"], "opposing_brains": []},
            "labels": {"decision_action": "open", "decision_side": "long"},
        },
        {
            "event_time": "2026-04-26T02:00:00Z",
            "attribution": {"supporting_brains": ["Brain_A"], "opposing_brains": []},
            "labels": {"decision_action": "open", "decision_side": "long"},
        },
        {
            "event_time": "2026-04-26T03:00:00Z",
            "attribution": {"supporting_brains": ["Brain_B"], "opposing_brains": []},
            "labels": {"decision_action": "open", "decision_side": "short"},
        },
    ]
    board = aggregate_by_brain(decisions)
    assert len(board) == 2
    a = next(b for b in board if b["brain_id"] == "Brain_A")
    assert a["signal_count"] == 2
    assert a["direction_distribution"]["long_pct"] == 1.0
    assert a["direction_distribution"]["short_pct"] == 0.0
    b = next(b for b in board if b["brain_id"] == "Brain_B")
    assert b["signal_count"] == 1
    assert b["trade_performance"] is None


def test_aggregate_by_brain_with_labels(tmp_path: Path):
    lp = tmp_path / "labels.jsonl"
    lp.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "schema_version": "training_label.v1",
                    "label_id": "l1",
                    "is_closed": True,
                    "pnl": 10.0,
                    "label": "win",
                    "side": "long",
                    "open_recorded_at": "2026-04-26T01:05:00Z",
                },
                {
                    "schema_version": "training_label.v1",
                    "label_id": "l2",
                    "is_closed": True,
                    "pnl": -5.0,
                    "label": "loss",
                    "side": "long",
                    "open_recorded_at": "2026-04-26T02:10:00Z",
                },
            ]
        )
        + "\n"
    )
    labels = load_labels(lp)
    decisions = [
        {
            "event_time": "2026-04-26T01:00:00Z",
            "attribution": {"supporting_brains": ["Brain_A"], "opposing_brains": []},
            "labels": {"decision_side": "long"},
        },
        {
            "event_time": "2026-04-26T02:00:00Z",
            "attribution": {"supporting_brains": ["Brain_A"], "opposing_brains": []},
            "labels": {"decision_side": "long"},
        },
    ]
    board = aggregate_by_brain(decisions, labels=labels)
    a = board[0]
    assert a["trade_performance"]["linked_trades"] == 2
    assert a["trade_performance"]["win_rate"] == 0.5
    assert a["trade_performance"]["total_pnl"] == 5.0


def test_link_decision_to_label_within_window():
    labels = [
        {"open_recorded_at": "2026-04-26T01:10:00Z", "is_closed": True, "pnl": 5.0, "label": "win"},
    ]
    result = _link_decision_to_label("2026-04-26T01:00:00Z", labels, window_minutes=30)
    assert result is not None
    assert result["pnl"] == 5.0


def test_link_decision_to_label_outside_window():
    labels = [
        {"open_recorded_at": "2026-04-26T02:00:00Z", "is_closed": True, "pnl": 5.0, "label": "win"},
    ]
    result = _link_decision_to_label("2026-04-26T01:00:00Z", labels, window_minutes=30)
    assert result is None


def test_build_report_with_real_data():
    import pytest
    data_dir = Path("data/decisions")
    if not data_dir.exists() or not any(data_dir.iterdir()):
        pytest.skip("No real decision data available (CI environment)")
    report = build_report(data_dir)
    assert report["total_decisions"] > 0
    assert report["total_brains"] >= 1
    lb = report["leaderboard"]
    assert len(lb) > 0
    assert all("brain_id" in b for b in lb)


def test_build_report_no_decisions(tmp_path: Path):
    report = build_report(tmp_path / "nonexistent")
    assert "error" in report


def test_cli_help():
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "scripts/training/brain_leaderboard.py", "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "decisions" in proc.stdout
