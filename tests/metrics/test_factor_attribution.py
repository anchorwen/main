"""Tests for core/metrics/factor_attribution.py."""

from __future__ import annotations

import numpy as np
import pytest

from core.metrics.factor_attribution import (
    FactorAttributionReport,
    build_factor_returns,
    decompose_pnl,
    exponential_decay_weights,
)


class TestExponentialDecayWeights:
    def test_weights_sum_to_one(self):
        w = exponential_decay_weights(10, half_life=5)
        assert w.sum() == pytest.approx(1.0)

    def test_recent_weight_larger(self):
        w = exponential_decay_weights(5, half_life=2)
        assert w[-1] > w[0]  # most recent period largest

    def test_half_life_effect(self):
        w_short = exponential_decay_weights(10, half_life=2)
        w_long = exponential_decay_weights(10, half_life=20)
        # Short half-life → steeper decay → last weight larger, first smaller
        assert w_short[-1] > w_long[-1]

    def test_zero_periods(self):
        w = exponential_decay_weights(0)
        assert len(w) == 0


class TestBuildFactorReturns:
    def test_four_factors_returned(self):
        prices = np.array([2000, 2001, 2002, 1999, 2003], dtype=np.float64)
        factors = build_factor_returns(prices)
        assert set(factors.keys()) == {"market", "momentum", "volatility", "carry"}
        for arr in factors.values():
            assert len(arr) == len(prices)

    def test_market_factor_captures_direction(self):
        prices = np.array([2000, 2002, 2004], dtype=np.float64)
        factors = build_factor_returns(prices)
        assert factors["market"][1] > 0  # price up → positive return
        assert factors["market"][2] > 0

    def test_volatility_with_atr(self):
        prices = np.array([2000, 2001, 2002, 2003, 2004], dtype=np.float64)
        atr = np.array([2, 3, 4, 5, 6], dtype=np.float64)
        factors = build_factor_returns(prices, atr_series=atr)
        assert np.all(factors["volatility"] >= 0)

    def test_short_input_returns_zeros(self):
        factors = build_factor_returns(np.array([2000.0]))
        assert len(factors["market"]) == 1


class TestDecomposePnl:
    def test_market_only_strategy(self):
        np.random.seed(42)
        n = 100
        market = np.random.normal(0, 0.02, n)
        strategy = 0.8 * market  # 80% market beta
        report = decompose_pnl(strategy, {"market": market}, half_life=21)
        assert report.r_squared > 0.5
        assert report.factor_contributions["market"] > 0.5

    def test_noise_strategy(self):
        np.random.seed(42)
        n = 100
        market = np.random.normal(0, 0.02, n)
        strategy = np.random.normal(0, 0.02, n)  # pure noise
        report = decompose_pnl(strategy, {"market": market}, half_life=21)
        assert report.residual > 0  # high residual

    def test_empty_factors(self):
        report = decompose_pnl(np.array([0.01, -0.02, 0.03]), {}, half_life=5)
        assert report.r_squared == 0.0
        assert report.residual == 1.0

    def test_short_series(self):
        report = decompose_pnl(np.array([0.01]), {"a": np.array([0.02])}, half_life=5)
        assert report.n_periods == 1

    def test_multiple_factors(self):
        np.random.seed(42)
        n = 150
        market = np.random.normal(0, 0.02, n)
        mom = np.roll(market, 2)
        vol = np.abs(np.random.normal(0, 0.01, n))
        strategy = 0.5 * market + 0.3 * mom + 0.05 * vol
        report = decompose_pnl(
            strategy, {"market": market, "momentum": mom, "volatility": vol}, half_life=21
        )
        assert report.r_squared > 0.3
        assert len(report.factor_contributions) == 3

    def test_to_dict(self):
        report = FactorAttributionReport(
            factor_contributions={"market": 0.7, "momentum": 0.2},
            r_squared=0.85,
            residual=0.1,
            n_periods=100,
            half_life=10,
        )
        d = report.to_dict()
        assert d["factor_contributions"]["market"] == 0.7
        assert d["r_squared"] == 0.85
        assert d["n_periods"] == 100
