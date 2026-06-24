"""Tests for InvariantEngine — UGR v3.1 15 binary invariant checker.

Covers:
- Default 15 invariants registered
- Each invariant: passing and failing scenarios
- Shadow mode: invariant failure never raises
- check_all_and_alert routes to alert hub
- Registration, enable, disable
- get_status reflects engine state
"""

from __future__ import annotations

import tempfile
import time
from unittest.mock import MagicMock

import pytest

from core.data.write_ahead_log import WALConfig, WriteAheadLog
from core.observability.invariant_engine import (
    InvariantDef,
    InvariantEngine,
    InvariantViolation,
)


@pytest.fixture
def engine():
    """Create a fresh InvariantEngine with no WAL or alert hub."""
    return InvariantEngine(wal=None, alert_hub=None)


@pytest.fixture
def engine_with_wal(tmp_path):
    """Create an InvariantEngine with a real WAL."""
    wal_path = tmp_path / "test_wal.jsonl"
    wal_config = WALConfig(path=wal_path)
    wal = WriteAheadLog(wal_config)
    wal.append({"event": "test"})
    return InvariantEngine(wal=wal, alert_hub=None), wal


# ═══════════════════════════════════════════════════════════════════════════
# Registration
# ═══════════════════════════════════════════════════════════════════════════


class TestRegistration:
    """Tests for invariant registration and lifecycle."""

    def test_all_15_defaults_registered(self, engine: InvariantEngine) -> None:
        """All 15 default invariants are registered at init."""
        assert len(engine._invariants) == 15
        expected = {
            "wal_hash_chain_intact",
            "circuit_breaker_not_open",
            "position_count_bounded",
            "risk_budget_positive",
            "no_duplicate_tickets",
            "feature_age_within_sla",
            "live_brain_count_positive",
            "governance_state_present",
            "calibrator_variance_nonzero",
            "alert_queue_pressure_ok",
            "supervisor_heartbeat_recent",
            "clock_monotonic_increasing",
            "journal_ledger_aligned",
            "no_consecutive_cycle_failures",
            "data_dir_writable",
        }
        assert set(engine._invariants.keys()) == expected

    def test_register_custom_invariant(self, engine: InvariantEngine) -> None:
        """Additional invariants can be registered."""
        inv = InvariantDef(
            name="custom_check",
            description="A custom invariant",
            check=lambda ctx: (True, "ok"),
        )
        engine.register(inv)
        assert "custom_check" in engine._invariants
        assert len(engine._invariants) == 16

    def test_disable_and_enable(self, engine: InvariantEngine) -> None:
        """Invariants can be disabled and re-enabled."""
        engine.disable("wal_hash_chain_intact")
        assert not engine._invariants["wal_hash_chain_intact"].enabled

        engine.enable("wal_hash_chain_intact")
        assert engine._invariants["wal_hash_chain_intact"].enabled

    def test_disabled_invariant_not_checked(self, engine: InvariantEngine) -> None:
        """Disabled invariants are skipped in check_all()."""
        engine.disable("wal_hash_chain_intact")
        violations = engine.check_all({})
        # wal_hash_chain_intact would normally fail without WAL, but it's disabled
        wal_violations = [v for v in violations if v.invariant == "wal_hash_chain_intact"]
        assert len(wal_violations) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Individual Invariant Checks
# ═══════════════════════════════════════════════════════════════════════════


class TestInvariantWAL:
    """Invariant 1: WAL hash chain intact."""

    def test_passes_with_wal(self, engine_with_wal: tuple[InvariantEngine, WriteAheadLog]) -> None:
        engine, wal = engine_with_wal
        violations = engine.check_all({"wal": wal})
        wal_v = [v for v in violations if v.invariant == "wal_hash_chain_intact"]
        assert len(wal_v) == 0

    def test_skips_when_no_wal(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({})
        wal_v = [v for v in violations if v.invariant == "wal_hash_chain_intact"]
        assert len(wal_v) == 0  # Skips gracefully


class TestInvariantCircuitBreaker:
    """Invariant 2: Circuit breaker not persistently open."""

    def test_passes_closed(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"circuit_breaker_state": "closed"})
        cb_v = [v for v in violations if v.invariant == "circuit_breaker_not_open"]
        assert len(cb_v) == 0

    def test_passes_half_open(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"circuit_breaker_state": "half_open"})
        cb_v = [v for v in violations if v.invariant == "circuit_breaker_not_open"]
        assert len(cb_v) == 0

    def test_fails_open(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"circuit_breaker_state": "open"})
        cb_v = [v for v in violations if v.invariant == "circuit_breaker_not_open"]
        assert len(cb_v) == 1
        assert "OPEN" in cb_v[0].detail


