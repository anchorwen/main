"""Tests for core.metrics.financial_metrics — pure financial math functions (Phase 3c gap fill).

Covers: _as_array, max_drawdown, annualized_sharpe, annualized_sortino,
calmar_ratio, omega_ratio, win_rate, profit_factor, profit_factor_from_pnls,
expectancy, directional_accuracy, precision_recall_f1, compute_metrics.
"""

from __future__ import annotations

import numpy as np
import pytest

from core.metrics.financial_metrics import (
    _as_array,
    annualized_sharpe,
    annualized_sortino,
    calmar_ratio,
    compute_metrics,
    directional_accuracy,
    expectancy,
    max_drawdown,
    omega_ratio,
    precision_recall_f1,
    profit_factor,
    profit_factor_from_pnls,
    win_rate,
)


class TestAsArray:
    def test_list_to_array(self) -> None:
        result = _as_array([1.0, 2.0, 3.0])
        assert isinstance(result, np.ndarray)
        assert result.dtype == np.float64

    def test_array_passthrough(self) -> None:
        arr = np.array([1.0, 2.0])
        result = _as_array(arr)
        assert result.dtype == np.float64


class TestMaxDrawdown:
    def test_no_drawdown(self) -> None:
        r = [0.01, 0.01, 0.01]
        dd_abs, dd_pct = max_drawdown(r)
        assert dd_pct == 0.0
        assert dd_abs == 0.0

    def test_with_drawdown(self) -> None:
        r = [0.01, -0.05, 0.02]
        dd_abs, dd_pct = max_drawdown(r)
        assert dd_pct > 0
        assert dd_abs > 0

    def test_severe_drawdown(self) -> None:
        r = [0.0, -0.10, -0.10, -0.10]
        _, dd_pct = max_drawdown(r)
        assert dd_pct > 20  # significant drawdown

    def test_single_return_zero(self) -> None:
        dd_abs, dd_pct = max_drawdown([0.01])
        assert dd_pct == 0.0


class TestAnnualizedSharpe:
    def test_positive_returns(self) -> None:
        r = [0.001] * 252  # consistent small positive
        s = annualized_sharpe(r, periods_per_year=252)
        assert s > 0

    def test_zero_returns(self) -> None:
        r = [0.0] * 100
        s = annualized_sharpe(r)
        assert s == 0.0

    def test_insufficient_data(self) -> None:
        assert annualized_sharpe([0.01]) == 0.0

    def test_negative_returns(self) -> None:
        r = [-0.01, -0.02, -0.005, -0.015, -0.01]  # varied negative
        s = annualized_sharpe(r, periods_per_year=12)
        assert s < 0

    def test_different_periods_per_year(self) -> None:
        r = [0.01, -0.005, 0.02, -0.01, 0.015] * 20  # more variance
        s_daily = annualized_sharpe(r, periods_per_year=252)
        s_monthly = annualized_sharpe(r, periods_per_year=12)
        # Different annualization factors produce different absolute Sharpe values
        assert abs(s_daily) != abs(s_monthly)


class TestAnnualizedSortino:
    def test_all_positive(self) -> None:
        r = [0.001] * 100
        s = annualized_sortino(r, periods_per_year=252)
        assert s == float("inf")  # no downside deviation

    def test_with_downside(self) -> None:
        r = [0.01, -0.02, 0.01, -0.01] * 25
        s = annualized_sortino(r, periods_per_year=52)
        assert np.isfinite(s)

    def test_insufficient_data(self) -> None:
        assert annualized_sortino([0.01]) == 0.0

    def test_no_downside_negative_mean(self) -> None:
        r = [-0.001] * 100  # all negative, but threshold=0 catches all
        s = annualized_sortino(r, periods_per_year=252)
        assert s < 0  # negative mean, all below threshold


class TestCalmarRatio:
    def test_positive(self) -> None:
        r = [0.001] * 200
        c = calmar_ratio(r, periods_per_year=252)
        assert c > 0

    def test_insufficient_data(self) -> None:
        assert calmar_ratio([0.01]) == 0.0

    def test_negative_returns(self) -> None:
        r = [-0.01] * 50
        c = calmar_ratio(r, periods_per_year=12)
        assert c < 0


class TestOmegaRatio:
    def test_gains_only(self) -> None:
        r = [0.01, 0.02, 0.03]
        o = omega_ratio(r, threshold=0.0)
        assert o == float("inf")

    def test_losses_only(self) -> None:
        r = [-0.01, -0.02]
        o = omega_ratio(r, threshold=0.0)
        assert o == 0.0

    def test_mixed(self) -> None:
        r = [0.02, 0.01, -0.01, -0.005]
        o = omega_ratio(r, threshold=0.0)
        assert 1.0 < o < 10.0


