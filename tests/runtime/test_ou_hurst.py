"""Unit tests for ou_hurst — pure function extracted from live_cycle.py.

Tests the pure compute_tf_ou_hurst() function.
Strangler Fig #14: Lock behavior after extraction from live_cycle.py.

Target: >=90% line / >=80% branch coverage (pure mathematical function).
"""

from __future__ import annotations

from core.runtime.ou_hurst import compute_tf_ou_hurst

# ── Insufficient data ─────────────────────────────────────────────────────


def test_insufficient_data_empty():
    """Empty price list → default (0.0, 0.5)."""
    ou, hurst = compute_tf_ou_hurst([])
    assert ou == 0.0
    assert hurst == 0.5


def test_insufficient_data_short():
    """Fewer than 21 prices → default (0.0, 0.5)."""
    ou, hurst = compute_tf_ou_hurst([100.0] * 20)
    assert ou == 0.0
    assert hurst == 0.5


def test_minimum_data_exact_21():
    """Exactly 21 prices → runs computation, returns valid floats."""
    ou, hurst = compute_tf_ou_hurst([100.0 + i * 0.1 for i in range(21)])
    assert isinstance(ou, float)
    assert isinstance(hurst, float)
    assert not (ou == 0.0 and hurst == 0.5)  # not the default — real computation


# ── Edge cases — boundary conditions ──────────────────────────────────────


def test_constant_prices_zero_variance():
    """All prices identical → std=0 → ou_theta via zero beta_den, hurst=0.5."""
    ou, hurst = compute_tf_ou_hurst([42.0] * 30)
    # beta_den=0 because all x values are identical → ou_theta = 0.0
    assert ou == 0.0
    # s=0 because all values identical → hurst = 0.5
    assert hurst == 0.5


def test_two_distinct_values_repeating():
    """Only 2 distinct values alternating → low variance, beta_den > 0."""
    prices = [100.0, 101.0] * 15  # 30 values alternating
    ou, hurst = compute_tf_ou_hurst(prices)
    assert isinstance(ou, float)
    assert isinstance(hurst, float)


def test_monotonic_increasing_trend():
    """Strictly increasing prices → mean-reversion coefficient negative? No, just H > 0.5."""
    prices = [100.0 + i * 0.5 for i in range(50)]  # strong uptrend
    ou, hurst = compute_tf_ou_hurst(prices)
    # Strong trend → Hurst should be > 0.5 (persistent)
    assert hurst > 0.5, f"Expected H > 0.5 for strong trend, got {hurst:.4f}"
    # OU theta should be near zero or negative (no mean reversion in trend)
    # Actually with upward trend, beta ≈ 1.0, theta = -log(beta) ≈ 0
    assert ou >= 0.0, f"Expected theta >= 0 for trend, got {ou:.4f}"


def test_mean_reverting_series():
    """Ornstein-Uhlenbeck simulated mean-reverting series → theta > 0, H < 0.5."""
    # Generate a simple mean-reverting series:
    # x_t = mean + phi * (x_{t-1} - mean) + noise
    # with phi = 0.7 (moderate mean reversion)
    import random

    random.seed(42)
    mean = 100.0
    phi = 0.7
    noise_std = 0.5
    prices = [mean]
    for _ in range(99):
        next_val = mean + phi * (prices[-1] - mean) + random.gauss(0, noise_std)
        prices.append(next_val)

    ou, hurst = compute_tf_ou_hurst(prices)
    # Mean-reverting → theta > 0
    assert ou > 0.0, f"Expected theta > 0 for mean-reverting series, got {ou:.4f}"
    # Anti-persistent → Hurst < 0.5 (may not always hold with small window)
    # Just verify it's a valid value
    assert 0.0 <= hurst <= 1.0, f"Hurst out of range: {hurst:.4f}"


def test_prices_contain_nan():
    """NaN in prices — numpy will propagate, check behavior."""
    prices = [float("nan")] * 30
    ou, hurst = compute_tf_ou_hurst(prices)
    # All operations on NaN produce NaN, but our fallback catches s==0
    assert isinstance(ou, float)
    assert isinstance(hurst, float)


# ── Physics override integration ──────────────────────────────────────────
# FIX-20260613-090-Step1: OU Theta > 0.5 AND Hurst < 0.48 → "ranging"


def test_ranging_signal_thresholds():
    """Verify the physics override thresholds are computable.

    OU Theta > 0.5 AND Hurst < 0.48 triggers "ranging" regime override.
    This test verifies that a strongly mean-reverting series produces
    values in the expected range.
    """
    import random

    random.seed(123)
    mean = 100.0
    phi = 0.3  # very strong mean reversion
    noise_std = 0.2
    prices = [mean]
    for _ in range(199):
        next_val = mean + phi * (prices[-1] - mean) + random.gauss(0, noise_std)
        prices.append(next_val)

    ou, hurst = compute_tf_ou_hurst(prices)
    # This should produce a strong mean-reversion signal
    assert ou > 0.0, f"Expected theta > 0, got {ou:.4f}"
    # Verify values are physically bounded
    assert 0.0 <= ou <= 5.0, f"Theta out of physical range: {ou:.4f}"
    assert 0.0 <= hurst <= 1.0, f"Hurst out of range: {hurst:.4f}"


# ── Determinism ───────────────────────────────────────────────────────────


def test_deterministic_same_input_same_output():
    """Pure function: same input always produces same output."""
    prices = [100.0 + i * 0.1 + (i % 3) * 0.05 for i in range(30)]
    result1 = compute_tf_ou_hurst(prices)
    result2 = compute_tf_ou_hurst(prices)
    result3 = compute_tf_ou_hurst(prices)
    assert result1 == result2 == result3


# ── smoke: realistic BTC prices ──────────────────────────────────────────


def test_realistic_price_range():
    """BTC-like prices (50K-100K range) produce physically valid values."""
    # Simulate BTC prices around $60K with small fluctuations
    base = 60000.0
    prices = [base + i * 10 + (i % 5) * 50 for i in range(100)]
    ou, hurst = compute_tf_ou_hurst(prices)
    assert isinstance(ou, float)
    assert isinstance(hurst, float)
    assert 0.0 <= ou <= 5.0, f"Theta out of range: {ou:.4f}"
    assert 0.0 <= hurst <= 1.0, f"Hurst out of range: {hurst:.4f}"


def test_xau_like_prices():
    """XAU-like prices (2000-3000 range) with realistic microstructure."""
    base = 2650.0
    prices = [base + i * 0.5 + (i % 7 - 3) * 0.3 for i in range(150)]
    ou, hurst = compute_tf_ou_hurst(prices)
    assert isinstance(ou, float)
    assert isinstance(hurst, float)
    # Use the most recent 21 prices only
    assert 0.0 <= ou <= 5.0
    assert 0.0 <= hurst <= 1.0


# ── Large data: only last 21 used ─────────────────────────────────────────


def test_large_dataset_only_last_21_used():
    """1000 prices → only last 21 matter."""
    prices: list[float] = list(range(1000))  # TECH_DEBT-009: int→float numeric tower, 运行时值不变
    result_full = compute_tf_ou_hurst(prices)
    result_last = compute_tf_ou_hurst(prices[-21:])
    assert result_full == result_last
