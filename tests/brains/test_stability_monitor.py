"""Tests for core/brains/services/stability_monitor.py."""

from __future__ import annotations

import numpy as np
import pytest

from core.brains.services.stability_monitor import (
    PSI_CRITICAL,
    StabilityReport,
    build_stability_report,
    compute_csi,
    compute_psi,
)


class TestComputePSI:
    def test_identical_distributions(self):
        rng = np.random.default_rng(42)
        ref = rng.normal(0, 1, 1000)
        prod = rng.normal(0, 1, 1000)
        psi = compute_psi(ref, prod)
        assert psi < 0.05  # near zero for same distribution

    def test_shifted_distribution(self):
        rng = np.random.default_rng(42)
        ref = rng.normal(0, 1, 1000)
        prod = rng.normal(2, 1, 1000)
        psi = compute_psi(ref, prod)
        assert psi > PSI_CRITICAL  # clearly drifted

    def test_constant_input(self):
        ref = np.ones(100)
        prod = np.ones(100)
        psi = compute_psi(ref, prod)
        assert psi == pytest.approx(0.0)

    def test_empty_input(self):
        psi = compute_psi(np.array([]), np.array([]))
        assert psi == 0.0

    def test_different_sample_sizes(self):
        ref = np.random.default_rng(1).normal(0, 1, 500)
        prod = np.random.default_rng(1).normal(0, 1, 2000)
        psi = compute_psi(ref, prod)
        assert psi < 0.05


class TestComputeCSI:
    def test_per_feature_csi(self):
        rng = np.random.default_rng(42)
        ref = rng.normal(0, 1, (2000, 4))
        prod = rng.normal(0, 1, (2000, 4))
        csi = compute_csi(ref, prod)
        assert len(csi) == 4
        assert all(c < 0.10 for c in csi), f"CSI values: {csi}"

    def test_one_drifted_feature(self):
        rng = np.random.default_rng(42)
        ref = rng.normal(0, 1, (200, 3))
        prod = rng.normal(0, 1, (200, 3))
        prod[:, 1] += 3.0  # shift one feature
        csi = compute_csi(ref, prod)
        assert len(csi) == 3
        assert csi[1] > PSI_CRITICAL

    def test_dimension_mismatch_raises(self):
        ref = np.zeros((10, 3))
        prod = np.zeros((10, 5))
        with pytest.raises(ValueError):
            compute_csi(ref, prod)

    def test_1d_arrays_raise(self):
        with pytest.raises(ValueError):
            compute_csi(np.zeros(10), np.zeros(10))


class TestBuildStabilityReport:
    def test_stable_report(self):
        rng = np.random.default_rng(42)
        preds = rng.normal(0, 1, 200)
        feats = rng.normal(0, 1, (200, 3))
        report = build_stability_report(preds, preds, feats, feats)
        assert report.psi_status == "stable"
        assert report.retrain_recommended is False

    def test_critical_report(self):
        rng = np.random.default_rng(42)
        ref_preds = rng.normal(0, 1, 200)
        prod_preds = rng.normal(3, 1, 200)
        feats = rng.normal(0, 1, (200, 3))
        report = build_stability_report(ref_preds, prod_preds, feats, feats)
        assert report.psi_status == "critical"
        assert report.retrain_recommended is True

    def test_to_dict(self):
        report = StabilityReport(
            psi=0.15,
            csi_per_feature=np.array([0.05, 0.30, 0.02]),
            feature_names=["f1", "f2", "f3"],
            psi_status="warning",
            csi_alerts=["f2: CSI=0.3000 (critical)"],
            retrain_recommended=True,
        )
        d = report.to_dict()
        assert d["psi"] == 0.15
        assert d["psi_status"] == "warning"
        assert "f2" in d["csi_alerts"]
        assert d["retrain_recommended"] is True