class TestWinRate:
    def test_all_wins(self) -> None:
        assert win_rate([1.0, 2.0, 3.0]) == 1.0

    def test_all_losses(self) -> None:
        assert win_rate([-1.0, -2.0]) == 0.0

    def test_empty(self) -> None:
        assert win_rate([]) == 0.0

    def test_breakeven_not_win(self) -> None:
        assert win_rate([0.0, 1.0, -1.0]) == pytest.approx(1 / 3)


class TestProfitFactor:
    def test_inf_when_no_losses(self) -> None:
        assert profit_factor([1.0, 2.0], []) == float("inf")

    def test_zero_when_no_gains(self) -> None:
        assert profit_factor([], [1.0]) == 0.0

    def test_balanced(self) -> None:
        pf = profit_factor([100.0, 50.0], [-30.0, -20.0])
        assert pf == pytest.approx(150.0 / 50.0)


class TestProfitFactorFromPnls:
    def test_mixed(self) -> None:
        pf = profit_factor_from_pnls([10.0, -5.0, 20.0, -2.0])
        assert pf == pytest.approx(30.0 / 7.0)

    def test_all_wins(self) -> None:
        assert profit_factor_from_pnls([1.0, 2.0]) == float("inf")

    def test_all_losses(self) -> None:
        assert profit_factor_from_pnls([-1.0, -2.0]) == 0.0


class TestExpectancy:
    def test_positive(self) -> None:
        assert expectancy([1.0, 2.0, 3.0]) == 2.0

    def test_negative(self) -> None:
        assert expectancy([-1.0, -2.0]) == -1.5

    def test_empty(self) -> None:
        assert expectancy([]) == 0.0


class TestDirectionalAccuracy:
    def test_perfect(self) -> None:
        p = np.array([0, 1, 2])
        t = np.array([0, 1, 2])
        assert directional_accuracy(p, t) == 1.0

    def test_all_wrong(self) -> None:
        p = np.array([0, 0, 0])
        t = np.array([2, 2, 2])
        assert directional_accuracy(p, t) == 0.0

    def test_mixed(self) -> None:
        p = np.array([0, 1, 2, 0])
        t = np.array([0, 2, 2, 1])
        # 0→-1 vs 0→-1 ✓, 1→0 vs 2→+1 ✗, 2→+1 vs 2→+1 ✓, 0→-1 vs 1→0 ✗
        assert directional_accuracy(p, t) == 0.5


class TestPrecisionRecallF1:
    def test_perfect(self) -> None:
        p = np.array([1, 1, 0, 0])
        t = np.array([1, 1, 0, 0])
        result = precision_recall_f1(p, t, pos_label=1)
        assert result["precision"] == 1.0
        assert result["recall"] == 1.0
        assert result["f1"] == 1.0

    def test_no_positives(self) -> None:
        p = np.array([0, 0])
        t = np.array([0, 0])
        result = precision_recall_f1(p, t, pos_label=1)
        assert result["precision"] == 0.0
        assert result["recall"] == 0.0
        assert result["f1"] == 0.0

    def test_false_positives(self) -> None:
        p = np.array([1, 1, 0])
        t = np.array([1, 0, 0])
        result = precision_recall_f1(p, t, pos_label=1)
        assert result["precision"] == 0.5  # 1 TP / 2 predicted positive
        assert result["recall"] == 1.0  # 1 TP / 1 actual positive


