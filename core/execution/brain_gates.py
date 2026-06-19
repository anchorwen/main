"""Brain proposal gates — pure functions extracted from strategy_line.py.

Strangler Fig #17: extracted from ``StrategyLine.evaluate()`` L774-810.
Pure function contract: zero I/O, zero global state, same input → same output.

Related: Strangler Fig #13 (trend_isolation_gates), #16 (trend_volume_guard)
"""

from __future__ import annotations

from typing import Any


def count_valid_voters(proposals: list[Any]) -> int:
    """Count brains that produced a non-neutral directional signal.

    Brains with vote_weight <= 0 (contract-muted or governance-silenced)
    are excluded — they cannot influence consensus, so counting them as
    "valid voters" would create deadlock where muted_brain_count > 0
    but none can actually vote.

    Args:
        proposals: List of BrainSignal objects with ``vote_weight`` and
                   ``direction`` attributes.

    Returns:
        Number of valid voters (non-neutral direction, positive vote_weight).
    """
    count = 0
    for p in proposals:
        _vw_raw = getattr(p, "vote_weight", None)
        vw = float(_vw_raw) if _vw_raw is not None else 1.0
        if vw <= 0.0:
            continue
        direction = getattr(p, "direction", None)
        if direction is None:
            pred = getattr(p, "prediction", None) or {}
            direction = pred.get("direction_bias", "neutral") if isinstance(pred, dict) else "neutral"
        if direction != "neutral":
            count += 1
    return count


def check_min_valid_brains(
    proposals: list[Any],
    min_valid_brains: int,
) -> int:
    """Check whether enough valid brains support a trade.

    Args:
        proposals: Brain signal proposals.
        min_valid_brains: Minimum number of valid voters required for a trade.

    Returns:
        0 if the gate passes (enough voters OR zero voters — let consensus decide).
        Otherwise returns the actual voter count (for diagnostics).
    """
    valid = count_valid_voters(proposals)
    if 0 < valid < min_valid_brains:
        return valid  # gate blocks — caller constructs StrategyDecision
    return 0  # gate passes
