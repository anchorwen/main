"""InvariantEngine — 15 binary predicate invariants for UGR v3.1.

Runs in SHADOW MODE: violations are logged and reported but NEVER halt
trading.  The engine is called from the main trading loop on every cycle
(or at a configurable interval) and evaluates all registered invariants
against the current system context.

UGR v3.1 §A06: Each invariant is a named binary predicate that receives
a ``context`` dict and returns ``(ok: bool, detail: str)``.  Violations
are routed to the alert bus for async delivery.

Usage::

    engine = InvariantEngine(wal=wal, alert_hub=hub)
    context = {
        "open_positions": 3,
        "max_positions": 8,
        "daily_pnl": -120.0,
        "daily_loss_limit": -500.0,
        ...
    }
    violations = engine.check_all(context)
    # violations contain {"invariant": str, "detail": str, "severity": str}
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.data.write_ahead_log import WriteAheadLog
from core.runtime.fault_handler import fail_open_guard

# ═══════════════════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════════════════

# An invariant predicate: receives context → (ok, detail)
InvariantFn = Callable[[dict[str, Any]], tuple[bool, str]]


@dataclass
class InvariantDef:
    """Definition of a single invariant check."""

    name: str
    description: str
    check: InvariantFn
    severity: str = "warning"  # "critical" | "warning" | "info"
    enabled: bool = True


@dataclass
class InvariantViolation:
    """A single invariant violation."""

    invariant: str
    detail: str
    severity: str
    timestamp_wall: float = field(default_factory=time.time)


# ═══════════════════════════════════════════════════════════════════════════
# InvariantEngine
# ═══════════════════════════════════════════════════════════════════════════


class InvariantEngine:
    """Shadow-mode invariant checker — 15 binary predicates.

    Evaluates invariants against system context each cycle.  Violations
    are collected and can be consumed by the alert bus.  NEVER halts
    trading — this is a pure observation layer.

    Thread-safe for read-only check_all() calls.  Registration of new
    invariants is NOT thread-safe (expected at init time only).
    """

    def __init__(
        self,
        wal: WriteAheadLog | None = None,
        alert_hub: Any = None,  # LiveAlertHub (avoid circular import)
    ) -> None:
        self._wal = wal
        self._alert_hub = alert_hub
        self._invariants: dict[str, InvariantDef] = {}
        self._last_check_at: float = 0.0
        self._checks_run: int = 0
        self._violations_total: int = 0
        self._last_violations: list[InvariantViolation] = []
        self._last_monotonic: float = 0.0

        self._register_defaults()

    # ── Core API ────────────────────────────────────────────────────────

    def check_all(self, context: dict[str, Any]) -> list[InvariantViolation]:
        """Evaluate all enabled invariants. Returns violations (never raises).

        This is the primary entry point, called from the trading loop.
        Violations are shadow-logged — they never halt execution.
        """
        violations: list[InvariantViolation] = []
        now = time.monotonic()
        self._checks_run += 1
        self._last_check_at = now

        for inv in self._invariants.values():
            if not inv.enabled:
                continue
            try:
                ok, detail = inv.check(context)
                if not ok:
                    v = InvariantViolation(
                        invariant=inv.name,
                        detail=detail,
                        severity=inv.severity,
                    )
                    violations.append(v)
            except (TypeError, ValueError, RuntimeError, KeyError, AttributeError):
                with fail_open_guard("invariant_engine:check_all"):
                    # Shadow mode: invariant failure must never propagate
                    violations.append(
                        InvariantViolation(
                            invariant=inv.name,
                            detail="Invariant evaluation raised exception",
                            severity="warning",
                        )
                    )

        self._violations_total += len(violations)
        self._last_violations = violations

        # Update monotonic clock tracker
        mono = context.get("monotonic_now")
        if isinstance(mono, int | float):
            self._last_monotonic = float(mono)

        return violations

    def check_all_and_alert(self, context: dict[str, Any]) -> list[InvariantViolation]:
        """Evaluate invariants and route violations to the alert bus.

        Same as check_all(), but also pushes each violation to the
        alert hub (if configured) for async delivery.
        """
        violations = self.check_all(context)
        if violations and self._alert_hub is not None:
            for v in violations:
                with contextlib.suppress(Exception):
                    self._alert_hub.send_critical(
                        reason=f"invariant:{v.invariant}",
                        detail={
                            "invariant": v.invariant,
                            "detail": v.detail,
                            "severity": v.severity,
                        },
                    )
        return violations

    # ── Registration ────────────────────────────────────────────────────

    def register(self, inv: InvariantDef) -> None:
        """Register an additional invariant (not thread-safe — init time only)."""
        self._invariants[inv.name] = inv

    def disable(self, name: str) -> None:
        """Disable an invariant by name."""
        if name in self._invariants:
            self._invariants[name].enabled = False

    def enable(self, name: str) -> None:
        """Re-enable a previously disabled invariant."""
        if name in self._invariants:
            self._invariants[name].enabled = True

    # ── Status ──────────────────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        """Return engine health status."""
        return {
            "invariants_registered": len(self._invariants),
            "invariants_enabled": sum(1 for inv in self._invariants.values() if inv.enabled),
            "checks_run": self._checks_run,
            "violations_total": self._violations_total,
            "last_check_at": self._last_check_at,
            "last_violation_count": len(self._last_violations),
            "last_violations": [
                {"invariant": v.invariant, "detail": v.detail, "severity": v.severity}
                for v in self._last_violations[-10:]  # Last 10 only
            ],
        }

    # ═══════════════════════════════════════════════════════════════════
    # Default Invariants (15)
    # ═══════════════════════════════════════════════════════════════════

    def _register_defaults(self) -> None:
        """Register the 15 standard UGR v3.1 invariants."""
        defaults: list[InvariantDef] = [
            # ── Invariant 1: WAL hash chain intact ──
            InvariantDef(
                name="wal_hash_chain_intact",
                description="WAL hash chain is unbroken from genesis to tip",
                check=self._check_wal_integrity,
                severity="critical",
            ),
            # ── Invariant 2: Circuit breaker not persistently open ──
            InvariantDef(
                name="circuit_breaker_not_open",
                description="Circuit breaker is closed or half-open",
                check=self._check_circuit_breaker,
                severity="critical",
            ),
            # ── Invariant 3: Position count within limits ──
            InvariantDef(
                name="position_count_bounded",
                description="Open position count ≤ configured maximum",
                check=self._check_position_count,
                severity="warning",
            ),
            # ── Invariant 4: Risk budget non-negative ──
            InvariantDef(
                name="risk_budget_positive",
                description="Daily PnL has not breached daily loss limit",
                check=self._check_risk_budget,
                severity="critical",
            ),
            # ── Invariant 5: No duplicate position tickets ──
            InvariantDef(
                name="no_duplicate_tickets",
                description="No duplicate position tickets detected in current cycle",
                check=self._check_duplicate_tickets,
                severity="warning",
            ),
            # ── Invariant 6: Feature data within freshness SLA ──
            InvariantDef(
                name="feature_age_within_sla",
                description="Feature buffer age ≤ freshness SLA (310s)",
                check=self._check_feature_freshness,
                severity="warning",
            ),
            # ── Invariant 7: At least one live brain ──
            InvariantDef(
                name="live_brain_count_positive",
                description="At least 1 brain is in LIVE status",
                check=self._check_live_brain_count,
                severity="critical",
            ),
            # ── Invariant 8: Governance state present ──
            InvariantDef(
                name="governance_state_present",
                description="Governance state contains brain_states key",
                check=self._check_governance_state,
                severity="critical",
            ),
            # ── Invariant 9: Calibrator not contaminated ──
            InvariantDef(
                name="calibrator_variance_nonzero",
                description="Conformal calibrator has p_win variance > 0 (not collapsed to single value)",
                check=self._check_calibrator_health,
                severity="warning",
            ),
            # ── Invariant 10: Alert queue pressure OK ──
            InvariantDef(
                name="alert_queue_pressure_ok",
                description="Alert queue depth < 90% of capacity",
                check=self._check_alert_queue,
                severity="warning",
            ),
            # ── Invariant 11: Supervisor heartbeat recent ──
            InvariantDef(
                name="supervisor_heartbeat_recent",
                description="SupervisedScheduler supervisor has checked in recently",
                check=self._check_supervisor_health,
                severity="warning",
            ),
            # ── Invariant 12: Clock monotonic increasing ──
            InvariantDef(
                name="clock_monotonic_increasing",
                description="MonotonicInstant is strictly increasing between checks",
                check=self._check_clock_monotonic,
                severity="critical",
            ),
            # ── Invariant 13: Journal/Ledger row count aligned ──
            InvariantDef(
                name="journal_ledger_aligned",
                description="Journal and ledger record counts within 5% tolerance",
                check=self._check_journal_ledger_consistency,
                severity="warning",
            ),
            # ── Invariant 14: No consecutive cycle failures ──
            InvariantDef(
                name="no_consecutive_cycle_failures",
                description="Consecutive degraded/error cycles < 3",
                check=self._check_cycle_health,
                severity="critical",
            ),
            # ── Invariant 15: Data directory writable ──
            InvariantDef(
                name="data_dir_writable",
                description="Primary data directory exists and is writable",
                check=self._check_data_dir_writable,
                severity="critical",
            ),
        ]

        for inv in defaults:
            self._invariants[inv.name] = inv

    # ═══════════════════════════════════════════════════════════════════
    # Invariant Check Implementations
    # ═══════════════════════════════════════════════════════════════════

    # ── Invariant 1 ──

    def _check_wal_integrity(self, ctx: dict[str, Any]) -> tuple[bool, str]:
        """WAL hash chain is unbroken from genesis to tip."""
        wal = ctx.get("wal", self._wal)
        if wal is None:
            return True, "No WAL configured — skipping"
        try:
            ok, reason = wal.verify_integrity()
            if ok:
                return True, "WAL hash chain intact"
            return False, f"WAL integrity failure: {reason}"
        except (OSError, ValueError, KeyError) as e:
            return False, f"WAL integrity check raised: {e}"

    # ── Invariant 2 ──

    @staticmethod
    def _check_circuit_breaker(ctx: dict[str, Any]) -> tuple[bool, str]:
        """Circuit breaker is not persistently open."""
        cb_state = ctx.get("circuit_breaker_state", "closed")
        if cb_state == "open":
            return False, "Circuit breaker is OPEN — trading may be halted"
        return True, f"Circuit breaker state: {cb_state}"

    # ── Invariant 3 ──

    @staticmethod
    def _check_position_count(ctx: dict[str, Any]) -> tuple[bool, str]:
        """Open position count ≤ configured maximum."""
        open_positions = ctx.get("open_positions", 0)
        max_positions = ctx.get("max_positions", 8)
        if isinstance(open_positions, list | set):
            open_positions = len(open_positions)
        if open_positions > max_positions:
            return False, (f"Position count {open_positions} exceeds max {max_positions}")
        return True, f"Position count {open_positions}/{max_positions} OK"

    # ── Invariant 4 ──

    @staticmethod
    def _check_risk_budget(ctx: dict[str, Any]) -> tuple[bool, str]:
        """Daily PnL has not breached daily loss limit."""
        daily_pnl = ctx.get("daily_pnl", 0.0)
        loss_limit = ctx.get("daily_loss_limit", -500.0)
        try:
            daily_pnl = float(daily_pnl)
            loss_limit = float(loss_limit)
        except (TypeError, ValueError):
            return True, "Risk budget: non-numeric PnL/limit — skipping"
        if daily_pnl < loss_limit:
            return False, (f"Daily PnL {daily_pnl:.2f} breached limit {loss_limit:.2f}")
        return True, f"Daily PnL {daily_pnl:.2f} within limit {loss_limit:.2f}"

    # ── Invariant 5 ──

    @staticmethod
    def _check_duplicate_tickets(ctx: dict[str, Any]) -> tuple[bool, str]:
        """No duplicate position tickets detected in current cycle."""
        dup_count = ctx.get("duplicate_tickets_detected", 0)
        if isinstance(dup_count, int | float) and dup_count > 0:
            return False, f"Detected {int(dup_count)} duplicate position ticket(s)"
        return True, "No duplicate tickets detected"

    # ── Invariant 6 ──

    @staticmethod
    def _check_feature_freshness(ctx: dict[str, Any]) -> tuple[bool, str]:
        """Feature buffer age ≤ freshness SLA (310s)."""
        feature_age = ctx.get("feature_age_seconds", 0.0)
        sla = ctx.get("feature_freshness_sla", 310.0)
        try:
            feature_age = float(feature_age)
        except (TypeError, ValueError):
            return True, "Feature age unavailable — skipping"
        if feature_age > sla:
            return False, (f"Feature data age {feature_age:.0f}s exceeds SLA {sla:.0f}s")
        return True, f"Feature age {feature_age:.0f}s within SLA {sla:.0f}s"

    # ── Invariant 7 ──

    @staticmethod
    def _check_live_brain_count(ctx: dict[str, Any]) -> tuple[bool, str]:
        """At least 1 brain is in LIVE status."""
        live_count = ctx.get("live_brain_count", 0)
        if isinstance(live_count, list | set):
            live_count = len(live_count)
        try:
            live_count = int(live_count)
        except (TypeError, ValueError):
            return True, "Live brain count unavailable — skipping"
        if live_count == 0:
            return False, "ZERO live brains — trading may be blocked"
        return True, f"{live_count} live brain(s) active"

    # ── Invariant 8 ──

    @staticmethod
    def _check_governance_state(ctx: dict[str, Any]) -> tuple[bool, str]:
        """Governance state contains brain_states key."""
        gov = ctx.get("governance_state")
        if gov is None:
            return True, "Governance state not provided — skipping"
        if isinstance(gov, dict) and "brain_states" in gov:
            bs = gov["brain_states"]
            count = len(bs) if isinstance(bs, dict) else 0
            return True, f"Governance state present with {count} brain(s)"
        return False, "Governance state missing 'brain_states' key"

    # ── Invariant 9 ──

    @staticmethod
    def _check_calibrator_health(ctx: dict[str, Any]) -> tuple[bool, str]:
        """Conformal calibrator has p_win variance > 0."""
        p_win_values = ctx.get("calibrator_p_win_values")
        if p_win_values is None:
            return True, "Calibrator p_win data not provided — skipping"
        if not isinstance(p_win_values, list | tuple) or len(p_win_values) < 5:
            return True, "Calibrator: insufficient data for variance check"
        unique = set(round(v, 4) for v in p_win_values)
        if len(unique) <= 1:
            return False, (
                f"Calibrator p_win collapsed: all {len(p_win_values)} entries "
                f"= {list(unique)[0] if unique else '?'}"
            )
        return True, f"Calibrator healthy: {len(unique)} unique p_win values"

    # ── Invariant 10 ──

    @staticmethod
    def _check_alert_queue(ctx: dict[str, Any]) -> tuple[bool, str]:
        """Alert queue depth < 90% of capacity."""
        queue_depth = ctx.get("alert_queue_depth", 0)
        queue_capacity = ctx.get("alert_queue_capacity", 1000)
        try:
            queue_depth = int(queue_depth)
            queue_capacity = int(queue_capacity)
        except (TypeError, ValueError):
            return True, "Alert queue metrics unavailable — skipping"
        pct = (queue_depth / queue_capacity) * 100 if queue_capacity > 0 else 0
        if pct >= 90:
            return False, (f"Alert queue at {pct:.0f}% capacity ({queue_depth}/{queue_capacity})")
        return True, f"Alert queue at {pct:.0f}% ({queue_depth}/{queue_capacity})"

    # ── Invariant 11 ──

    @staticmethod
    def _check_supervisor_health(ctx: dict[str, Any]) -> tuple[bool, str]:
        """SupervisedScheduler supervisor has checked in recently."""
        last_heartbeat = ctx.get("supervisor_last_heartbeat", 0.0)
        max_gap = ctx.get("supervisor_max_heartbeat_gap", 2.0)
        try:
            last_heartbeat = float(last_heartbeat)
        except (TypeError, ValueError):
            return True, "Supervisor heartbeat unavailable — skipping"
        if last_heartbeat <= 0:
            return True, "Supervisor heartbeat not yet recorded — skipping"
        gap = time.monotonic() - last_heartbeat
        if gap > max_gap:
            return False, (
                f"Supervisor heartbeat stale: {gap:.1f}s since last check " f"(max {max_gap:.1f}s)"
            )
        return True, f"Supervisor heartbeat OK: {gap:.1f}s ago"

    # ── Invariant 12 ──

    def _check_clock_monotonic(self, ctx: dict[str, Any]) -> tuple[bool, str]:
        """Monotonic clock is strictly increasing between checks."""
        mono_now = ctx.get("monotonic_now")
        if mono_now is None:
            # Fall back to time.monotonic() for self-check
            mono_now = time.monotonic()
        try:
            mono_now = float(mono_now)
        except (TypeError, ValueError):
            return True, "Monotonic time unavailable — skipping"

        if self._last_monotonic > 0 and mono_now <= self._last_monotonic:
            return False, (f"Monotonic clock violation: {mono_now} ≤ {self._last_monotonic}")
        return True, f"Monotonic clock advancing: {self._last_monotonic} → {mono_now}"

    # ── Invariant 13 ──

    @staticmethod
    def _check_journal_ledger_consistency(ctx: dict[str, Any]) -> tuple[bool, str]:
        """Journal and ledger record counts within 5% tolerance."""
        journal_rows = ctx.get("journal_row_count", 0)
        ledger_rows = ctx.get("ledger_row_count", 0)
        try:
            journal_rows = int(journal_rows)
            ledger_rows = int(ledger_rows)
        except (TypeError, ValueError):
            return True, "Journal/ledger counts unavailable — skipping"
        if journal_rows == 0 and ledger_rows == 0:
            return True, "No journal/ledger data yet"
        if min(journal_rows, ledger_rows) == 0:
            return False, (f"Journal/Ledger mismatch: journal={journal_rows}, ledger={ledger_rows}")
        divergence = abs(journal_rows - ledger_rows) / max(journal_rows, ledger_rows)
        if divergence > 0.05:
            return False, (
                f"Journal/Ledger divergence {divergence:.1%}: "
                f"journal={journal_rows}, ledger={ledger_rows}"
            )
        return True, (
            f"Journal/Ledger aligned: {divergence:.1%} divergence "
            f"(journal={journal_rows}, ledger={ledger_rows})"
        )

    # ── Invariant 14 ──

    @staticmethod
    def _check_cycle_health(ctx: dict[str, Any]) -> tuple[bool, str]:
        """Consecutive degraded/error cycles < 3."""
        consecutive = ctx.get("consecutive_degraded_cycles", 0)
        try:
            consecutive = int(consecutive)
        except (TypeError, ValueError):
            return True, "Cycle health unavailable — skipping"
        if consecutive >= 3:
            return False, f"{consecutive} consecutive degraded cycles — possible spiral"
        return True, f"Cycle health OK: {consecutive} consecutive degraded"

    # ── Invariant 15 ──

    @staticmethod
    def _check_data_dir_writable(ctx: dict[str, Any]) -> tuple[bool, str]:
        """Primary data directory exists and is writable."""
        data_dir = ctx.get("data_dir", "")
        if not data_dir:
            return True, "Data dir not specified — skipping"
        import os

        if not os.path.isdir(str(data_dir)):
            return False, f"Data directory does not exist: {data_dir}"
        if not os.access(str(data_dir), os.W_OK):
            return False, f"Data directory not writable: {data_dir}"
        return True, f"Data directory writable: {data_dir}"
