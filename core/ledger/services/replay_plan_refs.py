"""Shared accessors for replay plan artifacts.

Centralizes replay plan dictionary access so gate/executor logic
does not duplicate nested `.get()` paths.
"""

from core.deployment.domain_keys import (
    PAYLOAD_KEY_GOVERNANCE_SUMMARY,
    PAYLOAD_KEY_ACKNOWLEDGED_MESSAGE_IDS,
    PAYLOAD_KEY_CORRELATION_ID,
    PAYLOAD_KEY_DELIVERY_SUMMARY,
    PAYLOAD_KEY_FINAL_STATUSES,
    PAYLOAD_KEY_MESSAGE_COUNT,
    PAYLOAD_KEY_MESSAGE_ID,
    PAYLOAD_KEY_MESSAGE_IDS,
    PAYLOAD_KEY_MESSAGE_PLANS,
    PAYLOAD_KEY_RECOMMENDED_STRATEGY,
    PAYLOAD_KEY_TARGET_MESSAGE_IDS,
)


def message_id(replay_plan: dict | None) -> str | None:
    if replay_plan is None:
        return None
    return replay_plan.get(PAYLOAD_KEY_MESSAGE_ID)


def correlation_id(replay_plan: dict | None) -> str | None:
    if replay_plan is None:
        return None
    return replay_plan.get(PAYLOAD_KEY_CORRELATION_ID)


def recommended_strategy(replay_plan: dict | None) -> str | None:
    if replay_plan is None:
        return None
    return replay_plan.get(PAYLOAD_KEY_RECOMMENDED_STRATEGY)


def message_ids(replay_plan: dict | None) -> list[str]:
    if replay_plan is None:
        return []
    return replay_plan.get(PAYLOAD_KEY_MESSAGE_IDS, [])


def target_message_ids(replay_plan: dict | None) -> list[str]:
    if replay_plan is None:
        return []
    return replay_plan.get(PAYLOAD_KEY_TARGET_MESSAGE_IDS) or replay_plan.get(PAYLOAD_KEY_MESSAGE_IDS, [])


def message_plans(replay_plan: dict | None) -> list[dict]:
    if replay_plan is None:
        return []
    return replay_plan.get(PAYLOAD_KEY_MESSAGE_PLANS, [])


def final_statuses(replay_plan: dict | None) -> list[str]:
    if replay_plan is None:
        return []
    return replay_plan.get(PAYLOAD_KEY_FINAL_STATUSES, [])


def message_count(replay_plan: dict | None) -> int:
    if replay_plan is None:
        return 0
    return replay_plan.get(PAYLOAD_KEY_MESSAGE_COUNT, 0)


def delivery_summary(replay_plan: dict | None) -> dict:
    if replay_plan is None:
        return {}
    return replay_plan.get(PAYLOAD_KEY_DELIVERY_SUMMARY, {})


def acknowledged_message_ids(replay_plan: dict | None) -> list[str]:
    return delivery_summary(replay_plan).get(PAYLOAD_KEY_ACKNOWLEDGED_MESSAGE_IDS, [])


def governance_summary(replay_plan: dict | None) -> dict | None:
    if replay_plan is None:
        return None
    return replay_plan.get(PAYLOAD_KEY_GOVERNANCE_SUMMARY)
