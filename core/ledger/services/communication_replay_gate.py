from core.contracts.enums import ReplayGateDecision
from core.deployment.domain_keys import (
    PAYLOAD_KEY_ATTEMPT_COUNT,
    PAYLOAD_KEY_ATTEMPT_SUMMARY,
    PAYLOAD_KEY_CURRENT_STATUS,
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_GOVERNANCE_SUMMARY,
    PAYLOAD_KEY_GOVERNANCE_TAGS,
    PAYLOAD_KEY_POSTURE,
    PAYLOAD_KEY_REASONS,
    PAYLOAD_KEY_RECOMMENDED_STRATEGY,
    PAYLOAD_KEY_REVIEW_ISSUE_CODES,
    PAYLOAD_KEY_TARGET_ISSUE_CODES,
    REPLAY_GATE_REASON_CANCELLED_RECEIPT_DETECTED,
    REPLAY_GATE_REASON_CLEAN_CORRELATION_REPLAY_CANDIDATE,
    REPLAY_GATE_REASON_CLEAN_REPLAY_CANDIDATE,
    REPLAY_GATE_REASON_CORRELATION_CONTAINS_CANCELLED_RECEIPT,
    REPLAY_GATE_REASON_CORRELATION_CONTAINS_FAILED_MESSAGE,
    REPLAY_GATE_REASON_CORRELATION_CONTAINS_NON_CLEAN_HISTORY,
    REPLAY_GATE_REASON_CORRELATION_CONTAINS_REJECTED_RECEIPT,
    REPLAY_GATE_REASON_CORRELATION_CONTAINS_STALE_RECEIPT,
    REPLAY_GATE_REASON_CORRELATION_CONTAINS_TERMINAL_RECEIPTS,
    REPLAY_GATE_REASON_EMPTY_CORRELATION_PLAN,
    REPLAY_GATE_REASON_MESSAGE_FAILED_PREVIOUSLY,
    REPLAY_GATE_REASON_MISSING_ATTEMPT_HISTORY,
    REPLAY_GATE_REASON_MISSING_REPLAY_PLAN,
    REPLAY_GATE_REASON_NON_CLEAN_ATTEMPT_HISTORY,
    REPLAY_GATE_REASON_REJECTED_RECEIPT_DETECTED,
    REPLAY_GATE_REASON_STALE_RECEIPT_DETECTED,
    REPLAY_GATE_REASON_TARGETED_TIMEOUT_REPLAY_CANDIDATE,
    REPLAY_GATE_REASON_TERMINAL_RECEIPT_ALREADY_RECORDED,
    REPLAY_GOVERNANCE_POSTURE_VALUE_AUTO_REPLAY,
    REPLAY_GOVERNANCE_POSTURE_VALUE_BLOCKED,
    REPLAY_GOVERNANCE_POSTURE_VALUE_HEALTHY,
    REPLAY_GOVERNANCE_POSTURE_VALUE_REVIEW_REQUIRED,
    REPLAY_GOVERNANCE_POSTURE_VALUE_TARGETED_REPLAY,
    REPLAY_GOVERNANCE_POSTURE_VALUE_UNKNOWN,
    REPLAY_STATUS_FAILED,
    REPLAY_STRATEGY_DO_NOT_REPLAY_TERMINAL_RECEIPT,
    REPLAY_STRATEGY_DO_NOT_REPLAY_TERMINAL_RECEIPTS,
    REPLAY_STRATEGY_REPLAY_CORRELATION_WITH_SEQUENCED_REVIEW,
    REPLAY_STRATEGY_REPLAY_ONLY_TIMED_OUT_MESSAGES,
    REPLAY_STRATEGY_REPLAY_WITH_GOVERNANCE_REVIEW,
    REPLAY_STRATEGY_REVIEW_CANCELLED_RECEIPT_BEFORE_REPLAY,
    REPLAY_STRATEGY_REVIEW_CANCELLED_RECEIPTS_BEFORE_REPLAY,
    REPLAY_STRATEGY_REVIEW_REJECTED_RECEIPT_BEFORE_REPLAY,
    REPLAY_STRATEGY_REVIEW_REJECTED_RECEIPTS_BEFORE_REPLAY,
    REPLAY_STRATEGY_REVIEW_STALE_RECEIPT_BEFORE_REPLAY,
    REPLAY_STRATEGY_REVIEW_STALE_RECEIPTS_BEFORE_REPLAY,
    REPLAY_TARGET_CODE_RECEIPT_ACCEPTED,
    REPLAY_TARGET_CODE_RECEIPT_FILLED,
    REPLAY_TARGET_CODE_RECEIPT_PARTIALLY_FILLED,
    REPLAY_TARGET_CODE_RECEIPT_TIMEOUT,
)
from core.ledger.services.gate_decision_refs import decision as gate_decision_value
from core.ledger.services.replay_plan_refs import (
    final_statuses as plan_final_statuses,
)
from core.ledger.services.replay_plan_refs import (
    message_count as plan_message_count,
)
from core.ledger.services.replay_plan_refs import (
    recommended_strategy as plan_recommended_strategy,
)


