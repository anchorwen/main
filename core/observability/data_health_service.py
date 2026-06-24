"""DataHealthService — unified data health monitoring for live trading.

Iron Law for Monitoring #1 (Stability & Isolation):
  - All file I/O wrapped in fail_open_guard — a check failure must NEVER
    propagate to the trading main loop.
  - LIGHT mode targets <50ms latency (CRITICAL-tier only, tail reads, no
    directory scans, no full-file parsing).

Iron Law for Monitoring #2 (Recoverability):
  - State persisted via atomic write (tmp + os.replace).
  - Corrupt state file → silent return to fresh_health_state().

Iron Law for Monitoring #3 (Decoupling):
  - DataHealthService produces HealthReport ONLY.  Zero alert_hub calls,
    zero notify_trade calls, zero side effects beyond file reads + state writes.

Iron Law for Monitoring #4 (Iterability):
  - All checks registered via @health_check decorator.  Engine dispatch
    iterates the registry filtered by tier — no if/elif chains.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from core.observability._health_helpers import (
    _utc_iso,
)
from core.observability.data_health_schema import (
    DEFAULT_THRESHOLDS,
    HealthReport,
    SourceCheckResult,
    SourceStatus,
    Tier,
    fresh_health_state,
    get_checks,
)
from core.observability.data_health_schema import (
    build_alert_context as _build_alert_context,
)
from core.observability.health_checks import HealthCheckMethods

# ── DataHealthService ──────────────────────────────────────────────────────


class DataHealthService(HealthCheckMethods):
    """Unified data health monitor — two invocation modes.

    LIGHT mode (per-cycle, <50ms): CRITICAL-tier checks only, tail reads, no
      directory scans.  Safe to call synchronously from the main trading loop.

    FULL mode (daily_ops, ~1-5s): all tiers + cross-source validation + orphan
      detection.  Designed for once-daily execution.
    """

    def __init__(
        self,
        base_dir: str,
        symbol: str,
        *,
        thresholds: dict[str, float | int] | None = None,
        mode: str = "full",
        position_manager: Any = None,  # FIX-20260611-002: for position_limit check
    ):
        self._base_dir = base_dir
        self._symbol = symbol
        self._thresholds = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            self._thresholds.update(thresholds)
        self._mode = mode
        self._start_time = time.perf_counter()
        self._position_manager = position_manager
        # ── FIX-20260611-002: behavioral compliance incremental scanner ──
        self._cached_behavioral_metrics: Any = None
        self._position_exceeded_streak: int = 0
        self._silent_cycle_streak: int = 0  # FIX-20260611-003

    # ── Threshold helpers ──────────────────────────────────────────────

    def _t(self, key: str) -> float:
        return float(self._thresholds.get(key, 0))

    # ── Top-level dispatch ─────────────────────────────────────────────

    def run_full(self) -> HealthReport:
        """Run all registered checks (CRITICAL + HIGH + MEDIUM) + cross-source
        validation + orphan detection."""
        return self._run(tiers=(Tier.CRITICAL, Tier.HIGH, Tier.MEDIUM), include_extras=True)

    def run_lightweight(self) -> HealthReport:
        """Run CRITICAL-tier checks only.  Target <50ms latency."""
        return self._run(tiers=(Tier.CRITICAL,), include_extras=False)

    def _run(self, tiers: tuple[Tier, ...], include_extras: bool) -> HealthReport:
        report = HealthReport(
            schema_version="data_health_report.v1",
            generated_at=_utc_iso(),
            base_dir=self._base_dir,
            symbol=self._symbol,
        )

        sources: list[SourceCheckResult] = []
        for meta in get_checks():
            if meta.tier not in tiers:
                continue
            try:
                result = meta.func(self)
            except Exception as exc:  # BLE001:FOG — Iron Law #1
                result = SourceCheckResult(
                    source=meta.source,
                    tier=meta.tier,
                    status=SourceStatus.FAIL,
                    primary_code=f"{meta.source.upper()}_CHECK_CRASHED",
                    message=f"Check raised {type(exc).__name__}: {exc}",
                    checked_at=_utc_iso(),
                )
            if result.metrics:
                pass  # already set
            sources.append(result)

        report.sources = sources

        # Cross-source validation (FULL only)
        if include_extras:
            report.cross_checks = [
                self._check_brain_registry_governance_alignment(),
                self._check_journal_vs_pnl_ledger(),
                self._check_open_vs_close_convergence(),
            ]
            report.orphans = self._detect_orphan_subsystems()

        # Aggregate
        fail_count = sum(
            1 for s in sources if s.status in (SourceStatus.FAIL, SourceStatus.MISSING)
        )
        warn_count = sum(1 for s in sources if s.status == SourceStatus.WARN)
        report.primary_codes = [
            s.primary_code for s in sources if s.primary_code and s.status != SourceStatus.PASS
        ]
        if fail_count > 0:
            report.alert_level = "CRITICAL"
        elif warn_count > 2:
            report.alert_level = "WARNING"
        else:
            report.alert_level = "OK"
        report.aggregated = {
            "total_sources": len(sources),
            "pass_count": sum(1 for s in sources if s.status == SourceStatus.PASS),
            "warn_count": warn_count,
            "fail_count": fail_count,
            "missing_count": sum(1 for s in sources if s.status == SourceStatus.MISSING),
            "skipped_count": sum(1 for s in sources if s.status == SourceStatus.SKIPPED),
        }
        report.elapsed_ms = round((time.perf_counter() - self._start_time) * 1000, 2)

        return report

    # ══════════════════════════════════════════════════════════════════════
    # State persistence
    # ══════════════════════════════════════════════════════════════════════

    def _state_path(self) -> str:
        return os.path.join(self._base_dir, "state", "data_health_state.json")

    def load_health_state(self) -> dict[str, Any]:
        """Load persisted health state — graceful degradation on corruption.

        Iron Law for Monitoring #2: corrupt file → silent return to fresh state.
        """
        path = self._state_path()
        try:
            if not os.path.exists(path):
                return fresh_health_state(self._symbol)
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
            # Validate minimum schema
            if not isinstance(state, dict) or "schema_version" not in state:
                return fresh_health_state(self._symbol)
            # Ensure v2 structure exists
            if "sources" not in state:
                state["sources"] = {}
            if "legacy" not in state:
                state["legacy"] = {"last_close_count": 0, "checked_at": ""}
            state["symbol"] = self._symbol
            return state
        except (json.JSONDecodeError, FileNotFoundError, OSError, TypeError, ValueError):
            return fresh_health_state(self._symbol)

    def save_health_state(self, report: HealthReport) -> None:
        """Persist health state snapshot with atomic write.

        Iron Law for Monitoring #2: write to .tmp → os.replace (atomic).
        """
        path = self._state_path()
        current = self.load_health_state()
        now = _utc_iso()

        # Update per-source records
        sources_state: dict[str, dict[str, Any]] = current.get("sources", {})
        for src in report.sources:
            record = sources_state.get(src.source, {})
            # Track error history (last 3)
            errors = record.get("error_history", [])
            if isinstance(errors, list):
                if src.status in (SourceStatus.FAIL, SourceStatus.WARN, SourceStatus.MISSING):
                    errors.insert(
                        0,
                        {
                            "utc": now,
                            "code": src.primary_code,
                            "message": src.message[:200],
                        },
                    )
                    errors = errors[:3]
                elif src.status == SourceStatus.PASS:
                    # Clear error history on recovery
                    if errors and errors[0].get("code") != "RECOVERED":
                        errors.insert(0, {"utc": now, "code": "RECOVERED", "message": ""})
                        errors = errors[:3]

            # Compute trend
            trend = "stable"
            if len(errors) >= 2:
                # More errors in recent half → degrading
                mid = len(errors) // 2
                recent_errors = sum(
                    1 for e in errors[:mid] if e.get("code", "") not in ("RECOVERED", "")
                )
                older_errors = sum(
                    1 for e in errors[mid:] if e.get("code", "") not in ("RECOVERED", "")
                )
                if recent_errors > older_errors:
                    trend = "degrading"
                elif recent_errors < older_errors:
                    trend = "improving"

            sources_state[src.source] = {
                "last_check_utc": now,
                "last_status": src.status.value,
                "last_primary_code": src.primary_code,
                "metrics_snapshot": {
                    k: v
                    for k, v in src.metrics.items()
                    if isinstance(v, int | float | str | bool | type(None))
                },
                "error_history": errors,
                "trend": trend,
            }

        # Update cross-check records
        cross_state: dict[str, dict[str, Any]] = current.get("cross_checks", {})
        for cc in report.cross_checks:
            cross_state[cc.check_name] = {
                "last_check_utc": now,
                "last_status": cc.status.value,
                "delta_pct": cc.metrics.get("delta_pct"),
            }

        # Compute journal close count for legacy compat
        jl_count = 0
        for src in report.sources:
            if src.source == "trade_journal":
                jl_count = src.metrics.get("close_count_tail", 0)
                break

        state = {
            "schema_version": "data_health_state.v2",
            "updated_at": now,
            "symbol": self._symbol,
            "last_full_run_utc": now
            if self._mode == "full"
            else current.get("last_full_run_utc", ""),
            "last_lightweight_run_utc": now
            if self._mode != "full"
            else current.get("last_lightweight_run_utc", ""),
            "overall_status": report.alert_level,
            "sources": sources_state,
            "cross_checks": cross_state,
            "orphan_subsystems": [
                {"source_path": o.source_path, "pattern": o.pattern, "detail": o.detail}
                for o in report.orphans
            ],
            "legacy": {
                "last_close_count": jl_count,
                "checked_at": now,
            },
        }

        # Atomic write via StateWriter gate (DQAF-046 Plan B)
        try:
            from core.state.catalog import lookup
            from core.state.writer import StateWriter

            writer = StateWriter(self._base_dir, symbol=self._symbol)
            writer.write_artifact(lookup("DATA_HEALTH_STATE"), self._symbol, state)
        except OSError:
            pass  # Iron Law #1: persistence failure must not crash

    # ══════════════════════════════════════════════════════════════════════
    # ALERT CONTEXT (for external dispatcher — Iron Law #3)
    # ══════════════════════════════════════════════════════════════════════

    def build_alert_context(self, report: HealthReport) -> dict[str, Any]:
        """Convert report to alert context keys for external evaluation.

        This method produces data only — the caller (daily_ops / live_intent_loop)
        passes the dict to AlertService.evaluate().  DataHealthService itself
        never calls alert_hub or sends notifications (Iron Law #3).
        """
        return _build_alert_context(report)

    # ── FIX-20260611-002: Behavioral compliance — incremental log scanner ──
