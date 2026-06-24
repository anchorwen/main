"""Brain promotion evaluator — automated lifecycle decisions from live performance.

Evaluates each brain's live performance against configurable thresholds and
produces promotion/retention/retirement decisions. Designed to be called
daily or at startup from live_intent_loop.

State flow:
  candidate → probation → active
       ↓          ↓          ↓
    retired    retired    throttled / retired

Usage:
  from core.brains.services.brain_promotion import BrainPromotionEvaluator
  evaluator = BrainPromotionEvaluator()
  decisions = evaluator.evaluate_all(brain_states, performance_records)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class BrainPromotionDecision:
    brain_id: str
    current_status: str
    action: str  # "promote", "throttle", "retire", "hold"
    target_status: str | None
    approved: bool
    reasons: list[str]
    metrics_snapshot: dict[str, Any]
    evaluated_at: str = ""


# ── Default thresholds ──


@dataclass
class BrainPromotionThresholds:
    """Configurable thresholds for brain promotion/retirement decisions."""

    # Universal minimum live execution samples before ANY lifecycle decision.
    # Below this threshold the statistical confidence is too low — all
    # promote/throttle/retire decisions are bypassed (STATUS_QUO).
    # FIX-20260621-029: Single Source of Truth — this threshold applies to
    # brain_performance.json (live execution outcomes), NOT all-time PnL.
    min_live_samples: int = 50  # live execution records required for any decision

    # Minimum signals before any decision
    min_signals_candidate: int = 20  # signals needed to exit candidate
    min_signals_probation: int = 50  # signals needed to exit probation
    min_signals_active: int = 100  # signals for active evaluation

    # Win rate thresholds
    promote_wr_candidate: float = 0.40  # min win rate: candidate → probation
    promote_wr_probation: float = 0.45  # min win rate: probation → active
    retire_wr: float = 0.30  # win rate below this → retire
    throttle_wr: float = 0.38  # win rate below this → throttle

    # Profit factor thresholds
    promote_pf_probation: float = 0.90  # min PF: candidate → probation
    promote_pf_active: float = 1.10  # min PF: probation → active

    # Maximum profit_factor cap to prevent extreme values near score=1.0
    max_profit_factor: float = 10.0
    retire_pf: float = 0.60  # PF below this → retire
    throttle_pf: float = 0.80  # PF below this → throttle

    # Consecutive losses
    max_consecutive_losses: int = 8  # more than this → retire

    # Signal count thresholds for active maintenance
    min_signals_per_week: int = 3  # below this → throttle (inactive)


# ── Evaluator ──


class BrainPromotionEvaluator:
    """Auditor: evaluates brain performance and produces audit reports.

    Does NOT write state.  Use GovernanceRuleEngine.execute_transitions()
    to apply the returned decisions.
    """

    def __init__(self, thresholds: BrainPromotionThresholds | None = None):
        self._thresholds = thresholds or BrainPromotionThresholds()

    @property
    def thresholds(self) -> BrainPromotionThresholds:
        return self._thresholds

    # ── Main entry point ──

    def evaluate_all(
        self,
        brain_states: dict[str, dict[str, Any]],
        performance: dict[str, dict[str, Any]],
    ) -> list[BrainPromotionDecision]:
        """Evaluate all brains and return a list of promotion decisions.

        Args:
            brain_states: Dict of brain_id → state dict from governance_state.json.
            performance: Dict of brain_id → performance metrics from BrainPerformanceTracker.

        Returns:
            List of BrainPromotionDecision, one per brain.
        """
        decisions: list[BrainPromotionDecision] = []
        now_iso = datetime.now(UTC).replace(tzinfo=None).replace(microsecond=0).isoformat()

        for brain_id, state in brain_states.items():
            perf = performance.get(brain_id, {})
            decision = self._evaluate_one(brain_id, state, perf)
            decision.evaluated_at = now_iso
            decisions.append(decision)

        return decisions

    def _evaluate_one(
        self,
        brain_id: str,
        state: dict[str, Any],
        perf: dict[str, Any],
    ) -> BrainPromotionDecision:
        status = state.get("status", "candidate")
        t = self._thresholds

        # Extract metrics
        wr = float(perf.get("win_rate", 0.0))
        pf = float(perf.get("profit_factor", 0.0))
        signal_count = int(perf.get("signal_count", 0))
        cons_losses = int(perf.get("consecutive_losses", 0))
        recent_wr = float(perf.get("recent_win_rate", wr))

        metrics_snapshot = {
            "win_rate": round(wr, 4),
            "profit_factor": round(pf, 4),
            "signal_count": signal_count,
            "consecutive_losses": cons_losses,
            "recent_win_rate": round(recent_wr, 4),
        }

        # ── FIX-20260621-029: Minimum Live Sample Gate ──
        # Single Source of Truth: brain_performance.json (live execution outcomes).
        # Below min_live_samples, statistical confidence is too low for ANY
        # lifecycle decision.  Bypass ALL promote/throttle/retire/freeze —
        # the brain stays in STATUS_QUO until sufficient live data accumulates.
        #
        # This gate eliminates the dual-track data-source conflict:
        #   OLD: SSOT reconcile promoted on all-time PnL (thousands of trades),
        #        evaluator throttled on live window (1-41 records) → oscillation.
        #   NEW: Both paths see the same signal_count from brain_performance.
        #        Below 50 → HOLD.  Above 50 → existing per-state logic applies.
        if signal_count < t.min_live_samples:
            return BrainPromotionDecision(
                brain_id=brain_id,
                current_status=status,
                action="hold",
                target_status=None,
                approved=False,
                reasons=[
                    f"insufficient_live_samples({signal_count} < {t.min_live_samples})"
                ],
                metrics_snapshot=metrics_snapshot,
            )

        # ── Universal retirement checks (apply in any state) ──
        # Protection: brains with < min_signals_active get graduated demotion, not direct retire
        _new_brain = signal_count < t.min_signals_active
        if signal_count >= t.min_signals_candidate:
            if cons_losses > t.max_consecutive_losses:
                if _new_brain:
                    return BrainPromotionDecision(
                        brain_id=brain_id,
                        current_status=status,
                        action="throttle",
                        target_status="probation",
                        approved=True,
                        reasons=[
                            f"consecutive_losses({cons_losses}) > {t.max_consecutive_losses} — probation (protected)"
                        ],
                        metrics_snapshot=metrics_snapshot,
                    )
                target = "retired" if status in ("frozen",) else "frozen"
                return BrainPromotionDecision(
                    brain_id=brain_id,
                    current_status=status,
                    action="retire" if target == "retired" else "freeze",
                    target_status=target,
                    approved=True,
                    reasons=[f"consecutive_losses({cons_losses}) > {t.max_consecutive_losses}"],
                    metrics_snapshot=metrics_snapshot,
                )
            if wr < t.retire_wr and signal_count >= t.min_signals_probation:
                if _new_brain:
                    return BrainPromotionDecision(
                        brain_id=brain_id,
                        current_status=status,
                        action="throttle",
                        target_status="probation",
                        approved=True,
                        reasons=[f"win_rate({wr:.2%}) < {t.retire_wr:.0%} — probation (protected)"],
                        metrics_snapshot=metrics_snapshot,
                    )
                target = "retired" if status in ("frozen",) else "frozen"
                return BrainPromotionDecision(
                    brain_id=brain_id,
                    current_status=status,
                    action="retire" if target == "retired" else "freeze",
                    target_status=target,
                    approved=True,
                    reasons=[f"win_rate({wr:.2%}) < {t.retire_wr:.0%}"],
                    metrics_snapshot=metrics_snapshot,
                )
            if pf < t.retire_pf and signal_count >= t.min_signals_probation:
                if _new_brain:
                    return BrainPromotionDecision(
                        brain_id=brain_id,
                        current_status=status,
                        action="throttle",
                        target_status="probation",
                        approved=True,
                        reasons=[
                            f"profit_factor({pf:.2f}) < {t.retire_pf:.2f} — probation (protected)"
                        ],
                        metrics_snapshot=metrics_snapshot,
                    )
                target = "retired" if status in ("frozen",) else "frozen"
                return BrainPromotionDecision(
                    brain_id=brain_id,
                    current_status=status,
                    action="retire" if target == "retired" else "freeze",
                    target_status=target,
                    approved=True,
                    reasons=[f"profit_factor({pf:.2f}) < {t.retire_pf:.2f}"],
                    metrics_snapshot=metrics_snapshot,
                )
        else:
            # ── Low-signal-count protection: < min_signals_candidate ──
            # Too few signals for statistical confidence — never retire,
            # only probation at worst.  Previously these brains fell through
            # to state-specific logic with no universal protection.
            if cons_losses > t.max_consecutive_losses:
                return BrainPromotionDecision(
                    brain_id=brain_id,
                    current_status=status,
                    action="throttle",
                    target_status="probation",
                    approved=True,
                    reasons=[
                        f"consecutive_losses({cons_losses}) > {t.max_consecutive_losses} "
                        f"— probation (low-signal protected, {signal_count} < {t.min_signals_candidate})"
                    ],
                    metrics_snapshot=metrics_snapshot,
                )

        # ── State-specific promotion logic ──
        # FIX-20260613-074: Promotion check runs BEFORE throttle check.
        # Previously throttle (line 235) intercepted probation brains with
        # cold recent-20 streaks before they could reach promotion evaluation.
        # Now: if you qualify for promotion, you get promoted regardless of
        # recent streak.  Throttle only applies to brains that did NOT qualify.
        if status == "candidate":
            return self._eval_candidate(brain_id, status, metrics_snapshot, t)
        elif status == "probation":
            decision = self._eval_probation(brain_id, status, metrics_snapshot, t)
            if decision.approved and decision.action == "promote":
                return decision  # promotion wins over throttle
        elif status in ("active", "live"):
            decision = self._eval_active(brain_id, status, metrics_snapshot, t)
            if decision.approved and decision.action == "promote":
                return decision

        # ── Throttle checks (active/probation/live only, AFTER promotion eval) ──
        if status in ("active", "live", "probation") and signal_count >= t.min_signals_probation:
            if pf < t.throttle_pf:
                return BrainPromotionDecision(
                    brain_id=brain_id,
                    current_status=status,
                    action="throttle",
                    target_status="probation",
                    approved=True,
                    reasons=[f"profit_factor({pf:.2f}) < {t.throttle_pf:.2f}"],
                    metrics_snapshot=metrics_snapshot,
                )
            if recent_wr < t.throttle_wr:
                return BrainPromotionDecision(
                    brain_id=brain_id,
                    current_status=status,
                    action="throttle",
                    target_status="probation",
                    approved=True,
                    reasons=[f"recent_win_rate({recent_wr:.2%}) < {t.throttle_wr:.0%}"],
                    metrics_snapshot=metrics_snapshot,
                )

        # ── Fallback: return the promotion decision (hold or demote) ──
        if status == "probation" and 'decision' in locals():
            return decision
        elif status in ("active", "live") and 'decision' in locals():
            return decision
        else:
            return BrainPromotionDecision(
                brain_id=brain_id,
                current_status=status,
                action="hold",
                target_status=None,
                approved=False,
                reasons=[f"no_evaluation_rule_for_{status}"],
                metrics_snapshot=metrics_snapshot,
            )

    def _eval_candidate(
        self,
        brain_id: str,
        status: str,
        m: dict[str, Any],
        t: BrainPromotionThresholds,
    ) -> BrainPromotionDecision:
        if m["signal_count"] < t.min_signals_candidate:
            return BrainPromotionDecision(
                brain_id=brain_id,
                current_status=status,
                action="hold",
                target_status=None,
                approved=False,
                reasons=[f"signal_count({m['signal_count']}) < {t.min_signals_candidate}"],
                metrics_snapshot=m,
            )
        if m["win_rate"] >= t.promote_wr_candidate and m["profit_factor"] >= t.promote_pf_probation:
            return BrainPromotionDecision(
                brain_id=brain_id,
                current_status=status,
                action="promote",
                target_status="probation",
                approved=True,
                reasons=[
                    f"candidate_thresholds_met: wr={m['win_rate']:.2%}, pf={m['profit_factor']:.2f}"
                ],
                metrics_snapshot=m,
            )
        # Candidate meets signal minimums but not quality thresholds → hold
        return BrainPromotionDecision(
            brain_id=brain_id,
            current_status=status,
            action="hold",
            target_status=None,
            approved=False,
            reasons=[
                f"quality_below_threshold: wr={m['win_rate']:.2%} (need {t.promote_wr_candidate:.0%})"
            ],
            metrics_snapshot=m,
        )

    def _eval_probation(
        self,
        brain_id: str,
        status: str,
        m: dict[str, Any],
        t: BrainPromotionThresholds,
    ) -> BrainPromotionDecision:
        if m["signal_count"] < t.min_signals_probation:
            return BrainPromotionDecision(
                brain_id=brain_id,
                current_status=status,
                action="hold",
                target_status=None,
                approved=False,
                reasons=[f"signal_count({m['signal_count']}) < {t.min_signals_probation}"],
                metrics_snapshot=m,
            )
        if m["win_rate"] >= t.promote_wr_probation and m["profit_factor"] >= t.promote_pf_active:
            return BrainPromotionDecision(
                brain_id=brain_id,
                current_status=status,
                action="promote",
                target_status="live",
                approved=True,
                reasons=[
                    f"probation_thresholds_met: wr={m['win_rate']:.2%}, pf={m['profit_factor']:.2f}"
                ],
                metrics_snapshot=m,
            )
        return BrainPromotionDecision(
            brain_id=brain_id,
            current_status=status,
            action="hold",
            target_status=None,
            approved=False,
            reasons=[
                f"probation_ongoing: wr={m['win_rate']:.2%} (need {t.promote_wr_probation:.0%})"
            ],
            metrics_snapshot=m,
        )

    def _eval_active(
        self,
        brain_id: str,
        status: str,
        m: dict[str, Any],
        t: BrainPromotionThresholds,
    ) -> BrainPromotionDecision:
        reasons = []
        if m["signal_count"] < t.min_signals_per_week:
            reasons.append(f"low_activity: {m['signal_count']} signals")
        if not reasons:
            reasons.append("active_and_healthy")
        needs_throttle = m["signal_count"] < t.min_signals_per_week
        return BrainPromotionDecision(
            brain_id=brain_id,
            current_status=status,
            action="throttle" if needs_throttle else "hold",
            target_status="probation" if needs_throttle else None,
            approved=needs_throttle,
            reasons=reasons,
            metrics_snapshot=m,
        )


# ── Governance state update helper ──


def apply_promotion_decisions(
    # DEPRECATED: use GovernanceRuleEngine.execute_transitions() for state writes.
    # This function is kept for backward compatibility with scripts/ that call it
    # directly.  New code should route through the Auditor→Executor pipeline.
    governance_path: Path,
    decisions: list[BrainPromotionDecision],
    *,
    dry_run: bool = False,
) -> list[str]:
    """Apply promotion decisions to governance_state.json.

    Args:
        governance_path: Path to governance_state.json.
        decisions: List of promotion decisions from evaluator.
        dry_run: If True, do not write to disk.

    Returns:
        List of change descriptions.
    """
    if not governance_path.exists():
        return ["governance_state_not_found"]

    gov = json.loads(governance_path.read_text(encoding="utf-8"))
    brain_states = gov.setdefault("brain_states", {})
    transition_log = gov.setdefault("transition_log", [])
    changes: list[str] = []

    for d in decisions:
        if not d.approved or d.target_status is None:
            continue

        brain_id = d.brain_id
        if brain_id not in brain_states:
            changes.append(f"{brain_id}: not in governance — skipping")
            continue

        old_status = brain_states[brain_id].get("status", "unknown")
        if old_status == d.target_status:
            continue

        # Validate transition against the governance state machine
        try:
            from core.governance.governance_service import GovernanceService

            valid_targets = GovernanceService.VALID_TRANSITIONS.get(old_status, set())
            if d.target_status not in valid_targets:
                logger.warning(
                    "apply_promotion_decisions: rejected %s %s→%s (not in VALID_TRANSITIONS[%s]=%s)",
                    brain_id,
                    old_status,
                    d.target_status,
                    old_status,
                    valid_targets,
                )
                changes.append(
                    f"{brain_id}: rejected {old_status}→{d.target_status} — invalid transition"
                )
                continue
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass  # non-critical — state machine validation is best-effort
        brain_states[brain_id]["status"] = d.target_status
        brain_states[brain_id]["last_transition_at"] = d.evaluated_at
        brain_states[brain_id]["transition_count"] = (
            brain_states[brain_id].get("transition_count", 0) + 1
        )

        entry = {
            "brain_id": brain_id,
            "from_status": old_status,
            "to_status": d.target_status,
            "action": d.action,
            "reasons": d.reasons,
            "evaluated_at": d.evaluated_at,
        }
        transition_log.append(entry)
        changes.append(
            f"{brain_id}: {old_status} → {d.target_status} ({d.action}) — {', '.join(d.reasons)}"
        )

    if not dry_run and changes:
        gov["updated_at"] = (
            datetime.now(UTC).replace(tzinfo=None).replace(microsecond=0).isoformat()
        )
        # FIX-20260604-088: locked, atomic write via GovernanceService
        svc = GovernanceService()
        svc._brain_states = brain_states
        svc._transition_log = transition_log
        svc.save(str(governance_path), lock_timeout=30.0)

    return changes if changes else ["no_changes"]