class TestInvariantPositionCount:
    """Invariant 3: Position count within limits."""

    def test_passes_within_limit(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"open_positions": 3, "max_positions": 8})
        pc_v = [v for v in violations if v.invariant == "position_count_bounded"]
        assert len(pc_v) == 0

    def test_fails_exceeds_limit(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"open_positions": 10, "max_positions": 8})
        pc_v = [v for v in violations if v.invariant == "position_count_bounded"]
        assert len(pc_v) == 1
        assert "10" in pc_v[0].detail


class TestInvariantRiskBudget:
    """Invariant 4: Risk budget non-negative."""

    def test_passes_within_limit(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"daily_pnl": -200.0, "daily_loss_limit": -500.0})
        rb_v = [v for v in violations if v.invariant == "risk_budget_positive"]
        assert len(rb_v) == 0

    def test_fails_breached(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"daily_pnl": -600.0, "daily_loss_limit": -500.0})
        rb_v = [v for v in violations if v.invariant == "risk_budget_positive"]
        assert len(rb_v) == 1
        assert "breached" in rb_v[0].detail


class TestInvariantDuplicateTickets:
    """Invariant 5: No duplicate position tickets."""

    def test_passes_zero_dupes(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"duplicate_tickets_detected": 0})
        dt_v = [v for v in violations if v.invariant == "no_duplicate_tickets"]
        assert len(dt_v) == 0

    def test_fails_with_dupes(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"duplicate_tickets_detected": 3})
        dt_v = [v for v in violations if v.invariant == "no_duplicate_tickets"]
        assert len(dt_v) == 1
        assert "3" in dt_v[0].detail


class TestInvariantFeatureFreshness:
    """Invariant 6: Feature data within freshness SLA."""

    def test_passes_fresh(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"feature_age_seconds": 120.0})
        ff_v = [v for v in violations if v.invariant == "feature_age_within_sla"]
        assert len(ff_v) == 0

    def test_fails_stale(self, engine: InvariantEngine) -> None:
        violations = engine.check_all(
            {"feature_age_seconds": 500.0, "feature_freshness_sla": 310.0}
        )
        ff_v = [v for v in violations if v.invariant == "feature_age_within_sla"]
        assert len(ff_v) == 1
        assert "exceeds SLA" in ff_v[0].detail


class TestInvariantLiveBrainCount:
    """Invariant 7: At least one live brain."""

    def test_passes_with_brains(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"live_brain_count": 3})
        lb_v = [v for v in violations if v.invariant == "live_brain_count_positive"]
        assert len(lb_v) == 0

    def test_fails_zero_brains(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"live_brain_count": 0})
        lb_v = [v for v in violations if v.invariant == "live_brain_count_positive"]
        assert len(lb_v) == 1
        assert "ZERO" in lb_v[0].detail


class TestInvariantGovernanceState:
    """Invariant 8: Governance state present."""

    def test_passes_with_brain_states(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"governance_state": {"brain_states": {"V1": {}, "V2": {}}}})
        gs_v = [v for v in violations if v.invariant == "governance_state_present"]
        assert len(gs_v) == 0

    def test_fails_missing_key(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"governance_state": {"schema_version": 1}})
        gs_v = [v for v in violations if v.invariant == "governance_state_present"]
        assert len(gs_v) == 1
        assert "brain_states" in gs_v[0].detail


