"""
Unified Ticket Resolution — SSOT extraction for position ticket IDs.

DQAF-20260623-073: Institutional Docket for Schema Drift Consolidation
========================================================================
Problem: 83 files across the codebase extract the same business concept
(position ticket ID) using at least 4 different patterns:

  1. ``rec.get("position_ticket")``                         — 61 files, no fallback
  2. ``rec.get("position_ticket") or detail.get("order")``  — 18 files (DQAF-067 fix)
  3. ``rec.get("ticket") or rec.get("position_ticket")``    — 4 files
  4. ``detail.get("order")`` directly                       — 0 remaining (all migrated)

Each new consumer faces the same "which field do I use?" choice,
producing N consumers with N interpretations — schema drift in action.

This module provides the SINGLE canonical extraction path.  All consumers
MUST use these functions; raw ``.get("position_ticket")`` is deprecated
for new code and should be migrated on touch.

Migration Phases (DQAF-20260623-073):
  P0 (this commit): Deploy resolver + migrate 3 key consumers
                    (label_builder, daily_ops, audit_data_exhaustive)
  P1 (touch-migration): Migrate remaining files as they are next modified
  P2 (sweep): Full-sweep migration of remaining ~80 files
"""

from __future__ import annotations

from typing import Any, TypeGuard


def resolve(record: dict[str, Any]) -> int | None:
    """Extract the canonical position ticket ID from any record type.

    Handles the three known storage locations in priority order:

    1. ``position_ticket`` — direct field on journal entries, labels,
       ledger events, and snapshots.  Present in ~95% of records.

    2. ``detail.order`` — alternative location used by some MT5 bridge
       journal entry formats where the ticket is nested inside the
       ``detail`` sub-document.

    3. ``ticket`` — bare field used by position_snapshot records and
       some internal event structures.

    Returns ``None`` only when no ticket can be resolved from any of
    the three locations, or when the resolved value is not a positive
    integer.

    >>> resolve({"position_ticket": 3894759701})
    3894759701
    >>> resolve({"detail": {"order": 3894759701}})
    3894759701
    >>> resolve({"ticket": 123})
    123
    >>> resolve({"other": "data"})
    None
    """
    # Priority 1: direct position_ticket
    ticket = record.get("position_ticket")
    if _is_valid_ticket(ticket):
        return int(ticket)

    # Priority 2: detail.order (MT5 bridge nested format)
    detail = record.get("detail")
    if isinstance(detail, dict):
        ticket = detail.get("order")
        if _is_valid_ticket(ticket):
            return int(ticket)

    # Priority 3: bare ticket field (snapshots, internal events)
    ticket = record.get("ticket")
    if _is_valid_ticket(ticket):
        return int(ticket)

    return None


def resolve_required(record: dict[str, Any]) -> int:
    """Like :func:`resolve` but raises on failure.

    Use this when a missing ticket is a data integrity violation
    (e.g. label generation, PnL settlement), not a normal edge case.
    """
    ticket = resolve(record)
    if ticket is None:
        raise TicketResolutionError(
            f"Cannot resolve position ticket from record: " f"keys={list(record.keys())[:10]}"
        )
    return ticket


class TicketResolutionError(ValueError):
    """Raised by :func:`resolve_required` when ticket resolution fails."""


def _is_valid_ticket(value: object) -> TypeGuard[int]:
    """Check if a value is a valid positive ticket ID (type guard for mypy)."""
    return isinstance(value, int) and value > 0