def build_replay_governance_summary(replay_plan: dict | None, replay_gate: dict | None) -> dict:
    """Build the canonical replay governance summary shared by gate, record, and views."""
    replay_plan = replay_plan or {}
    replay_gate = replay_gate or {}
    governance_tags = replay_gate.get(PAYLOAD_KEY_GOVERNANCE_TAGS, [])
    review_issue_codes = replay_plan.get(PAYLOAD_KEY_REVIEW_ISSUE_CODES, [])
    target_issue_codes = replay_plan.get(PAYLOAD_KEY_TARGET_ISSUE_CODES, [])
    recommended_strategy = plan_recommended_strategy(replay_plan)
    decision = gate_decision_value(replay_gate)

    if decision == ReplayGateDecision.DENY:
        posture = REPLAY_GOVERNANCE_POSTURE_VALUE_BLOCKED
    elif decision == ReplayGateDecision.REVIEW:
        posture = REPLAY_GOVERNANCE_POSTURE_VALUE_REVIEW_REQUIRED
    elif target_issue_codes in (
        [REPLAY_TARGET_CODE_RECEIPT_ACCEPTED],
        [REPLAY_TARGET_CODE_RECEIPT_PARTIALLY_FILLED],
        [REPLAY_TARGET_CODE_RECEIPT_FILLED],
    ):
        posture = REPLAY_GOVERNANCE_POSTURE_VALUE_HEALTHY
    elif target_issue_codes == [REPLAY_TARGET_CODE_RECEIPT_TIMEOUT]:
        posture = REPLAY_GOVERNANCE_POSTURE_VALUE_TARGETED_REPLAY
    elif review_issue_codes:
        posture = REPLAY_GOVERNANCE_POSTURE_VALUE_REVIEW_REQUIRED
    elif decision == ReplayGateDecision.ALLOW:
        posture = REPLAY_GOVERNANCE_POSTURE_VALUE_AUTO_REPLAY
    else:
        posture = REPLAY_GOVERNANCE_POSTURE_VALUE_UNKNOWN

    return {
        PAYLOAD_KEY_DECISION: decision,
        PAYLOAD_KEY_POSTURE: posture,
        PAYLOAD_KEY_RECOMMENDED_STRATEGY: recommended_strategy,
        PAYLOAD_KEY_TARGET_ISSUE_CODES: target_issue_codes,
        PAYLOAD_KEY_REVIEW_ISSUE_CODES: review_issue_codes,
        PAYLOAD_KEY_GOVERNANCE_TAGS: governance_tags,
    }


