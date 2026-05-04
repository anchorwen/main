"""Shared governance source constants for replay/operations contracts."""

from core.deployment.domain_keys import (
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED,
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
)

SUPPORTED_REPLAY_GOVERNANCE_SUMMARY_SOURCES = (
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_EXTENSIONS,
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_GATE,
    REPLAY_GOVERNANCE_SUMMARY_SOURCE_DERIVED,
)


def is_supported_replay_governance_summary_source(source: str | None) -> bool:
    return source in SUPPORTED_REPLAY_GOVERNANCE_SUMMARY_SOURCES
