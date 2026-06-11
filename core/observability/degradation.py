"""Degradation model — progressive risk reduction based on data quality.

FIX-20260611-022: Four-level progressive degradation replaces binary
circuit breaker.  When data health degrades, position sizing is reduced
gradually rather than suddenly stopping all trading.

Levels::

    NORMAL  → All data sources healthy, full trading
    YELLOW  → Single source degraded, 40% position size
    ORANGE  → Multiple sources degraded or cross-source mismatch, 15% size
    RED     → Core safety degraded, management-only (no new trades)

Recovery is automatic when health checks return to NORMAL.

Usage::

    from core.observability.degradation import evaluate_degradation, DegradationLevel

    level = evaluate_degradation(health_report)
    if level >= DegradationLevel.ORANGE:
        block_new_trades()
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class DegradationLevel(IntEnum):
    """Progressive degradation levels.  Higher = more restricted.

    IntEnum so we can compare: level >= DegradationLevel.ORANGE.
    """

    NORMAL = 0  # Full trading, all sources healthy
    YELLOW = 1  # Single-source degradation, reduced size
    ORANGE = 2  # Multi-source degradation, minimal size, no new positions
    RED = 3  # Core safety degraded, management-only, close-only


@dataclass
class DegradationConstraints:
    """Trading constraints derived from degradation level."""

    level: DegradationLevel
    max_position_size_pct: float  # Fraction of normal position size
    allow_new_positions: bool
    allow_trail_updates: bool
    reason: str

    @classmethod
    def for_level(cls, level: DegradationLevel, reason: str = "") -> DegradationConstraints:
        """Return the constraint set for a given degradation level."""
        if level == DegradationLevel.NORMAL:
            return cls(
                level=level,
                max_position_size_pct=1.0,
                allow_new_positions=True,
                allow_trail_updates=True,
                reason=reason or "All data sources healthy",
            )
        elif level == DegradationLevel.YELLOW:
            return cls(
                level=level,
                max_position_size_pct=0.40,
                allow_new_positions=True,
                allow_trail_updates=True,
                reason=reason or "Single data source degraded",
            )
        elif level == DegradationLevel.ORANGE:
            return cls(
                level=level,
                max_position_size_pct=0.15,
                allow_new_positions=False,
                allow_trail_updates=True,
                reason=reason or "Multiple data sources degraded or cross-source mismatch",
            )
        else:  # RED
            return cls(
                level=level,
                max_position_size_pct=0.0,
                allow_new_positions=False,
                allow_trail_updates=False,
                reason=reason or "Core safety degraded — management-only mode",
            )


# ── Degradation evaluation ──────────────────────────────────────────────────


def evaluate_degradation(
    health_report: dict[str, Any],
    *,
    custom_rules: dict[str, Any] | None = None,
) -> DegradationConstraints:
    """Map a health report to degradation level and trading constraints.

    Args:
        health_report: Output from DataHealthService.check_data_health().
        custom_rules: Optional per-source overrides for severity mapping.

    Returns:
        DegradationConstraints with the appropriate level and limits.
    """
    sources = health_report.get("sources", {})
    cross_checks = health_report.get("cross_checks", [])

    # ── Count failures and warnings by tier ──
    critical_fails: list[str] = []
    critical_warns: list[str] = []
    other_fails: list[str] = []
    cross_fails: list[str] = []

    for source_name, source_result in sources.items():
        status = source_result.get("status", "unknown")
        tier = source_result.get("tier", "info")

        if status == "fail":
            if tier == "critical":
                critical_fails.append(source_name)
            else:
                other_fails.append(source_name)
        elif status == "warn":
            if tier == "critical":
                critical_warns.append(source_name)

    for cc in cross_checks:
        if cc.get("status") == "fail":
            cross_fails.append(cc.get("name", "unknown_cross_check"))

    # ── Degradation decision tree ──

    # RED: Any core safety check has failed
    core_safety_checks = {
        "execution_state",
        "bar_sync_state",
        "mt5_bridge_health",
        "journal_completeness",
    }
    red_fails = [f for f in critical_fails if f in core_safety_checks]
    if red_fails or cross_fails:
        reasons = []
        if red_fails:
            reasons.append(f"Core safety checks failed: {', '.join(red_fails)}")
        if cross_fails:
            reasons.append(f"Cross-source reconciliation failed: {', '.join(cross_fails)}")
        return DegradationConstraints.for_level(
            DegradationLevel.RED,
            reason="; ".join(reasons),
        )

    # ORANGE: Multiple critical failures or cross-source mismatch
    if len(critical_fails) >= 2:
        return DegradationConstraints.for_level(
            DegradationLevel.ORANGE,
            reason=f"Multiple critical checks failed: {', '.join(critical_fails)}",
        )

    if len(critical_fails) >= 1 and len(critical_warns) >= 2:
        return DegradationConstraints.for_level(
            DegradationLevel.ORANGE,
            reason=f"Critical failure ({critical_fails[0]}) + {len(critical_warns)} critical warnings",
        )

    # YELLOW: Single critical failure with no warnings, or multiple warnings
    if len(critical_fails) == 1:
        return DegradationConstraints.for_level(
            DegradationLevel.YELLOW,
            reason=f"Single critical check failed: {critical_fails[0]}",
        )

    if len(critical_warns) >= 3:
        return DegradationConstraints.for_level(
            DegradationLevel.YELLOW,
            reason=f"Multiple critical warnings: {', '.join(critical_warns[:3])}",
        )

    if other_fails:
        return DegradationConstraints.for_level(
            DegradationLevel.YELLOW,
            reason=f"Non-critical failures: {', '.join(other_fails[:3])}",
        )

    # NORMAL: Everything is fine
    return DegradationConstraints.for_level(DegradationLevel.NORMAL)


# ── Staleness-based degradation ─────────────────────────────────────────────


def evaluate_staleness(
    sources: dict[str, Any],
    *,
    fresh_threshold_min: float = 5.0,
    stale_threshold_min: float = 15.0,
    critical_threshold_min: float = 30.0,
) -> DegradationLevel | None:
    """Evaluate degradation based purely on data source staleness.

    This is the simplest trigger — if key data sources haven't been
    updated recently, something is wrong regardless of other checks.

    Args:
        sources: Dict mapping source_name → {"age_minutes": float, ...}.
        fresh_threshold_min: Data is "fresh" if age < this many minutes.
        stale_threshold_min: Data is "stale" if age >= this many minutes.
        critical_threshold_min: Data is "critical" if age >= this many minutes.

    Returns:
        DegradationLevel, or None if all sources are fresh.
    """
    key_sources = {"bar_sync_state", "execution_state", "golden_master"}
    max_age = 0.0
    stale_count = 0
    critical_count = 0

    for source_name in key_sources:
        source = sources.get(source_name, {})
        age = source.get("age_minutes", 0.0)
        if age > max_age:
            max_age = age
        if age >= stale_threshold_min:
            stale_count += 1
        if age >= critical_threshold_min:
            critical_count += 1

    if critical_count >= 2:
        return DegradationLevel.RED
    if critical_count >= 1 or stale_count >= 2:
        return DegradationLevel.ORANGE
    if stale_count >= 1:
        return DegradationLevel.YELLOW

    return None  # All fresh


# ── Integration helper ──────────────────────────────────────────────────────


def apply_degradation_to_decision(
    constraints: DegradationConstraints,
    volume: float,
    should_trade: bool,
) -> tuple[float, bool, str]:
    """Apply degradation constraints to a trade decision.

    Returns:
        (adjusted_volume, adjusted_should_trade, reason_suffix)
    """
    if constraints.level == DegradationLevel.NORMAL:
        return volume, should_trade, ""

    if constraints.level == DegradationLevel.RED:
        return 0.0, False, f" [degraded:RED:{constraints.reason}]"

    if constraints.level == DegradationLevel.ORANGE:
        if not constraints.allow_new_positions:
            return 0.0, False, f" [degraded:ORANGE:no_new_positions:{constraints.reason}]"
        adjusted = max(0.01, round(volume * constraints.max_position_size_pct, 2))
        return adjusted, should_trade, f" [degraded:ORANGE:{constraints.reason}]"

    # YELLOW
    adjusted = max(0.01, round(volume * constraints.max_position_size_pct, 2))
    return adjusted, should_trade, f" [degraded:YELLOW:{constraints.reason}]"
