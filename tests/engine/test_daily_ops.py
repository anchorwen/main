"""Tests for daily_ops module."""

import json
import os
import time
from pathlib import Path

# ── run_daily_ops tests ──


def test_run_all_steps_skip_all():
    """With all steps skipped, should produce an empty steps report."""
    from scripts.daily_ops import run_daily_ops

    report = run_daily_ops(
        skip_shadow=True,
        skip_label_builder=True,
        skip_feedback=True,
        skip_governance=True,
        skip_champion=True,
        skip_retraining=True,
        skip_recap=True,
        skip_alpha=True,
        skip_alpha_allocation=True,
        skip_online_feedback=True,
        skip_paper_simulation=True,
        skip_fs_maintenance=True,
    )
    assert report["total_steps"] >= 2  # SSOT reconciliation + ledger retention always run (FIX-081)
    assert report["errors"] == 0
    assert report["actions_total"] == 0
    assert report["schema_version"] == "daily_ops.v1"


def test_governance_step_empty(tmp_path: Path):
    """Governance step with empty tracker should report 0 actions."""
    from scripts.daily_ops import _step_governance

    # data_btc (crypto_24_7) → P12 gate 恒评估, 不受周末 forex 休市影响 (FIX-20260820-002)
    result = _step_governance(str(tmp_path / "data_btc"), dry_run=True)
    assert result["step"] == "governance"
    assert result["status"] == "ok"
    assert result["brains_assessed"] == 0


def test_champion_step_empty(tmp_path: Path):
    """Champion/challenger step with empty tracker should report 0 comparisons."""
    from scripts.daily_ops import _step_champion_challenger

    result = _step_champion_challenger(str(tmp_path), dry_run=True)
    assert result["step"] == "champion_challenger"
    assert result["status"] == "ok"
    assert result["comparisons"] == 0


def test_label_prune_prunes_old_files(tmp_path: Path):
    """FIX-20260820-002: 过期 labels 文件 (mtime < 30d) 被删除, 新文件保留."""
    from scripts.daily_ops import _step_label_prune

    labels_dir = tmp_path / "labels"
    labels_dir.mkdir()
    old = labels_dir / "a.jsonl"
    old.write_text("{}", encoding="utf-8")
    _old_ts = time.time() - 40 * 86400  # 40 天前 (> retention 30)
    os.utime(old, (_old_ts, _old_ts))
    fresh = labels_dir / "b.jsonl"
    fresh.write_text("{}", encoding="utf-8")  # mtime = now, 保留

    result = _step_label_prune(str(tmp_path), retention_days=30)
    assert result["step"] == "label_prune"
    assert result["status"] == "ok"
    assert result["pruned"] == 1
    assert result["failed"] == 0
    assert not old.exists()
    assert fresh.exists()


def test_label_prune_dry_run_counts_without_deleting(tmp_path: Path):
    """dry_run=True: 计数但保留文件 (pipeline 干跑可安全预览)."""
    from scripts.daily_ops import _step_label_prune

    labels_dir = tmp_path / "labels"
    labels_dir.mkdir()
    old = labels_dir / "a.jsonl"
    old.write_text("{}", encoding="utf-8")
    _old_ts = time.time() - 40 * 86400
    os.utime(old, (_old_ts, _old_ts))

    result = _step_label_prune(str(tmp_path), dry_run=True, retention_days=30)
    assert result["status"] == "ok"
    assert result["pruned"] == 1
    assert old.exists()  # 未删除


def test_label_prune_missing_labels_dir(tmp_path: Path):
    """无 labels 目录 → 幂等 ok / pruned 0 (绝不抛异常)."""
    from scripts.daily_ops import _step_label_prune

    result = _step_label_prune(str(tmp_path))
    assert result["step"] == "label_prune"
    assert result["status"] == "ok"
    assert result["pruned"] == 0


def test_governance_p12_btc_crypto_always_evaluates(tmp_path: Path):
    """P12: BTC (crypto_24_7) 恒评估 — 周末照常跑 governance."""
    from scripts.daily_ops import _step_governance

    result = _step_governance(str(tmp_path / "data_btc"), dry_run=True)
    assert result["step"] == "governance"
    assert result["status"] == "ok"


def test_governance_p12_forex_weekend_skipped(tmp_path: Path, monkeypatch):
    """P12: XAU (forex_24_5) 周末休市 → skipped (market_closed_weekend)."""
    import core.execution.pre_trade_guards as ptg
    from scripts.daily_ops import _step_governance

    monkeypatch.setattr(ptg, "detect_session", lambda market_type: {"risk_tier": "off"})

    result = _step_governance(str(tmp_path / "data_xau"), dry_run=True)
    assert result["step"] == "governance"
    assert result["status"] == "skipped"
    assert result["reason"] == "market_closed_weekend"
    assert result["risk_tier"] == "off"


def test_daily_ops_dry_run():
    """Dry-run flag should be reflected in report."""
    from scripts.daily_ops import run_daily_ops

    report = run_daily_ops(
        skip_shadow=True,
        skip_label_builder=True,  # label_builder over live data = 207s (data growth); 断言不依赖它
        skip_feedback=True,
        skip_governance=True,
        skip_champion=True,
        skip_retraining=True,
        skip_recap=True,
        skip_alpha=True,
        skip_alpha_allocation=True,
        skip_online_feedback=True,
        skip_paper_simulation=True,
        skip_fs_maintenance=True,
        dry_run=True,
    )
    assert report["dry_run"] is True


def test_daily_ops_error_count():
    """Steps with errors should be counted."""
    from scripts.daily_ops import run_daily_ops

    report = run_daily_ops(
        skip_shadow=True,
        skip_label_builder=True,  # label_builder over live data = 207s; 断言不依赖它
        skip_feedback=True,
        skip_governance=True,
        skip_champion=True,
        skip_retraining=True,
        skip_recap=True,
        skip_alpha=True,
        skip_alpha_allocation=True,
        skip_online_feedback=True,
        skip_paper_simulation=True,
        skip_fs_maintenance=True,
    )
    assert report["errors"] == 0
    assert "steps" in report


# ── CLI smoke tests ──


def test_main_dry_run(tmp_path: Path, monkeypatch):
    """CLI --dry-run should exit 0 when all steps skipped."""
    import io
    import sys

    from scripts.daily_ops import main

    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        exit_code = main(
            [
                "--skip-shadow",
                "--skip-label-builder",
                "--skip-feedback",
                "--skip-governance",
                "--skip-champion",
                "--skip-retraining",
                "--skip-recap",
                "--skip-alpha",
                "--skip-alpha-allocation",
                "--skip-online-feedback",
                "--skip-paper-simulation",
                "--skip-fs-maintenance",
            ]
        )
    finally:
        sys.stdout = old_stdout

    assert exit_code == 0


def test_main_output_file(tmp_path: Path, monkeypatch):
    """CLI --output should write report JSON to file."""
    from scripts.daily_ops import main

    out = tmp_path / "daily_ops.json"
    exit_code = main(
        [
            "--skip-shadow",
            "--skip-label-builder",
            "--skip-feedback",
            "--skip-governance",
            "--skip-champion",
            "--skip-retraining",
            "--skip-recap",
            "--skip-alpha",
            "--skip-alpha-allocation",
            "--skip-online-feedback",
            "--skip-paper-simulation",
            "--skip-fs-maintenance",
            "--output",
            str(out),
        ]
    )
    assert exit_code == 0
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == "daily_ops.v1"
    assert data["total_steps"] >= 2  # SSOT reconciliation always runs
