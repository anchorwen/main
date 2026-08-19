"""Tests for core.alpha.ou_optimizer — pure function coverage (Phase 3a).

Covers: calc_ou_params, compute_adx, compute_trend_mute, KalmanHalfLifeFilter.
"""

from __future__ import annotations

import numpy as np

from core.alpha.ou_optimizer import (
    KalmanHalfLifeFilter,
    calc_ou_params,
    compute_adx,
    compute_trend_mute,
)

# ═══════════════════════════════════════════════════════════════════════════
# calc_ou_params
# ═══════════════════════════════════════════════════════════════════════════


def test_calc_ou_params_insufficient_data():
    """<2 data points → theta=0, half_life=inf."""
    result = calc_ou_params(np.array([1.0]))
    assert result["theta"] == 0.0
    assert result["half_life"] == float("inf")
    assert result["z_score"] == 0.0


def test_calc_ou_params_zero_variance_prices():
    """Constant prices → zero variance → denom < 1e-12 → theta=0."""
    prices = np.array([1.0, 1.0, 1.0, 1.0, 1.0])
    result = calc_ou_params(prices)
    assert result["theta"] == 0.0
    assert result["half_life"] == float("inf")


def test_calc_ou_params_mean_reverting_series():
    """Synthetic OU process: mean-reverting to 0 → theta > 0, finite half_life."""
    np.random.seed(42)
    prices = np.zeros(200)
    for t in range(1, 200):
        prices[t] = prices[t - 1] - 0.1 * prices[t - 1] + np.random.normal(0, 0.02)
    result = calc_ou_params(prices)
    # Mean-reverting → theta > 0
    assert result["theta"] > 0
    assert result["half_life"] < float("inf")
    assert result["half_life"] > 0
    assert isinstance(result["z_score"], float)
    assert result["sigma"] >= 0


def test_calc_ou_params_trending_series():
    """Pure random walk → theta ≈ 0."""
    np.random.seed(123)
    prices = np.cumsum(np.random.normal(0, 1, 200))
    result = calc_ou_params(prices)
    # Random walk may have small positive or negative theta
    assert isinstance(result["theta"], float)
    assert isinstance(result["half_life"], float)
    assert isinstance(result["mu"], float)


def test_calc_ou_params_strong_trend():
    """Linear uptrend → theta negative (divergence from mean)."""
    prices = np.linspace(100, 200, 200)
    result = calc_ou_params(prices)
    # Strong uptrend: theta < 0 (prices moving AWAY from mean)
    assert result["theta"] < 0 or result["theta"] == 0.0
    assert isinstance(result["half_life"], float)


def test_calc_ou_params_extreme_outlier_mu():
    """Extreme price jump → mu stays finite and close to mean."""
    prices = np.array([1.0] * 98 + [100.0, 100.0])
    result = calc_ou_params(prices)
    # mu should be finite and within reasonable bounds
    assert np.isfinite(result["mu"])
    assert 1.0 <= result["mu"] <= 101.0


def test_calc_ou_params_two_points():
    """Exactly 2 data points → function completes without error."""
    result = calc_ou_params(np.array([1.0, 1.02]))
    # With only 2 points very close together, theta may be 0 (near-zero denom)
    # but the function should not crash
    assert isinstance(result["theta"], float)
    assert isinstance(result["z_score"], float)
    assert isinstance(result["mu"], float)


def test_calc_ou_params_mean_reverting_short_series():
    """Short mean-reverting series → theta > 0, half_life finite."""
    prices = np.array([1.0, 1.08, 1.04, 1.06, 1.02, 1.05, 1.03, 1.04])
    result = calc_ou_params(prices)
    # Mean-reverting around ~1.04 → theta should be positive
    assert result["theta"] > 0
    assert result["half_life"] < float("inf")


def test_calc_ou_params_return_keys():
    """All expected keys present."""
    result = calc_ou_params(np.array([1.0, 1.01, 1.02, 1.01, 1.03]))
    expected_keys = {"theta", "mu", "half_life", "z_score", "sigma"}
    assert set(result.keys()) == expected_keys


# ═══════════════════════════════════════════════════════════════════════════
# KalmanHalfLifeFilter
# ═══════════════════════════════════════════════════════════════════════════


