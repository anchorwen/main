"""Position ownership resolver — pure function extracted from live_cycle.py.

Strangler Fig #15: extracted from ``execute_live_cycle()`` L5128-5156.
Pure function contract: zero I/O, zero global state, same input → same output.

Determines which strategy "owns" a position by mapping its supporting brain IDs
through the contract group definitions.  Used for portfolio risk aggregation.

Related FIXes: FIX-20260609-006 (position registration), FIX-20260615-006 (cross-asset)
"""

from __future__ import annotations

from typing import Any


def resolve_position_owner(
    supporting_brain_ids: list[Any],
    brains: list[dict[str, Any]],
    *,
    micro_m15_types: set[str] | frozenset[str],
    micro_h1_types: set[str] | frozenset[str],
    micro_h4_types: set[str] | frozenset[str],
    micro_3bar_types: set[str] | frozenset[str],
    statarb_types: set[str] | frozenset[str],
    default_owner: str = "barrier_12bar",
) -> str:
    """Resolve which strategy owns a position from supporting brain IDs.

    Iterates the position's brain IDs against the brain registry and
    contract group definitions.  The first matching group wins, with
    higher-resolution timeframes checked first.

    Args:
        supporting_brain_ids: Brain IDs that voted for this position's entry.
        brains: Full brain registry list (dicts with ``brain_id``, ``brain_type``).
        micro_m15_types: Brain types belonging to the M15 micro group.
        micro_h1_types: Brain types belonging to the H1 micro group.
        micro_h4_types: Brain types belonging to the H4 micro group.
        micro_3bar_types: Brain types belonging to the 3-bar micro group.
        statarb_types: Brain types belonging to the statarb group.
        default_owner: Fallback when no brain matches any group.

    Returns:
        Strategy name (e.g. "micro_m15", "statarb_dynamic", "barrier_12bar").
    """
    if not supporting_brain_ids:
        return default_owner

    for bid in supporting_brain_ids:
        for bi in brains:
            if bi.get("brain_id") != bid:
                continue
            bt = bi.get("brain_type", "")
            # Ordered by specificity: higher-resolution timeframes first
            if bt in micro_m15_types:
                return "micro_m15"
            if bt in micro_h1_types:
                return "micro_h1"
            if bt in micro_h4_types:
                return "micro_h4"
            if bt in micro_3bar_types:
                return "micro_3bar"
            if bt in statarb_types:
                return "statarb_dynamic"
            # brain_id found but not in any group → next brain_id
            break

    return default_owner
