"""Shared accessors for replay record artifact sections.

Keeps access to replay record sections centralized so callers do not
duplicate nested `.get()` paths across services.
"""

from core.contracts.domain_keys import (
    EXECUTION_MODE_VALUE_FULL,
    EXECUTION_MODE_VALUE_TARGETED,
    PAYLOAD_KEY_BLOCKED_MESSAGES,
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_EXECUTION,
    PAYLOAD_KEY_EXECUTION_MODE,
    PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE,
    PAYLOAD_KEY_EXTENSIONS,
    PAYLOAD_KEY_GATE_DECISION,
    PAYLOAD_KEY_GOVERNANCE_DECISION,
    PAYLOAD_KEY_GOVERNANCE_POSTURE,
    PAYLOAD_KEY_GOVERNANCE_SUMMARY,
    PAYLOAD_KEY_MESSAGE_ID,
    PAYLOAD_KEY_PLAN,
    PAYLOAD_KEY_POSTURE,
    PAYLOAD_KEY_REASON,
    PAYLOAD_KEY_RESULTS,
    PAYLOAD_KEY_SKIPPED_MESSAGES,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_SUMMARY_SOURCE,
    PAYLOAD_KEY_TARGET_MESSAGE_IDS,
    PAYLOAD_KEY_TRACE,
    REPLAY_EXECUTION_STATUS_BLOCKED,
)


def execution_block(replay_record: dict) -> dict:
    return replay_record.get(PAYLOAD_KEY_EXECUTION, {})


def trace_block(replay_record: dict) -> dict:
    return replay_record.get(PAYLOAD_KEY_TRACE, {})


def gate_block(replay_record: dict) -> dict:
    return replay_record.get(PAYLOAD_KEY_GATE_DECISION, {})


def extensions_block(replay_record: dict) -> dict:
    return replay_record.get(PAYLOAD_KEY_EXTENSIONS, {})


def plan_block(replay_record: dict) -> dict:
    return replay_record.get(PAYLOAD_KEY_PLAN, {})


def results_block(replay_record: dict) -> dict:
    section = replay_record.get(PAYLOAD_KEY_RESULTS, {})
    return section if isinstance(section, dict) else {}


def skipped_messages(replay_record: dict) -> list[dict]:
    section = replay_record.get(PAYLOAD_KEY_SKIPPED_MESSAGES, [])
    return section if isinstance(section, list) else []


def blocked_messages(replay_record: dict) -> list[dict]:
    section = replay_record.get(PAYLOAD_KEY_BLOCKED_MESSAGES, [])
    return section if isinstance(section, list) else []


def targeted_message_ids(replay_record: dict) -> list[str]:
    trace = trace_block(replay_record)
    targeted = trace.get(PAYLOAD_KEY_TARGET_MESSAGE_IDS)
    if isinstance(targeted, list):
        return targeted
    message_id = trace.get(PAYLOAD_KEY_MESSAGE_ID)
    if message_id is None:
        return []
    return [message_id]


def grouped_reasons(items: list[dict]) -> dict:
    grouped: dict[str, list[str]] = {}
    for item in items:
        reason = item.get(PAYLOAD_KEY_REASON)
        message_id = item.get(PAYLOAD_KEY_MESSAGE_ID)
        if reason is None or message_id is None:
            continue
        grouped.setdefault(reason, []).append(message_id)
    return grouped


def message_ids(items: list[dict]) -> list[str]:
    return [
        item.get(PAYLOAD_KEY_MESSAGE_ID)
        for item in items
        if item.get(PAYLOAD_KEY_MESSAGE_ID) is not None
    ]


def execution_mode(replay_record: dict, *, skipped_message_ids: list[str]) -> str:
    execution = execution_block(replay_record)
    if execution.get(PAYLOAD_KEY_STATUS) == REPLAY_EXECUTION_STATUS_BLOCKED:
        return REPLAY_EXECUTION_STATUS_BLOCKED
    mode = execution.get(PAYLOAD_KEY_EXECUTION_MODE)
    if mode is not None:
        return mode
    return EXECUTION_MODE_VALUE_TARGETED if skipped_message_ids else EXECUTION_MODE_VALUE_FULL


def governance_sources(
    replay_record: dict,
    *,
    summary_source_derived: str,
    summary_source_extensions: str,
    summary_source_gate: str,
    projection_source_execution: str,
) -> dict:
    summary_source = summary_source_derived
    if extensions_block(replay_record).get(PAYLOAD_KEY_GOVERNANCE_SUMMARY) is not None:
        summary_source = summary_source_extensions
    elif gate_block(replay_record).get(PAYLOAD_KEY_GOVERNANCE_SUMMARY) is not None:
        summary_source = summary_source_gate

    projection_source = None
    execution = execution_block(replay_record)
    if (
        execution.get(PAYLOAD_KEY_GOVERNANCE_DECISION) is not None
        or execution.get(PAYLOAD_KEY_GOVERNANCE_POSTURE) is not None
    ):
        projection_source = projection_source_execution

    return {
        PAYLOAD_KEY_SUMMARY_SOURCE: summary_source,
        PAYLOAD_KEY_EXECUTION_PROJECTION_SOURCE: projection_source,
    }


def governance_summary(
    replay_record: dict,
    *,
    fallback_builder,
) -> dict:
    summary = extensions_block(replay_record).get(PAYLOAD_KEY_GOVERNANCE_SUMMARY)
    if summary is not None:
        return summary
    summary = gate_block(replay_record).get(PAYLOAD_KEY_GOVERNANCE_SUMMARY)
    if summary is not None:
        return summary
    return fallback_builder(
        plan_block(replay_record),
        gate_block(replay_record),
    )


def execution_governance_projection(replay_record: dict) -> dict:
    execution = execution_block(replay_record)
    return {
        PAYLOAD_KEY_DECISION: execution.get(PAYLOAD_KEY_GOVERNANCE_DECISION),
        PAYLOAD_KEY_POSTURE: execution.get(PAYLOAD_KEY_GOVERNANCE_POSTURE),
    }