class TestComputeMetrics:
    def test_returns_only(self) -> None:
        r = [0.001, -0.001] * 100
        m = compute_metrics(returns=r)
        assert "sharpe_ratio" in m
        assert "max_drawdown_pct" in m
        assert m["win_rate"] == 0.0  # default — no pnls

    def test_pnls_only(self) -> None:
        p = [10.0, -5.0, 20.0]
        m = compute_metrics(pnls=p)
        assert "win_rate" in m
        assert "profit_factor" in m
        assert "expectancy" in m
        assert "sharpe_ratio" in m  # derived from P&L / 100k equity
        assert "total_pnl" in m
        assert m["total_pnl"] == 25.0

    def test_returns_and_pnls(self) -> None:
        r = [0.001, -0.002, 0.003]
        p = [1.0, -2.0, 3.0]
        m = compute_metrics(returns=r, pnls=p)
        assert m["win_rate"] == pytest.approx(2 / 3)
        assert m["sharpe_ratio"] != 0

    def test_predictions_and_targets(self) -> None:
        preds = np.array([0, 1, 2])
        targets = np.array([0, 1, 2])
        m = compute_metrics(predictions=preds, targets=targets)
        assert m["direction_accuracy"] == 1.0
        assert m["accuracy"] == 1.0

    def test_insufficient_returns_defaults_to_zero(self) -> None:
        m = compute_metrics(returns=[0.01])  # only 1 return
        assert m["sharpe_ratio"] == 0.0
        assert m["max_drawdown_pct"] == 0.0

    def test_empty_pnls_defaults_to_zero(self) -> None:
        m = compute_metrics(pnls=[])
        assert m["win_rate"] == 0.0
        assert m["profit_factor"] == 0.0

    def test_empty_input(self) -> None:
        m = compute_metrics()
        assert "sharpe_ratio" in m
        assert m["sharpe_ratio"] == 0.0

    def test_all_inputs_combined(self) -> None:
        r = [0.001, -0.002, 0.003, -0.001, 0.002] * 20
        p = [10.0, -5.0, 15.0, -3.0, 8.0] * 20
        preds = np.array([0, 1, 2, 0, 1] * 20)
        targets = np.array([0, 1, 2, 0, 1] * 20)
        m = compute_metrics(returns=r, pnls=p, predictions=preds, targets=targets)
        assert m["win_rate"] > 0
        assert m["profit_factor"] > 0
        assert m["direction_accuracy"] == 1.0
        assert m["accuracy"] == 1.0
        assert "precision" in m
        assert "recall" in m
        assert "f1" in m
        assert m["total_pnl"] > 0

    def test_custom_periods_per_year(self) -> None:
        r = [0.02, -0.01, 0.03] * 50
        m = compute_metrics(returns=r, periods_per_year=12)
        assert m["sharpe_ratio"] != 0.0

    def test_pnls_derive_returns(self) -> None:
        """When only pnls provided, returns derived from equity=100k."""
        p = [100.0, -50.0, 200.0]
        m = compute_metrics(pnls=p)
        assert "sharpe_ratio" in m  # derived from returns
        assert m["total_pnl"] == 250.0

    def test_predictions_only_no_returns(self) -> None:
        preds = np.array([0, 1, 1, 0])
        targets = np.array([0, 1, 0, 0])
        m = compute_metrics(predictions=preds, targets=targets)
        assert m["direction_accuracy"] >= 0
        assert m["accuracy"] == 0.75


class TestMaxDrawdownEdgeCases:
    def test_recovery_after_drawdown(self) -> None:
        r = [-0.05, -0.03, 0.10, 0.02]
        _, dd_pct = max_drawdown(r)
        assert dd_pct > 0  # drawdown from first 2 periods

    def test_flat_equity(self) -> None:
        dd_abs, dd_pct = max_drawdown([0.0, 0.0, 0.0])
        assert dd_abs == 0.0
        assert dd_pct == 0.0


class TestSharpeEdgeCases:
    def test_all_identical_returns(self) -> None:
        s = annualized_sharpe([0.01] * 100, periods_per_year=12)
        # Identical returns → near-zero std → very large or zero Sharpe
        # Due to floating-point, std is not exactly 0
        assert abs(s) > 1000 or s == 0.0

    def test_single_outlier(self) -> None:
        r = [0.001] * 99 + [0.50]
        s = annualized_sharpe(r, periods_per_year=252)
        assert s > 0

    def test_risk_free_rate_applied(self) -> None:
        r = [0.001] * 100
        s0 = annualized_sharpe(r, risk_free_rate=0.0)
        s1 = annualized_sharpe(r, risk_free_rate=0.05)
        assert s0 != s1  # risk-free rate changes Sharpe


class TestSortinoEdgeCases:
    def test_custom_target_return(self) -> None:
        r = [0.01, -0.02, 0.03, -0.01] * 25
        s = annualized_sortino(r, target_return=0.01)
        assert np.isfinite(s)


class TestCalmarEdgeCases:
    def test_zero_drawdown_positive_returns(self) -> None:
        r = [0.01] * 50
        c = calmar_ratio(r)
        assert c == float("inf")  # no drawdown → infinite ratio

    def test_zero_drawdown_negative_returns(self) -> None:
        r = [-0.01] * 50
        c = calmar_ratio(r)
        # Negative returns → negative annual return → negative Calmar
        assert c < 0

    def test_weekly_periods(self) -> None:
        r = [0.005, -0.002, 0.008] * 30
        c = calmar_ratio(r, periods_per_year=52)
        assert np.isfinite(c)


class TestOmegaEdgeCases:
    def test_custom_threshold(self) -> None:
        r = [0.02, 0.01, -0.01, 0.0, -0.005]
        o = omega_ratio(r, threshold=0.01)
        assert np.isfinite(o)


class TestProfitFactorEdgeCases:
    def test_empty_wins_empty_losses(self) -> None:
        assert profit_factor([], []) == 0.0

    def test_zero_losses_zero_wins(self) -> None:
        assert profit_factor([0.0], [0.0]) == 0.0


class TestDirectionalAccuracyEdgeCases:
    def test_binary_predictions(self) -> None:
        p = np.array([0, 2, 0, 2, 2])
        t = np.array([0, 2, 2, 2, 0])
        # 0→-1 vs 0→-1 ✓, 2→+1 vs 2→+1 ✓, 0→-1 vs 2→+1 ✗, 2→+1 vs 2→+1 ✓, 2→+1 vs 0→-1 ✗
        assert directional_accuracy(p, t) == 0.6
