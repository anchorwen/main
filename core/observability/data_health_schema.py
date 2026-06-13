"""Data health monitoring schema — types, enums, decorator registry, thresholds.

Iron Law for Monitoring #4 (Iterability): All health checks registered via
the @health_check decorator.  Adding a new check requires only a decorated
function — zero changes to the engine dispatch loop.

Iron Law for Monitoring #3 (Decoupling): This module defines PURE DATA types
only.  No I/O, no alert sending, no side effects.  DataHealthService produces
a HealthReport; external callers decide how to route it.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# ── Enums ──────────────────────────────────────────────────────────────────


class Tier(str, enum.Enum):
    """Severity tier — determines which checks run in LIGHT vs FULL mode."""

    CRITICAL = "critical"  # LIGHT + FULL
    HIGH = "high"  # FULL only
    MEDIUM = "medium"  # FULL only


class SourceStatus(str, enum.Enum):
    """Outcome of a single health check."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    MISSING = "missing"  # file does not exist
    SKIPPED = "skipped"  # check not applicable (e.g. no positions open)


# ── Result types ───────────────────────────────────────────────────────────


@dataclass
class SourceCheckResult:
    """Result of checking one data source."""

    source: str  # e.g. "trade_journal", "feature_store"
    tier: Tier
    status: SourceStatus
    primary_code: str  # e.g. "JOURNAL_PNL_NULL_RATE_HIGH"
    metrics: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    checked_at: str = ""  # ISO UTC


@dataclass
class CrossCheckResult:
    """Result of cross-validating two independent data sources."""

    check_name: str  # e.g. "journal_vs_pnl_ledger"
    status: SourceStatus
    primary_code: str
    metrics: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    checked_at: str = ""


@dataclass
class OrphanFinding:
    """A subsystem whose state file exists but contains only initial/empty data."""

    source_path: str  # relative to base_dir
    pattern: str  # "zero_data" | "never_written" | "empty_init"
    detail: str  # human-readable description


@dataclass
class BehavioralMetrics:
    """Incremental counters for behavioral compliance checks (FIX-20260611-002).

    Uses cursor-based log scanning (seek/tell) to avoid double-counting
    events across overlapping time windows.  Counters are reset each audit
    tick; the file cursor persists to enable incremental reads only.
    """

    gate_bypass_count: int = 0
    brain_alerts: dict[str, int] = field(default_factory=dict)
    intent_dispatched_count: int = 0
    strategy_rejections: int = 0
    cycle_count: int = 0
    last_line_count: int = 0  # line-number cursor (safer than byte seek/tell in text mode)
    intent_log_path: str = ""


@dataclass
class HealthReport:
    """Aggregate health report produced by DataHealthService.

    This is the ONLY output of the monitoring system (Iron Law #3).
    Alert routing is the caller's responsibility.
    """

    schema_version: str = "data_health_report.v1"
    generated_at: str = ""  # ISO UTC
    base_dir: str = ""
    symbol: str = ""
    alert_level: str = "OK"  # OK | WARNING | CRITICAL
    primary_codes: list[str] = field(default_factory=list)
    sources: list[SourceCheckResult] = field(default_factory=list)
    cross_checks: list[CrossCheckResult] = field(default_factory=list)
    orphans: list[OrphanFinding] = field(default_factory=list)
    aggregated: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0


# ── Decorator registry (Iron Law for Monitoring #4) ────────────────────────


@dataclass
class HealthCheckMeta:
    """Metadata for a registered health check function."""

    tier: Tier
    source: str
    func: Callable[..., SourceCheckResult]
    description: str = ""


# Global registry populated by @health_check decorator.
_HEALTH_CHECKS: dict[str, HealthCheckMeta] = {}


def health_check(*, tier: Tier, source: str, description: str = ""):
    """Decorator: register a DataHealthService method as a health check.

    Usage:
        class DataHealthService:
            @health_check(tier=Tier.CRITICAL, source="trade_journal",
                          description="Check PnL null rate and entry completeness")
            def check_trade_journal(self) -> SourceCheckResult: ...

    The engine iterates _HEALTH_CHECKS filtered by tier — adding a new check
    requires ONLY a decorated method, zero changes to the dispatch loop.
    """

    def decorator(func: Callable[..., SourceCheckResult]):
        _HEALTH_CHECKS[source] = HealthCheckMeta(
            tier=tier,
            source=source,
            func=func,
            description=description,
        )
        return func

    return decorator


def get_checks(tier: Tier | None = None) -> list[HealthCheckMeta]:
    """Return registered checks, optionally filtered by tier."""
    checks = list(_HEALTH_CHECKS.values())
    if tier is not None:
        checks = [c for c in checks if c.tier == tier]
    return sorted(checks, key=lambda c: (c.tier.value, c.source))


