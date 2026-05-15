"""Tests for governance_scheduler module."""

import json
from pathlib import Path

from core.feedback.brain_performance_tracker import BrainPerformanceTracker
from core.governance.governance_service import GovernanceService


def _populate_tracker(
    tracker: BrainPerformanceTracker,
    brain_id: str,
    scores: list[float],
    outcomes: list[str] | None = None,
):
    """Record a series of outcomes with given scores for a brain."""
    if outcomes is None:
        outcomes = ["filled"] * len(scores)
    for score, outcome in zip(scores, outcomes, strict=False):
        tracker.record_outcome(
            brain_id,
            {
                "composite_score": score,
                "execution_outcome": outcome,
            },
        )


# ── run_governance_cycle tests ──


def test_empty_tracker():
    from scripts.training.governance_scheduler import run_governance_cycle

    tracker = BrainPerformanceTracker()
    gov = GovernanceService()
    report = run_governance_cycle(tracker, gov)
    assert report["brains_assessed"] == 0
    assert report["actions_applied"] == []


def test_healthy_brain_no_action():
    from scripts.training.governance_scheduler import run_governance_cycle

    tracker = BrainPerformanceTracker()
    gov = GovernanceService()
    gov.register_brain("Brain_A", "live")
    # 0.60 with 20 samples → "stable" → "maintain" (not eligible_for_promotion)
    _populate_tracker(tracker, "Brain_A", [0.60] * 20)

    report = run_governance_cycle(tracker, gov)
    assert report["brains_assessed"] == 1
    assert report["actions_applied"] == []
    assert report["actions_flagged"] == []


def test_critical_brain_auto_freeze():
    from scripts.training.governance_scheduler import run_governance_cycle

    tracker = BrainPerformanceTracker()
    gov = GovernanceService()
    gov.register_brain("Brain_B", "live")
    # Critical: many breaches → health = critical → recommendation = freeze
    _populate_tracker(tracker, "Brain_B", [0.1] * 10, outcomes=["breach"] * 5 + ["rejected"] * 5)

    report = run_governance_cycle(tracker, gov, dry_run=False)
    applied = report["actions_applied"]
    assert len(applied) == 1
    assert applied[0]["brain_id"] == "Brain_B"
    assert applied[0]["recommendation"] == "freeze"
    assert applied[0]["result"]["action"] == "transitioned"
    assert applied[0]["result"]["to"] == "frozen"

    # Verify state actually changed
    state = gov.get_brain_state("Brain_B")
    assert state["status"] == "frozen"


def test_degraded_brain_auto_demote():
    from scripts.training.governance_scheduler import run_governance_cycle

    tracker = BrainPerformanceTracker()
    gov = GovernanceService()
    gov.register_brain("Brain_C", "live")
    # recent_mean < 0.3 → degraded, but breach_rate <= 0.3 → not critical
    outcomes = ["filled"] * 18 + ["breach"] * 2  # breach_rate = 2/20 = 0.1
    _populate_tracker(tracker, "Brain_C", [0.25] * 20, outcomes=outcomes)

    report = run_governance_cycle(tracker, gov, dry_run=False)
    applied = report["actions_applied"]
    assert len(applied) >= 1
    brain_c = [a for a in applied if a["brain_id"] == "Brain_C"]
    assert len(brain_c) == 1
    assert brain_c[0]["result"]["to"] == "probation"


def test_dry_run_does_not_apply():
    from scripts.training.governance_scheduler import run_governance_cycle

    tracker = BrainPerformanceTracker()
    gov = GovernanceService()
    gov.register_brain("Brain_D", "live")
    _populate_tracker(tracker, "Brain_D", [0.1] * 10, outcomes=["breach"] * 5 + ["rejected"] * 5)

    report = run_governance_cycle(tracker, gov, dry_run=True)
    applied = report["actions_applied"]
    assert len(applied) >= 1
    assert applied[0]["result"]["action"] == "would_apply"

    # State should NOT have changed
    state = gov.get_brain_state("Brain_D")
    assert state["status"] == "live"


def test_eligible_for_promotion_flagged_not_auto():
    from scripts.training.governance_scheduler import run_governance_cycle

    tracker = BrainPerformanceTracker()
    gov = GovernanceService()
    gov.register_brain("Brain_E", "candidate")
    _populate_tracker(tracker, "Brain_E", [0.85] * 30)

    report = run_governance_cycle(tracker, gov, dry_run=False)
    # Should be flagged, not auto-applied
    assert len(report["actions_flagged"]) >= 1
    flagged = [a for a in report["actions_flagged"] if a["brain_id"] == "Brain_E"]
    assert len(flagged) == 1
    assert flagged[0]["recommendation"] == "eligible_for_promotion"
    # Status should NOT have changed (promotion requires confirmation)
    state = gov.get_brain_state("Brain_E")
    assert state["status"] == "candidate"


def test_maintain_observe_skipped():
    from scripts.training.governance_scheduler import run_governance_cycle

    tracker = BrainPerformanceTracker()
    gov = GovernanceService()
    gov.register_brain("Brain_F", "live")
    _populate_tracker(tracker, "Brain_F", [0.55] * 20)

    report = run_governance_cycle(tracker, gov)
    assert report["actions_applied"] == []
    assert report["actions_flagged"] == []


# ── CLI smoke tests ──


def test_main_dry_run(tmp_path: Path, monkeypatch):
    import io
    import sys

    from scripts.training.governance_scheduler import main

    base = tmp_path / "data"
    base.mkdir()

    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        exit_code = main(["--base-dir", str(base), "--dry-run"])
    finally:
        sys.stdout = old_stdout

    # Empty tracker, no PnL data, no actions
    assert exit_code == 0


def test_main_output_file(tmp_path: Path, monkeypatch):
    from scripts.training.governance_scheduler import main

    base = tmp_path / "data"
    base.mkdir()
    out = tmp_path / "actions.json"
    exit_code = main(["--base-dir", str(base), "--dry-run", "--output", str(out)])
    assert exit_code == 0
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == "governance_scheduler.v2"