class TestInvariantCalibrator:
    """Invariant 9: Calibrator not contaminated."""

    def test_passes_variance(self, engine: InvariantEngine) -> None:
        violations = engine.check_all(
            {"calibrator_p_win_values": [0.45, 0.52, 0.48, 0.51, 0.49, 0.55]}
        )
        cal_v = [v for v in violations if v.invariant == "calibrator_variance_nonzero"]
        assert len(cal_v) == 0

    def test_fails_collapsed(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"calibrator_p_win_values": [0.5, 0.5, 0.5, 0.5, 0.5, 0.5]})
        cal_v = [v for v in violations if v.invariant == "calibrator_variance_nonzero"]
        assert len(cal_v) == 1
        assert "collapsed" in cal_v[0].detail


class TestInvariantAlertQueue:
    """Invariant 10: Alert queue pressure OK."""

    def test_passes_low_pressure(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"alert_queue_depth": 100, "alert_queue_capacity": 1000})
        aq_v = [v for v in violations if v.invariant == "alert_queue_pressure_ok"]
        assert len(aq_v) == 0

    def test_fails_high_pressure(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"alert_queue_depth": 950, "alert_queue_capacity": 1000})
        aq_v = [v for v in violations if v.invariant == "alert_queue_pressure_ok"]
        assert len(aq_v) == 1
        assert "95%" in aq_v[0].detail


class TestInvariantSupervisor:
    """Invariant 11: Supervisor heartbeat recent."""

    def test_passes_recent(self, engine: InvariantEngine) -> None:
        violations = engine.check_all(
            {
                "supervisor_last_heartbeat": time.monotonic() - 0.1,
                "supervisor_max_heartbeat_gap": 2.0,
            }
        )
        sv_v = [v for v in violations if v.invariant == "supervisor_heartbeat_recent"]
        assert len(sv_v) == 0

    def test_fails_stale(self, engine: InvariantEngine) -> None:
        violations = engine.check_all(
            {
                "supervisor_last_heartbeat": time.monotonic() - 10.0,
                "supervisor_max_heartbeat_gap": 2.0,
            }
        )
        sv_v = [v for v in violations if v.invariant == "supervisor_heartbeat_recent"]
        assert len(sv_v) == 1
        assert "stale" in sv_v[0].detail


class TestInvariantClockMonotonic:
    """Invariant 12: Clock monotonic increasing."""

    def test_passes_increasing(self, engine: InvariantEngine) -> None:
        now = time.monotonic()
        engine._last_monotonic = now - 1.0
        violations = engine.check_all({"monotonic_now": now})
        cm_v = [v for v in violations if v.invariant == "clock_monotonic_increasing"]
        assert len(cm_v) == 0

    def test_fails_not_increasing(self, engine: InvariantEngine) -> None:
        engine._last_monotonic = 1000.0
        violations = engine.check_all({"monotonic_now": 999.0})
        cm_v = [v for v in violations if v.invariant == "clock_monotonic_increasing"]
        assert len(cm_v) == 1
        assert "violation" in cm_v[0].detail.lower()


class TestInvariantJournalLedger:
    """Invariant 13: Journal/Ledger row count aligned."""

    def test_passes_aligned(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"journal_row_count": 100, "ledger_row_count": 102})
        jl_v = [v for v in violations if v.invariant == "journal_ledger_aligned"]
        assert len(jl_v) == 0

    def test_fails_divergent(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"journal_row_count": 100, "ledger_row_count": 50})
        jl_v = [v for v in violations if v.invariant == "journal_ledger_aligned"]
        assert len(jl_v) == 1
        assert "divergence" in jl_v[0].detail.lower()


class TestInvariantCycleHealth:
    """Invariant 14: No consecutive cycle failures."""

    def test_passes_low_degraded(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"consecutive_degraded_cycles": 1})
        ch_v = [v for v in violations if v.invariant == "no_consecutive_cycle_failures"]
        assert len(ch_v) == 0

    def test_fails_spiral(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"consecutive_degraded_cycles": 5})
        ch_v = [v for v in violations if v.invariant == "no_consecutive_cycle_failures"]
        assert len(ch_v) == 1
        assert "spiral" in ch_v[0].detail


class TestInvariantDataDir:
    """Invariant 15: Data directory writable."""

    def test_passes_writable(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"data_dir": str(tempfile.gettempdir())})
        dd_v = [v for v in violations if v.invariant == "data_dir_writable"]
        assert len(dd_v) == 0

    def test_fails_nonexistent(self, engine: InvariantEngine) -> None:
        violations = engine.check_all({"data_dir": "/nonexistent/path/xyz"})
        dd_v = [v for v in violations if v.invariant == "data_dir_writable"]
        assert len(dd_v) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Shadow Mode Behavior
