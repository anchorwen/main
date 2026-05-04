"""Daily recap contract tests."""

import json
from pathlib import Path

from scripts.live_daily_recap import (
    _derive_run_state,
    _generate_evolution_block,
    _run_feature_quality,
    _run_shadow_ensemble,
    build_report,
    main,
)


def test_derive_run_state_active():
    tq = {"total": 5, "counts": {"accepted": 4, "rejected": 1}, "rejection_rate": 0.2}
    dq = {"summary": {"issues_count": 2}}
    assert "活跃" in _derive_run_state(tq, dq, False)


def test_derive_run_state_silent():
    tq = {"total": 0, "counts": {}, "rejection_rate": 0.0}
    dq = {"summary": {"issues_count": 0}}
    assert "静默" in _derive_run_state(tq, dq, False)


def test_derive_run_state_blocked():
    tq = {"total": 5, "counts": {"accepted": 4}, "rejection_rate": 0.2}
    dq = {"summary": {"issues_count": 2}}
    assert "阻断" in _derive_run_state(tq, dq, True)


def test_derive_run_state_all_rejected():
    tq = {"total": 3, "counts": {"accepted": 0, "rejected": 3}, "rejection_rate": 1.0}
    dq = {"summary": {"issues_count": 0}}
    assert "告警" in _derive_run_state(tq, dq, False)


def test_evolution_block_basic():
    block = _generate_evolution_block(
        "2026-05-04",
        "活跃（有成交）",
        {"total": 5, "counts": {"accepted": 4, "rejected": 1}, "rejection_rate": 0.2},
        {"summary": {"issues_count": 2}, "outbox_staleness": {"stale_count": 0}},
        False,
    )
    assert "2026-05-04" in block
    assert "活跃" in block
    assert "接受=4" in block
    assert "拒绝=1" in block


def test_evolution_block_with_ensemble():
    ensemble = {
        "total_brains": 3,
        "comparison": {
            "consensus": "long",
            "agreement_score": 0.66,
            "total_brains": 3,
        },
    }
    block = _generate_evolution_block(
        "2026-05-04",
        "活跃（有成交）",
        {"total": 3, "counts": {"accepted": 3}, "rejection_rate": 0.0},
        {"summary": {"issues_count": 0}, "outbox_staleness": {"stale_count": 0}},
        False,
        shadow_ensemble=ensemble,
    )
    assert "多模型共识: long" in block
    assert "一致性=66%" in block


def test_evolution_block_without_ensemble():
    block = _generate_evolution_block(
        "2026-05-04",
        "活跃（有成交）",
        {"total": 3, "counts": {"accepted": 3}, "rejection_rate": 0.0},
        {"summary": {"issues_count": 0}, "outbox_staleness": {"stale_count": 0}},
        False,
    )
    assert "多模型共识" not in block


def test_build_report_minimal(tmp_path: Path):
    (tmp_path / "live_trade_journal.jsonl").write_text("", encoding="utf-8")
    report = build_report(tmp_path, "XAUUSDc")
    assert report["schema_version"] == "live_daily_recap.v1"
    assert "date_key" in report
    assert "run_state" in report


def test_build_report_with_flag(tmp_path: Path):
    (tmp_path / "live_trade_journal.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "live_dispatch_block.flag").write_text(
        json.dumps({"reason": "test_block"}), encoding="utf-8"
    )
    report = build_report(tmp_path, "XAUUSDc")
    assert report["flag_present"] is True
    assert report["flag_payload"]["reason"] == "test_block"


def test_build_report_includes_ensemble_when_brains_dir(tmp_path: Path):
    (tmp_path / "live_trade_journal.jsonl").write_text("", encoding="utf-8")
    brains_dir = tmp_path / "brains"
    brains_dir.mkdir()
    (brains_dir / "test.json").write_text(
        json.dumps(
            {
                "schema_version": "brain_registry_entry.v1",
                "brain_id": "Test",
                "brain_type": "onnx_v9",
                "brain_role": "alpha_brain",
                "model_version": "v9.0",
                "status": "shadow",
                "artifact_path": str(tmp_path / "model.onnx"),
            }
        ),
        encoding="utf-8",
    )
    report = build_report(tmp_path, "XAUUSDc", brains_dir=brains_dir)
    assert "shadow_ensemble" in report
    # Even with a brain entry that fails to build, the ensemble section exists
    assert isinstance(report["shadow_ensemble"], dict)


def test_run_shadow_ensemble_with_real_brains():
    result = _run_shadow_ensemble(Path("configs/brains"))
    assert "error" not in result
    assert result.get("total_brains", 0) >= 1
    assert "comparison" in result


def test_run_feature_quality_no_store(tmp_path: Path):
    norm_path = tmp_path / "norm.json"
    norm_path.write_text(
        json.dumps(
            {
                "schema_version": "brain_normalization.v1",
                "brain_id": "test",
                "feature_schema_id": "v9_institutional_40",
                "mean": [0.0] * 40,
                "std": [1.0] * 40,
            }
        )
    )
    result = _run_feature_quality(tmp_path / "nonexistent", norm_path)
    assert "error" in result or result.get("sample_size", 0) == 0


def test_evolution_block_with_alignment():
    alignment = {
        "live_metrics": {"win_rate": 0.55, "closed_trades": 8},
        "backtest_metrics": {"win_rate": 0.60},
        "alignment": {"severity": "ok", "issues": []},
    }
    block = _generate_evolution_block(
        "2026-05-04",
        "活跃（有成交）",
        {"total": 8, "counts": {"accepted": 8}, "rejection_rate": 0.0},
        {"summary": {"issues_count": 0}, "outbox_staleness": {"stale_count": 0}},
        False,
        eval_alignment=alignment,
    )
    assert "线上线下对齐" in block
    assert "实盘胜率=0.55" in block
    assert "回测胜率=0.6" in block


def test_build_report_with_eval_alignment(tmp_path: Path):
    (tmp_path / "live_trade_journal.jsonl").write_text("", encoding="utf-8")
    lp = tmp_path / "labels.jsonl"
    lp.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "schema_version": "training_label.v1",
                    "label_id": "x",
                    "is_closed": True,
                    "pnl": 1.0,
                    "label": "win",
                    "side": "long",
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    bp = tmp_path / "bt.json"
    bp.write_text(json.dumps({"model_id": "t", "lane": "arb", "metrics": {"winrate_pct": 55}}))
    report = build_report(tmp_path, "XAUUSDc", labels_path=lp, backtest_path=bp)
    assert "eval_alignment" in report
    assert "error" not in report["eval_alignment"]
    assert report["eval_alignment"]["live_metrics"]["closed_trades"] == 1


def test_evolution_block_without_alignment():
    block = _generate_evolution_block(
        "2026-05-04",
        "活跃（有成交）",
        {"total": 3, "counts": {"accepted": 3}, "rejection_rate": 0.0},
        {"summary": {"issues_count": 0}, "outbox_staleness": {"stale_count": 0}},
        False,
    )
    assert "线上线下对齐" not in block


def test_cli_help():
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "scripts/live_daily_recap.py", "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "brains-dir" in proc.stdout


def test_cli_dry_run_with_brains_dir(tmp_path: Path):
    (tmp_path / "live_trade_journal.jsonl").write_text("", encoding="utf-8")
    out = tmp_path / "recap.json"
    ret = main(["--base-dir", str(tmp_path), "--output", str(out)])
    assert ret == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["schema_version"] == "live_daily_recap.v1"
