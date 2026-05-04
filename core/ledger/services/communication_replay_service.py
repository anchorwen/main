from core.deployment.domain_keys import (
    PAYLOAD_KEY_ACKNOWLEDGED_MESSAGE_IDS,
    PAYLOAD_KEY_ATTEMPT_SUMMARY,
    PAYLOAD_KEY_CORRELATION_ID,
    PAYLOAD_KEY_CURRENT_STATUS,
    PAYLOAD_KEY_DEGRADED_COUNT,
    PAYLOAD_KEY_DELIVERY_STATE,
    PAYLOAD_KEY_DELIVERY_SUMMARY,
    PAYLOAD_KEY_FAILED_COUNT,
    PAYLOAD_KEY_FINAL_STATUSES,
    PAYLOAD_KEY_GOVERNANCE_SUMMARY,
    PAYLOAD_KEY_ISSUE_CODE,
    PAYLOAD_KEY_ISSUE_COUNTS,
    PAYLOAD_KEY_MESSAGE_COUNT,
    PAYLOAD_KEY_MESSAGE_ID,
    PAYLOAD_KEY_MESSAGE_IDS,
    PAYLOAD_KEY_MESSAGE_PLANS,
    PAYLOAD_KEY_MESSAGE_TYPE,
    PAYLOAD_KEY_NON_REUSABLE_FIELDS,
    PAYLOAD_KEY_RECOMMENDED_STRATEGY,
    PAYLOAD_KEY_REPLAY_PAYLOAD,
    PAYLOAD_KEY_REVIEW_ISSUE_CODES,
    PAYLOAD_KEY_SCOPE,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_TARGET,
    PAYLOAD_KEY_TARGET_ISSUE_CODES,
    PAYLOAD_KEY_TARGET_MESSAGE_IDS,
    REPLAY_NON_REUSABLE_FIELD_ACK_ID,
    REPLAY_NON_REUSABLE_FIELD_ATTEMPTS,
    REPLAY_NON_REUSABLE_FIELD_DEGRADE_REASON,
    REPLAY_NON_REUSABLE_FIELD_DISPATCH_ID,
    REPLAY_NON_REUSABLE_FIELD_FAILURE_REASON,
    REPLAY_REVIEW_CODE_ATTEMPT_HISTORY_REQUIRES_REVIEW,
    REPLAY_STATUS_DEGRADED,
    REPLAY_STATUS_FAILED,
    REPLAY_STRATEGY_DIRECT_REPLAY_CANDIDATE,
    REPLAY_STRATEGY_DO_NOT_REPLAY_TERMINAL_RECEIPT,
    REPLAY_STRATEGY_DO_NOT_REPLAY_TERMINAL_RECEIPTS,
    REPLAY_STRATEGY_REPLAY_AFTER_RECEIPT_TIMEOUT,
    REPLAY_STRATEGY_REPLAY_CORRELATION_DIRECT,
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
    REPLAY_TARGET_CODE_RECEIPT_CANCELLED,
    REPLAY_TARGET_CODE_RECEIPT_FILLED,
    REPLAY_TARGET_CODE_RECEIPT_PARTIALLY_FILLED,
    REPLAY_TARGET_CODE_RECEIPT_REJECTED,
    REPLAY_TARGET_CODE_RECEIPT_TIMEOUT,
    REPLAY_TARGET_CODE_STALE_RECEIPT,
    REPLAY_TRACE_SCOPE_CORRELATION,
    REPLAY_TRACE_SCOPE_MESSAGE,
)
from core.ledger.services.communication_replay_gate import build_replay_governance_summary
from core.ledger.services.communication_trace_refs import (
    attempt_summary as trace_attempt_summary,
)
from core.ledger.services.communication_trace_refs import (
    delivery_state_block as trace_delivery_state,
)
from core.ledger.services.communication_trace_refs import (
    delivery_summary_block as trace_delivery_summary,
)
from core.ledger.services.communication_trace_refs import (
    final_statuses as trace_final_statuses,
)
from core.ledger.services.communication_trace_refs import (
    issue_code as trace_issue_code,
)
from core.ledger.services.communication_trace_refs import (
    issue_counts as trace_issue_counts,
)
from core.ledger.services.communication_trace_refs import (
    issue_message_ids as trace_issue_message_ids,
)
from core.ledger.services.communication_trace_refs import (
    message_count as trace_message_count,
)
from core.ledger.services.communication_trace_refs import (
    message_ids as trace_message_ids,
)
from core.ledger.services.communication_trace_refs import (
    records as trace_records,
)
from core.ledger.services.communication_trace_refs import (
    stale_receipt_message_ids as trace_stale_receipt_message_ids,
)
from core.ledger.services.communication_trace_refs import (
    timed_out_message_ids as trace_timed_out_message_ids,
)
from core.ledger.services.communication_trace_refs import (
    trace_message_id,
)


