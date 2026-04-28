from core.contracts.enums import ReplayGateDecision
from core.deployment.domain_keys import (
    PAYLOAD_KEY_BLOCK_REASONS,
    PAYLOAD_KEY_DISPATCH_RESULT,
    PAYLOAD_KEY_GATE_DECISION,
    PAYLOAD_KEY_SKIP_REASONS,
    PAYLOAD_KEY_MESSAGE_ID,
    PAYLOAD_KEY_REASON,
    PAYLOAD_KEY_RESULTS,
    PAYLOAD_KEY_BLOCKED_MESSAGE_IDS,
    PAYLOAD_KEY_BLOCKED_MESSAGES,
    PAYLOAD_KEY_CORRELATION_ID,
    PAYLOAD_KEY_EXECUTION_STATE,
    PAYLOAD_KEY_MESSAGE_COUNT,
    PAYLOAD_KEY_RECOMMENDED_STRATEGY,
    PAYLOAD_KEY_REPLAY,
    PAYLOAD_KEY_REPLAY_CORRELATION_ID,
    PAYLOAD_KEY_REPLAY_LEDGER_PATH,
    PAYLOAD_KEY_REPLAY_MESSAGE_ID,
    PAYLOAD_KEY_REPLAY_RECORD,
    PAYLOAD_KEY_REPLAY_TRACE,
    PAYLOAD_KEY_SCOPE,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_SKIPPED_MESSAGE_IDS,
    PAYLOAD_KEY_SKIPPED_MESSAGES,
    PAYLOAD_KEY_TARGET_MESSAGE_IDS,
    REPLAY_GATE_REASON_CANCELLED_RECEIPT_DETECTED,
    REPLAY_GATE_REASON_CORRELATION_CONTAINS_CANCELLED_RECEIPT,
    REPLAY_GATE_REASON_CORRELATION_CONTAINS_FAILED_MESSAGE,
    REPLAY_GATE_REASON_CORRELATION_CONTAINS_NON_CLEAN_HISTORY,
    REPLAY_GATE_REASON_CORRELATION_CONTAINS_REJECTED_RECEIPT,
    REPLAY_GATE_REASON_CORRELATION_CONTAINS_STALE_RECEIPT,
    REPLAY_GATE_REASON_CORRELATION_CONTAINS_TERMINAL_RECEIPTS,
    REPLAY_GATE_REASON_MESSAGE_FAILED_PREVIOUSLY,
    REPLAY_GATE_REASON_NON_CLEAN_ATTEMPT_HISTORY,
    REPLAY_GATE_REASON_REJECTED_RECEIPT_DETECTED,
    REPLAY_GATE_REASON_STALE_RECEIPT_DETECTED,
    REPLAY_GATE_REASON_TERMINAL_RECEIPT_ALREADY_RECORDED,
    REPLAY_EXECUTOR_BLOCK_REASON_CANCELLED_RECEIPT,
    REPLAY_EXECUTOR_BLOCK_REASON_GATE_BLOCKED,
    REPLAY_EXECUTOR_BLOCK_REASON_REJECTED_RECEIPT,
    REPLAY_EXECUTOR_BLOCK_REASON_REVIEW_REQUIRED,
    REPLAY_EXECUTOR_BLOCK_REASON_STALE_RECEIPT,
    REPLAY_EXECUTOR_BLOCK_REASON_TERMINAL_RECEIPT,
    REPLAY_EXECUTOR_SKIP_REASON_ACKNOWLEDGED_MESSAGE,
    REPLAY_EXECUTOR_SKIP_REASON_NOT_TARGETED,
    REPLAY_EXECUTION_STATE_DISPATCHED,
    REPLAY_EXECUTION_STATE_NOT_EXECUTED,
    REPLAY_EXECUTION_STATUS_BLOCKED,
    REPLAY_EXECUTION_STATUS_EXECUTED,
    REPLAY_TRACE_SCOPE_CORRELATION,
    REPLAY_TRACE_SCOPE_MESSAGE,
)
from core.ledger.services.gate_decision_refs import (
    decision as gate_decision_value,
    reason_set as gate_reason_set,
)
from core.ledger.services.replay_plan_refs import (
    acknowledged_message_ids as plan_acknowledged_message_ids,
    correlation_id as plan_correlation_id,
    message_id as plan_message_id,
    message_ids as plan_message_ids,
    message_plans as plan_message_plans,
    recommended_strategy as plan_recommended_strategy,
    target_message_ids as plan_target_message_ids,
)
from core.ledger.services.replay_trace_refs import (
    blocked_message_ids as trace_blocked_message_ids,
    correlation_id as trace_correlation_id,
    execution_state as trace_execution_state,
    message_count as trace_message_count,
    message_id as trace_message_id,
    scope as trace_scope,
    skipped_message_ids as trace_skipped_message_ids,
    target_message_ids as trace_target_message_ids,
)
from core.ledger.services.replay_record_refs import grouped_reasons as replay_grouped_reasons


