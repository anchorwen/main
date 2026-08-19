"""Unit tests for market_efficiency.py — Kaufman Efficiency Ratio + normalization check.

Covers:
  - compute_kaufman_er: basic computation, edge cases (empty, short, flat, trending, noisy)
  - check_market_normalized: threshold logic, ratio computation, all return paths
"""

from __future__ import annotations

import numpy as np
import pytest

from core.execution.market_efficiency import check_market_normalized, compute_kaufman_er

# ═══════════════════════════════════════════════════════════════════════════
# compute_kaufman_er
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeKaufmanER:
    """Pure function: Kaufman Efficiency Ratio."""

    # ── Basic cases ──

    def test_perfect_trend_returns_1(self) -> None:
        """A straight-line trend → ER = 1.0."""
        prices = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        result = compute_kaufman_er(prices, period=10)
        assert result == pytest.approx(1.0)

    def test_perfect_noise_returns_near_0(self) -> None:
        """Alternating up/down → high volatility, low direction → ER ≈ 0."""
        prices = [1.0, 2.0, 1.0, 2.0, 1.0, 2.0, 1.0, 2.0, 1.0, 2.0]
        result = compute_kaufman_er(prices, period=10)
        # |2-1| / (9 × 1.0) = 1/9 ≈ 0.111
        assert result < 0.2

    def test_mixed_market(self) -> None:
        """Realistic price series → intermediate ER."""
        prices = [100.0, 101.0, 99.0, 102.0, 103.0, 101.0, 104.0, 106.0, 105.0, 108.0]
        result = compute_kaufman_er(prices, period=10)
        assert 0.0 < result < 1.0

    def test_uses_only_last_period_bars(self) -> None:
        """Only the last `period` elements are used."""
        prices = list(range(100)) + [1.0, 2.0, 3.0, 4.0, 5.0]
        result = compute_kaufman_er(prices, period=5)
        # Last 5: [1,2,3,4,5] → perfect trend → ER=1.0
        assert result == pytest.approx(1.0)

    def test_default_period_10(self) -> None:
        """Default period is 10."""
        prices: list[float] = list(
            range(20)
        )  # TECH_DEBT-009: int→float numeric tower, 运行时值不变
        result = compute_kaufman_er(prices)
        assert result == pytest.approx(1.0)

    # ── Edge cases ──

    def test_empty_list_returns_0(self) -> None:
        assert compute_kaufman_er([]) == 0.0

    def test_single_price_returns_0(self) -> None:
        assert compute_kaufman_er([42.0]) == 0.0

    def test_two_prices_returns_1(self) -> None:
        """Two prices → direction = volatility → ER = 1.0."""
        assert compute_kaufman_er([10.0, 20.0], period=2) == pytest.approx(1.0)

    def test_fewer_prices_than_period_returns_0(self) -> None:
        """If len(prices) < period, returns 0.0 (insufficient data)."""
        assert compute_kaufman_er([1.0, 2.0, 3.0], period=10) == 0.0

    def test_all_same_price_returns_0(self) -> None:
        """Flat prices → volatility = 0 → returns 0.0."""
        result = compute_kaufman_er([5.0, 5.0, 5.0, 5.0, 5.0])
        assert result == 0.0

    def test_near_zero_volatility_returns_0(self) -> None:
        """Tiny price changes below 1e-10 threshold."""
        prices = [100.0, 100.0 + 1e-12, 100.0 + 2e-12, 100.0 + 3e-12]
        result = compute_kaufman_er(prices, period=4)
        assert result == 0.0

    def test_negative_prices(self) -> None:
        """Handles negative prices (e.g. interest rates)."""
        prices = [-10.0, -9.0, -8.0, -7.0, -6.0]
        result = compute_kaufman_er(prices, period=5)
        # Straight trend → 1.0
        assert result == pytest.approx(1.0)

    def test_period_equals_2(self) -> None:
        """Minimum valid period."""
        prices = [5.0, 6.0, 7.0]
        result = compute_kaufman_er(prices, period=2)
        # Last 2: [6,7] → |7-6|/|7-6| = 1.0
        assert result == pytest.approx(1.0)

    def test_period_larger_than_data_returns_0(self) -> None:
        prices = [1.0, 2.0, 3.0]
        result = compute_kaufman_er(prices, period=100)
        assert result == 0.0

    # ── numpy array input ──

    def test_accepts_numpy_array(self) -> None:
        prices = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
        result = compute_kaufman_er(prices, period=10)
        assert result == pytest.approx(1.0)

    def test_accepts_numpy_float32(self) -> None:
        prices = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float32)
        result = compute_kaufman_er(prices, period=5)
        assert result == pytest.approx(1.0)


