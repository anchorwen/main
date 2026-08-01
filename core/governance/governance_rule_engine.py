import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# FIX-20260801-012: Status rank for observation-hold demotion detection.
# A transition is a "demotion" when the target rank is strictly below the
# current rank (e.g. live→probation throttle).  Promotions / lateral moves
# pass through the observation hold untouched.
_HOLD_STATUS_RANK: dict[str, int] = {
    "live": 5,
    "probation": 4,
    "candidate": 3,
    "shadow": 2,
    "frozen": 1,
    "retired": 0,
}


class GovernanceRule:
    """A single declarative governance rule."""

    def __init__(self, name: str, condition_fn, action_fn, priority: int = 0):
        self.name = name
        self._condition = condition_fn
        self._action = action_fn
        self.priority = priority

    def matches(self, context: dict) -> bool:
        return self._condition(context)

    def execute(self, context: dict) -> dict:
        return self._action(context)


class GovernanceRuleEngine:
    """Evaluates declarative governance rules against brain and system state.

    Rules are evaluated in priority order.  All matching rules fire;
    results are collected and applied via the GovernanceService.
    """

    def __init__(self, governance_service, audit_log=None):
        self._governance = governance_service
        self._audit = audit_log
        self._rules: list[GovernanceRule] = []
        # FIX-20260801-012: Observation holds (grace periods) — brain_id →
        # naive-UTC expiry datetime.  Populated from brain config governance
        # blocks (L1 human SSOT) by the governance evaluation orchestrator.
        # During an active hold, this Executor refuses any automated demotion
        # (e.g. throttle live→probation) — the IC's strategic observation
        # period has explicit priority over machine demotion.
        self._observation_holds: dict[str, datetime] = {}

    def set_observation_holds(self, holds: dict[str, datetime]) -> None:
        """Replace the active observation-hold map (brain_id → expiry).

        Called once per evaluation cycle by the governance orchestrator.
        Idempotent — safe to call every 60s cycle.
        """
        self._observation_holds = dict(holds) if holds else {}

    def _hold_blocked(self, brain_id: str, current_status: str, target_status: str | None) -> bool:
        """True when an active observation hold forbids this transition.

        A transition is blocked only when BOTH:
          1. The brain has a hold whose expiry is still in the future.
          2. The transition is a demotion (target rank < current rank).

        Promotions (probation→live) and lateral moves pass through — the hold
        protects against automated *downgrades* during a strategic observation
        window, it never blocks recovery/promotion.
        """
        if target_status is None:
            return False
        hold_until = self._observation_holds.get(brain_id)
        if hold_until is None:
            return False
        if datetime.now(UTC).replace(tzinfo=None) >= hold_until:
            return False  # hold expired — automated demotion resumes
        cur_rank = _HOLD_STATUS_RANK.get(current_status, -1)
        tgt_rank = _HOLD_STATUS_RANK.get(target_status, -1)
        return tgt_rank < cur_rank

    def add_rule(self, rule: GovernanceRule) -> None:
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority, reverse=True)

    # Severity ordering for conflict resolution (higher = more severe)
    _SEVERITY: dict[str, int] = {
        "retired": 5,
        "frozen": 4,
        "probation": 3,
        "live": 2,
        "candidate": 1,
    }

    @staticmethod
    def _most_severe(results: list[dict]) -> dict | None:
        """Return the result with the most severe transition_to, or None."""
        best = None
        best_sev = -1
        for r in results:
            target = r.get("transition_to", "")
            sev = GovernanceRuleEngine._SEVERITY.get(target, 0)
            if sev > best_sev:
                best_sev = sev
                best = r
        return best

    def evaluate(self, brain_summaries: dict, system_context: dict | None = None) -> list[dict]:
        system_context = system_context or {}
        fired = []

        for brain_id, summary in brain_summaries.items():
            state = self._governance.get_brain_state(brain_id)
            current_status = str(state.get("status")) if state else "unknown"
            context = {
                "brain_id": brain_id,
                "current_status": current_status,
                **summary,
                **system_context,
            }
            matches = []
            for rule in self._rules:
                if rule.matches(context):
                    result = rule.execute(context)
                    result["rule_name"] = rule.name
                    result["brain_id"] = brain_id
                    matches.append(result)

            if not matches:
                continue

            # Apply the most severe result across all matching rules
            chosen = self._most_severe(matches)
            assert chosen is not None, "matches non-empty → _most_severe always returns a result"
            if chosen.get("transition_to"):
                # FIX-20260801-012: observation-hold intercept on the declarative
                # rule path too — a held brain is never demoted by any rule.
                # The firing is still audited + returned below (visibility), only
                # the state write is suppressed.
                if self._hold_blocked(brain_id, current_status, chosen["transition_to"]):
                    _hold_until = self._observation_holds.get(brain_id)
                    logger.warning(
                        "[HOLD] action=hold_throttle reason=observation_period_active "
                        "brain=%s %s→%s hold_until=%s rule=%s",
                        brain_id,
                        current_status,
                        chosen["transition_to"],
                        _hold_until.isoformat() if _hold_until else "?",
                        chosen["rule_name"],
                    )
                    result = {}  # held — no state write; firing still audited below
                else:
                    result = self._governance.transition(
                        brain_id,
                        chosen["transition_to"],
                        reason=f"rule:{chosen['rule_name']}",
                    )
                if result.get("action") == "rejected":
                    logger.warning(
                        "GovernanceRuleEngine: transition(%s → %s) rejected by state machine: %s",
                        brain_id,
                        chosen["transition_to"],
                        result.get("reason", "unknown"),
                    )

            for result in matches:
                if self._audit:
                    self._audit.log_governance_signal(
                        brain_id=brain_id,
                        signal_type=f"rule_fired:{result['rule_name']}",
                        recommendation=result.get("transition_to", "none"),
                        health_signal=summary.get("health_signal", "unknown"),
                    )

            fired.append(chosen)

        return fired

    def execute_transitions(
        self,
        report: list[Any],
        *,
        dry_run: bool = False,
    ) -> list[str]:
        """Executor: apply a promotion audit report as state transitions.

        This is the sole writer for automated lifecycle state changes.
        Auditor (BrainPromotionEvaluator) reads → Executor (this method) writes.

        Args:
            report: List of BrainPromotionDecision from BrainPromotionEvaluator.
            dry_run: If True, log what would happen without writing.

        Returns:
            List of change descriptions.
        """
        changes: list[str] = []
        for d in report:
            if not d.approved or d.target_status is None:
                continue
            brain_id = d.brain_id
            current = self._governance.get_brain_state(brain_id)
            if current is None:
                changes.append(f"{brain_id}: not registered — skipping")
                continue
            old_status = current.get("status", "unknown")
            if old_status == d.target_status:
                continue
            # ── FIX-20260801-012: Observation hold (grace period) intercept ──
            # The IC's strategic observation window has explicit priority over
            # automated demotion.  A brain under an active observation hold is
            # never demoted by the sole writer (live→probation throttle,
            # live→frozen, probation→retired, ...).  Promotions pass through.
            if self._hold_blocked(brain_id, old_status, d.target_status):
                _hold_until = self._observation_holds.get(brain_id)
                _h = _hold_until.isoformat() if _hold_until else "?"
                logger.warning(
                    "[HOLD] action=hold_throttle reason=observation_period_active "
                    "brain=%s %s→%s hold_until=%s reasons=%s",
                    brain_id,
                    old_status,
                    d.target_status,
                    _h,
                    "; ".join(d.reasons),
                )
                changes.append(
                    f"{brain_id}: {old_status} → {d.target_status} BLOCKED "
                    f"(observation_hold_until {_h})"
                )
                continue
            if not dry_run:
                self._governance.transition(
                    brain_id,
                    d.target_status,
                    reason=f"promotion:{d.action} — {'; '.join(d.reasons)}",
                )
            changes.append(f"{brain_id}: {old_status} → {d.target_status} ({d.action})")
        return changes if changes else ["no_changes"]

    @classmethod
    def with_default_rules(cls, governance_service, audit_log=None) -> "GovernanceRuleEngine":
        engine = cls(governance_service, audit_log)
        gs = governance_service

        # ── FIX-20260611-017: Hard stop-loss — negative Sharpe for sufficient samples ──
        # OU_Params_V6_Sniper: SR=-30, PnL_R=-1409, still "live" — missing validation (RC-07).
        def _sr_freeze_condition(ctx):
            _sr = ctx.get("sharpe_ratio") or ctx.get("sharpe") or 0.0
            _count = ctx.get("sample_count", 0)
            _status = ctx.get("current_status")
            if _status not in ("live", "active"):
                return False
            if _count < 50:
                return False  # insufficient sample — let promotion pipeline handle
            if _sr >= -1.0:
                return False  # borderline — not catastrophic
            return True

        engine.add_rule(
            GovernanceRule(
                name="auto_freeze_negative_sr",
                condition_fn=_sr_freeze_condition,
                action_fn=lambda ctx: {
                    "transition_to": "frozen",
                    "reason": f"auto_freeze_negative_sr: sharpe={ctx.get('sharpe_ratio', ctx.get('sharpe', 0)):.1f}",
                },
                priority=110,  # above auto_freeze_critical (100)
            )
        )

        engine.add_rule(
            GovernanceRule(
                name="auto_freeze_critical",
                condition_fn=lambda ctx: ctx.get("health_signal") == "critical"
                and ctx.get("sample_count", 0) >= 10,
                action_fn=lambda ctx: {"transition_to": "frozen", "reason": "auto_freeze_critical"},
                priority=100,
            )
        )

        def _demote_condition(ctx):
            if ctx.get("health_signal") != "degraded" or ctx.get("sample_count", 0) < 15:
                return False
            state = gs.get_brain_state(ctx["brain_id"])
            if state is None or state.get("status") != "live":
                return False
            # FIX-20260628-162: Don't demote the LAST live brain.
            # Demoting the sole live brain triggers DQAF-059 (0 live →
            # fail-closed p_win=0.40 → all trading blocked).  A degraded
            # live brain still trades with guard parameters; zero live
            # brains blocks the entire trading pipeline.
            live_brains = [
                bid for bid, bs in gs.get_all_states().items() if bs.get("status") == "live"
            ]
            if len(live_brains) <= 1:
                return False
            return True

        engine.add_rule(
            GovernanceRule(
                name="auto_demote_degraded",
                condition_fn=_demote_condition,
                action_fn=lambda ctx: {
                    "transition_to": "probation",
                    "reason": "auto_demote_degraded",
                },
                priority=90,
            )
        )

        engine.add_rule(
            GovernanceRule(
                name="auto_promote_healthy",
                condition_fn=lambda ctx: (
                    ctx.get("health_signal") == "healthy"
                    and ctx.get("composite_mean", 0) >= 0.75
                    and ctx.get("sample_count", 0) >= 30
                ),
                action_fn=lambda ctx: {"transition_to": "live", "reason": "auto_promote_healthy"},
                priority=50,
            )
        )

        def _probation_demote_condition(ctx):
            if ctx.get("current_status") != "probation":
                return False
            if ctx.get("health_signal") not in {"critical", "degraded"}:
                return False
            if ctx.get("sample_count", 0) < 20:
                return False
            state = gs.get_brain_state(ctx["brain_id"])
            if state is None:
                return False
            # Must have been on probation for at least some cycles
            return state.get("freeze_count", 0) < 3  # not already repeatedly frozen

        engine.add_rule(
            GovernanceRule(
                name="auto_demote_probation_to_frozen",
                condition_fn=_probation_demote_condition,
                action_fn=lambda ctx: {
                    "transition_to": "frozen",
                    "reason": "auto_demote_probation_to_frozen",
                },
                priority=80,
            )
        )

        def _auto_retire_condition(ctx):
            state = gs.get_brain_state(ctx["brain_id"])
            if state is None:
                return False
            freeze_count = state.get("freeze_count", 0)
            # Retire if frozen 3+ times or frozen for the 2nd time with critical health
            if freeze_count >= 3:
                return True
            if freeze_count >= 2 and ctx.get("health_signal") == "critical":
                return True
            return False

        engine.add_rule(
            GovernanceRule(
                name="auto_retire_repeated_frozen",
                condition_fn=_auto_retire_condition,
                action_fn=lambda ctx: {
                    "transition_to": "retired",
                    "reason": "auto_retire_repeated_frozen",
                },
                priority=110,
            )
        )

        engine.add_rule(
            GovernanceRule(
                name="unfreeze_recovered",
                condition_fn=lambda ctx: (
                    ctx.get("current_status") == "frozen"
                    and ctx.get("health_signal") in {"stable", "healthy"}
                    and ctx.get("recommendation") != "freeze"
                ),
                action_fn=lambda ctx: {
                    "transition_to": "probation",
                    "reason": "unfreeze_recovered",
                },
                priority=40,
            )
        )

        # ── Auto-shadow promotion rules ──

        def _shadow_to_probation_condition(ctx):
            if ctx.get("current_status") != "candidate":
                return False
            shadow_count = ctx.get("shadow_signal_count", 0)
            if shadow_count < 50:
                return False
            # Average confidence must be above noise floor
            if ctx.get("shadow_avg_confidence", 0.0) < 0.50:
                return False

            # ── DQAF-20260630-202: Macro-Regime Diversity Exemption ──
            # H4/D1 timeframes produce directionally-monopolistic signals by
            # design — a genuine H4 trend follower in a multi-week downtrend
            # SHOULD output 100% SHORT.  Requiring long≥5 AND short≥5 would
            # structurally exclude the best macro-trend brains.
            #
            # Detection: probe BrainRegistry for contract_group / timeframe.
            # Falls open to legacy diversity check on lookup failure.
            brain_id = ctx.get("brain_id", "")
            is_macro = False
            if brain_id:
                try:
                    from core.brains.brain_registry import BrainRegistry

                    registry = BrainRegistry.instance()
                    entry = registry.get(brain_id)
                    if entry is not None:
                        cg = (entry.contract_group or "").lower()
                        tf = (entry.raw.get("timeframe", "") or "").upper()
                        is_macro = "h4" in cg or "d1" in cg or tf in ("H4", "D1")
                except (RuntimeError, ValueError, KeyError, TypeError, OSError, AttributeError):
                    pass  # fail-open: fall through to legacy diversity check

            if is_macro:
                # Macro timeframes: exempt from directional diversity.
                # 50+ signals + confidence ≥0.50 is sufficient proof of
                # responsive-to-market behaviour.
                return True

            # Non-macro: legacy diversity check (require both directions)
            long_ct = ctx.get("shadow_long_count", 0)
            short_ct = ctx.get("shadow_short_count", 0)
            if long_ct < 5 or short_ct < 5:
                return False
            return True

        engine.add_rule(
            GovernanceRule(
                name="auto_promote_shadow_to_probation",
                condition_fn=_shadow_to_probation_condition,
                action_fn=lambda ctx: {
                    "transition_to": "probation",
                    "reason": (
                        "auto_promote_shadow_to_probation: "
                        f"{ctx.get('shadow_signal_count', 0)} signals, "
                        f"long/short={ctx.get('shadow_long_count', 0)}/"
                        f"{ctx.get('shadow_short_count', 0)}, "
                        f"avg_conf={ctx.get('shadow_avg_confidence', 0):.3f}"
                    ),
                },
                priority=85,
            )
        )

        def _probation_to_live_condition(ctx):
            if ctx.get("current_status") != "probation":
                return False
            sample_count = ctx.get("sample_count", 0)
            if sample_count < 100:
                return False
            health = ctx.get("health_signal", "unknown")
            if health not in ("stable", "healthy"):
                return False
            composite = ctx.get("composite_mean", 0)
            if composite < 0.55:
                return False
            return True

        engine.add_rule(
            GovernanceRule(
                name="auto_promote_probation_to_live",
                condition_fn=_probation_to_live_condition,
                action_fn=lambda ctx: {
                    "transition_to": "live",
                    "reason": "auto_promote_probation_to_live: 100+ signals, "
                    "stable/healthy, composite >= 0.55",
                },
                priority=75,
            )
        )

        return engine
