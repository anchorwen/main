"""Shared accessors for replay trace payloads."""

from core.contracts.domain_keys import (
    PAYLOAD_KEY_BLOCKED_MESSAGE_IDS,
    PAYLOAD_KEY_CORRELATION_ID,
    PAYLOAD_KEY_EXECUTION_STATE,
    PAYLOAD_KEY_MESSAGE_COUNT,
    PAYLOAD_KEY_MESSAGE_ID,
    PAYLOAD_KEY_SCOPE,
    PAYLOAD_KEY_SKIPPED_MESSAGE_IDS,
    PAYLOAD_KEY_TARGET_MESSAGE_IDS,
)


def scope(replay_trace: dict | None):
    if replay_trace is None:
        return None
    return replay_trace.get(PAYLOAD_KEY_SCOPE)


def message_id(replay_trace: dict | None):
    if replay_trace is None:
        return None
    return replay_trace.get(PAYLOAD_KEY_MESSAGE_ID)


def correlation_id(replay_trace: dict | None):
    if replay_trace is None:
        return None
    return replay_trace.get(PAYLOAD_KEY_CORRELATION_ID)


def execution_state(replay_trace: dict | None):
    if replay_trace is None:
        return None
    return replay_trace.get(PAYLOAD_KEY_EXECUTION_STATE)


def message_count(replay_trace: dict | None) -> int:
    if replay_trace is None:
        return 0
    return replay_trace.get(PAYLOAD_KEY_MESSAGE_COUNT, 0)


def target_message_ids(replay_trace: dict | None) -> list[str]:
    if replay_trace is None:
        return []
    section = replay_trace.get(PAYLOAD_KEY_TARGET_MESSAGE_IDS, [])
    return section if isinstance(section, list) else []


def blocked_message_ids(replay_trace: dict | None) -> list[str]:
    if replay_trace is None:
        return []
    section = replay_trace.get(PAYLOAD_KEY_BLOCKED_MESSAGE_IDS, [])
    return section if isinstance(section, list) else []


def skipped_message_ids(replay_trace: dict | None) -> list[str]:
    if replay_trace is None:
        return []
    section = replay_trace.get(PAYLOAD_KEY_SKIPPED_MESSAGE_IDS, [])
    return section if isinstance(section, list) else []
