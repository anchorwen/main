"""Shared accessors for replay gate decision payloads."""

from core.contracts.domain_keys import (
    PAYLOAD_KEY_DECISION,
    PAYLOAD_KEY_GOVERNANCE_SUMMARY,
    PAYLOAD_KEY_REASONS,
)


def decision(gate_decision: dict | None):
    if gate_decision is None:
        return None
    return gate_decision.get(PAYLOAD_KEY_DECISION)


def reasons(gate_decision: dict | None) -> list[str]:
    if gate_decision is None:
        return []
    section = gate_decision.get(PAYLOAD_KEY_REASONS, [])
    return section if isinstance(section, list) else []


def reason_set(gate_decision: dict | None) -> set[str]:
    return set(reasons(gate_decision))


def governance_summary(gate_decision: dict | None) -> dict | None:
    if gate_decision is None:
        return None
    return gate_decision.get(PAYLOAD_KEY_GOVERNANCE_SUMMARY)
