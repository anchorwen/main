"""Shared accessors for communication trace artifacts."""

from core.deployment.domain_keys import (
    PAYLOAD_KEY_ATTEMPT_SUMMARY,
    PAYLOAD_KEY_DELIVERY_POSTURE,
    PAYLOAD_KEY_DELIVERY_STATE,
    PAYLOAD_KEY_DELIVERY_SUMMARY,
    PAYLOAD_KEY_FINAL_STATUSES,
    PAYLOAD_KEY_ISSUE_CODE,
    PAYLOAD_KEY_ISSUE_COUNTS,
    PAYLOAD_KEY_ISSUE_MESSAGE_IDS,
    PAYLOAD_KEY_MESSAGE_COUNT,
    PAYLOAD_KEY_MESSAGE_ID,
    PAYLOAD_KEY_MESSAGE_IDS,
    PAYLOAD_KEY_RECORDS,
    PAYLOAD_KEY_STALE_RECEIPT_MESSAGE_IDS,
    PAYLOAD_KEY_TIMED_OUT_MESSAGE_IDS,
    REPLAY_TRACE_SCOPE_CORRELATION,
)


def delivery_state_block(trace: dict | None) -> dict:
    if trace is None:
        return {}
    return trace.get(PAYLOAD_KEY_DELIVERY_STATE, {})


def delivery_summary_block(trace: dict | None) -> dict:
    if trace is None:
        return {}
    return trace.get(PAYLOAD_KEY_DELIVERY_SUMMARY, {})


def delivery_posture(trace: dict | None, *, scope: str, default: str) -> str:
    if trace is None:
        return default
    if scope == REPLAY_TRACE_SCOPE_CORRELATION:
        return delivery_summary_block(trace).get(PAYLOAD_KEY_DELIVERY_POSTURE, default)
    return delivery_state_block(trace).get(PAYLOAD_KEY_DELIVERY_POSTURE, default)


def attempt_summary(trace: dict | None) -> dict:
    if trace is None:
        return {}
    return trace.get(PAYLOAD_KEY_ATTEMPT_SUMMARY, {})


def issue_code(trace: dict | None):
    return delivery_state_block(trace).get(PAYLOAD_KEY_ISSUE_CODE)


def issue_counts(trace: dict | None) -> dict:
    return delivery_summary_block(trace).get(PAYLOAD_KEY_ISSUE_COUNTS, {})


def issue_message_ids(trace: dict | None) -> dict:
    return delivery_summary_block(trace).get(PAYLOAD_KEY_ISSUE_MESSAGE_IDS, {})


def timed_out_message_ids(trace: dict | None) -> list[str]:
    return delivery_summary_block(trace).get(PAYLOAD_KEY_TIMED_OUT_MESSAGE_IDS, [])


def stale_receipt_message_ids(trace: dict | None) -> list[str]:
    return delivery_summary_block(trace).get(PAYLOAD_KEY_STALE_RECEIPT_MESSAGE_IDS, [])


def message_count(trace: dict | None) -> int:
    if trace is None:
        return 0
    return trace.get(PAYLOAD_KEY_MESSAGE_COUNT, 0)


def message_ids(trace: dict | None) -> list[str]:
    if trace is None:
        return []
    return trace.get(PAYLOAD_KEY_MESSAGE_IDS, [])


def final_statuses(trace: dict | None) -> list[str]:
    if trace is None:
        return []
    return trace.get(PAYLOAD_KEY_FINAL_STATUSES, [])


def records(trace: dict | None) -> list[dict]:
    if trace is None:
        return []
    return trace.get(PAYLOAD_KEY_RECORDS, [])


def trace_message_id(trace: dict | None) -> str | None:
    if trace is None:
        return None
    return trace.get(PAYLOAD_KEY_MESSAGE_ID)
