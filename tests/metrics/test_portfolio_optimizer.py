"""Tests for portfolio optimisation."""

from __future__ import annotations

import numpy as np
import pytest

from core.metrics.portfolio_optimizer import (
    efficient_frontier,
    equal_weights,
    max_sharpe_weights,
    min_variance_weights,
    risk_parity_weights,
    sample_covariance,
    shrunk_covariance,
)


def _make_returns(n_periods: int = 200, n_assets: int = 4) -> np.ndarray:
    rng = np.random.default_rng(42)
    means = np.linspace(0.0005, 0.002, n_assets)
    return rng.normal(means, 0.01, (n_periods, n_assets))


class TestCovariance:
    def test_sample_cov_shape(self):
        rets = _make_returns(200, 4)
        cov = sample_covariance(rets)
        assert cov.shape == (4, 4)
        assert np.all(np.diag(cov) >= 0)

    def test_shrunk_cov_shape(self):
        rets = _make_returns(200, 4)
        cov = shrunk_covariance(rets, delta=0.3)
        assert cov.shape == (4, 4)
        assert np.allclose(cov, cov.T)  # symmetric

    def test_shrunk_delta_zero_equals_sample(self):
        rets = _make_returns(100, 3)
        samp = sample_covariance(rets)
        shrunk = shrunk_covariance(rets, delta=0.0)
        assert np.allclose(samp, shrunk)

    def test_shrunk_delta_one_is_target(self):
        rets = _make_returns(100, 3)
        full_shrink = shrunk_covariance(rets, delta=1.0)
        assert np.all(np.diag(full_shrink) > 0)

    def test_sample_cov_too_short_raises(self):
        with pytest.raises(ValueError):
            sample_covariance(np.array([[0.01, 0.02]]))  # 1 row


class TestMinVariance:
    def test_weights_sum_to_one(self):
        rets = _make_returns(200, 5)
        cov = sample_covariance(rets)
        w = min_variance_weights(cov)
        assert w.sum() == pytest.approx(1.0)
        assert np.all(w >= 0)

    def test_equal_variance_assets(self):
        """Identity covariance → equal weights."""
        cov = np.eye(4)
        w = min_variance_weights(cov)
        assert np.allclose(w, 0.25, atol=1e-10)

    def test_diagonal_cov(self):
        cov = np.diag([1.0, 4.0, 9.0])
        w = min_variance_weights(cov)
        # Most weight to lowest variance (first)
        assert w[0] > w[1] > w[2]


class TestMaxSharpe:
    def test_weights_sum_to_one(self):
        rets = _make_returns(200, 4)
        cov = sample_covariance(rets)
        er = np.mean(rets, axis=0)
        w = max_sharpe_weights(cov, er)
        assert w.sum() == pytest.approx(1.0)
        assert np.all(w >= 0)

    def test_no_expected_returns(self):
        cov = sample_covariance(_make_returns(200, 3))
        w = max_sharpe_weights(cov)  # uses vol proxy
        assert w.sum() == pytest.approx(1.0)

    def test_length_mismatch_raises(self):
        cov = sample_covariance(_make_returns(200, 3))
        with pytest.raises(ValueError):
            max_sharpe_weights(cov, np.array([0.01, 0.02]))


class TestRiskParity:
    def test_weights_sum_to_one(self):
        rets = _make_returns(300, 5)
        cov = sample_covariance(rets)
        w = risk_parity_weights(cov)
        assert w.sum() == pytest.approx(1.0)
        assert np.all(w >= 0)

    def test_equal_vol_assets(self):
        """Assets with equal variance → equal weights."""
        cov = np.eye(4)
        w = risk_parity_weights(cov)
        assert np.allclose(w, 0.25, atol=1e-8)

    def test_converges_in_max_iter(self):
        cov = sample_covariance(_make_returns(200, 6))
        w = risk_parity_weights(cov, max_iter=50)
        assert w.sum() == pytest.approx(1.0)


class TestEqualWeights:
    def test_equal_weights(self):
        w = equal_weights(5)
        assert len(w) == 5
        assert np.allclose(w, 0.2)


class TestEfficientFrontier:
    def test_returns_n_points(self):
        rets = _make_returns(200, 4)
        cov = sample_covariance(rets)
        er = np.mean(rets, axis=0)
        ef = efficient_frontier(cov, er, n_points=15)
        assert len(ef.weights) == 15
        assert len(ef.returns) == 15
        assert len(ef.volatilities) == 15
        assert len(ef.sharpe_ratios) == 15

    def test_frontier_has_valid_range(self):
        rets = _make_returns(200, 3)
        cov = sample_covariance(rets)
        ef = efficient_frontier(cov, n_points=5)
        # All volatilities should be positive and finite
        assert all(v > 0 and np.isfinite(v) for v in ef.volatilities)
        # All returns should be finite
        assert all(np.isfinite(r) for r in ef.returns)

    def test_to_dict(self):
        cov = sample_covariance(_make_returns(200, 3))
        ef = efficient_frontier(cov, n_points=5)
        d = ef.to_dict()
        assert len(d["points"]) == 5
        assert "weights" in d["points"][0]
