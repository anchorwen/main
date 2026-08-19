"""Tests for champion_challenger module."""

from typing import Any

from core.feedback.brain_performance_tracker import BrainPerformanceTracker
from core.governance.governance_service import GovernanceService


def _populate_tracker(tracker, brain_id, scores, outcomes=None):
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


def _brain_state(gov: GovernanceService, name: str) -> dict[str, Any]:
    state = gov.get_brain_state(name)
    assert (
        state is not None
    )  # TECH_DEBT-009: get_brain_state 返回 dict|None, 已注册 brain 契约下恒非 None
    return state


# ── run_promotion_cycle tests ──


def test_empty_tracker():
    from scripts.training.champion_challenger import run_promotion_cycle

    tracker = BrainPerformanceTracker()
    gov = GovernanceService()
    report = run_promotion_cycle(tracker, gov)
    assert report["brains_assessed"] == 0
    assert report["promotions"] == []


def test_no_live_champion_no_comparison():
    from scripts.training.champion_challenger import run_promotion_cycle

    tracker = BrainPerformanceTracker()
    gov = GovernanceService()
    gov.register_brain("V9", "candidate")
    gov.register_brain("XGB", "candidate")
    _populate_tracker(tracker, "V9", [0.70] * 25)
    _populate_tracker(tracker, "XGB", [0.80] * 25)

    report = run_promotion_cycle(tracker, gov)
    # No live brain in any lane → no comparisons
    assert len(report["comparisons"]) == 0
    assert report["promotions"] == []


def test_challenger_not_enough_samples():
    from scripts.training.champion_challenger import run_promotion_cycle

    tracker = BrainPerformanceTracker()
    gov = GovernanceService()
    gov.register_brain("V9", "live")
    gov.register_brain("V9_v2", "candidate")
    _populate_tracker(tracker, "V9", [0.60] * 30)
    _populate_tracker(tracker, "V9_v2", [0.82] * 5)  # only 5 samples — not enough

    report = run_promotion_cycle(tracker, gov)
    # V9_v2 doesn't have enough samples → lane comparison exists but ineligible
    assert len(report["comparisons"]) >= 1
    comp = [c for c in report["comparisons"] if c["champion"]["brain_id"] == "V9"]
    assert len(comp) == 1
    assert comp[0]["eligible"] is False
    assert "challenger_samples" in (comp[0]["reason"] or "")
    assert report["promotions"] == []


def test_challenger_beats_champion_promotion():
    from scripts.training.champion_challenger import run_promotion_cycle

    tracker = BrainPerformanceTracker()
    gov = GovernanceService()
    gov.register_brain("V9", "live")
    gov.register_brain("V9_v2", "candidate")
    _populate_tracker(tracker, "V9", [0.55] * 30)
    _populate_tracker(tracker, "V9_v2", [0.75] * 30)  # +0.20 > MIN_COMPOSITE_DELTA

    report = run_promotion_cycle(tracker, gov, dry_run=False)
    assert len(report["comparisons"]) >= 1
    comp = [c for c in report["comparisons"] if c["champion"]["brain_id"] == "V9"]
    assert len(comp) == 1
    assert comp[0]["eligible"] is True
    assert comp[0]["delta"] >= 0.10

    # Promotion should have occurred
    assert len(report["promotions"]) == 1
    assert report["promotions"][0]["lane"] == "sur"

    # Verify state changes
    assert _brain_state(gov, "V9")["status"] == "probation"  # demoted
    assert _brain_state(gov, "V9_v2")["status"] == "live"  # promoted


def test_challenger_delta_too_small():
    from scripts.training.champion_challenger import run_promotion_cycle

    tracker = BrainPerformanceTracker()
    gov = GovernanceService()
    gov.register_brain("XGB", "live")
    gov.register_brain("XGB_v2", "candidate")
    _populate_tracker(tracker, "XGB", [0.60] * 30)
    _populate_tracker(tracker, "XGB_v2", [0.65] * 30)  # only +0.05

    report = run_promotion_cycle(tracker, gov, dry_run=False)
    comp = [c for c in report["comparisons"] if c["champion"]["brain_id"] == "XGB"]
    assert len(comp) == 1
    assert comp[0]["eligible"] is False
    assert report["promotions"] == []

    # No state changes
    assert _brain_state(gov, "XGB")["status"] == "live"
    assert _brain_state(gov, "XGB_v2")["status"] == "candidate"


def test_dry_run_does_not_apply():
    from scripts.training.champion_challenger import run_promotion_cycle

    tracker = BrainPerformanceTracker()
    gov = GovernanceService()
    gov.register_brain("V9", "live")
    gov.register_brain("V9_v2", "candidate")
    _populate_tracker(tracker, "V9", [0.55] * 30)
    _populate_tracker(tracker, "V9_v2", [0.75] * 30)

    report = run_promotion_cycle(tracker, gov, dry_run=True)
    assert len(report["promotions"]) == 1
    assert report["promotions"][0]["demoted_champion"]["action"] == "would_demote"
    assert report["promotions"][0]["promoted_challenger"]["action"] == "would_promote"

    # State should NOT have changed
    assert _brain_state(gov, "V9")["status"] == "live"
    assert _brain_state(gov, "V9_v2")["status"] == "candidate"


def test_multi_lane_independence():
    from scripts.training.champion_challenger import run_promotion_cycle

    tracker = BrainPerformanceTracker()
    gov = GovernanceService()
    # sur lane: champion underperforms
    gov.register_brain("V9", "live")
    gov.register_brain("V9_v2", "candidate")
    _populate_tracker(tracker, "V9", [0.50] * 30)
    _populate_tracker(tracker, "V9_v2", [0.72] * 30)
    # mtx lane: champion doing fine
    gov.register_brain("XGB", "live")
    gov.register_brain("XGB_v2", "candidate")
    _populate_tracker(tracker, "XGB", [0.65] * 30)
    _populate_tracker(tracker, "XGB_v2", [0.55] * 30)

    report = run_promotion_cycle(tracker, gov, dry_run=False)
    # sur lane: promotion
    sur_promos = [p for p in report["promotions"] if p["lane"] == "sur"]
    assert len(sur_promos) == 1
    # mtx lane: no promotion
    mtx_promos = [p for p in report["promotions"] if p["lane"] == "mtx"]
    assert len(mtx_promos) == 0

    assert _brain_state(gov, "V9_v2")["status"] == "live"
    assert _brain_state(gov, "XGB")["status"] == "live"


def test_ineligibility_reason():
    from scripts.training.champion_challenger import _ineligibility_reason

    reason = _ineligibility_reason(5, 15, 0.05)
    assert "challenger_samples" in reason
    assert "delta" in reason