class CommunicationReplayExecutor:
    # Backward-compatible aliases for tests/integrations that reference class attributes directly.
    SKIP_REASON_NOT_TARGETED = REPLAY_EXECUTOR_SKIP_REASON_NOT_TARGETED
    SKIP_REASON_ACKNOWLEDGED = REPLAY_EXECUTOR_SKIP_REASON_ACKNOWLEDGED_MESSAGE
    BLOCK_REASON_GATE_BLOCKED = REPLAY_EXECUTOR_BLOCK_REASON_GATE_BLOCKED
    BLOCK_REASON_STALE_RECEIPT = REPLAY_EXECUTOR_BLOCK_REASON_STALE_RECEIPT
    BLOCK_REASON_REJECTED_RECEIPT = REPLAY_EXECUTOR_BLOCK_REASON_REJECTED_RECEIPT
    BLOCK_REASON_CANCELLED_RECEIPT = REPLAY_EXECUTOR_BLOCK_REASON_CANCELLED_RECEIPT
    BLOCK_REASON_REVIEW_REQUIRED = REPLAY_EXECUTOR_BLOCK_REASON_REVIEW_REQUIRED
    BLOCK_REASON_TERMINAL_RECEIPT = REPLAY_EXECUTOR_BLOCK_REASON_TERMINAL_RECEIPT

    def __init__(self, replay_gate, dispatcher, replay_execution_writer=None):
        self._replay_gate = replay_gate
        self._dispatcher = dispatcher
        self._replay_execution_writer = replay_execution_writer

    def execute_message_replay(self, replay_plan: dict | None, envelope, *, route_policy=None, transport_hints=None, governance=None) -> dict:
        gate_decision = self._replay_gate.evaluate_message_plan(replay_plan)
        decision = gate_decision_value(gate_decision)

        if decision != ReplayGateDecision.ALLOW:
            blocked_messages = self._build_message_block_entries(replay_plan, gate_decision)
            result = {
                PAYLOAD_KEY_STATUS: REPLAY_EXECUTION_STATUS_BLOCKED,
                PAYLOAD_KEY_GATE_DECISION: gate_decision,
                PAYLOAD_KEY_DISPATCH_RESULT: None,
                PAYLOAD_KEY_BLOCKED_MESSAGES: blocked_messages,
                PAYLOAD_KEY_SKIPPED_MESSAGES: [],
                PAYLOAD_KEY_SKIP_REASONS: {},
                PAYLOAD_KEY_BLOCK_REASONS: replay_grouped_reasons(blocked_messages),
                PAYLOAD_KEY_REPLAY_TRACE: {
                    PAYLOAD_KEY_SCOPE: REPLAY_TRACE_SCOPE_MESSAGE,
                    PAYLOAD_KEY_MESSAGE_ID: plan_message_id(replay_plan),
                    PAYLOAD_KEY_CORRELATION_ID: plan_correlation_id(replay_plan),
                    PAYLOAD_KEY_EXECUTION_STATE: REPLAY_EXECUTION_STATE_NOT_EXECUTED,
                },
            }
            result[PAYLOAD_KEY_REPLAY_TRACE] = self._trace_summary(result)
            return self._attach_replay_record(result, envelope=envelope)

        dispatch_result = self._dispatcher.dispatch(
            envelope,
            route_policy=route_policy or {},
            transport_hints=transport_hints or {},
            governance={
                **(governance or {}),
                PAYLOAD_KEY_REPLAY: True,
                PAYLOAD_KEY_REPLAY_MESSAGE_ID: plan_message_id(replay_plan),
            },
        )
        result = {
            PAYLOAD_KEY_STATUS: REPLAY_EXECUTION_STATUS_EXECUTED,
            PAYLOAD_KEY_GATE_DECISION: gate_decision,
            PAYLOAD_KEY_DISPATCH_RESULT: dispatch_result,
            PAYLOAD_KEY_RESULTS: [
                {
                    PAYLOAD_KEY_MESSAGE_ID: plan_message_id(replay_plan),
                    PAYLOAD_KEY_DISPATCH_RESULT: dispatch_result,
                }
            ],
            PAYLOAD_KEY_SKIPPED_MESSAGES: [],
            PAYLOAD_KEY_BLOCKED_MESSAGES: [],
            PAYLOAD_KEY_SKIP_REASONS: {},
            PAYLOAD_KEY_BLOCK_REASONS: {},
            PAYLOAD_KEY_REPLAY_TRACE: {
                PAYLOAD_KEY_SCOPE: REPLAY_TRACE_SCOPE_MESSAGE,
                PAYLOAD_KEY_MESSAGE_ID: plan_message_id(replay_plan),
                PAYLOAD_KEY_CORRELATION_ID: plan_correlation_id(replay_plan),
                PAYLOAD_KEY_EXECUTION_STATE: REPLAY_EXECUTION_STATE_DISPATCHED,
                PAYLOAD_KEY_RECOMMENDED_STRATEGY: plan_recommended_strategy(replay_plan),
            },
        }
        result[PAYLOAD_KEY_REPLAY_TRACE] = self._trace_summary(result)
        return self._attach_replay_record(result, envelope=envelope)

    def execute_correlation_replay(self, replay_plan: dict, envelopes_by_message_id: dict, *, route_policy=None, transport_hints=None, governance=None) -> dict:
        gate_decision = self._replay_gate.evaluate_correlation_plan(replay_plan)
        decision = gate_decision_value(gate_decision)

        sample_envelope = next(iter(envelopes_by_message_id.values()), None)
        target_message_ids = plan_target_message_ids(replay_plan)
        skipped_message_ids = [
            message_id
            for message_id in plan_message_ids(replay_plan)
            if message_id not in target_message_ids
        ]

        if decision != ReplayGateDecision.ALLOW:
            blocked_messages = self._build_blocked_entries(target_message_ids, gate_decision)
            skipped_messages = self._build_skipped_entries(replay_plan, skipped_message_ids)
            result = {
                PAYLOAD_KEY_STATUS: REPLAY_EXECUTION_STATUS_BLOCKED,
                PAYLOAD_KEY_GATE_DECISION: gate_decision,
                PAYLOAD_KEY_RESULTS: [],
                PAYLOAD_KEY_BLOCKED_MESSAGES: blocked_messages,
                PAYLOAD_KEY_SKIPPED_MESSAGES: skipped_messages,
                PAYLOAD_KEY_SKIP_REASONS: replay_grouped_reasons(skipped_messages),
                PAYLOAD_KEY_BLOCK_REASONS: replay_grouped_reasons(blocked_messages),
                PAYLOAD_KEY_REPLAY_TRACE: {
                    PAYLOAD_KEY_SCOPE: REPLAY_TRACE_SCOPE_CORRELATION,
                    PAYLOAD_KEY_CORRELATION_ID: plan_correlation_id(replay_plan),
                    PAYLOAD_KEY_EXECUTION_STATE: REPLAY_EXECUTION_STATE_NOT_EXECUTED,
                    PAYLOAD_KEY_TARGET_MESSAGE_IDS: target_message_ids,
                    PAYLOAD_KEY_BLOCKED_MESSAGE_IDS: target_message_ids,
                    PAYLOAD_KEY_SKIPPED_MESSAGE_IDS: skipped_message_ids,
                },
            }
            result[PAYLOAD_KEY_REPLAY_TRACE] = self._trace_summary(result)
            return self._attach_replay_record(result, envelope=sample_envelope)

        results = []
        for message_plan in plan_message_plans(replay_plan):
            message_id = plan_message_id(message_plan)
            if message_id not in target_message_ids:
                continue
            envelope = envelopes_by_message_id[message_id]
            dispatch_result = self._dispatcher.dispatch(
                envelope,
                route_policy=route_policy or {},
                transport_hints=transport_hints or {},
                governance={
                    **(governance or {}),
                    PAYLOAD_KEY_REPLAY: True,
                    PAYLOAD_KEY_REPLAY_CORRELATION_ID: plan_correlation_id(replay_plan),
                    PAYLOAD_KEY_REPLAY_MESSAGE_ID: message_id,
                },
            )
            results.append(
                {
                    PAYLOAD_KEY_MESSAGE_ID: message_id,
                    PAYLOAD_KEY_DISPATCH_RESULT: dispatch_result,
                }
            )

        skipped_messages = self._build_skipped_entries(replay_plan, skipped_message_ids)
        result = {
            PAYLOAD_KEY_STATUS: REPLAY_EXECUTION_STATUS_EXECUTED,
            PAYLOAD_KEY_GATE_DECISION: gate_decision,
            PAYLOAD_KEY_RESULTS: results,
            PAYLOAD_KEY_BLOCKED_MESSAGES: [],
            PAYLOAD_KEY_SKIPPED_MESSAGES: skipped_messages,
            PAYLOAD_KEY_SKIP_REASONS: replay_grouped_reasons(skipped_messages),
            PAYLOAD_KEY_BLOCK_REASONS: {},
            PAYLOAD_KEY_REPLAY_TRACE: {
                PAYLOAD_KEY_SCOPE: REPLAY_TRACE_SCOPE_CORRELATION,
                PAYLOAD_KEY_CORRELATION_ID: plan_correlation_id(replay_plan),
                PAYLOAD_KEY_EXECUTION_STATE: REPLAY_EXECUTION_STATE_DISPATCHED,
                PAYLOAD_KEY_MESSAGE_COUNT: len(results),
                PAYLOAD_KEY_TARGET_MESSAGE_IDS: target_message_ids,
                PAYLOAD_KEY_BLOCKED_MESSAGE_IDS: [],
                PAYLOAD_KEY_SKIPPED_MESSAGE_IDS: skipped_message_ids,
            },
        }
        result[PAYLOAD_KEY_REPLAY_TRACE] = self._trace_summary(result)
        return self._attach_replay_record(result, envelope=sample_envelope)

    def _build_message_block_entries(self, replay_plan: dict | None, gate_decision: dict) -> list[dict]:
        if plan_message_id(replay_plan) is None:
            return []
        return self._build_blocked_entries([plan_message_id(replay_plan)], gate_decision)

    def _build_blocked_entries(self, message_ids: list[str], gate_decision: dict) -> list[dict]:
        return [
            {
                PAYLOAD_KEY_MESSAGE_ID: message_id,
                PAYLOAD_KEY_REASON: self._map_block_reason(gate_decision),
            }
            for message_id in message_ids
        ]

    def _build_skipped_entries(self, replay_plan: dict, skipped_message_ids: list[str]) -> list[dict]:
        acknowledged_message_ids = set(plan_acknowledged_message_ids(replay_plan))
        entries = []
        for message_id in skipped_message_ids:
            reason = (
                REPLAY_EXECUTOR_SKIP_REASON_ACKNOWLEDGED_MESSAGE
                if message_id in acknowledged_message_ids
                else REPLAY_EXECUTOR_SKIP_REASON_NOT_TARGETED
            )
            entries.append({
                PAYLOAD_KEY_MESSAGE_ID: message_id,
                PAYLOAD_KEY_REASON: reason,
            })
        return entries

    def _map_block_reason(self, gate_decision: dict) -> str:
        reasons = gate_reason_set(gate_decision)
        if (
            REPLAY_GATE_REASON_TERMINAL_RECEIPT_ALREADY_RECORDED in reasons
            or REPLAY_GATE_REASON_CORRELATION_CONTAINS_TERMINAL_RECEIPTS in reasons
        ):
            return REPLAY_EXECUTOR_BLOCK_REASON_TERMINAL_RECEIPT
        if REPLAY_GATE_REASON_STALE_RECEIPT_DETECTED in reasons or REPLAY_GATE_REASON_CORRELATION_CONTAINS_STALE_RECEIPT in reasons:
            return REPLAY_EXECUTOR_BLOCK_REASON_STALE_RECEIPT
        if (
            REPLAY_GATE_REASON_REJECTED_RECEIPT_DETECTED in reasons
            or REPLAY_GATE_REASON_CORRELATION_CONTAINS_REJECTED_RECEIPT in reasons
        ):
            return REPLAY_EXECUTOR_BLOCK_REASON_REJECTED_RECEIPT
        if (
            REPLAY_GATE_REASON_CANCELLED_RECEIPT_DETECTED in reasons
            or REPLAY_GATE_REASON_CORRELATION_CONTAINS_CANCELLED_RECEIPT in reasons
        ):
            return REPLAY_EXECUTOR_BLOCK_REASON_CANCELLED_RECEIPT
        if REPLAY_GATE_REASON_MESSAGE_FAILED_PREVIOUSLY in reasons or REPLAY_GATE_REASON_CORRELATION_CONTAINS_FAILED_MESSAGE in reasons:
            return REPLAY_EXECUTOR_BLOCK_REASON_REVIEW_REQUIRED
        if REPLAY_GATE_REASON_NON_CLEAN_ATTEMPT_HISTORY in reasons or REPLAY_GATE_REASON_CORRELATION_CONTAINS_NON_CLEAN_HISTORY in reasons:
            return REPLAY_EXECUTOR_BLOCK_REASON_REVIEW_REQUIRED
        return REPLAY_EXECUTOR_BLOCK_REASON_GATE_BLOCKED

    def _attach_replay_record(self, execution_result: dict, *, envelope):
        if self._replay_execution_writer is None or envelope is None:
            return execution_result
        replay_record, replay_ledger_path = self._replay_execution_writer.write_record(
            execution_result,
            date_key=envelope.event_time.strftime("%Y-%m-%d"),
            symbol=envelope.target,
        )
        execution_result[PAYLOAD_KEY_REPLAY_RECORD] = replay_record
        execution_result[PAYLOAD_KEY_REPLAY_LEDGER_PATH] = replay_ledger_path
        return execution_result

    @staticmethod
    def _trace_summary(execution_result: dict) -> dict:
        replay_trace = execution_result.get(PAYLOAD_KEY_REPLAY_TRACE, {})
        normalized = {}
        if PAYLOAD_KEY_SCOPE in replay_trace:
            normalized[PAYLOAD_KEY_SCOPE] = trace_scope(replay_trace)
        if PAYLOAD_KEY_MESSAGE_ID in replay_trace:
            normalized[PAYLOAD_KEY_MESSAGE_ID] = trace_message_id(replay_trace)
        if PAYLOAD_KEY_CORRELATION_ID in replay_trace:
            normalized[PAYLOAD_KEY_CORRELATION_ID] = trace_correlation_id(replay_trace)
        if PAYLOAD_KEY_EXECUTION_STATE in replay_trace:
            normalized[PAYLOAD_KEY_EXECUTION_STATE] = trace_execution_state(replay_trace)
        if PAYLOAD_KEY_MESSAGE_COUNT in replay_trace:
            normalized[PAYLOAD_KEY_MESSAGE_COUNT] = trace_message_count(replay_trace)
        if PAYLOAD_KEY_TARGET_MESSAGE_IDS in replay_trace:
            normalized[PAYLOAD_KEY_TARGET_MESSAGE_IDS] = trace_target_message_ids(replay_trace)
        if PAYLOAD_KEY_BLOCKED_MESSAGE_IDS in replay_trace:
            normalized[PAYLOAD_KEY_BLOCKED_MESSAGE_IDS] = trace_blocked_message_ids(replay_trace)
        if PAYLOAD_KEY_SKIPPED_MESSAGE_IDS in replay_trace:
            normalized[PAYLOAD_KEY_SKIPPED_MESSAGE_IDS] = trace_skipped_message_ids(replay_trace)
        return normalized
