from datetime import datetime

from core.contracts.domain_keys import (
    PAYLOAD_KEY_ACK_STATUS,
    PAYLOAD_KEY_ACKNOWLEDGED_MESSAGE_IDS,
    PAYLOAD_KEY_ADAPTER_NAME,
    PAYLOAD_KEY_ADAPTER_SEQUENCE,
    PAYLOAD_KEY_ATTEMPT_COUNT,
    PAYLOAD_KEY_ATTEMPT_SUMMARY,
    PAYLOAD_KEY_ATTEMPTS,
    PAYLOAD_KEY_CHANNEL,
    PAYLOAD_KEY_CORRELATION_ID,
    PAYLOAD_KEY_DEADLINE_AT,
    PAYLOAD_KEY_DEADLINE_MISSED,
    PAYLOAD_KEY_DEGRADED_COUNT,
    PAYLOAD_KEY_DELIVERY_POSTURE,
    PAYLOAD_KEY_DELIVERY_STATE,
    PAYLOAD_KEY_DELIVERY_SUMMARY,
    PAYLOAD_KEY_DISPATCH,
    PAYLOAD_KEY_DISPATCH_STATUS,
    PAYLOAD_KEY_ENVELOPE,
    PAYLOAD_KEY_EXECUTION_EVENT_COUNT,
    PAYLOAD_KEY_EXECUTION_TERMINAL_COUNT,
    PAYLOAD_KEY_EXECUTION_TIMELINE,
    PAYLOAD_KEY_EXECUTION_TOTAL_FILLED_QUANTITY,
    PAYLOAD_KEY_FAILED_COUNT,
    PAYLOAD_KEY_FALLBACK_ADAPTER_NAME,
    PAYLOAD_KEY_FINAL_STATUSES,
    PAYLOAD_KEY_IS_TERMINAL,
    PAYLOAD_KEY_ISSUE_CODE,
    PAYLOAD_KEY_ISSUE_COUNTS,
    PAYLOAD_KEY_ISSUE_MESSAGE_IDS,
    PAYLOAD_KEY_MESSAGE_COUNT,
    PAYLOAD_KEY_MESSAGE_ID,
    PAYLOAD_KEY_MESSAGE_IDS,
    PAYLOAD_KEY_MESSAGE_TRACES,
    PAYLOAD_KEY_MESSAGE_TYPE,
    PAYLOAD_KEY_OUTCOME,
    PAYLOAD_KEY_PHASE,
    PAYLOAD_KEY_PHASE_COUNTS,
    PAYLOAD_KEY_RECEIPT,
    PAYLOAD_KEY_RECEIPT_IS_STALE,
    PAYLOAD_KEY_RECEIPT_PRESENT,
    PAYLOAD_KEY_RECEIPT_STATUS,
    PAYLOAD_KEY_RECEIVED_AT,
    PAYLOAD_KEY_RECORDED_AT,
    PAYLOAD_KEY_RECORDS,
    PAYLOAD_KEY_SCOPE,
    PAYLOAD_KEY_STALE_RECEIPT_MESSAGE_IDS,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_SUCCEEDED_COUNT,
    PAYLOAD_KEY_TARGET,
    PAYLOAD_KEY_TIMED_OUT_MESSAGE_IDS,
    PAYLOAD_KEY_TOTAL_FILLED_QUANTITY,
    PAYLOAD_KEY_WAITING_MESSAGE_IDS,
    REPLAY_STATUS_DEGRADED,
    REPLAY_STATUS_FAILED,
    REPLAY_TARGET_CODE_RECEIPT_TIMEOUT,
    REPLAY_TARGET_CODE_STALE_RECEIPT,
    REPLAY_TRACE_SCOPE_CORRELATION,
)


