"""Correlation Sizer — √N decay for multi-strategy consensus positions.

Tier 3 of the three-tier institutional sizing architecture.
When N correlated strategies fire in the same direction on the same symbol,
total position size is NOT N × base_volume.  Instead, it is Σvolumes / √N.

This preserves √N risk scaling (standard under IID assumption) while preventing
linear risk concentration from homogeneous signals.

Trap 2 fix: After √N discount, positions are rounded to lot_step granularity.
Strategies that fall below min_lot after discounting are dropped (weakest first)
rather than being silently bumped up (risk undercount) or truncated to zero
without audit trail.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass
class ClusterResult:
    """Audit record for a single √N discount cluster."""

    direction: str
    n_same_direction: int
    raw_total_volume: float
    discounted_volume: float
    dropped_strategies: list[str]


def apply_sqrt_n_discount(
    decisions: list[Any],
    lot_step: float = 0.01,
    min_lot: float = 0.01,
    policy: str = "drop_weakest",
) -> tuple[list[Any], list[ClusterResult]]:
    """Apply √N correlation discount to all active strategy decisions.

    Groups decisions by direction (long/short), applies 1/√n discount per
    cluster, rounds to lot_step, and drops strategies that fall below min_lot.

    Args:
        decisions: List of StrategyDecision objects (modified in place).
        lot_step: Minimum lot increment (0.01 for cent accounts).
        min_lot: Minimum allowed lot size.
        policy: "drop_weakest" — drop strategies with lowest confidence×volume
                when post-discount volume < min_lot.

    Returns:
        (modified decisions, cluster audit records)
    """
    # Partition by direction
    longs = [
        d
        for d in decisions
        if getattr(d, "should_trade", False) and getattr(d, "direction", "") == "long"
    ]
    shorts = [
        d
        for d in decisions
        if getattr(d, "should_trade", False) and getattr(d, "direction", "") == "short"
    ]

    results: list[ClusterResult] = []

    for direction, cluster in [("long", longs), ("short", shorts)]:
        n = len(cluster)
        if n <= 1:
            continue

        discount = 1.0 / math.sqrt(n)
        raw_total = sum(getattr(d, "volume", 0.0) for d in cluster)

        # Apply discount and round to lot_step
        for d in cluster:
            raw = getattr(d, "volume", 0.0)
            # Guard against NaN/Inf — treat as zero (no position)
            if math.isnan(raw) or math.isinf(raw):
                d.volume = 0.0
                d.should_trade = False
                prefix = f"{getattr(d, 'reason', '')} | " if getattr(d, "reason", "") else ""
                d.reason = f"{prefix}sqrt_n_dropped:invalid_volume"
                continue
            discounted = raw * discount
            stepped = max(0.0, round(discounted / lot_step) * lot_step)
            if stepped < min_lot:
                # Mark for drop — will be handled below
                d.volume = 0.0
                d.should_trade = False
                prefix = f"{getattr(d, 'reason', '')} | " if getattr(d, "reason", "") else ""
                d.reason = f"{prefix}sqrt_n_dropped:n={n}_vol_below_min_lot"
            else:
                d.volume = max(min_lot, stepped)

        # Collect dropped strategy names
        dropped = [
            getattr(d, "strategy_name", "unknown")
            for d in cluster
            if getattr(d, "volume", 0.0) == 0.0 and getattr(d, "should_trade", False) is False
        ]

        discounted_total = sum(getattr(d, "volume", 0.0) for d in cluster)

        results.append(
            ClusterResult(
                direction=direction,
                n_same_direction=n,
                raw_total_volume=round(raw_total, 4),
                discounted_volume=round(discounted_total, 4),
                dropped_strategies=dropped,
            )
        )

    return decisions, results