class CommunicationReplayService:
    def __init__(self, inspection_service):
        self._inspection_service = inspection_service

    def build_message_replay_plan(
        self, *, date_key: str, target: str, message_id: str
    ) -> dict | None:
        trace = self._inspection_service.get_message_trace(
            date_key=date_key,
            target=target,
            message_id=message_id,
        )
        if trace is None:
            return None
        delivery_state = trace_delivery_state(trace)
        issue_code = trace_issue_code(trace)
        plan = {
            PAYLOAD_KEY_SCOPE: REPLAY_TRACE_SCOPE_MESSAGE,
            PAYLOAD_KEY_MESSAGE_ID: trace[PAYLOAD_KEY_MESSAGE_ID],
            PAYLOAD_KEY_CORRELATION_ID: trace[PAYLOAD_KEY_CORRELATION_ID],
            PAYLOAD_KEY_TARGET: trace[PAYLOAD_KEY_TARGET],
            PAYLOAD_KEY_MESSAGE_TYPE: trace[PAYLOAD_KEY_MESSAGE_TYPE],
            PAYLOAD_KEY_CURRENT_STATUS: trace[PAYLOAD_KEY_STATUS],
            PAYLOAD_KEY_ATTEMPT_SUMMARY: trace[PAYLOAD_KEY_ATTEMPT_SUMMARY],
            PAYLOAD_KEY_DELIVERY_STATE: delivery_state,
            PAYLOAD_KEY_ISSUE_CODE: issue_code,
            PAYLOAD_KEY_TARGET_ISSUE_CODES: [issue_code] if issue_code is not None else [],
            PAYLOAD_KEY_REVIEW_ISSUE_CODES: self._select_review_issue_codes_for_message(trace),
            PAYLOAD_KEY_REPLAY_PAYLOAD: {
                PAYLOAD_KEY_MESSAGE_ID: trace[PAYLOAD_KEY_MESSAGE_ID],
                PAYLOAD_KEY_CORRELATION_ID: trace[PAYLOAD_KEY_CORRELATION_ID],
                PAYLOAD_KEY_TARGET: trace[PAYLOAD_KEY_TARGET],
            },
            PAYLOAD_KEY_NON_REUSABLE_FIELDS: [
                REPLAY_NON_REUSABLE_FIELD_DISPATCH_ID,
                REPLAY_NON_REUSABLE_FIELD_ACK_ID,
                REPLAY_NON_REUSABLE_FIELD_ATTEMPTS,
                REPLAY_NON_REUSABLE_FIELD_FAILURE_REASON,
                REPLAY_NON_REUSABLE_FIELD_DEGRADE_REASON,
            ],
            PAYLOAD_KEY_RECOMMENDED_STRATEGY: self._recommend_strategy(trace),
        }
        return {
            **plan,
            PAYLOAD_KEY_GOVERNANCE_SUMMARY: build_replay_governance_summary(plan, None),
        }

    def build_correlation_replay_plan(
        self, *, date_key: str, target: str, correlation_id: str
    ) -> dict:
        trace = self._inspection_service.get_correlation_trace(
            date_key=date_key,
            target=target,
            correlation_id=correlation_id,
        )
        records = trace_records(trace)
        delivery_summary = trace_delivery_summary(trace)
        message_plans = [
            self.build_message_replay_plan(
                date_key=date_key,
                target=target,
                message_id=trace_message_id(item),  # type: ignore[reportArgumentType]
            )
            for item in records
            if trace_message_id(item) is not None
        ]
        message_plans = [item for item in message_plans if item is not None]
        plan = {
            PAYLOAD_KEY_SCOPE: REPLAY_TRACE_SCOPE_CORRELATION,
            PAYLOAD_KEY_CORRELATION_ID: correlation_id,
            PAYLOAD_KEY_MESSAGE_COUNT: trace_message_count(trace),
            PAYLOAD_KEY_MESSAGE_IDS: trace_message_ids(trace),
            PAYLOAD_KEY_FINAL_STATUSES: trace_final_statuses(trace),
            PAYLOAD_KEY_DELIVERY_SUMMARY: delivery_summary,
            PAYLOAD_KEY_ISSUE_COUNTS: trace_issue_counts(trace),
            PAYLOAD_KEY_MESSAGE_PLANS: message_plans,
            PAYLOAD_KEY_TARGET_MESSAGE_IDS: self._select_target_message_ids(trace),
            PAYLOAD_KEY_TARGET_ISSUE_CODES: self._select_target_issue_codes(trace),
            PAYLOAD_KEY_REVIEW_ISSUE_CODES: self._select_review_issue_codes_for_correlation(trace),
            PAYLOAD_KEY_RECOMMENDED_STRATEGY: self._recommend_correlation_strategy(trace),
        }
        return {
            **plan,
            PAYLOAD_KEY_GOVERNANCE_SUMMARY: build_replay_governance_summary(plan, None),
        }

    def _recommend_strategy(self, trace: dict) -> str:
        attempt_summary = trace_attempt_summary(trace)
        issue_code = trace_issue_code(trace)
        if issue_code == REPLAY_TARGET_CODE_RECEIPT_TIMEOUT:
            return REPLAY_STRATEGY_REPLAY_AFTER_RECEIPT_TIMEOUT
        if issue_code == REPLAY_TARGET_CODE_STALE_RECEIPT:
            return REPLAY_STRATEGY_REVIEW_STALE_RECEIPT_BEFORE_REPLAY
        if issue_code == REPLAY_TARGET_CODE_RECEIPT_REJECTED:
            return REPLAY_STRATEGY_REVIEW_REJECTED_RECEIPT_BEFORE_REPLAY
        if issue_code in {
            REPLAY_TARGET_CODE_RECEIPT_ACCEPTED,
            REPLAY_TARGET_CODE_RECEIPT_PARTIALLY_FILLED,
            REPLAY_TARGET_CODE_RECEIPT_FILLED,
        }:
            return REPLAY_STRATEGY_DO_NOT_REPLAY_TERMINAL_RECEIPT
        if issue_code == REPLAY_TARGET_CODE_RECEIPT_CANCELLED:
            return REPLAY_STRATEGY_REVIEW_CANCELLED_RECEIPT_BEFORE_REPLAY
        if (
            attempt_summary.get(PAYLOAD_KEY_FAILED_COUNT, 0) > 0
            or attempt_summary.get(PAYLOAD_KEY_DEGRADED_COUNT, 0) > 0
        ):
            return REPLAY_STRATEGY_REPLAY_WITH_GOVERNANCE_REVIEW
        return REPLAY_STRATEGY_DIRECT_REPLAY_CANDIDATE

    def _recommend_correlation_strategy(self, trace: dict) -> str:
        final_statuses = trace_final_statuses(trace)
        delivery_summary = trace_delivery_summary(trace)
        issue_counts = trace_issue_counts(trace)
        timed_out_message_ids = trace_timed_out_message_ids(trace)
        acknowledged_message_ids = delivery_summary.get(PAYLOAD_KEY_ACKNOWLEDGED_MESSAGE_IDS, [])
        message_count = trace_message_count(trace)

        if any(
            status in {REPLAY_STATUS_DEGRADED, REPLAY_STATUS_FAILED} for status in final_statuses
        ):
            return REPLAY_STRATEGY_REPLAY_CORRELATION_WITH_SEQUENCED_REVIEW
        if issue_counts.get(REPLAY_TARGET_CODE_STALE_RECEIPT, 0) > 0:
            return REPLAY_STRATEGY_REVIEW_STALE_RECEIPTS_BEFORE_REPLAY
        if issue_counts.get(REPLAY_TARGET_CODE_RECEIPT_REJECTED, 0) > 0:
            return REPLAY_STRATEGY_REVIEW_REJECTED_RECEIPTS_BEFORE_REPLAY
        if issue_counts.get(REPLAY_TARGET_CODE_RECEIPT_CANCELLED, 0) > 0:
            return REPLAY_STRATEGY_REVIEW_CANCELLED_RECEIPTS_BEFORE_REPLAY
        if (
            issue_counts.get(REPLAY_TARGET_CODE_RECEIPT_ACCEPTED, 0) > 0
            or issue_counts.get(REPLAY_TARGET_CODE_RECEIPT_PARTIALLY_FILLED, 0) > 0
            or issue_counts.get(REPLAY_TARGET_CODE_RECEIPT_FILLED, 0) > 0
        ):
            return REPLAY_STRATEGY_DO_NOT_REPLAY_TERMINAL_RECEIPTS
        if (
            timed_out_message_ids
            and len(acknowledged_message_ids) + len(timed_out_message_ids) == message_count
        ):
            return REPLAY_STRATEGY_REPLAY_ONLY_TIMED_OUT_MESSAGES
        return REPLAY_STRATEGY_REPLAY_CORRELATION_DIRECT

    def _select_target_message_ids(self, trace: dict) -> list[str]:
        timed_out_message_ids = trace_timed_out_message_ids(trace)
        stale_receipt_message_ids = trace_stale_receipt_message_ids(trace)
        issue_message_ids = trace_issue_message_ids(trace)
        rejected_message_ids = issue_message_ids.get(REPLAY_TARGET_CODE_RECEIPT_REJECTED, [])
        cancelled_message_ids = issue_message_ids.get(REPLAY_TARGET_CODE_RECEIPT_CANCELLED, [])
        if stale_receipt_message_ids:
            return stale_receipt_message_ids
        if rejected_message_ids:
            return rejected_message_ids
        if cancelled_message_ids:
            return cancelled_message_ids
        if timed_out_message_ids:
            return timed_out_message_ids
        return trace_message_ids(trace)

    def _select_target_issue_codes(self, trace: dict) -> list[str]:
        issue_counts = trace_issue_counts(trace)
        if issue_counts.get(REPLAY_TARGET_CODE_STALE_RECEIPT, 0) > 0:
            return [REPLAY_TARGET_CODE_STALE_RECEIPT]
        if issue_counts.get(REPLAY_TARGET_CODE_RECEIPT_REJECTED, 0) > 0:
            return [REPLAY_TARGET_CODE_RECEIPT_REJECTED]
        if issue_counts.get(REPLAY_TARGET_CODE_RECEIPT_CANCELLED, 0) > 0:
            return [REPLAY_TARGET_CODE_RECEIPT_CANCELLED]
        if issue_counts.get(REPLAY_TARGET_CODE_RECEIPT_TIMEOUT, 0) > 0:
            return [REPLAY_TARGET_CODE_RECEIPT_TIMEOUT]
        if issue_counts.get(REPLAY_TARGET_CODE_RECEIPT_ACCEPTED, 0) > 0:
            return [REPLAY_TARGET_CODE_RECEIPT_ACCEPTED]
        if issue_counts.get(REPLAY_TARGET_CODE_RECEIPT_PARTIALLY_FILLED, 0) > 0:
            return [REPLAY_TARGET_CODE_RECEIPT_PARTIALLY_FILLED]
        if issue_counts.get(REPLAY_TARGET_CODE_RECEIPT_FILLED, 0) > 0:
            return [REPLAY_TARGET_CODE_RECEIPT_FILLED]
        return list(issue_counts.keys())

    def _select_review_issue_codes_for_message(self, trace: dict) -> list[str]:
        issue_code = trace_issue_code(trace)
        if issue_code in {
            REPLAY_TARGET_CODE_STALE_RECEIPT,
            REPLAY_TARGET_CODE_RECEIPT_REJECTED,
            REPLAY_TARGET_CODE_RECEIPT_CANCELLED,
            REPLAY_TARGET_CODE_RECEIPT_PARTIALLY_FILLED,
        }:
            return [issue_code]
        attempt_summary = trace_attempt_summary(trace)
        if (
            attempt_summary.get(PAYLOAD_KEY_FAILED_COUNT, 0) > 0
            or attempt_summary.get(PAYLOAD_KEY_DEGRADED_COUNT, 0) > 0
        ):
            return [REPLAY_REVIEW_CODE_ATTEMPT_HISTORY_REQUIRES_REVIEW]
        return []

    def _select_review_issue_codes_for_correlation(self, trace: dict) -> list[str]:
        issue_counts = trace_issue_counts(trace)
        review_issue_codes = []
        if issue_counts.get(REPLAY_TARGET_CODE_STALE_RECEIPT, 0) > 0:
            review_issue_codes.append(REPLAY_TARGET_CODE_STALE_RECEIPT)
        if issue_counts.get(REPLAY_TARGET_CODE_RECEIPT_REJECTED, 0) > 0:
            review_issue_codes.append(REPLAY_TARGET_CODE_RECEIPT_REJECTED)
        if issue_counts.get(REPLAY_TARGET_CODE_RECEIPT_CANCELLED, 0) > 0:
            review_issue_codes.append(REPLAY_TARGET_CODE_RECEIPT_CANCELLED)
        if issue_counts.get(REPLAY_TARGET_CODE_RECEIPT_PARTIALLY_FILLED, 0) > 0:
            review_issue_codes.append(REPLAY_TARGET_CODE_RECEIPT_PARTIALLY_FILLED)
        if any(
            status in {REPLAY_STATUS_DEGRADED, REPLAY_STATUS_FAILED}
            for status in trace_final_statuses(trace)
        ):
            review_issue_codes.append(REPLAY_REVIEW_CODE_ATTEMPT_HISTORY_REQUIRES_REVIEW)
        return review_issue_codes