# ═══════════════════════════════════════════════════════════════════════════
# check_market_normalized
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckMarketNormalized:
    """Pure function: market normalization checker."""

    # ── Normalization signals ──

    def test_vol_normalized_when_atr_below_threshold(self) -> None:
        """ATR below rolling mean → vol_normalized."""
        ok, reason = check_market_normalized(
            current_atr=10.0,
            rolling_atr_mean=12.0,
            rolling_atr_std=2.0,
            kaufman_er=0.8,
            atr_threshold=1.2,
            er_threshold=0.5,
        )
        assert ok is True
        assert "vol_normalized" in reason
        assert "0.83" in reason  # 10/12 ≈ 0.833

    def test_er_safe_when_vol_high_but_er_low(self) -> None:
        """ER below threshold → safe even if vol is elevated."""
        ok, reason = check_market_normalized(
            current_atr=20.0,
            rolling_atr_mean=10.0,
            rolling_atr_std=2.0,
            kaufman_er=0.3,
            atr_threshold=1.2,
            er_threshold=0.5,
        )
        assert ok is True
        assert "er_safe" in reason

    def test_toxic_when_both_high(self) -> None:
        """Both ATR and ER elevated → toxic."""
        ok, reason = check_market_normalized(
            current_atr=20.0,
            rolling_atr_mean=10.0,
            rolling_atr_std=2.0,
            kaufman_er=0.9,
            atr_threshold=1.2,
            er_threshold=0.5,
        )
        assert ok is False
        assert "toxic_vol" in reason

    # ── Boundary cases ──

    def test_atr_exactly_at_threshold(self) -> None:
        """ATR ratio == threshold → vol_normalized (strict < check)."""
        ok, reason = check_market_normalized(
            current_atr=12.0,
            rolling_atr_mean=10.0,
            rolling_atr_std=2.0,
            kaufman_er=0.9,
            atr_threshold=1.2,
            er_threshold=0.5,
        )
        # 12/10 = 1.2, which is NOT < 1.2 → falls through to er check → er=0.9 NOT < 0.5 → toxic
        assert ok is False

    def test_er_exactly_at_threshold(self) -> None:
        """ER == threshold → NOT safe (strict < check)."""
        ok, reason = check_market_normalized(
            current_atr=20.0,
            rolling_atr_mean=10.0,
            rolling_atr_std=2.0,
            kaufman_er=0.5,
            atr_threshold=1.2,
            er_threshold=0.5,
        )
        # ATR 20/10=2.0 NOT < 1.2, ER=0.5 NOT < 0.5 → toxic
        assert ok is False

    # ── Custom thresholds ──

    def test_custom_atr_threshold(self) -> None:
        ok, reason = check_market_normalized(
            current_atr=15.0,
            rolling_atr_mean=10.0,
            rolling_atr_std=2.0,
            kaufman_er=0.9,
            atr_threshold=2.0,
            er_threshold=0.5,
        )
        assert ok is True
        assert "vol_normalized" in reason

    # ── Zero mean guard ──

    def test_zero_rolling_mean_does_not_divide_by_zero(self) -> None:
        """max(rolling_atr_mean, 1e-8) prevents division by zero."""
        ok, reason = check_market_normalized(
            current_atr=0.00001,
            rolling_atr_mean=0.0,
            rolling_atr_std=0.0,
            kaufman_er=0.9,
        )
        # atr_ratio = 0.00001 / 1e-8 = 1000 → NOT < 1.2 → er check → 0.9 NOT < 0.5 → toxic
        assert ok is False