# ── Default thresholds ─────────────────────────────────────────────────────
# Overridable via configs/live_btc.yaml → data_health.thresholds


DEFAULT_THRESHOLDS: dict[str, float | int] = {
    # Feature store
    "feature_store_max_age_minutes": 15,
    "feature_store_max_zero_pct": 0.30,
    "feature_store_max_nan_pct": 0.05,
    "feature_store_expected_growth_per_hour": 12,  # M5 = 12 records/hour
    # Journal
    "journal_pnl_null_max_pct": 0.10,
    "journal_close_open_ratio_min": 0.30,
    "journal_max_entry_age_hours": 168,  # 7 days
    # State files
    "execution_state_max_age_minutes": 15,
    "governance_min_live_brains": 1,
    "meta_filter_max_atr_freeze_hours": 6,
    "bridge_heartbeat_max_age_seconds": 120,
    "bar_sync_max_lag_count": 10,
    "state_file_staleness_minutes": 60,
    # Brain / ledger
    "brain_perf_min_records_per_brain": 5,
    "pnl_ledger_max_settled_pending_ratio": 10.0,
    # Cross-source tolerance
    "cross_source_journal_pnl_tolerance_pct": 0.10,
    "cross_source_close_settled_tolerance_pct": 0.05,
    "cross_source_open_close_max_age_hours": 24,
    # ── FIX-20260611-002: behavioral compliance ──
    "gate_bypass_max_count": 0,
    "position_limit_consecutive_alerts": 3,
    "brain_output_min_productive_brains": 1,
    "brain_output_max_alerts_per_brain": 5,
    "trade_activity_max_silent_cycles": 60,
    "behavioral_max_lines_per_tick": 500,
}


# ── Alert context keys ─────────────────────────────────────────────────────
# Keys injected into the alert system context dict by the caller.
# alert_system.rules (RULE-012..016) evaluate against these.


def build_alert_context(report: HealthReport) -> dict[str, Any]:
    """Convert a HealthReport into alert-system context keys.

    Called externally (daily_ops / live_intent_loop) — NOT inside
    DataHealthService (Iron Law #3: generator/dispatcher separation).

    Iron Law #13 (D1 — Data as Contract): source-level detail is carried as
    ``list[dict]`` — never pre-flattened into ``\\n``-joined strings.
    Channel renderers iterate the lists and produce bullet-point sections.
    """
    fail_count = sum(
        1 for s in report.sources if s.status in (SourceStatus.FAIL, SourceStatus.MISSING)
    )
    warn_count = sum(1 for s in report.sources if s.status == SourceStatus.WARN)
    cross_fail = sum(1 for c in report.cross_checks if c.status != SourceStatus.PASS)

    # Structured source detail (D1): list[dict] — channel renders as bullets
    failed_sources: list[dict[str, str]] = [
        {
            "source": s.source,
            "code": s.primary_code,
            "message": s.message[:200] if s.message else "",
        }
        for s in report.sources
        if s.status in (SourceStatus.FAIL, SourceStatus.MISSING)
    ]
    warned_sources: list[dict[str, str]] = [
        {
            "source": s.source,
            "code": s.primary_code,
            "message": s.message[:200] if s.message else "",
        }
        for s in report.sources
        if s.status == SourceStatus.WARN
    ]

    return {
        # Aggregate counts (for RULE-012..016 threshold evaluation)
        "data_health_critical_fail_count": fail_count,
        "data_health_warn_count": warn_count,
        "cross_source_discrepancy_count": cross_fail,
        "orphan_subsystem_count": len(report.orphans),
        "stale_state_file_count": sum(
            1 for s in report.sources
            if "STALE" in s.primary_code or "STALENESS" in s.primary_code
        ),
        "data_health_overall": report.alert_level,
        # Structured payload (D1): list[dict] — channel renders as bullet sections
        "data_health_failed_sources": failed_sources,
        "data_health_warned_sources": warned_sources,
    }


# ── Persisted health state helpers ─────────────────────────────────────────


@dataclass
class SourceHealthRecord:
    """Per-source health tracking persisted across runs."""

    last_check_utc: str = ""
    last_status: str = "unknown"
    last_primary_code: str = ""
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)
    error_history: list[dict[str, str]] = field(default_factory=list)  # last 3
    trend: str = "stable"  # improving | stable | degrading


def fresh_health_state(symbol: str = "") -> dict[str, Any]:
    """Return a clean initial health state (Iron Law #2: graceful degradation)."""
    return {
        "schema_version": "data_health_state.v2",
        "updated_at": "",
        "symbol": symbol,
        "last_full_run_utc": "",
        "last_lightweight_run_utc": "",
        "overall_status": "unknown",
        "sources": {},
        "cross_checks": {},
        "orphan_subsystems": [],
        "legacy": {"last_close_count": 0, "checked_at": ""},
    }
