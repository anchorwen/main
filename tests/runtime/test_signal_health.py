"""Tests for core.runtime.signal_health — pre-inference feature gates.

FIX-20260619-030: Tier 1 zero-coverage breakout #2.
Covers FeatureGate, _RollingStats, SignalHealthMonitor, and run_signal_health_checks.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import numpy as np
import pytest

from core.runtime.signal_health import (
    FeatureGate,
    GateResult,
    SignalHealthMonitor,
    _RollingStats,
    run_signal_health_checks,
)


class TestGateResult:
    def test_passed_true(self) -> None:
        assert GateResult(True, "OK", "all good")

    def test_passed_false(self) -> None:
        assert not GateResult(False, "FAIL", "bad")

    def test_bool_conversion(self) -> None:
        assert bool(GateResult(True)) is True
        assert bool(GateResult(False)) is False


class TestFeatureGate:
    def test_all_checks_pass_with_valid_data(self) -> None:
        fv = np.ones(40, dtype=np.float64)
        result = FeatureGate.check(feature_vector=fv, atr=6.0, mid_price=4700.0)
        assert result.passed
        assert result.reason_code == ""

    def test_nan_detection_above_threshold(self) -> None:
        fv = np.zeros(40, dtype=np.float64)
        fv[:6] = float("nan")
        result = FeatureGate.check(feature_vector=fv)
        assert not result.passed
        assert result.reason_code == "FEATURE_NAN"

    def test_nan_below_threshold_passes(self) -> None:
        fv = np.ones(40, dtype=np.float64)
        fv[:5] = float("nan")
        result = FeatureGate.check(feature_vector=fv, atr=1.0, mid_price=1.0)
        # 5 NaN <= 5 threshold — passes NaN check, hits the zero check
        # Since fv has 5 NaN + 35 ones = non-zero, should pass
        assert result.passed

    def test_zero_vector_detection(self) -> None:
        fv = np.zeros(40, dtype=np.float64)
        result = FeatureGate.check(feature_vector=fv)
        assert not result.passed
        assert result.reason_code == "FEATURE_ZERO_VECTOR"

    def test_atr_zero_or_negative(self) -> None:
        result = FeatureGate.check(atr=0.0, mid_price=100.0)
        assert not result.passed
        assert result.reason_code == "FEATURE_STALE"

    def test_mid_price_zero_or_negative(self) -> None:
        result = FeatureGate.check(atr=1.0, mid_price=0.0)
        assert not result.passed
        assert result.reason_code == "FEATURE_STALE"

    def test_micro_all_zeros_returns_cold_start(self) -> None:
        mv = np.zeros(9, dtype=np.float64)
        result = FeatureGate.check(feature_vector=np.ones(40), micro_vector=mv, atr=1.0, mid_price=1.0)
        assert not result.passed
        assert result.reason_code == "FEATURE_COLD_START"

    def test_micro_with_data_passes(self) -> None:
        mv = np.array([1.0] * 9, dtype=np.float64)
        result = FeatureGate.check(feature_vector=np.ones(40), micro_vector=mv, atr=1.0, mid_price=1.0)
        assert result.passed

    def test_no_feature_vector_passes_sanity_checks(self) -> None:
        result = FeatureGate.check(atr=1.0, mid_price=1.0)
        assert result.passed

    def test_inf_values_pass_nan_gate(self) -> None:
        """Inf is not NaN — passes feature gate, caught downstream by brain adapters."""
        fv = np.ones(40, dtype=np.float64)
        fv[:6] = float("inf")
        result = FeatureGate.check(feature_vector=fv, atr=1.0, mid_price=4700.0)
        # inf != NaN, nan_count stays 0. zero_count=0 (34 ones + 6 inf).
        # All checks pass — the gate doesn't block inf (brain adapter catches it).
        assert result.passed

    def test_exception_in_validation_safe_fallback(self) -> None:
        """Any exception in feature validation returns safe FAIL."""
        # Create an object that will raise during iteration
        class BadVector:
            @property
            def flat(self):
                raise RuntimeError("boom")
            def ravel(self):
                return self
        result = FeatureGate.check(feature_vector=BadVector())
        assert not result.passed
        assert result.reason_code == "FEATURE_ZERO_VECTOR"


class TestRollingStats:
    def test_empty_buffer_mean_zero(self) -> None:
        rs = _RollingStats(maxlen=10)
        assert rs.mean() == 0.0

    def test_empty_buffer_std_zero(self) -> None:
        rs = _RollingStats(maxlen=10)
        assert rs.std() == 0.0

    def test_empty_buffer_percentile_zero(self) -> None:
        rs = _RollingStats(maxlen=10)
        assert rs.percentile(50) == 0.0

    def test_single_value_mean(self) -> None:
        rs = _RollingStats(maxlen=10)
        rs.push(5.0)
        assert rs.mean() == 5.0

    def test_single_value_std_zero(self) -> None:
        rs = _RollingStats(maxlen=10)
        rs.push(5.0)
        assert rs.std() == 0.0

    def test_multiple_values_mean(self) -> None:
        rs = _RollingStats(maxlen=10)
        for v in [1.0, 2.0, 3.0]:
            rs.push(v)
        assert rs.mean() == pytest.approx(2.0)

    def test_percentile_median(self) -> None:
        rs = _RollingStats(maxlen=10)
        for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
            rs.push(v)
        assert rs.percentile(50) == pytest.approx(3.0)

    def test_percentile_extremes(self) -> None:
        rs = _RollingStats(maxlen=10)
        for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
            rs.push(v)
        assert rs.percentile(0) == pytest.approx(1.0)
        assert rs.percentile(100) == pytest.approx(5.0)

    def test_iqr(self) -> None:
        rs = _RollingStats(maxlen=10)
        for v in range(1, 6):
            rs.push(float(v))
        q25, q50, q75 = rs.iqr()
        assert q50 == pytest.approx(3.0)
        assert q25 < q50 < q75

    def test_overflow_evicts_oldest(self) -> None:
        rs = _RollingStats(maxlen=3)
        rs.push(1.0)
        rs.push(2.0)
        rs.push(3.0)
        rs.push(100.0)
        assert rs.count == 3
        assert rs.mean() == pytest.approx((2.0 + 3.0 + 100.0) / 3.0)

    def test_count(self) -> None:
        rs = _RollingStats(maxlen=5)
        assert rs.count == 0
        rs.push(1.0)
        assert rs.count == 1


class TestSignalHealthMonitor:
    def make_monitor(self) -> SignalHealthMonitor:
        return SignalHealthMonitor(
            atr_history_len=200,
            spread_history_len=50,
            prediction_history_len=100,
        )

    # ── check_data_freshness ──

    def test_freshness_no_data_yet(self) -> None:
        m = self.make_monitor()
        r = m.check_data_freshness()
        assert not r["warning"]
        assert r["reason"] == "no_data_yet"

    def test_freshness_within_limit(self) -> None:
        m = self.make_monitor()
        m.mark_feature_received(datetime.now(UTC) - timedelta(seconds=30))
        r = m.check_data_freshness()
        assert not r["warning"]

    def test_freshness_stale(self) -> None:
        m = self.make_monitor()
        m.mark_feature_received(datetime.now(UTC) - timedelta(seconds=300))
        r = m.check_data_freshness()
        assert r["warning"]
        assert "stale_data" in r["reason"]

    # ── check_atr_anomaly ──

    def test_atr_no_atr_provided(self) -> None:
        m = self.make_monitor()
        r = m.check_atr_anomaly(None)
        assert not r["warning"]
        assert r["reason"] == "no_atr_provided"

    def test_atr_baseline_building(self) -> None:
        m = self.make_monitor()
        r = m.check_atr_anomaly(6.0)
        assert not r["warning"]
        assert r["reason"] == "baseline_building"

    def test_atr_within_range(self) -> None:
        m = self.make_monitor()
        for v in [5.0, 6.0, 7.0] * 20:  # 60 samples > 30 threshold
            m.feed_atr(v)
        r = m.check_atr_anomaly(6.0)
        assert not r["warning"]

    def test_atr_outlier_detected(self) -> None:
        m = self.make_monitor()
        # Feed varied values to produce non-zero IQR
        for v in [5.0, 5.5, 6.0, 6.5, 7.0] * 12:  # 60 samples
            m.feed_atr(v)
        # Feed one more to reach the check (check_atr_anomaly also feeds)
        r = m.check_atr_anomaly(50.0)  # far outlier
        assert r["warning"]
        assert r["reason"] == "atr_outlier"

    # ── check_prediction_drift ──

    def test_drift_insufficient_samples(self) -> None:
        m = self.make_monitor()
        r = m.check_prediction_drift()
        assert not r["warning"]
        assert r["reason"] == "insufficient_samples"

    def test_drift_no_shift(self) -> None:
        m = self.make_monitor()
        for _ in range(80):
            m.feed_prediction(0.5, 0.4, 0.6)
        r = m.check_prediction_drift()
        assert not r["warning"]

    def test_drift_confidence_collapse(self) -> None:
        m = self.make_monitor()
        for _ in range(80):
            m.feed_prediction(0.5, 0.4, 0.1)
        r = m.check_prediction_drift()
        assert r["warning"]
        assert "confidence_collapse" in r["reason"]

    # ── check_spread_expansion ──

    def test_spread_no_spread_provided(self) -> None:
        m = self.make_monitor()
        r = m.check_spread_expansion(None)
        assert not r["warning"]

    def test_spread_baseline_building(self) -> None:
        m = self.make_monitor()
        r = m.check_spread_expansion(0.001)
        assert not r["warning"]
        assert r["reason"] == "baseline_building"

    def test_spread_normal(self) -> None:
        m = self.make_monitor()
        for _ in range(30):
            m.feed_spread(0.001)
        r = m.check_spread_expansion(0.001)
        assert not r["warning"]

    def test_spread_anomaly(self) -> None:
        m = self.make_monitor()
        # Feed varied values to produce non-zero IQR
        for v in [0.0005, 0.001, 0.0015] * 10:  # 30 samples
            m.feed_spread(v)
        r = m.check_spread_expansion(0.05)  # extreme outlier
        assert r["warning"]
        assert r["reason"] == "spread_expansion"

    # ── check_all ──

    def test_check_all_healthy_when_no_issues(self) -> None:
        m = self.make_monitor()
        m.mark_feature_received()
        for _ in range(60):
            m.feed_atr(6.0)
            m.feed_spread(0.001)
        for _ in range(80):
            m.feed_prediction(0.5, 0.4, 0.6)
        r = m.check_all(current_atr=6.0, current_spread_pct=0.001)
        assert r["healthy"]
        assert r["warnings"] == 0

    def test_check_all_detects_stale_data(self) -> None:
        m = self.make_monitor()
        m.mark_feature_received(datetime.now(UTC) - timedelta(seconds=300))
        r = m.check_all()
        assert not r["healthy"]

    # ── _derive_actions ──

    def test_derive_stale_data_action(self) -> None:
        m = self.make_monitor()
        actions = m._derive_actions({"data_freshness": {"warning": True, "age_seconds": 300}})
        assert any(a["action"] == "skip_new_positions" for a in actions)

    def test_derive_atr_outlier_action(self) -> None:
        m = self.make_monitor()
        actions = m._derive_actions({"atr_anomaly": {"warning": True, "z_score": 5.5}})
        assert any(a["action"] == "reduce_new_position_sizes" for a in actions)

    def test_derive_spread_action(self) -> None:
        m = self.make_monitor()
        actions = m._derive_actions({
            "spread_expansion": {
                "warning": True,
                "current_spread_pct": 0.05,
                "spread_median_pct": 0.001,
            }
        })
        assert any(a["action"] == "reduce_new_position_sizes" for a in actions)

    def test_dedup_keep_most_restrictive(self) -> None:
        m = self.make_monitor()
        checks = {
            "atr_anomaly": {"warning": True, "z_score": 4.5},
            "spread_expansion": {
                "warning": True,
                "current_spread_pct": 0.006,
                "spread_median_pct": 0.001,
            },
        }
        actions = m._derive_actions(checks)
        reduce = [a for a in actions if a["action"] == "reduce_new_position_sizes"]
        assert len(reduce) <= 1

    # ── as_summary ──

    def test_summary_new_monitor(self) -> None:
        m = self.make_monitor()
        s = m.as_summary()
        assert s["atr_samples"] == 0
        assert s["prediction_samples"] == 0

    def test_summary_after_feeds(self) -> None:
        m = self.make_monitor()
        m.feed_atr(6.0)
        m.feed_spread(0.001)
        m.feed_prediction(0.5, 0.4, 0.6)
        m.mark_feature_received()
        s = m.as_summary()
        assert s["atr_samples"] == 1
        assert s["spread_samples"] == 1
        assert s["prediction_samples"] == 1
        assert s["last_feature_age_s"] is not None


class TestRunSignalHealthChecks:
    def test_returns_report_healthy(self) -> None:
        m = SignalHealthMonitor()
        m.mark_feature_received()
        for _ in range(60):
            m.feed_atr(6.0)
            m.feed_spread(0.001)
        for _ in range(80):
            m.feed_prediction(0.5, 0.4, 0.6)
        report = run_signal_health_checks(m, current_atr=6.0, current_spread_pct=0.001)
        assert report["healthy"]

    def test_prints_warning_when_unhealthy(self) -> None:
        m = SignalHealthMonitor()
        m.mark_feature_received(datetime.now(UTC) - timedelta(seconds=300))
        with patch("builtins.print") as mock_print:
            run_signal_health_checks(m)
        mock_print.assert_called_once()