# ═══════════════════════════════════════════════════════════════════════════


class TestShadowMode:
    """Tests for shadow mode: invariant failures NEVER raise."""

    def test_invariant_exception_does_not_propagate(self, engine: InvariantEngine) -> None:
        """If an invariant raises, it's caught and reported as violation."""
        engine.register(
            InvariantDef(
                name="explosive",
                description="Always raises",
                check=lambda ctx: (_ for _ in ()).throw(RuntimeError("boom")),
            )
        )
        # Must not raise
        violations = engine.check_all({})
        explosive_v = [v for v in violations if v.invariant == "explosive"]
        assert len(explosive_v) == 1

    def test_empty_context_does_not_crash(self, engine: InvariantEngine) -> None:
        """check_all({}) with empty context must not raise."""
        violations = engine.check_all({})
        assert isinstance(violations, list)

    def test_none_context_values_handled(self, engine: InvariantEngine) -> None:
        """None values in context are handled gracefully."""
        violations = engine.check_all(
            {
                "open_positions": None,
                "daily_pnl": None,
                "circuit_breaker_state": None,
            }
        )
        assert isinstance(violations, list)


# ═══════════════════════════════════════════════════════════════════════════
# Alert Integration
# ═══════════════════════════════════════════════════════════════════════════


class TestAlertIntegration:
    """Tests for check_all_and_alert."""

    def test_violations_routed_to_alert_hub(self) -> None:
        """Violations are sent to alert_hub.send_critical()."""
        mock_hub = MagicMock()
        engine = InvariantEngine(wal=None, alert_hub=mock_hub)

        # Trigger a violation
        violations = engine.check_all_and_alert(
            {
                "live_brain_count": 0,
                "circuit_breaker_state": "open",
            }
        )

        assert len(violations) >= 2
        # alert_hub.send_critical should have been called for each violation
        assert mock_hub.send_critical.call_count >= 2

    def test_alert_hub_none_is_safe(self, engine: InvariantEngine) -> None:
        """check_all_and_alert with no alert hub is safe."""
        violations = engine.check_all_and_alert({"live_brain_count": 0})
        assert len(violations) >= 1

    def test_alert_hub_exception_does_not_propagate(self) -> None:
        """If alert_hub.send_critical raises, it's caught."""
        mock_hub = MagicMock()
        mock_hub.send_critical.side_effect = RuntimeError("alert failed")
        engine = InvariantEngine(wal=None, alert_hub=mock_hub)

        # Must not raise
        violations = engine.check_all_and_alert({"live_brain_count": 0})
        assert len(violations) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# Status
# ═══════════════════════════════════════════════════════════════════════════


class TestStatus:
    """Tests for get_status()."""

    def test_initial_status(self, engine: InvariantEngine) -> None:
        status = engine.get_status()
        assert status["invariants_registered"] == 15
        assert status["invariants_enabled"] == 15
        assert status["checks_run"] == 0
        assert status["violations_total"] == 0

    def test_status_after_checks(self, engine: InvariantEngine) -> None:
        engine.check_all({"live_brain_count": 0, "circuit_breaker_state": "open"})
        status = engine.get_status()
        assert status["checks_run"] == 1
        assert status["violations_total"] >= 2
        assert status["last_violation_count"] >= 2

    def test_status_reflects_disabled(self, engine: InvariantEngine) -> None:
        engine.disable("wal_hash_chain_intact")
        status = engine.get_status()
        assert status["invariants_enabled"] == 14


# ═══════════════════════════════════════════════════════════════════════════
# Violation Dataclass
# ═══════════════════════════════════════════════════════════════════════════


class TestInvariantViolation:
    """Tests for InvariantViolation dataclass."""

    def test_creation(self) -> None:
        v = InvariantViolation(
            invariant="test_check",
            detail="Something went wrong",
            severity="critical",
        )
        assert v.invariant == "test_check"
        assert v.detail == "Something went wrong"
        assert v.severity == "critical"
        assert v.timestamp_wall > 0
