"""Tests for implementation shortfall decomposition."""

from __future__ import annotations

from core.execution.quality_analyzer import compute_implementation_shortfall


class TestImplementationShortfall:
    def test_no_slippage_no_shortfall(self):
        result = compute_implementation_shortfall(
            order_id="ord_1",
            symbol="XAUUSDc",
            side="long",
            decision_price=2000.0,
            arrival_price=2000.0,
            average_fill_price=2000.0,
            filled_quantity=0.01,
            requested_quantity=0.01,
        )
        assert result.total_shortfall_bps == 0.0
        assert result.delay_cost_bps == 0.0
        assert result.market_impact_bps == 0.0
        assert result.opportunity_cost_bps == 0.0
        assert result.fill_rate == 1.0

    def test_buy_market_impact_positive(self):
        """Buy at higher price → positive shortfall (cost)."""
        result = compute_implementation_shortfall(
            order_id="ord_2",
            symbol="XAUUSDc",
            side="buy",
            decision_price=2000.0,
            arrival_price=2000.5,
            average_fill_price=2001.0,
            filled_quantity=0.01,
            requested_quantity=0.01,
        )
        # Delay: 2000.0→2000.5 = +0.5 → +2.5 bps
        # Market: 2000.5→2001.0 = +0.5 → +2.4975 bps
        # Total: 2000.0→2001.0 = +1.0 → +5.0 bps
        assert result.total_shortfall_bps > 0
        assert result.delay_cost_bps > 0
        assert result.market_impact_bps > 0

    def test_sell_favourable_slippage(self):
        """Sell at higher price → negative shortfall (benefit)."""
        result = compute_implementation_shortfall(
            order_id="ord_3",
            symbol="XAUUSDc",
            side="sell",
            decision_price=2000.0,
            arrival_price=2001.0,
            average_fill_price=2002.0,
            filled_quantity=0.01,
            requested_quantity=0.01,
        )
        assert result.total_shortfall_bps < 0  # favourable for seller

    def test_partial_fill_opportunity_cost(self):
        result = compute_implementation_shortfall(
            order_id="ord_4",
            symbol="XAUUSDc",
            side="long",
            decision_price=2000.0,
            arrival_price=2005.0,
            average_fill_price=2005.0,
            filled_quantity=0.005,
            requested_quantity=0.01,
        )
        assert result.fill_rate == 0.5
        assert result.opportunity_cost_bps > 0

    def test_full_fill_no_opportunity_cost(self):
        result = compute_implementation_shortfall(
            order_id="ord_5",
            symbol="XAUUSDc",
            side="long",
            decision_price=2000.0,
            arrival_price=1999.0,
            average_fill_price=1999.0,
            filled_quantity=0.01,
            requested_quantity=0.01,
        )
        assert result.opportunity_cost_bps == 0.0
        assert result.fill_rate == 1.0

    def test_zero_requested_quantity(self):
        result = compute_implementation_shortfall(
            order_id="ord_6",
            symbol="XAUUSDc",
            side="long",
            decision_price=2000.0,
            arrival_price=2001.0,
            filled_quantity=0.0,
            requested_quantity=0.0,
        )
        assert result.fill_rate == 1.0  # defaults when no quantity

    def test_to_dict(self):
        result = compute_implementation_shortfall(
            order_id="s1",
            symbol="XAUUSDc",
            side="buy",
            decision_price=2000.0,
            arrival_price=2001.0,
            average_fill_price=2002.0,
            filled_quantity=0.01,
            requested_quantity=0.01,
        )
        d = result.to_dict()
        assert d["order_id"] == "s1"
        assert "total_shortfall_bps" in d
        assert "delay_cost_bps" in d

    def test_short_favourable_fill(self):
        """Short entry: higher fill than decision → benefit (negative cost)."""
        result = compute_implementation_shortfall(
            order_id="s",
            symbol="XAUUSDc",
            side="short",
            decision_price=2000.0,
            arrival_price=2000.5,
            average_fill_price=2001.0,
            filled_quantity=0.01,
            requested_quantity=0.01,
        )
        # Short selling higher than decision → negative shortfall (benefit)
        assert result.total_shortfall_bps < 0
