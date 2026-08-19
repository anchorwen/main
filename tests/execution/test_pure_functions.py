"""Branch coverage tests for strategy_line pure functions.

Target functions: sigmoid_exhaustion, apply_mvs, z_depth_penalty,
trend_maturity_discount, check_z_inflection.
All zero I/O, deterministic — ideal for parameterized coverage.
"""

from __future__ import annotations

import pytest

from core.execution.strategy_line import (
    apply_mvs,
    check_z_inflection,
    sigmoid_exhaustion,
    trend_maturity_discount,
    z_depth_penalty,
)

# ── sigmoid_exhaustion ────────────────────────────────────────────────────


class TestSigmoidExhaustion:
    def test_midpoint_returns_half(self):
        assert sigmoid_exhaustion(1.75) == pytest.approx(0.5, abs=0.01)

    def test_low_z_small_factor(self):
        r = sigmoid_exhaustion(0.5)
        assert 0.0 < r < 0.5

    def test_high_z_near_one(self):
        r = sigmoid_exhaustion(4.0)
        assert r > 0.95

    def test_zero_z(self):
        r = sigmoid_exhaustion(0.0)
        assert 0.0 < r < 0.5

    def test_custom_params(self):
        r = sigmoid_exhaustion(2.0, z_mid=2.0, k=5.0)
        assert r == pytest.approx(0.5, abs=0.01)


# ── apply_mvs ─────────────────────────────────────────────────────────────


class TestApplyMVS:
    def test_below_threshold_killed(self):
        assert apply_mvs(0.10) == 0.0

    def test_at_threshold(self):
        assert apply_mvs(0.20) == 0.20

    def test_above_threshold(self):
        assert apply_mvs(0.50) == 0.50

    def test_custom_threshold(self):
        assert apply_mvs(0.25, threshold=0.30) == 0.0


# ── z_depth_penalty ───────────────────────────────────────────────────────


class TestZDepthPenalty:
    def test_below_entry_no_penalty(self):
        assert z_depth_penalty(1.0) == 1.0

    def test_at_entry_no_penalty(self):
        assert z_depth_penalty(1.5) == 1.0

    def test_deep_z_penalty(self):
        r = z_depth_penalty(3.0)
        assert r < 0.8

    def test_extreme_z_penalty(self):
        r = z_depth_penalty(5.0)
        assert r < 0.5


# ── trend_maturity_discount ───────────────────────────────────────────────


class TestTrendMaturityDiscount:
    def test_non_trend_strategy_no_discount(self):
        assert trend_maturity_discount(strategy_family="mean_reversion") == 1.0

    def test_strong_hurst_no_discount(self):
        r = trend_maturity_discount(hurst=0.60, strategy_family="trend_following")
        assert r > 0.9

    def test_random_walk_discount(self):
        r = trend_maturity_discount(hurst=0.50, strategy_family="trend_following")
        assert r < 0.8

    def test_anti_persistent_discount(self):
        r = trend_maturity_discount(hurst=0.45, strategy_family="trend_following")
        assert r < 0.6  # floor is 0.40; hurst=0.45 clipped→0.45, discount=1-0.45=0.55

    def test_kalman_weak_discount(self):
        r = trend_maturity_discount(trend_strength=0.30, strategy_family="swing")
        assert r < 0.8

    def test_combined_discount_floored(self):
        r = trend_maturity_discount(
            hurst=0.45, trend_strength=0.25, strategy_family="trend_following"
        )
        assert r >= 0.40

    def test_swing_strategy_discounts(self):
        r = trend_maturity_discount(hurst=0.50, strategy_family="swing")
        assert r < 0.8


# ── check_z_inflection ────────────────────────────────────────────────────


class TestCheckZInflection:
    def test_first_cycle_no_prev_z(self):
        ok, reason = check_z_inflection(-2.0, None, "long")
        assert ok is True
        assert "first" in reason

    def test_long_turning(self):
        ok, _ = check_z_inflection(-1.8, -2.0, "long")
        assert ok is True

    def test_long_still_falling_blocked(self):
        ok, _ = check_z_inflection(-2.2, -2.0, "long")
        assert ok is False

    def test_short_turning(self):
        ok, _ = check_z_inflection(1.8, 2.0, "short")
        assert ok is True

    def test_short_still_rising_blocked(self):
        ok, _ = check_z_inflection(2.2, 2.0, "short")
        assert ok is False

    def test_neutral_passes(self):
        ok, _ = check_z_inflection(1.0, 2.0, "neutral")
        assert ok is True
