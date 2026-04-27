from datetime import datetime
from typing import Any, Dict, List

from core.contracts.domain.risk_verdict import RiskVerdict
from core.risk.schema_versions import SCHEMA_RISK_VERDICT
from core.contracts.enums import RiskDecisionStatus
from core.contracts.ids import new_verdict_id


SEVERITY_ORDER = {
    RiskDecisionStatus.DENY: 0,
    RiskDecisionStatus.LIQUIDATE_ONLY: 1,
    RiskDecisionStatus.FORCE_REDUCE: 2,
    RiskDecisionStatus.DEFER: 3,
    RiskDecisionStatus.ALLOW_LIMITED: 4,
    RiskDecisionStatus.ALLOW: 5,
}


class RiskEvaluationService:
    """Evaluates an intent against a chain of risk policies and produces a verdict.

    Each policy runs independently; the final verdict takes the most
    restrictive outcome across all policies.  Blocking reasons, warning
    reasons, and constraints are aggregated from all policy results.
    """

    def __init__(self, policies: list | None = None):
        self._policies = list(policies) if policies else []

    def add_policy(self, policy) -> None:
        self._policies.append(policy)

    def evaluate(
        self,
        intent,
        control_snapshot,
        *,
        context: dict | None = None,
    ) -> RiskVerdict:
        context = context or {}

        if intent.is_passive():
            return RiskVerdict(
                schema_version=SCHEMA_RISK_VERDICT,
                verdict_id=new_verdict_id(),
                intent_id=intent.intent_id,
                evaluated_at=datetime.utcnow(),
                status=RiskDecisionStatus.DENY,
                mode=control_snapshot.mode_state.current_mode,
                risk_tier="minimal",
                blocking_reasons=["passive_intent"],
            )

        policy_results = []
        for policy in self._policies:
            result = policy.evaluate(intent, control_snapshot, context)
            policy_results.append({"policy": policy.name, **result})

        return self._merge_results(
            intent=intent,
            control_snapshot=control_snapshot,
            policy_results=policy_results,
        )

    def _merge_results(self, *, intent, control_snapshot, policy_results: list[dict]) -> RiskVerdict:
        if not policy_results:
            return RiskVerdict(
                schema_version=SCHEMA_RISK_VERDICT,
                verdict_id=new_verdict_id(),
                intent_id=intent.intent_id,
                evaluated_at=datetime.utcnow(),
                status=RiskDecisionStatus.ALLOW,
                mode=control_snapshot.mode_state.current_mode,
                risk_tier="standard",
            )

        final_status = RiskDecisionStatus.ALLOW
        blocking_reasons = []
        warning_reasons = []
        constraints = {}
        risk_tiers = set()

        for pr in policy_results:
            status = pr["status"]
            reason = pr.get("reason")
            tier = pr.get("tier", "unknown")
            constraint = pr.get("constraint")

            risk_tiers.add(tier)

            if SEVERITY_ORDER.get(status, 5) < SEVERITY_ORDER.get(final_status, 5):
                final_status = status

            if status in {RiskDecisionStatus.DENY, RiskDecisionStatus.DEFER}:
                if reason:
                    blocking_reasons.append(reason)
            elif status in {RiskDecisionStatus.FORCE_REDUCE, RiskDecisionStatus.LIQUIDATE_ONLY}:
                if reason:
                    blocking_reasons.append(reason)
            elif status == RiskDecisionStatus.ALLOW_LIMITED:
                if reason:
                    warning_reasons.append(reason)
                if constraint:
                    constraints.update(constraint)
            elif reason:
                warning_reasons.append(reason)

        if final_status in {RiskDecisionStatus.ALLOW, RiskDecisionStatus.ALLOW_LIMITED} and blocking_reasons:
            blocking_reasons_copy = list(blocking_reasons)
            blocking_reasons = []
            warning_reasons.extend(blocking_reasons_copy)

        risk_tier = self._determine_tier(risk_tiers, final_status)

        return RiskVerdict(
            schema_version=SCHEMA_RISK_VERDICT,
            verdict_id=new_verdict_id(),
            intent_id=intent.intent_id,
            evaluated_at=datetime.utcnow(),
            status=final_status,
            mode=control_snapshot.mode_state.current_mode,
            risk_tier=risk_tier,
            blocking_reasons=blocking_reasons,
            warning_reasons=warning_reasons,
            constraints=constraints,
            trace={"policy_results": policy_results},
        )

    def _determine_tier(self, risk_tiers: set, status: RiskDecisionStatus) -> str:
        if status in {RiskDecisionStatus.DENY, RiskDecisionStatus.LIQUIDATE_ONLY}:
            return "critical"
        if status in {RiskDecisionStatus.FORCE_REDUCE, RiskDecisionStatus.DEFER}:
            return "elevated"
        if status == RiskDecisionStatus.ALLOW_LIMITED:
            return "cautious"
        return "standard"
