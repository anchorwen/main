"""Shared helpers for governance summary payload fields."""

from core.contracts.domain_keys import (
    COMPLIANCE_LEVEL_WARN,
    PAYLOAD_KEY_GOVERNANCE_FOCUS,
    PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT,
    PAYLOAD_KEY_LEVEL,
    PAYLOAD_KEY_STATUS,
)

__all__ = (
    "build_governance_summary",
    "count_governance_warnings",
    "extract_governance_summary",
)


def _is_warn_item(item: dict) -> bool:
    return (
        item.get(PAYLOAD_KEY_LEVEL) == COMPLIANCE_LEVEL_WARN
        or item.get(PAYLOAD_KEY_STATUS) == COMPLIANCE_LEVEL_WARN
    )


def count_governance_warnings(focus: list[dict]) -> int:
    """Count warn-level items from a normalized governance focus list."""
    return len([item for item in focus if _is_warn_item(item)])


def extract_governance_summary(payload: dict | None) -> dict:
    """Return normalized governance summary fields from a payload."""
    payload = payload or {}
    raw_focus = payload.get(PAYLOAD_KEY_GOVERNANCE_FOCUS, [])
    if isinstance(raw_focus, list):
        focus = [item for item in raw_focus if isinstance(item, dict)]
    else:
        focus = []
    warning_count = count_governance_warnings(focus)
    return {
        PAYLOAD_KEY_GOVERNANCE_FOCUS: focus,
        PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT: warning_count,
    }


def build_governance_summary(*, focus: object, warning_count: object | None = None) -> dict:
    """Build and normalize governance summary fields from raw parts.

    warning_count is accepted for backward compatibility, but the normalized
    result is always derived from focus warn items.
    """
    return extract_governance_summary(
        {
            PAYLOAD_KEY_GOVERNANCE_FOCUS: focus,
            PAYLOAD_KEY_GOVERNANCE_WARNING_COUNT: warning_count,
        }
    )
