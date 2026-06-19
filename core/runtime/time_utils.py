"""Shared time utilities — single source of truth for UTC timestamps.

Extracted from 18 duplicate definitions of _utc_iso() across the codebase.
All modules import from here instead of redefining.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_iso() -> str:
    """Return current UTC time as an ISO-8601 string with Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# Backward-compatible alias for modules that use _utc_iso internally
_utc_iso = utc_iso
