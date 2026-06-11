import logging
from typing import Any

logger = logging.getLogger(__name__)


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
            current_status = state.get("status") if state else None
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
            return state is not None and state.get("status") == "live"

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
            # Require minimum diversity (not all long or all short)
            long_ct = ctx.get("shadow_long_count", 0)
            short_ct = ctx.get("shadow_short_count", 0)
            if long_ct < 5 or short_ct < 5:
                return False
            # Average confidence must be above noise floor
            if ctx.get("shadow_avg_confidence", 0.0) < 0.50:
                return False
            return True

        engine.add_rule(
            GovernanceRule(
                name="auto_promote_shadow_to_probation",
                condition_fn=_shadow_to_probation_condition,
                action_fn=lambda ctx: {
                    "transition_to": "probation",
                    "reason": "auto_promote_shadow_to_probation: 50+ shadow signals, "
                    "min 5 long/5 short, avg confidence >= 0.50",
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
