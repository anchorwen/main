"""Online-offline eval alignment contract tests."""

import json
from pathlib import Path

from scripts.training.eval_alignment import (
    build_report,
    compare_metrics,
    compute_label_metrics,
    extract_backtest_metrics,
    load_backtest_result,
    load_labels,
    main,
)


def _write_labels_file(path: Path, labels: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in labels) + "\n",
        encoding="utf-8",
    )


def test_load_labels_basic(tmp_path: Path):
    labels = [
        {
            "schema_version": "training_label.v1",
            "label_id": "test_1",
            "position_ticket": 1001,
            "symbol": "XAUUSDc",
            "side": "long",
            "pnl": 10.0,
            "label": "win",
            "is_closed": True,
        },
        {
            "schema_version": "training_label.v1",
            "label_id": "test_2",
            "position_ticket": 1002,
            "symbol": "XAUUSDc",
            "side": "short",
            "pnl": -5.0,
            "label": "loss",
            "is_closed": True,
        },
    ]
    lp = tmp_path / "labels.jsonl"
    _write_labels_file(lp, labels)
    loaded = load_labels(lp)
    assert len(loaded) == 2
    assert loaded[0]["label_id"] == "test_1"


def test_load_labels_empty(tmp_path: Path):
    assert load_labels(tmp_path / "nonexistent.jsonl") == []


def test_load_labels_skips_non_label(tmp_path: Path):
    recs = [
        {
            "schema_version": "training_label.v1",
            "label_id": "a",
            "pnl": 1.0,
            "label": "win",
            "is_closed": True,
        },
        {"not": "a label"},
    ]
    lp = tmp_path / "labels.jsonl"
    _write_labels_file(lp, recs)
    loaded = load_labels(lp)
    assert len(loaded) == 1


def test_compute_label_metrics():
    labels = [
        {"is_closed": True, "pnl": 10.0, "label": "win", "side": "long"},
        {"is_closed": True, "pnl": -5.0, "label": "loss", "side": "short"},
        {"is_closed": True, "pnl": 3.0, "label": "win", "side": "long"},
        {"is_closed": True, "pnl": 0.0, "label": "breakeven", "side": "long"},
    ]
    metrics = compute_label_metrics(labels)
    assert metrics["closed_trades"] == 4
    assert metrics["wins"] == 2
    assert metrics["losses"] == 1
    assert metrics["breakeven"] == 1
    assert metrics["win_rate"] == 0.5
    assert metrics["total_pnl"] == 8.0
    assert metrics["direction_bias"] == 0.75
    assert metrics["avg_pnl"] == 2.0
    assert metrics["pnl_range"]["min"] == -5.0
    assert metrics["pnl_range"]["max"] == 10.0


def test_compute_label_metrics_no_closed():
    labels = [
        {"is_closed": False, "pnl": None, "label": "unlabeled", "side": "long"},
    ]
    metrics = compute_label_metrics(labels)
    assert metrics["closed_trades"] == 0
    assert metrics["win_rate"] is None
    assert "error" in metrics


def test_extract_backtest_metrics_arb_style():
    bt = {
        "model_id": "CRT.arb.test",
        "lane": "arb",
        "metrics": {
            "winrate_pct": 55.0,
            "total_pnl": 150.0,
            "sharpe": 1.85,
            "profit_factor": 2.1,
            "backtest_metrics": {
                "winrate": 0.55,
                "total_trades": 324,
            },
        },
    }
    m = extract_backtest_metrics(bt)
    assert m["available"] is True
    assert m["win_rate"] == 0.55
    assert m["total_pnl"] == 150.0
    assert m["sharpe"] == 1.85


def test_extract_backtest_metrics_empty():
    m = extract_backtest_metrics({})
    assert m["available"] is False


def test_compare_metrics_aligned():
    live = {"closed_trades": 10, "win_rate": 0.60, "total_pnl": 25.0, "wins": 6, "losses": 4}
    bt = {"win_rate": 0.55, "total_pnl": 20.0}
    result = compare_metrics(live, bt)
    assert result["severity"] == "ok"
    assert len(result["issues"]) == 0


