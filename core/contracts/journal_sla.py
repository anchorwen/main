"""Journal data integrity SLA — thresholds, tolerances, and health classification.

Institutional Data SLA Column 2 (Journal SSOT): Defines quantitative boundaries
for journal health monitoring.  Every threshold is sourced from the Instrument
Committee's Production Implementation Mandate (2026-06-26).

Usage:
    from core.contracts.journal_sla import FLOAT_TOLERANCE, QUARANTINE_DAILY_LIMIT, assess_coverage
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

# ═══════════════════════════════════════════════════════════════════════════
# Instrument-Specific Floating-Point Delta Tolerance
# ═══════════════════════════════════════════════════════════════════════════
#
# MT5 C++ double and Python float / JSON serialisation produce different
# IEEE 754 bit patterns for the same monetary value (magnitude ±0.000001).
# ABSOLUTE EQUALITY (journal_pnl == deal.profit) IS FORBIDDEN.
#
# Tolerances are pegged to each instrument's minimum PnL increment (tick size
# × contract size).  A delta smaller than the minimum tick increment cannot
# represent a real accounting difference — it is pure floating-point noise.
#
# 投委会防线 #2 — Floating-Point Delta Tolerance

FLOAT_TOLERANCE: dict[str, float] = {
    "BTC": 0.01,  # 1 lot ≈ $74,000, tick=0.01 → min PnL increment $0.01
    "XAU": 0.001,  # 1 lot ≈ $3,200, tick=0.001 → min PnL increment $0.001
}

FLOAT_TOLERANCE_FALLBACK: float = 0.01  # Conservative default for unknown symbols


def get_tolerance(symbol: str) -> float:
    """Return the float tolerance for *symbol*, case-insensitive."""
    for key, tol in FLOAT_TOLERANCE.items():
        if key.upper() in symbol.upper():
            return tol
    return FLOAT_TOLERANCE_FALLBACK


# ═══════════════════════════════════════════════════════════════════════════
# Reconciliation Status Labels
# ═══════════════════════════════════════════════════════════════════════════


class ReconStatus:
    """Status tags emitted by PnL reconciliation."""

    WITHIN_TOLERANCE: ClassVar[str] = "within_tolerance"
    NORMALIZED: ClassVar[str] = "normalized"
    DISPUTED: ClassVar[str] = "disputed"


# ═══════════════════════════════════════════════════════════════════════════
# Quarantine Poison Pill Thresholds
# ═══════════════════════════════════════════════════════════════════════════
#
# 投委会防线 #1 — Quarantine Poison Pill
# 隔离 ≠ 安全。幽灵订单在券商端有真实爆仓风险，隔离只是防火墙上的一道门。
#
# The quarantine journal_orphan_quarantine.jsonl is a safety valve, not a
# trash can.  If it accumulates entries, something is wrong at the broker
# level — untracked positions may have real liquidation risk.

QUARANTINE_DAILY_LIMIT: int = 10  # >= 10 orphans/day → P0 DingTalk alert
QUARANTINE_DEGRADED: int = 5  # >= 5 → "degraded" in health report


# ═══════════════════════════════════════════════════════════════════════════
# Journal Health SLA Thresholds
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class JournalHealthSLA:
    """Quantitative health thresholds for the live trade journal.

    All thresholds are applied per-instrument per daily_ops run.
    """

    MIN_COVERAGE_PCT: ClassVar[float] = 95.0
    """Journal must cover >= 95% of MT5 trades (by ticket overlap)."""

    MAX_PNL_MISMATCH_PCT: ClassVar[float] = 1.0
    """< 1% of overlapping trades may have PnL mismatch beyond tolerance."""

    MAX_ORPHAN_CLOSES_PER_DAY: ClassVar[int] = 5
    """Maximum orphan close events (close without open) per day."""

    MAX_NULL_PNL_CLOSES_PCT: ClassVar[float] = 5.0
    """< 5% of close entries may have null (unresolved) PnL."""

    MAX_BREAKEVEN_CLASSIFICATION_PCT: ClassVar[float] = 2.0
    """< 2% of closes classified as 'breakeven' — red flag for pnl=0 bugs."""

    @classmethod
    def assess(cls, health: dict) -> str:
        """Return 'compliant' | 'degraded' | 'violated' from a health report dict."""
        violations = 0
        degradations = 0

        coverage = health.get("coverage_pct", 100.0)
        if coverage < cls.MIN_COVERAGE_PCT:
            violations += 1

        pnl_mismatch = health.get("pnl_mismatch_pct", 0.0)
        if pnl_mismatch > cls.MAX_PNL_MISMATCH_PCT:
            violations += 1

        orphans = health.get("orphan_count", 0)
        if orphans >= QUARANTINE_DAILY_LIMIT:
            violations += 1
        elif orphans >= QUARANTINE_DEGRADED:
            degradations += 1

        null_pnl = health.get("null_pnl_pct", 0.0)
        if null_pnl > cls.MAX_NULL_PNL_CLOSES_PCT:
            degradations += 1

        breakeven = health.get("breakeven_pct", 0.0)
        if breakeven > cls.MAX_BREAKEVEN_CLASSIFICATION_PCT:
            degradations += 1

        if violations > 0:
            return "violated"
        if degradations > 0:
            return "degraded"
        return "compliant"