class CommunicationInspectionService:
    ISSUE_CODE_CLEAN = "clean"
    ISSUE_CODE_WAITING_RECEIPT = "waiting_receipt"
    ISSUE_CODE_RECEIPT_TIMEOUT = "receipt_timeout"
    ISSUE_CODE_STALE_RECEIPT = "stale_receipt"
    ISSUE_CODE_RECEIPT_OBSERVED = "receipt_observed"
    ISSUE_CODE_RECEIPT_REJECTED = "receipt_rejected"
    ISSUE_CODE_RECEIPT_ACCEPTED = "receipt_accepted"
    ISSUE_CODE_RECEIPT_PARTIALLY_FILLED = "receipt_partially_filled"
    ISSUE_CODE_RECEIPT_FILLED = "receipt_filled"
    ISSUE_CODE_RECEIPT_CANCELLED = "receipt_cancelled"
    ISSUE_CODE_DISPATCH_PENDING = "dispatch_pending"

    DELIVERY_POSTURE_HEALTHY = "healthy"
    DELIVERY_POSTURE_OBSERVE = "observe"
    DELIVERY_POSTURE_ACTION_REQUIRED = "action_required"

    def __init__(self, record_reader, receipt_reader=None, execution_event_reader=None):
        self._record_reader = record_reader
        self._receipt_reader = receipt_reader
        self._execution_event_reader = execution_event_reader

    def get_message_trace(self, *, date_key: str, target: str, message_id: str) -> dict | None:
        record = self._record_reader.find_by_message_id(
            date_key=date_key,
            target=target,
            message_id=message_id,
        )
        if record is None:
            return None
        return self._build_message_trace(date_key=date_key, target=target, record=record)

    def get_correlation_trace(self, *, date_key: str, target: str, correlation_id: str) -> dict:
        records = self._record_reader.find_by_correlation_id(
            date_key=date_key,
            target=target,
            correlation_id=correlation_id,
        )
        message_traces = [
            self._build_message_trace(date_key=date_key, target=target, record=record)
            for record in records
        ]
        return {
            PAYLOAD_KEY_SCOPE: REPLAY_TRACE_SCOPE_CORRELATION,
            PAYLOAD_KEY_CORRELATION_ID: correlation_id,
            PAYLOAD_KEY_MESSAGE_COUNT: len(records),
            PAYLOAD_KEY_MESSAGE_IDS: [item[PAYLOAD_KEY_MESSAGE_ID] for item in records],
            PAYLOAD_KEY_FINAL_STATUSES: [
                item.get(PAYLOAD_KEY_DISPATCH, {}).get(PAYLOAD_KEY_STATUS) for item in records
            ],
            PAYLOAD_KEY_RECORDS: records,
            PAYLOAD_KEY_MESSAGE_TRACES: message_traces,
            PAYLOAD_KEY_DELIVERY_SUMMARY: self._summarize_correlation_delivery(message_traces),
        }

    def summarize_attempts(self, record: dict) -> dict:
        attempts = record.get(PAYLOAD_KEY_DISPATCH, {}).get(PAYLOAD_KEY_ATTEMPTS, [])
        return {
            PAYLOAD_KEY_ATTEMPT_COUNT: len(attempts),
            PAYLOAD_KEY_FAILED_COUNT: sum(
                1 for item in attempts if item.get(PAYLOAD_KEY_STATUS) == REPLAY_STATUS_FAILED
            ),
            PAYLOAD_KEY_DEGRADED_COUNT: sum(
                1 for item in attempts if item.get(PAYLOAD_KEY_STATUS) == REPLAY_STATUS_DEGRADED
            ),
            PAYLOAD_KEY_SUCCEEDED_COUNT: sum(
                1 for item in attempts if item.get(PAYLOAD_KEY_STATUS) == "succeeded"
            ),
            PAYLOAD_KEY_ADAPTER_SEQUENCE: [item.get(PAYLOAD_KEY_ADAPTER_NAME) for item in attempts],
        }

    def _build_message_trace(self, *, date_key: str, target: str, record: dict) -> dict:
        receipt = self._load_receipt(
            date_key=date_key, target=target, message_id=record[PAYLOAD_KEY_MESSAGE_ID]
        )
        execution_timeline = self._load_execution_timeline(
            date_key=date_key,
            correlation_id=record[PAYLOAD_KEY_CORRELATION_ID],
            message_id=record[PAYLOAD_KEY_MESSAGE_ID],
        )
        delivery_state = self._build_delivery_state(record, receipt)
        return {
            PAYLOAD_KEY_MESSAGE_ID: record[PAYLOAD_KEY_MESSAGE_ID],
            PAYLOAD_KEY_CORRELATION_ID: record[PAYLOAD_KEY_CORRELATION_ID],
            PAYLOAD_KEY_TARGET: record[PAYLOAD_KEY_CHANNEL].get(PAYLOAD_KEY_TARGET),
            PAYLOAD_KEY_MESSAGE_TYPE: record[PAYLOAD_KEY_CHANNEL].get(PAYLOAD_KEY_MESSAGE_TYPE),
            PAYLOAD_KEY_STATUS: record[PAYLOAD_KEY_DISPATCH].get(PAYLOAD_KEY_STATUS),
            PAYLOAD_KEY_ADAPTER_NAME: record[PAYLOAD_KEY_DISPATCH].get(PAYLOAD_KEY_ADAPTER_NAME),
            PAYLOAD_KEY_FALLBACK_ADAPTER_NAME: record[PAYLOAD_KEY_DISPATCH].get(
                PAYLOAD_KEY_FALLBACK_ADAPTER_NAME
            ),
            PAYLOAD_KEY_ATTEMPTS: record[PAYLOAD_KEY_DISPATCH].get(PAYLOAD_KEY_ATTEMPTS, []),
            PAYLOAD_KEY_ATTEMPT_SUMMARY: self.summarize_attempts(record),
            PAYLOAD_KEY_DELIVERY_STATE: delivery_state,
            PAYLOAD_KEY_RECEIPT: receipt,
            PAYLOAD_KEY_EXECUTION_TIMELINE: execution_timeline,
            PAYLOAD_KEY_OUTCOME: record.get(PAYLOAD_KEY_OUTCOME, {}),
        }

    def _summarize_correlation_delivery(self, message_traces: list[dict]) -> dict:
        phase_counts = {}
        issue_counts = {}
        issue_message_ids = {}
        for item in message_traces:
            delivery_state = item[PAYLOAD_KEY_DELIVERY_STATE]
            phase = delivery_state[PAYLOAD_KEY_PHASE]
            issue_code = delivery_state[PAYLOAD_KEY_ISSUE_CODE]
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
            issue_counts[issue_code] = issue_counts.get(issue_code, 0) + 1
            issue_message_ids.setdefault(issue_code, []).append(item[PAYLOAD_KEY_MESSAGE_ID])

        execution_timelines = [
            item.get(PAYLOAD_KEY_EXECUTION_TIMELINE)
            for item in message_traces
            if item.get(PAYLOAD_KEY_EXECUTION_TIMELINE) is not None
        ]
        terminal_count = sum(1 for t in execution_timelines if t.get(PAYLOAD_KEY_IS_TERMINAL))  # type: ignore[reportOptionalMemberAccess]
        total_filled_qty = sum(
            t.get(PAYLOAD_KEY_TOTAL_FILLED_QUANTITY, 0)
            for t in execution_timelines  # type: ignore[reportOptionalMemberAccess]
        )

        return {
            PAYLOAD_KEY_PHASE_COUNTS: phase_counts,
            PAYLOAD_KEY_ISSUE_COUNTS: issue_counts,
            PAYLOAD_KEY_ISSUE_MESSAGE_IDS: issue_message_ids,
            PAYLOAD_KEY_TIMED_OUT_MESSAGE_IDS: [
                item[PAYLOAD_KEY_MESSAGE_ID]
                for item in message_traces
                if item[PAYLOAD_KEY_DELIVERY_STATE][PAYLOAD_KEY_PHASE]
                == REPLAY_TARGET_CODE_RECEIPT_TIMEOUT
            ],
            PAYLOAD_KEY_STALE_RECEIPT_MESSAGE_IDS: [
                item[PAYLOAD_KEY_MESSAGE_ID]
                for item in message_traces
                if item[PAYLOAD_KEY_DELIVERY_STATE][PAYLOAD_KEY_PHASE]
                == REPLAY_TARGET_CODE_STALE_RECEIPT
            ],
            PAYLOAD_KEY_ACKNOWLEDGED_MESSAGE_IDS: [
                item[PAYLOAD_KEY_MESSAGE_ID]
                for item in message_traces
                if item[PAYLOAD_KEY_DELIVERY_STATE][PAYLOAD_KEY_PHASE] == "receipt_acknowledged"
            ],
            PAYLOAD_KEY_WAITING_MESSAGE_IDS: [
                item[PAYLOAD_KEY_MESSAGE_ID]
                for item in message_traces
                if item[PAYLOAD_KEY_DELIVERY_STATE][PAYLOAD_KEY_PHASE]
                == self.ISSUE_CODE_WAITING_RECEIPT
            ],
            PAYLOAD_KEY_DELIVERY_POSTURE: self._build_correlation_delivery_posture(issue_counts),
            PAYLOAD_KEY_EXECUTION_EVENT_COUNT: len(execution_timelines),
            PAYLOAD_KEY_EXECUTION_TERMINAL_COUNT: terminal_count,
            PAYLOAD_KEY_EXECUTION_TOTAL_FILLED_QUANTITY: total_filled_qty,
        }

    def _load_receipt(self, *, date_key: str, target: str, message_id: str) -> dict | None:
        if self._receipt_reader is None:
            return None
        return self._receipt_reader.find_by_message_id(
            date_key=date_key,
            target=target,
            message_id=message_id,
        )

    def _load_execution_timeline(
        self, *, date_key: str, correlation_id: str, message_id: str
    ) -> dict | None:
        if self._execution_event_reader is None:
            return None
        return self._execution_event_reader.build_execution_timeline(
            date_key=date_key,
            correlation_id=correlation_id,
            message_id=message_id,
        )

    def _build_delivery_state(self, record: dict, receipt: dict | None) -> dict:
        dispatch = record.get(PAYLOAD_KEY_DISPATCH, {})
        channel = record.get(PAYLOAD_KEY_CHANNEL, {})
        dispatch_status = dispatch.get(PAYLOAD_KEY_STATUS)
        receipt_status = None if receipt is None else receipt.get(PAYLOAD_KEY_ACK_STATUS)
        received_at = None if receipt is None else receipt.get(PAYLOAD_KEY_RECEIVED_AT)
        recorded_at = dispatch.get(PAYLOAD_KEY_RECORDED_AT)
        deadline_at = channel.get(PAYLOAD_KEY_DEADLINE_AT) or record.get(
            PAYLOAD_KEY_ENVELOPE, {}
        ).get(PAYLOAD_KEY_DEADLINE_AT)
        deadline_missed = self._is_deadline_missed(recorded_at, deadline_at)
        receipt_is_stale = self._is_receipt_stale(received_at, deadline_at)

        if receipt_status == "acknowledged":
            phase = "stale_receipt" if receipt_is_stale else "receipt_acknowledged"
        elif receipt_status == "rejected":
            phase = "receipt_rejected"
        elif receipt_status == "accepted":
            phase = "receipt_accepted"
        elif receipt_status == "partially_filled":
            phase = "receipt_partially_filled"
        elif receipt_status == "filled":
            phase = "receipt_filled"
        elif receipt_status == "cancelled":
            phase = "receipt_cancelled"
        elif receipt is not None:
            phase = "receipt_observed"
        elif dispatch_status == "transport_delivered":
            phase = "receipt_timeout" if deadline_missed else "waiting_receipt"
        else:
            phase = "dispatch_recorded"

        return {
            PAYLOAD_KEY_PHASE: phase,
            PAYLOAD_KEY_ISSUE_CODE: self._map_issue_code(phase),
            PAYLOAD_KEY_DELIVERY_POSTURE: self._map_delivery_posture(phase),
            PAYLOAD_KEY_DISPATCH_STATUS: dispatch_status,
            PAYLOAD_KEY_RECEIPT_PRESENT: receipt is not None,
            PAYLOAD_KEY_RECEIPT_STATUS: receipt_status,
            PAYLOAD_KEY_RECEIVED_AT: received_at,
            PAYLOAD_KEY_RECORDED_AT: recorded_at,
            PAYLOAD_KEY_DEADLINE_AT: deadline_at,
            PAYLOAD_KEY_DEADLINE_MISSED: deadline_missed,
            PAYLOAD_KEY_RECEIPT_IS_STALE: receipt_is_stale,
        }

    def _map_issue_code(self, phase: str) -> str:
        if phase == "receipt_acknowledged":
            return self.ISSUE_CODE_CLEAN
        if phase == "waiting_receipt":
            return self.ISSUE_CODE_WAITING_RECEIPT
        if phase == "receipt_timeout":
            return self.ISSUE_CODE_RECEIPT_TIMEOUT
        if phase == "stale_receipt":
            return self.ISSUE_CODE_STALE_RECEIPT
        if phase == "receipt_observed":
            return self.ISSUE_CODE_RECEIPT_OBSERVED
        if phase == "receipt_rejected":
            return self.ISSUE_CODE_RECEIPT_REJECTED
        if phase == "receipt_accepted":
            return self.ISSUE_CODE_RECEIPT_ACCEPTED
        if phase == "receipt_partially_filled":
            return self.ISSUE_CODE_RECEIPT_PARTIALLY_FILLED
        if phase == "receipt_filled":
            return self.ISSUE_CODE_RECEIPT_FILLED
        if phase == "receipt_cancelled":
            return self.ISSUE_CODE_RECEIPT_CANCELLED
        return self.ISSUE_CODE_DISPATCH_PENDING

    def _map_delivery_posture(self, phase: str) -> str:
        if phase in {
            "receipt_acknowledged",
            "receipt_accepted",
            "receipt_partially_filled",
            "receipt_filled",
        }:
            return self.DELIVERY_POSTURE_HEALTHY
        if phase in {"waiting_receipt", "receipt_observed"}:
            return self.DELIVERY_POSTURE_OBSERVE
        return self.DELIVERY_POSTURE_ACTION_REQUIRED

    def _build_correlation_delivery_posture(self, issue_counts: dict) -> str:
        if (
            issue_counts.get(self.ISSUE_CODE_RECEIPT_TIMEOUT, 0) > 0
            or issue_counts.get(self.ISSUE_CODE_STALE_RECEIPT, 0) > 0
            or issue_counts.get(self.ISSUE_CODE_RECEIPT_REJECTED, 0) > 0
            or issue_counts.get(self.ISSUE_CODE_RECEIPT_CANCELLED, 0) > 0
        ):
            return self.DELIVERY_POSTURE_ACTION_REQUIRED
        if (
            issue_counts.get(self.ISSUE_CODE_WAITING_RECEIPT, 0) > 0
            or issue_counts.get(self.ISSUE_CODE_RECEIPT_OBSERVED, 0) > 0
        ):
            return self.DELIVERY_POSTURE_OBSERVE
        return self.DELIVERY_POSTURE_HEALTHY

    def _is_deadline_missed(self, recorded_at: str | None, deadline_at: str | None) -> bool:
        if recorded_at is None or deadline_at is None:
            return False
        return self._parse_dt(recorded_at) > self._parse_dt(deadline_at)

    def _is_receipt_stale(self, received_at: str | None, deadline_at: str | None) -> bool:
        if received_at is None or deadline_at is None:
            return False
        return self._parse_dt(received_at) > self._parse_dt(deadline_at)

    def _parse_dt(self, value: str) -> datetime:
        return datetime.fromisoformat(value)