class CommunicationReplayGate:
    def evaluate_message_plan(self, replay_plan: dict | None) -> dict:
        if replay_plan is None:
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.DENY,
                [REPLAY_GATE_REASON_MISSING_REPLAY_PLAN],
                ["replay_unavailable"],
            )

        strategy = plan_recommended_strategy(replay_plan)
        current_status = replay_plan.get(PAYLOAD_KEY_CURRENT_STATUS)
        attempt_summary = replay_plan.get(PAYLOAD_KEY_ATTEMPT_SUMMARY, {})

        if current_status == REPLAY_STATUS_FAILED:
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.REVIEW,
                [REPLAY_GATE_REASON_MESSAGE_FAILED_PREVIOUSLY],
                ["requires_manual_review", "failed_history"],
            )

        if strategy == REPLAY_STRATEGY_REVIEW_STALE_RECEIPT_BEFORE_REPLAY:
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.REVIEW,
                [REPLAY_GATE_REASON_STALE_RECEIPT_DETECTED],
                ["requires_governance_review", "stale_receipt"],
            )

        if strategy == REPLAY_STRATEGY_REVIEW_REJECTED_RECEIPT_BEFORE_REPLAY:
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.REVIEW,
                [REPLAY_GATE_REASON_REJECTED_RECEIPT_DETECTED],
                ["requires_governance_review", "receipt_rejected"],
            )

        if strategy == REPLAY_STRATEGY_REVIEW_CANCELLED_RECEIPT_BEFORE_REPLAY:
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.REVIEW,
                [REPLAY_GATE_REASON_CANCELLED_RECEIPT_DETECTED],
                ["requires_governance_review", "receipt_cancelled"],
            )

        if strategy == REPLAY_STRATEGY_DO_NOT_REPLAY_TERMINAL_RECEIPT:
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.DENY,
                [REPLAY_GATE_REASON_TERMINAL_RECEIPT_ALREADY_RECORDED],
                ["replay_not_required", "terminal_receipt"],
            )

        if strategy == REPLAY_STRATEGY_REPLAY_WITH_GOVERNANCE_REVIEW:
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.REVIEW,
                [REPLAY_GATE_REASON_NON_CLEAN_ATTEMPT_HISTORY],
                ["requires_governance_review", "degraded_or_failed_attempts"],
            )

        if attempt_summary.get(PAYLOAD_KEY_ATTEMPT_COUNT, 0) == 0:
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.DENY,
                [REPLAY_GATE_REASON_MISSING_ATTEMPT_HISTORY],
                ["insufficient_trace"],
            )

        return self._build_decision(
            replay_plan,
            ReplayGateDecision.ALLOW,
            [REPLAY_GATE_REASON_CLEAN_REPLAY_CANDIDATE],
            ["auto_replay_eligible"],
        )

    def evaluate_correlation_plan(self, replay_plan: dict) -> dict:
        if plan_message_count(replay_plan) == 0:
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.DENY,
                [REPLAY_GATE_REASON_EMPTY_CORRELATION_PLAN],
                ["replay_unavailable"],
            )

        strategy = plan_recommended_strategy(replay_plan)
        final_statuses = plan_final_statuses(replay_plan)

        if any(status == REPLAY_STATUS_FAILED for status in final_statuses):
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.REVIEW,
                [REPLAY_GATE_REASON_CORRELATION_CONTAINS_FAILED_MESSAGE],
                ["sequenced_review_required", "failed_history"],
            )

        if strategy == REPLAY_STRATEGY_REVIEW_STALE_RECEIPTS_BEFORE_REPLAY:
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.REVIEW,
                [REPLAY_GATE_REASON_CORRELATION_CONTAINS_STALE_RECEIPT],
                ["sequenced_review_required", "stale_receipt"],
            )

        if strategy == REPLAY_STRATEGY_REVIEW_REJECTED_RECEIPTS_BEFORE_REPLAY:
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.REVIEW,
                [REPLAY_GATE_REASON_CORRELATION_CONTAINS_REJECTED_RECEIPT],
                ["sequenced_review_required", "receipt_rejected"],
            )

        if strategy == REPLAY_STRATEGY_REVIEW_CANCELLED_RECEIPTS_BEFORE_REPLAY:
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.REVIEW,
                [REPLAY_GATE_REASON_CORRELATION_CONTAINS_CANCELLED_RECEIPT],
                ["sequenced_review_required", "receipt_cancelled"],
            )

        if strategy == REPLAY_STRATEGY_DO_NOT_REPLAY_TERMINAL_RECEIPTS:
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.DENY,
                [REPLAY_GATE_REASON_CORRELATION_CONTAINS_TERMINAL_RECEIPTS],
                ["replay_not_required", "terminal_receipt"],
            )

        if strategy == REPLAY_STRATEGY_REPLAY_CORRELATION_WITH_SEQUENCED_REVIEW:
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.REVIEW,
                [REPLAY_GATE_REASON_CORRELATION_CONTAINS_NON_CLEAN_HISTORY],
                ["sequenced_review_required", "degraded_history"],
            )

        if strategy == REPLAY_STRATEGY_REPLAY_ONLY_TIMED_OUT_MESSAGES:
            return self._build_decision(
                replay_plan,
                ReplayGateDecision.ALLOW,
                [REPLAY_GATE_REASON_TARGETED_TIMEOUT_REPLAY_CANDIDATE],
                ["auto_replay_eligible", "timeout_targeted_replay"],
            )

        return self._build_decision(
            replay_plan,
            ReplayGateDecision.ALLOW,
            [REPLAY_GATE_REASON_CLEAN_CORRELATION_REPLAY_CANDIDATE],
            ["auto_replay_eligible"],
        )

    def _build_decision(
        self,
        replay_plan: dict | None,
        decision: str,
        reasons: list[str],
        governance_tags: list[str],
    ) -> dict:
        gate_decision = {
            PAYLOAD_KEY_DECISION: decision,
            PAYLOAD_KEY_REASONS: reasons,
            PAYLOAD_KEY_GOVERNANCE_TAGS: governance_tags,
        }
        gate_decision[PAYLOAD_KEY_GOVERNANCE_SUMMARY] = build_replay_governance_summary(
            replay_plan, gate_decision
        )
        return gate_decision
