"""Tests for market impact estimation."""

from __future__ import annotations

from core.execution.market_impact import (
    estimate_market_impact,
    impact_volatility_adjustment,
)


class TestEstimateMarketImpact:
    def test_zero_volume_no_impact(self):
        est = estimate_market_impact(order_volume=0.0, daily_volume=5000, volatility=0.0012)
        assert est.total_impact_bps == 0.0
        assert est.permanent_impact_bps == 0.0
        assert est.temporary_impact_bps == 0.0

    def test_normal_sized_order(self):
        est = estimate_market_impact(
            order_volume=0.05,
            daily_volume=5000,
            volatility=0.0012,
            spread_bps=1.5,
        )
        assert est.total_impact_bps > 0
        assert est.temporary_impact_bps >= 0  # always non-negative
        assert isinstance(est.permanent_impact_bps, float)

    def test_large_order_higher_impact(self):
        small = estimate_market_impact(
            order_volume=0.01,
            daily_volume=5000,
            volatility=0.0012,
        )
        large = estimate_market_impact(
            order_volume=1.0,
            daily_volume=5000,
            volatility=0.0012,
        )
        assert large.total_impact_bps > small.total_impact_bps

    def test_high_volatility_higher_impact(self):
        low_vol = estimate_market_impact(
            order_volume=0.05,
            daily_volume=5000,
            volatility=0.0005,
        )
        high_vol = estimate_market_impact(
            order_volume=0.05,
            daily_volume=5000,
            volatility=0.0050,
        )
        assert high_vol.total_impact_bps > low_vol.total_impact_bps

    def test_fast_execution_higher_impact(self):
        """Shorter time_fraction → higher participation rate → higher temp impact."""
        slow = estimate_market_impact(
            order_volume=0.05,
            daily_volume=5000,
            volatility=0.0012,
            time_fraction=1.0,
        )
        fast = estimate_market_impact(
            order_volume=0.05,
            daily_volume=5000,
            volatility=0.0012,
            time_fraction=0.1,
        )
        assert fast.temporary_impact_bps > slow.temporary_impact_bps

    def test_long_vs_short_sign(self):
        long_est = estimate_market_impact(
            order_volume=0.05,
            daily_volume=5000,
            volatility=0.0012,
            side="long",
        )
        short_est = estimate_market_impact(
            order_volume=0.05,
            daily_volume=5000,
            volatility=0.0012,
            side="short",
        )
        # Long should have positive impact (buying pushes price up)
        assert long_est.total_impact_bps > 0
        # Short should have negative total impact (price moves against)
        assert short_est.total_impact_bps < 0

    def test_to_dict(self):
        est = estimate_market_impact(
            order_volume=0.05,
            daily_volume=5000,
            volatility=0.0012,
        )
        d = est.to_dict()
        assert "permanent_impact_bps" in d
        assert "temporary_impact_bps" in d
        assert "total_impact_bps" in d

    def test_negative_volume_abs_applied(self):
        est = estimate_market_impact(order_volume=-0.05, daily_volume=5000, volatility=0.0012)
        assert est.total_impact_bps > 0  # abs() applied to volume


class TestImpactVolatilityAdjustment:
    def test_returns_bps_from_atr(self):
        bps = impact_volatility_adjustment(
            order_volume=0.05,
            daily_volume=5000,
            current_atr=10.0,
            mid_price=2000.0,
        )
        assert bps > 0
        assert isinstance(bps, float)

    def test_zero_price_returns_zero(self):
        assert impact_volatility_adjustment(0.05, 5000, 10, 0.0) == 0.0

    def test_zero_volume_returns_zero(self):
        assert impact_volatility_adjustment(0.0, 5000, 10, 2000.0) == 0.0
