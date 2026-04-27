from datetime import datetime


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
            for rule in self._rules:
                if rule.matches(context):
                    result = rule.execute(context)
                    result["rule_name"] = rule.name
                    result["brain_id"] = brain_id

                    if result.get("transition_to"):
                        self._governance.transition(
                            brain_id, result["transition_to"],
                            reason=f"rule:{rule.name}",
                        )

                    if self._audit:
                        self._audit.log_governance_signal(
                            brain_id=brain_id,
                            signal_type=f"rule_fired:{rule.name}",
                            recommendation=result.get("transition_to", "none"),
                            health_signal=summary.get("health_signal", "unknown"),
                        )

                    fired.append(result)
                    break

        return fired

    @classmethod
    def with_default_rules(cls, governance_service, audit_log=None) -> "GovernanceRuleEngine":
        engine = cls(governance_service, audit_log)
        gs = governance_service

        engine.add_rule(GovernanceRule(
            name="auto_freeze_critical",
            condition_fn=lambda ctx: ctx.get("health_signal") == "critical" and ctx.get("sample_count", 0) >= 10,
            action_fn=lambda ctx: {"transition_to": "frozen", "reason": "auto_freeze_critical"},
            priority=100,
        ))

        def _demote_condition(ctx):
            if ctx.get("health_signal") != "degraded" or ctx.get("sample_count", 0) < 15:
                return False
            state = gs.get_brain_state(ctx["brain_id"])
            return state is not None and state.get("status") == "live"

        engine.add_rule(GovernanceRule(
            name="auto_demote_degraded",
            condition_fn=_demote_condition,
            action_fn=lambda ctx: {"transition_to": "probation", "reason": "auto_demote_degraded"},
            priority=90,
        ))

        engine.add_rule(GovernanceRule(
            name="auto_promote_healthy",
            condition_fn=lambda ctx: (
                ctx.get("health_signal") == "healthy"
                and ctx.get("composite_mean", 0) >= 0.75
                and ctx.get("sample_count", 0) >= 30
            ),
            action_fn=lambda ctx: {"transition_to": "live", "reason": "auto_promote_healthy"},
            priority=50,
        ))

        engine.add_rule(GovernanceRule(
            name="unfreeze_recovered",
            condition_fn=lambda ctx: (
                ctx.get("current_status") == "frozen"
                and ctx.get("health_signal") in {"stable", "healthy"}
                and ctx.get("recommendation") != "freeze"
            ),
            action_fn=lambda ctx: {"transition_to": "probation", "reason": "unfreeze_recovered"},
            priority=40,
        ))

        return engine
