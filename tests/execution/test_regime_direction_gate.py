"""Tests for core.execution.regime_direction_gate — Phase 3c gap fill.

Covers: RegimeDirectionGate.__init__, _resolve_trend, filter, reset_streaks.
"""

from __future__ import annotations

from core.execution.regime_direction_gate import RegimeDirectionGate


class TestInit:
    def test_default_thresholds(self) -> None:
        g = RegimeDirectionGate()
        assert g._adx_threshold == 25.0
        assert g._stale_warn_cycles == 20
        assert g._long_blocked_streak == 0
        assert g._short_blocked_streak == 0
        assert g._total_cycles == 0

    def test_custom_thresholds(self) -> None:
        g = RegimeDirectionGate(adx_threshold=30.0, stale_warn_cycles=10)
        assert g._adx_threshold == 30.0
        assert g._stale_warn_cycles == 10


class TestResolveTrend:
    def test_ranging_default_with_no_info(self) -> None:
        g = RegimeDirectionGate()
        assert g._resolve_trend({}) == "ranging"

    def test_ranging_with_adx(self) -> None:
        g = RegimeDirectionGate()
        result = g._resolve_trend({"adx": 30, "plus_di": 30, "minus_di": 15})
        assert result == "ranging"  # Feature-Not-Gate: all pass through

    def test_ranging_with_trend_direction(self) -> None:
        g = RegimeDirectionGate()
        result = g._resolve_trend({"trend_direction": "up", "adx": 40})
        assert result == "ranging"

    def test_physics_cold_start_ignored(self) -> None:
        """ou_theta=0.0, hurst=0.5 are cold-start defaults → ignored."""
        g = RegimeDirectionGate()
        result = g._resolve_trend({"ou_theta_m5": 0.0, "hurst_m5": 0.5})
        assert result == "ranging"

    def test_physics_nan_values_skipped(self) -> None:
        g = RegimeDirectionGate()
        result = g._resolve_trend({"ou_theta_m5": float("nan"), "hurst_m5": float("nan")})
        assert result == "ranging"

    def test_physics_out_of_range_skipped(self) -> None:
        g = RegimeDirectionGate()
        result = g._resolve_trend({"ou_theta_m5": -1.0, "hurst_m5": 2.0})
        assert result == "ranging"

    def test_adx_logged_but_not_blocking(self) -> None:
        """ADX is logged for audit but never blocks."""
        g = RegimeDirectionGate()
        # Should return "ranging" even with high ADX
        result = g._resolve_trend({"adx": 50, "plus_di": 40, "minus_di": 10})
        assert result == "ranging"


class TestFilter:
    def test_all_signals_pass_through(self) -> None:
        g = RegimeDirectionGate()
        signals = [
            {"brain_id": "brain_1", "direction": "long"},
            {"brain_id": "brain_2", "direction": "short"},
            {"brain_id": "brain_3", "direction": "long"},
        ]
        passed, audit = g.filter(signals, {"adx": 40, "trend_direction": "up"})
        assert len(passed) == 3
        assert audit["total_signals_in"] == 3
        assert audit["passed"] == 3
        assert audit["gate"] == "RegimeDirectionGate"

    def test_empty_signals(self) -> None:
        g = RegimeDirectionGate()
        passed, audit = g.filter([], {})
        assert passed == []
        assert audit["total_signals_in"] == 0

    def test_feature_not_gate_no_blocking(self) -> None:
        """Feature-Not-Gate: All signals pass through regardless of trend.
        Blocking counters are recorded for audit only."""
        g = RegimeDirectionGate()
        signals = [
            {"brain_id": "brain_short", "direction": "short"},
        ]
        passed, audit = g.filter(signals, {"adx": 40, "trend_direction": "up"})
        assert len(passed) == 1  # always passes through
        # _resolve_trend returns "ranging" → no blocking counters incremented
        assert audit["trend"] == "ranging"

    def test_cycle_counter_increments(self) -> None:
        g = RegimeDirectionGate()
        g.filter([], {})
        g.filter([], {})
        assert g._total_cycles == 2

    def test_streak_stays_zero_on_ranging(self) -> None:
        """In ranging (Feature-Not-Gate), no streaks accumulate."""
        g = RegimeDirectionGate()
        for _ in range(3):
            g.filter([{"brain_id": "b", "direction": "short"}], {"trend_direction": "up"})
        assert g._short_blocked_streak == 0  # ranging → never blocked

    def test_audit_contains_physics_state(self) -> None:
        g = RegimeDirectionGate()
        _, audit = g.filter([], {"ou_theta_m5": 0.0, "hurst_m5": 0.5})
        assert "physics_calibration" in audit
        assert audit["physics_calibration"]["active"] is False


class TestResetStreaks:
    def test_resets_all_streaks(self) -> None:
        g = RegimeDirectionGate()
        g._long_blocked_streak = 5
        g._short_blocked_streak = 3
        g.reset_streaks()
        assert g._long_blocked_streak == 0
        assert g._short_blocked_streak == 0