def test_compare_metrics_diverged():
    live = {"closed_trades": 10, "win_rate": 0.30, "total_pnl": -50.0}
    bt = {"win_rate": 0.55, "total_pnl": 20.0}
    result = compare_metrics(live, bt)
    assert result["severity"] == "critical"
    assert any("win_rate" in i for i in result["issues"])


def test_compare_metrics_small_sample():
    live = {"closed_trades": 1, "win_rate": 1.0, "total_pnl": 5.0}
    bt = {"win_rate": 0.55, "total_pnl": 20.0}
    result = compare_metrics(live, bt)
    assert any("insufficient" in i for i in result["issues"])


def test_compare_metrics_pnl_sign_mismatch():
    live = {"closed_trades": 10, "win_rate": 0.60, "total_pnl": -30.0}
    bt = {"win_rate": 0.55, "total_pnl": 20.0}
    result = compare_metrics(live, bt)
    assert any("sign_mismatch" in i for i in result["issues"])


def test_build_report_full(tmp_path: Path):
    labels = [
        {
            "schema_version": "training_label.v1",
            "label_id": "l1",
            "is_closed": True,
            "pnl": 10.0,
            "label": "win",
            "side": "long",
        },
        {
            "schema_version": "training_label.v1",
            "label_id": "l2",
            "is_closed": True,
            "pnl": -3.0,
            "label": "loss",
            "side": "short",
        },
        {
            "schema_version": "training_label.v1",
            "label_id": "l3",
            "is_closed": True,
            "pnl": 5.0,
            "label": "win",
            "side": "long",
        },
    ]
    lp = tmp_path / "labels.jsonl"
    _write_labels_file(lp, labels)

    bp = tmp_path / "backtest.json"
    bp.write_text(
        json.dumps(
            {
                "model_id": "CRT.sur.test",
                "lane": "sur",
                "metrics": {"winrate_pct": 60.0, "total_pnl": 15.0, "sharpe": 1.5},
            }
        ),
        encoding="utf-8",
    )

    report = build_report(lp, bp)
    assert report["schema_version"] == "eval_alignment.v1"
    assert report["live_metrics"]["closed_trades"] == 3
    assert abs(report["live_metrics"]["win_rate"] - 2 / 3) < 0.001
    assert report["alignment"]["severity"] == "ok"


def test_build_report_no_labels(tmp_path: Path):
    bp = tmp_path / "backtest.json"
    bp.write_text(json.dumps({"metrics": {"winrate_pct": 50}}))
    report = build_report(tmp_path / "nolabels.jsonl", bp)
    assert "error" in report
    assert report["error"] == "no_labels_found"


def test_build_report_no_backtest(tmp_path: Path):
    lp = tmp_path / "labels.jsonl"
    _write_labels_file(
        lp,
        [
            {
                "schema_version": "training_label.v1",
                "label_id": "x",
                "is_closed": True,
                "pnl": 1.0,
                "label": "win",
                "side": "long",
            },
        ],
    )
    report = build_report(lp, tmp_path / "nobt.json")
    assert "error" in report


def test_load_backtest_result(tmp_path: Path):
    bp = tmp_path / "bt.json"
    bp.write_text(json.dumps({"model_id": "test", "lane": "sur"}), encoding="utf-8")
    result = load_backtest_result(bp)
    assert result["model_id"] == "test"


def test_main_dry_run(tmp_path: Path):
    lp = tmp_path / "labels.jsonl"
    _write_labels_file(
        lp,
        [
            {
                "schema_version": "training_label.v1",
                "label_id": "x",
                "is_closed": True,
                "pnl": 1.0,
                "label": "win",
                "side": "long",
            },
        ],
    )
    bp = tmp_path / "bt.json"
    bp.write_text(json.dumps({"model_id": "t", "lane": "arb", "metrics": {"winrate_pct": 55}}))

    out = tmp_path / "align.json"
    ret = main(["--labels", str(lp), "--backtest", str(bp), "--output", str(out)])
    assert ret == 0
    assert out.exists()
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["schema_version"] == "eval_alignment.v1"


def test_cli_help():
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "scripts/training/eval_alignment.py", "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "labels" in proc.stdout
