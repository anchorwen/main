"""Cooldown gate — pure function extracted from live_cycle.py.

Strangler Fig #24 (FIX-20260619-044): Extracted cooldown_blocks_fire()
as a standalone pure function.  Deterministic, zero I/O, zero state.
"""

from __future__ import annotations


def cooldown_blocks_fire(now: float, last_fire: float, cooldown_seconds: float) -> bool:
    """Check if a cooldown period has not yet elapsed.

    Args:
        now: Current Unix timestamp.
        last_fire: Unix timestamp of the last fire event.
        cooldown_seconds: Minimum seconds between fires.

    Returns:
        True if the cooldown is still active (fire should be blocked).
    """
    return (now - last_fire) < cooldown_seconds
