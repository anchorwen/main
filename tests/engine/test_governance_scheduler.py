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
    assert (
        state is not None
    )  # TECH_DEBT-009: get_brain_state 返回 dict|None, 已注册 brain 契约下恒非 None
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
    assert (
        state is not None
    )  # TECH_DEBT-009: get_brain_state 返回 dict|None, 已注册 brain 契约下恒非 None
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
    assert (
        state is not None
    )  # TECH_DEBT-009: get_brain_state 返回 dict|None, 已注册 brain 契约下恒非 None
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


# ── FIX-20260621-043: Journal metrics type normalization tests ──


def test_journal_dict_converted_to_brain_pnl_metrics():
    """Journal dicts must be converted to BrainPnLMetrics before use.

    Regression test for FIX-043: compute_journal_brain_metrics() returns
    plain dicts, but run_governance_cycle() expects BrainPnLMetrics
    instances with attribute access. _dict_to_pnl_metrics() bridges this gap.
    """
    from scripts.training.governance_scheduler import _dict_to_pnl_metrics

    journal_dict = {
        "brain_id": "TestBrain",
        "sample_count": 42,
        "cumulative_pnl": 15.5,
        "pnl_r": 15.5,
        "win_rate": 0.55,
        "profit_factor": 1.8,
        "sharpe_ratio": 1.2,
        "max_drawdown": 5.0,
        "long_win_rate": 0.60,
        "short_win_rate": 0.50,
        "long_count": 25,
        "short_count": 17,
    }

    result = _dict_to_pnl_metrics("TestBrain", journal_dict)

    # Must be a BrainPnLMetrics instance (not a dict)
    assert not isinstance(result, dict), "Must NOT be dict — would cause AttributeError downstream"
    from core.feedback.brain_pnl_ledger import BrainPnLMetrics

    assert isinstance(result, BrainPnLMetrics), "Must be BrainPnLMetrics dataclass"

    # Verify field mapping
    assert result.brain_id == "TestBrain"
    assert result.sample_count == 42
    assert result.cumulative_pnl == 15.5
    assert result.win_rate == 0.55
    assert result.sharpe_ratio == 1.2
    assert result.profit_factor == 1.8
    assert result.max_drawdown == 5.0
    assert result.long_win_rate == 0.60
    assert result.short_win_rate == 0.50
    assert result.long_count == 25
    assert result.short_count == 17


def test_dict_to_pnl_metrics_handles_missing_fields():
    """Missing optional fields default to 0 without error."""
    from scripts.training.governance_scheduler import _dict_to_pnl_metrics

    minimal_dict = {"brain_id": "Minimal", "sample_count": 5, "win_rate": 0.4}
    result = _dict_to_pnl_metrics("Minimal", minimal_dict)

    assert result.sample_count == 5
    assert result.win_rate == 0.4
    assert result.cumulative_pnl == 0.0  # default
    assert result.sharpe_ratio == 0.0  # default
    assert result.profit_factor == 0.0  # default


def test_governance_cycle_with_pnl_store_no_dict_crash(tmp_path: Path):
    """Full governance cycle with PnL store must not crash on type mismatch.

    Simulates what happens when pnl_store returns BrainPnLMetrics.
    The journal augmentation path is tested indirectly — this test
    verifies the non-journal path still works after FIX-043 changes.

    FIX-20260621-044x-2: Test was environment-dependent — passed locally
    when data_btc/ had real journal data but failed in CI (0 brains assessed).
    Root cause:
      1. settle_all() without force_all=True skips signals with TTL > 0
         (record_signal sets TTL=expected_horizon=1, never decremented).
      2. base_dir defaulted to "data_btc" — real trade journal data leaked
         into all_metrics, masking the TTL settlement bug.
    Fix: use force_all=True + isolated tmp_path base_dir.
    """
    from scripts.training.governance_scheduler import run_governance_cycle

    tracker = BrainPerformanceTracker()
    gov = GovernanceService()

    gov.register_brain("Brain_J", "candidate")

    from core.feedback.brain_pnl_ledger import BrainPnLStore

    store = BrainPnLStore()
    # Populate PnP store via public API: record a signal + settle
    store.record_signal(
        brain_id="Brain_J",
        symbol="XAUUSDc",
        direction="long",
        entry_price=4700.0,
        confidence=0.6,
    )
    # force_all=True: TTL is 1 (set by record_signal), and settle_all()
    # skips signals with ttl > 0 by default. We need immediate settlement.
    store.settle_all(close_price=4720.0, force_all=True)

    # Use isolated tmp_path to prevent real journal data leakage
    # (default base_dir="data_btc" would inject live brain metrics on dev machines)
    # This must NOT raise AttributeError
    report = run_governance_cycle(
        tracker,
        gov,
        dry_run=True,
        pnl_store=store,
        base_dir=str(tmp_path),
    )
    assert report["brains_assessed"] >= 1


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
