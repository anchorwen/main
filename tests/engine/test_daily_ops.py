"""Tests for daily_ops module."""

import json
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
    assert report["total_steps"] == 0
    assert report["errors"] == 0
    assert report["actions_total"] == 0
    assert report["schema_version"] == "daily_ops.v1"


def test_governance_step_empty(tmp_path: Path):
    """Governance step with empty tracker should report 0 actions."""
    from scripts.daily_ops import _step_governance

    result = _step_governance(str(tmp_path), dry_run=True)
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


def test_daily_ops_dry_run():
    """Dry-run flag should be reflected in report."""
    from scripts.daily_ops import run_daily_ops

    report = run_daily_ops(
        skip_shadow=True,
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
    assert data["total_steps"] == 0