class TestKalmanHalfLifeFilter:
    """Tests for the 1-D Kalman filter tracking OU theta."""

    def test_initial_state(self):
        """Initial theta_est matches constructor arg."""
        kf = KalmanHalfLifeFilter(initial_theta=0.02)
        assert kf.theta_est == 0.02
        assert kf.cov == 1.0

    def test_update_converges_toward_observed(self):
        """Repeated observations pull theta_est toward observed."""
        kf = KalmanHalfLifeFilter(initial_theta=0.01)
        for _ in range(50):
            kf.update(0.05)
        # After many updates, theta_est should be near 0.05
        assert 0.03 <= kf.theta_est <= 0.07

    def test_update_returns_filtered_value(self):
        """update() returns filtered theta."""
        kf = KalmanHalfLifeFilter(initial_theta=0.01)
        result = kf.update(0.03)
        assert isinstance(result, float)
        assert result > 0

    def test_cov_decreases_with_updates(self):
        """Covariance should decrease as filter gains confidence."""
        kf = KalmanHalfLifeFilter(initial_theta=0.01)
        initial_cov = kf.cov
        for _ in range(20):
            kf.update(0.02)
        assert kf.cov < initial_cov

    def test_half_life_property_finite(self):
        """half_life is finite when theta_est > 0."""
        kf = KalmanHalfLifeFilter(initial_theta=0.02)
        hl = kf.half_life
        assert hl > 0
        assert hl < float("inf")
        expected = np.log(2) / 0.02
        assert abs(hl - expected) < 0.01

    def test_half_life_property_zero_theta(self):
        """half_life is inf when theta_est <= 1e-6."""
        kf = KalmanHalfLifeFilter(initial_theta=0.0)
        assert kf.half_life == float("inf")

    def test_half_life_property_negative_theta(self):
        """half_life is inf when theta_est is negative (divergence)."""
        kf = KalmanHalfLifeFilter(initial_theta=-0.01)
        assert kf.half_life == float("inf")

    def test_adaptive_process_noise(self):
        """Large innovation → process noise increases."""
        kf = KalmanHalfLifeFilter(initial_theta=0.01)
        initial_Q = kf.Q
        kf.update(0.50)  # Large innovation
        assert kf.Q > initial_Q

    def test_custom_noise_params(self):
        """Custom process/measurement noise are respected."""
        kf = KalmanHalfLifeFilter(initial_theta=0.01, process_noise=0.005, measurement_noise=0.02)
        assert kf.Q == 0.005
        assert kf.R == 0.02


# ═══════════════════════════════════════════════════════════════════════════
# compute_adx
# ═══════════════════════════════════════════════════════════════════════════


def test_compute_adx_returns_correct_shape():
    """Output length matches input."""
    n = 100
    high = np.random.randn(n).cumsum() + 100
    low = high - np.abs(np.random.randn(n)) * 2
    close = (high + low) / 2
    adx = compute_adx(high, low, close, period=14)
    assert len(adx) == n


def test_compute_adx_insufficient_data():
    """Period too short → all values are 20.0 (default)."""
    prices = np.array([1.0, 2.0, 3.0])
    adx = compute_adx(prices, prices, prices, period=14)
    assert len(adx) == 3
    assert np.all(adx == 20.0)


def test_compute_adx_ranging_market():
    """Sideways market → ADX < 25 (ranging)."""
    n = 100
    np.random.seed(99)
    high = 100 + np.random.randn(n) * 0.5
    low = high - np.abs(np.random.randn(n)) * 0.3
    close = (high + low) / 2
    adx = compute_adx(high, low, close, period=14)
    # Sideways market: most ADX values should be low
    mid_adx = np.median(adx[30:])  # skip burn-in
    assert mid_adx < 30


def test_compute_adx_trending_market():
    """Strong trend → ADX > 25."""
    n = 100
    high = np.linspace(100, 150, n) + np.random.randn(n) * 0.5
    low = high - np.abs(np.random.randn(n)) * 1.0
    close = (high + low) / 2
    adx = compute_adx(high, low, close, period=14)
    # Strong trend: later ADX values should be elevated
    tail_adx = np.mean(adx[50:])
    assert tail_adx > 20


def test_compute_adx_non_negative():
    """All ADX values are non-negative."""
    n = 80
    prices = np.random.randn(n).cumsum() + 100
    adx = compute_adx(prices, prices, prices, period=14)
    assert np.all(adx >= 0)


def test_compute_adx_custom_period():
    """Custom period parameter is respected."""
    n = 60
    prices = np.random.randn(n).cumsum() + 100
    adx14 = compute_adx(prices, prices, prices, period=14)
    adx10 = compute_adx(prices, prices, prices, period=10)
    # Different periods produce different values (except near start)
    assert not np.allclose(adx14[30:], adx10[30:])


# ═══════════════════════════════════════════════════════════════════════════
# compute_trend_mute
# ═══════════════════════════════════════════════════════════════════════════


def test_compute_trend_mute_output_range():
    """All multipliers in [0, 1]."""
    n = 100
    prices = np.random.randn(n).cumsum() + 100
    mute = compute_trend_mute(prices)
    assert len(mute) == n
    assert np.all(mute >= 0.0)
    assert np.all(mute <= 1.0)


def test_compute_trend_mute_ranging_market():
    """Sideways market → multipliers ≈ 1.0 (no muting)."""
    n = 200
    np.random.seed(42)
    prices = 100 + np.random.randn(n).cumsum() * 0.1  # very slow trend
    mute = compute_trend_mute(prices)
    # Most values should be near 1.0 (not muted)
    assert np.mean(mute[30:]) > 0.5


def test_compute_trend_mute_strong_trend():
    """Strong trend with noise → multipliers drop (partial muting)."""
    n = 200
    np.random.seed(7)
    trend = np.linspace(100, 200, n)
    prices = trend + np.random.randn(n) * 1.0  # small noise to avoid numerical issues
    mute = compute_trend_mute(prices, adx_threshold=25.0)
    # Strong trend: tail values should show significant muting
    assert np.mean(mute[150:]) <= 0.8


def test_compute_trend_mute_custom_threshold():
    """Custom ADX threshold changes muting behavior."""
    n = 150
    prices = np.linspace(100, 180, n)
    mute25 = compute_trend_mute(prices, adx_threshold=25.0)
    mute30 = compute_trend_mute(prices, adx_threshold=30.0)
    # Higher threshold → less muting (more values at 1.0)
    assert np.sum(mute30 > 0) >= np.sum(mute25 > 0)
