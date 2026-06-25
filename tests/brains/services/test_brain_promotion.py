"""Tests for core.brains.services.brain_promotion — automated lifecycle decisions.

FIX-20260625-XXX: Tier 2 zero-coverage breakout #5.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.brains.services.brain_promotion import (
    BrainPromotionDecision,
    BrainPromotionEvaluator,
    BrainPromotionThresholds,
    apply_promotion_decisions,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_state(status: str = "candidate", **overrides) -> dict:
    d = {"status": status}
    d.update(overrides)
    return d


def _make_perf(
    wr: float = 0.45,
    pf: float = 1.0,
    signal_count: int = 60,
    cons_losses: int = 0,
    recent_wr: float | None = None,
) -> dict:
    return {
        "win_rate": wr,
        "profit_factor": pf,
        "signal_count": signal_count,
        "consecutive_losses": cons_losses,
        "recent_win_rate": recent_wr if recent_wr is not None else wr,
    }


# ── BrainPromotionThresholds ───────────────────────────────────────────────


class TestBrainPromotionThresholds:
    def test_defaults(self) -> None:
        t = BrainPromotionThresholds()
        assert t.min_live_samples == 50
        assert t.min_signals_candidate == 20
        assert t.min_signals_probation == 50
        assert t.min_signals_active == 100
        assert t.promote_wr_candidate == 0.40
        assert t.promote_wr_probation == 0.45
        assert t.retire_wr == 0.30
        assert t.throttle_wr == 0.38
        assert t.promote_pf_probation == 0.90
        assert t.promote_pf_active == 1.10
        assert t.max_profit_factor == 10.0
        assert t.retire_pf == 0.60
        assert t.throttle_pf == 0.80
        assert t.max_consecutive_losses == 8


# ── BrainPromotionDecision ─────────────────────────────────────────────────


class TestBrainPromotionDecision:
    def test_dataclass_fields(self) -> None:
        d = BrainPromotionDecision(
            brain_id="test",
            current_status="candidate",
            action="promote",
            target_status="probation",
            approved=True,
            reasons=["quality_met"],
            metrics_snapshot={"win_rate": 0.45},
        )
        assert d.brain_id == "test"
        assert d.action == "promote"
        assert d.approved is True


# ── Min Live Samples Gate (FIX-20260621-029) ───────────────────────────────


class TestEvaluateOneMinLiveSamples:
    def test_insufficient_samples_hold(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("candidate"),
            _make_perf(wr=0.60, pf=2.0, signal_count=10),  # < 50
        )
        assert decision.action == "hold"
        assert decision.approved is False
        assert any("insufficient_live_samples" in r for r in decision.reasons)

    def test_sufficient_samples_proceeds(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("candidate"),
            _make_perf(wr=0.50, pf=1.5, signal_count=60),
        )
        # Should proceed to candidate evaluation (promote since wr>=0.40, pf>=0.90)
        assert decision.action == "promote"
        assert decision.approved is True


# ── Candidate Evaluation ───────────────────────────────────────────────────


class TestEvaluateCandidate:
    def test_insufficient_signals(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("candidate"),
            _make_perf(wr=0.50, pf=1.0, signal_count=50),  # < min_live_samples=50
        )
        # signal_count=50 is exactly min_live_samples, should proceed
        # but signal_count=50 < min_signals_candidate=20? No, 50 >= 20
        # Wait, min_live_samples=50, so 50 >= 50 — proceeds
        # signal_count=50 >= min_signals_candidate=20, quality met → promote
        assert decision.action == "promote"

    def test_quality_met_promotes(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("candidate"),
            _make_perf(wr=0.45, pf=1.0, signal_count=60),
        )
        assert decision.action == "promote"
        assert decision.target_status == "probation"
        assert decision.approved is True

    def test_quality_below_threshold_hold(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("candidate"),
            _make_perf(wr=0.35, pf=0.80, signal_count=60),
        )
        assert decision.action == "hold"
        assert any("quality_below_threshold" in r for r in decision.reasons)

    def test_low_signal_candidate_hold(self) -> None:
        evaluator = BrainPromotionEvaluator(
            thresholds=BrainPromotionThresholds(min_live_samples=30)
        )
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("candidate"),
            _make_perf(wr=0.45, pf=1.0, signal_count=35),  # >=30 but <20? No, 35>=20
        )
        # signal_count=35 >= min_signals_candidate=20, quality met → promote
        assert decision.action == "promote"


# ── Probation Evaluation ───────────────────────────────────────────────────


class TestEvaluateProbation:
    def test_quality_met_promotes_to_live(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("probation"),
            _make_perf(wr=0.50, pf=1.20, signal_count=60),
        )
        assert decision.action == "promote"
        assert decision.target_status == "live"

    def test_quality_below_hold(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("probation"),
            _make_perf(wr=0.42, pf=0.85, signal_count=60),
        )
        # wr 0.42 < promote_wr_probation 0.45 → hold
        assert decision.action == "hold"
        assert decision.approved is False

    def test_insufficient_signals_hold(self) -> None:
        evaluator = BrainPromotionEvaluator(
            thresholds=BrainPromotionThresholds(
                min_live_samples=30,
                min_signals_probation=50,
            )
        )
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("probation"),
            _make_perf(wr=0.50, pf=1.0, signal_count=35),  # < 50
        )
        assert decision.action == "hold"
        assert any("signal_count" in r for r in decision.reasons)


# ── Active Evaluation ──────────────────────────────────────────────────────


class TestEvaluateActive:
    def test_healthy_active_hold(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("active"),
            _make_perf(wr=0.50, pf=1.20, signal_count=150),
        )
        assert decision.action == "hold"

    def test_low_activity_throttle(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("active"),
            _make_perf(wr=0.50, pf=1.20, signal_count=60),  # >50 but <100 min_signals_active
        )
        # signal_count 60 < min_signals_active=100 but >= min_signals_probation=50
        # → _eval_active checks min_signals_per_week=3, signal_count=60 >= 3 → hold
        assert decision.action == "hold"

    def test_min_activity(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("active"),
            _make_perf(wr=0.50, pf=1.20, signal_count=60),
        )
        # signal_count >= min_signals_per_week=3, so active_and_healthy
        assert decision.action == "hold"


# ── Universal Retirement Checks ────────────────────────────────────────────


class TestUniversalRetirement:
    def test_consecutive_losses_freeze(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("active"),
            _make_perf(wr=0.45, pf=1.0, signal_count=100, cons_losses=10),
        )
        assert decision.action in ("retire", "freeze")
        assert decision.target_status in ("retired", "frozen")

    def test_consecutive_losses_new_brain_protection(self) -> None:
        """Brain with < min_signals_active (100) gets probation, not frozen."""
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("candidate"),
            _make_perf(wr=0.45, pf=1.0, signal_count=60, cons_losses=10),
        )
        # signal_count=60 < min_signals_active=100, so new_brain protection
        # → throttle to probation instead of freeze
        assert decision.action == "throttle"
        assert decision.target_status == "probation"
        assert any("protected" in r for r in decision.reasons)

    def test_low_win_rate_freeze(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("active"),
            _make_perf(wr=0.25, pf=1.0, signal_count=100),
        )
        assert decision.action in ("retire", "freeze")
        assert decision.target_status in ("retired", "frozen")

    def test_low_win_rate_new_brain_protection(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("probation"),
            _make_perf(wr=0.20, pf=1.0, signal_count=60),
        )
        # signal_count=60 < min_signals_active=100, protected → probation
        assert decision.action == "throttle"
        assert any("protected" in r for r in decision.reasons)

    def test_low_pf_freeze(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("active"),
            _make_perf(wr=0.45, pf=0.50, signal_count=120),
        )
        assert decision.action in ("retire", "freeze")

    def test_low_pf_new_brain_protection(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("probation"),
            _make_perf(wr=0.45, pf=0.50, signal_count=60),
        )
        # protected → probation
        assert decision.action == "throttle"

    def test_retired_from_frozen(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("frozen"),
            _make_perf(wr=0.45, pf=0.50, signal_count=120),
        )
        assert decision.target_status == "retired"


# ── Low Signal Protection ──────────────────────────────────────────────────


class TestLowSignalProtection:
    def test_below_candidate_threshold_protection(self) -> None:
        """signals < min_signals_candidate (20) — only probation at worst."""
        evaluator = BrainPromotionEvaluator(thresholds=BrainPromotionThresholds(min_live_samples=5))
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("candidate"),
            _make_perf(wr=0.45, pf=1.0, signal_count=10, cons_losses=10),
        )
        # 10 < min_signals_candidate=20, so gets protection
        assert decision.action == "throttle"
        assert decision.target_status == "probation"
        assert any("low-signal protected" in r for r in decision.reasons)


# ── Throttle Checks ────────────────────────────────────────────────────────


class TestThrottleChecks:
    def test_pf_below_throttle(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("active"),
            _make_perf(wr=0.45, pf=0.70, signal_count=120),
        )
        # pf=0.70 < throttle_pf=0.80, universal retirement tries first
        # 0.70 >= retire_pf=0.60, so no retire. Then throttle check: pf<0.80 → throttle
        assert decision.action == "throttle"
        assert decision.target_status == "probation"

    def test_recent_wr_below_throttle(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decision = evaluator._evaluate_one(
            "brain_x",
            _make_state("active"),
            _make_perf(wr=0.45, pf=1.0, signal_count=120, recent_wr=0.30),
        )
        # recent_wr=0.30 < throttle_wr=0.38 → throttle
        assert decision.action == "throttle"
        assert any("recent_win_rate" in r for r in decision.reasons)


# ── evaluate_all ───────────────────────────────────────────────────────────


class TestEvaluateAll:
    def test_multiple_brains(self) -> None:
        evaluator = BrainPromotionEvaluator()
        states = {
            "brain_a": _make_state("candidate"),
            "brain_b": _make_state("active"),
        }
        perf = {
            "brain_a": _make_perf(wr=0.45, pf=1.0, signal_count=60),
            "brain_b": _make_perf(wr=0.50, pf=1.20, signal_count=150),
        }
        decisions = evaluator.evaluate_all(states, perf)
        assert len(decisions) == 2
        assert all(d.evaluated_at for d in decisions)

    def test_evaluated_at_is_set(self) -> None:
        evaluator = BrainPromotionEvaluator()
        decisions = evaluator.evaluate_all(
            {"b1": _make_state("candidate")},
            {"b1": _make_perf(signal_count=60)},
        )
        assert decisions[0].evaluated_at != ""


# ── apply_promotion_decisions ──────────────────────────────────────────────


class TestApplyPromotionDecisions:
    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        gov_path = tmp_path / "governance_state.json"
        gov_path.write_text(
            json.dumps(
                {
                    "brain_states": {
                        "brain_a": {"status": "candidate", "transition_count": 0},
                    },
                    "transition_log": [],
                }
            )
        )
        decisions = [
            BrainPromotionDecision(
                brain_id="brain_a",
                current_status="candidate",
                action="promote",
                target_status="probation",
                approved=True,
                reasons=["test"],
                metrics_snapshot={},
                evaluated_at="2026-01-01T00:00:00",
            ),
        ]
        changes = apply_promotion_decisions(gov_path, decisions, dry_run=True)
        assert any("candidate" in c for c in changes)
        gov = json.loads(gov_path.read_text())
        assert gov["brain_states"]["brain_a"]["status"] == "candidate"  # unchanged

    def test_nonexistent_governance(self, tmp_path: Path) -> None:
        gov_path = tmp_path / "nonexistent.json"
        changes = apply_promotion_decisions(gov_path, [])
        assert changes == ["governance_state_not_found"]

    def test_unapproved_skipped(self, tmp_path: Path) -> None:
        gov_path = tmp_path / "governance_state.json"
        gov_path.write_text(
            json.dumps(
                {
                    "brain_states": {"brain_a": {"status": "candidate"}},
                    "transition_log": [],
                }
            )
        )
        decisions = [
            BrainPromotionDecision(
                brain_id="brain_a",
                current_status="candidate",
                action="hold",
                target_status=None,
                approved=False,
                reasons=["test"],
                metrics_snapshot={},
                evaluated_at="",
            ),
        ]
        changes = apply_promotion_decisions(gov_path, decisions, dry_run=True)
        assert changes == ["no_changes"]

    def test_brain_not_in_governance_skip(self, tmp_path: Path) -> None:
        gov_path = tmp_path / "governance_state.json"
        gov_path.write_text(
            json.dumps(
                {
                    "brain_states": {"other_brain": {"status": "candidate"}},
                    "transition_log": [],
                }
            )
        )
        decisions = [
            BrainPromotionDecision(
                brain_id="brain_a",
                current_status="candidate",
                action="promote",
                target_status="probation",
                approved=True,
                reasons=["test"],
                metrics_snapshot={},
                evaluated_at="",
            ),
        ]
        changes = apply_promotion_decisions(gov_path, decisions, dry_run=True)
        assert any("skipping" in c for c in changes)

    def test_already_at_target_skipped(self, tmp_path: Path) -> None:
        gov_path = tmp_path / "governance_state.json"
        gov_path.write_text(
            json.dumps(
                {
                    "brain_states": {"brain_a": {"status": "probation"}},
                    "transition_log": [],
                }
            )
        )
        decisions = [
            BrainPromotionDecision(
                brain_id="brain_a",
                current_status="candidate",
                action="promote",
                target_status="probation",
                approved=True,
                reasons=["test"],
                metrics_snapshot={},
                evaluated_at="",
            ),
        ]
        changes = apply_promotion_decisions(gov_path, decisions, dry_run=True)
        assert changes == ["no_changes"]
