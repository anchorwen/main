"""Tests for the progressive degradation model.

FIX-20260611-022: Four-level degradation decision tree tests.
"""

from __future__ import annotations

import pytest

from core.observability.degradation import (
    DegradationConstraints,
    DegradationLevel,
    apply_degradation_to_decision,
    evaluate_degradation,
    evaluate_staleness,
)


def _health_report(
    critical_fails: list[str] | None = None,
    critical_warns: list[str] | None = None,
    other_fails: list[str] | None = None,
    cross_fails: list[str] | None = None,
) -> dict:
    """Build a health report dict for testing."""
    sources = {}
    for name in (critical_fails or []):
        sources[name] = {"status": "fail", "tier": "critical"}
    for name in (critical_warns or []):
        sources[name] = {"status": "warn", "tier": "critical"}
    for name in (other_fails or []):
        sources[name] = {"status": "fail", "tier": "info"}

    cross_checks = []
    for name in (cross_fails or []):
        cross_checks.append({"name": name, "status": "fail"})

    return {"sources": sources, "cross_checks": cross_checks}


class TestDegradationLevels:
    def test_normal_when_all_healthy(self):
        report = _health_report()
        result = evaluate_degradation(report)
        assert result.level == DegradationLevel.NORMAL
        assert result.max_position_size_pct == 1.0
        assert result.allow_new_positions is True

    def test_yellow_single_critical_fail(self):
        report = _health_report(critical_fails=["feature_store"])
        result = evaluate_degradation(report)
        assert result.level == DegradationLevel.YELLOW
        assert result.max_position_size_pct == 0.40
        assert result.allow_new_positions is True

    def test_yellow_three_warnings(self):
        report = _health_report(critical_warns=["a", "b", "c"])
        result = evaluate_degradation(report)
        assert result.level == DegradationLevel.YELLOW

    def test_yellow_non_critical_fails(self):
        report = _health_report(other_fails=["alpha_allocation"])
        result = evaluate_degradation(report)
        assert result.level == DegradationLevel.YELLOW

    def test_orange_two_critical_fails(self):
        # Use non-core-safety checks to avoid triggering RED
        report = _health_report(critical_fails=["feature_store", "brain_performance"])
        result = evaluate_degradation(report)
        assert result.level == DegradationLevel.ORANGE
        assert result.max_position_size_pct == 0.15
        assert result.allow_new_positions is False

    def test_orange_fail_plus_two_warnings(self):
        report = _health_report(
            critical_fails=["feature_store"], critical_warns=["brain_performance", "calibrator_feed"]
        )
        result = evaluate_degradation(report)
        assert result.level == DegradationLevel.ORANGE, f"Got {result.level}: {result.reason}"

    def test_red_core_safety_fail(self):
        report = _health_report(critical_fails=["execution_state"])
        result = evaluate_degradation(report)
        assert result.level == DegradationLevel.RED
        assert result.max_position_size_pct == 0.0
        assert result.allow_new_positions is False

    def test_red_cross_source_mismatch(self):
        report = _health_report(cross_fails=["journal_vs_pnl_ledger"])
        result = evaluate_degradation(report)
        assert result.level == DegradationLevel.RED
        assert "journal_vs_pnl_ledger" in result.reason

    def test_red_overrides_orange(self):
        """RED (core safety) takes priority over ORANGE conditions."""
        report = _health_report(
            critical_fails=["execution_state", "bar_sync_state", "feature_store"],
            cross_fails=["journal_vs_pnl_ledger"],
        )
        result = evaluate_degradation(report)
        assert result.level == DegradationLevel.RED


class TestStalenessDegradation:
    def test_none_when_all_fresh(self):
        sources = {
            "bar_sync_state": {"age_minutes": 1.0},
            "execution_state": {"age_minutes": 2.0},
            "golden_master": {"age_minutes": 3.0},
        }
        result = evaluate_staleness(sources, stale_threshold_min=5.0)
        assert result is None

    def test_yellow_single_stale(self):
        sources = {
            "bar_sync_state": {"age_minutes": 6.0},
            "execution_state": {"age_minutes": 2.0},
            "golden_master": {"age_minutes": 3.0},
        }
        result = evaluate_staleness(sources, stale_threshold_min=5.0)
        assert result == DegradationLevel.YELLOW

    def test_orange_critical_single(self):
        sources = {
            "bar_sync_state": {"age_minutes": 12.0},
            "execution_state": {"age_minutes": 2.0},
            "golden_master": {"age_minutes": 3.0},
        }
        result = evaluate_staleness(sources, stale_threshold_min=5.0, critical_threshold_min=10.0)
        assert result == DegradationLevel.ORANGE

    def test_red_two_critical(self):
        sources = {
            "bar_sync_state": {"age_minutes": 12.0},
            "execution_state": {"age_minutes": 12.0},
            "golden_master": {"age_minutes": 3.0},
        }
        result = evaluate_staleness(sources, stale_threshold_min=5.0, critical_threshold_min=10.0)
        assert result == DegradationLevel.RED


class TestApplyDegradation:
    def test_normal_passes_through(self):
        c = DegradationConstraints.for_level(DegradationLevel.NORMAL)
        vol, trade, reason = apply_degradation_to_decision(c, 1.0, True)
        assert vol == 1.0
        assert trade is True
        assert reason == ""

    def test_yellow_reduces_size(self):
        c = DegradationConstraints.for_level(DegradationLevel.YELLOW)
        vol, trade, reason = apply_degradation_to_decision(c, 1.0, True)
        assert vol == 0.4
        assert trade is True
        assert "YELLOW" in reason

    def test_orange_blocks_new(self):
        c = DegradationConstraints.for_level(DegradationLevel.ORANGE)
        vol, trade, reason = apply_degradation_to_decision(c, 1.0, True)
        assert vol == 0.0
        assert trade is False
        assert "ORANGE" in reason

    def test_red_blocks_all(self):
        c = DegradationConstraints.for_level(DegradationLevel.RED)
        vol, trade, reason = apply_degradation_to_decision(c, 1.0, True)
        assert vol == 0.0
        assert trade is False
        assert "RED" in reason

    def test_level_comparison(self):
        """DegradationLevel is an IntEnum, so comparisons work."""
        assert DegradationLevel.RED > DegradationLevel.ORANGE
        assert DegradationLevel.ORANGE > DegradationLevel.YELLOW
        assert DegradationLevel.YELLOW > DegradationLevel.NORMAL
