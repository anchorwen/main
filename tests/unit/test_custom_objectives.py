"""Unit tests for custom training objectives."""

from __future__ import annotations

import numpy as np
import pytest

from core.training.custom_objectives import (
    compute_sample_weights,
    lightgbm_sharpe_eval,
    lightgbm_sharpe_obj,
    make_xgb_sharpe_obj,
    profit_factor_approx,
    weighted_logloss,
)


class TestWeightedLogloss:
    """Tests for weighted_logloss."""

    def test_perfect_predictions(self):
        y_true = np.array([1.0, 0.0, 1.0, 0.0])
        y_pred = np.array([0.99, 0.01, 0.99, 0.01])
        loss = weighted_logloss(y_true, y_pred)
        assert loss < 0.1

    def test_worst_predictions(self):
        y_true = np.array([1.0, 0.0, 1.0, 0.0])
        y_pred = np.array([0.01, 0.99, 0.01, 0.99])
        loss = weighted_logloss(y_true, y_pred)
        assert loss > 1.0

    def test_with_sample_weights(self):
        y_true = np.array([1.0, 0.0])
        y_pred = np.array([0.5, 0.5])
        loss_uniform = weighted_logloss(y_true, y_pred)
        loss_weighted = weighted_logloss(y_true, y_pred, sample_weight=np.array([2.0, 1.0]))
        # Weighted loss should be different from uniform
        assert loss_uniform != loss_weighted

    def test_uniform_weights_same_as_none(self):
        y_true = np.array([1.0, 0.0, 1.0])
        y_pred = np.array([0.7, 0.3, 0.8])
        loss_none = weighted_logloss(y_true, y_pred, sample_weight=None)
        loss_uniform = weighted_logloss(y_true, y_pred, sample_weight=np.ones(len(y_true)))
        assert loss_none == pytest.approx(loss_uniform)


class TestSharpeObjective:
    """Tests for Sharpe-ratio custom objectives."""

    def test_lightgbm_sharpe_obj_shape(self):
        y_true = np.random.RandomState(42).randint(0, 2, 100).astype(np.float64)
        y_pred = np.random.RandomState(43).randn(100).astype(np.float64)

        grad, hess = lightgbm_sharpe_obj(y_true, y_pred)
        assert grad.shape == y_true.shape
        assert hess.shape == y_true.shape
        assert grad.dtype == np.float64
        assert not np.isnan(grad).any()
        assert not np.isinf(grad).any()

    def test_lightgbm_sharpe_obj_with_pnl(self):
        y_true = np.random.RandomState(42).randint(0, 2, 100).astype(np.float64)
        y_pred = np.random.RandomState(43).randn(100).astype(np.float64)
        pnl = np.random.RandomState(44).randn(100).astype(np.float64)

        grad, hess = lightgbm_sharpe_obj(y_true, y_pred, pnl=pnl)
        assert grad.shape == y_true.shape
        assert not np.isnan(grad).any()

    def test_lightgbm_sharpe_eval(self):
        y_true = np.random.RandomState(42).randint(0, 2, 100).astype(np.float64)
        y_pred = np.random.RandomState(43).randn(100).astype(np.float64)

        name, value, higher_is_better = lightgbm_sharpe_eval(y_true, y_pred)
        assert name == "sharpe"
        assert isinstance(value, float)
        assert higher_is_better is True

    def test_lightgbm_sharpe_eval_with_pnl(self):
        y_true = np.random.RandomState(42).randint(0, 2, 100).astype(np.float64)
        y_pred = np.random.RandomState(43).randn(100).astype(np.float64)
        pnl = np.random.RandomState(44).randn(100).astype(np.float64)

        name, value, higher_is_better = lightgbm_sharpe_eval(y_true, y_pred, pnl=pnl)
        assert name == "sharpe"
        assert isinstance(value, float)


class TestXGBoostSharpeObjective:
    """Tests for XGBoost Sharpe custom objective."""

    def test_make_obj_returns_callable(self):
        obj = make_xgb_sharpe_obj()
        assert callable(obj)

    def test_make_obj_with_pnl(self):
        pnl = np.random.RandomState(42).randn(50).astype(np.float64)
        obj = make_xgb_sharpe_obj(pnl=pnl)
        assert callable(obj)

    def test_obj_returns_grad_hess(self):
        obj = make_xgb_sharpe_obj()

        # Simulate XGBoost DMatrix with get_label()
        class MockDMatrix:
            def get_label(self):
                return np.random.RandomState(42).randint(0, 2, 50).astype(np.float64)

        preds = np.random.RandomState(43).randn(50).astype(np.float64)
        grad, hess = obj(preds, MockDMatrix())
        assert grad.shape == (50,)
        assert hess.shape == (50,)
        assert grad.dtype == np.float64
        assert not np.isnan(grad).any()
        assert not np.isinf(grad).any()


class TestComputeSampleWeights:
    """Tests for compute_sample_weights."""

    def test_none_method(self):
        y = np.array([0, 1, -1, 0, 1])
        weights = compute_sample_weights(y, method="none")
        np.testing.assert_array_equal(weights, np.ones(5))

    def test_return_magnitude(self):
        y = np.array([0, 1, 0, 1, -1])
        pnl = np.array([0.01, 0.05, -0.02, 0.10, -0.03])
        weights = compute_sample_weights(y, pnl=pnl, method="return_magnitude")
        assert len(weights) == 5
        assert np.all(weights > 0)
        assert np.all(weights <= 5.0)  # capped at 5x
        # Largest PnL magnitude should have higher weight
        assert weights[3] > weights[0]  # |0.10| > |0.01|

    def test_return_magnitude_no_pnl(self):
        y = np.array([0, 1, 0])
        weights = compute_sample_weights(y, method="return_magnitude")
        np.testing.assert_array_equal(weights, np.ones(3))

    def test_return_magnitude_zero_pnl(self):
        y = np.array([0, 1])
        pnl = np.zeros(2)
        weights = compute_sample_weights(y, pnl=pnl, method="return_magnitude")
        # Should handle zero PnL gracefully
        assert len(weights) == 2
        assert np.all(np.isfinite(weights))

    def test_inverse_class_frequency(self):
        y = np.array([0, 0, 0, 1, 1, -1])
        weights = compute_sample_weights(y, method="inverse_class_frequency")
        assert len(weights) == 6
        # Class 0 (count=3) should have lower weight than class 1 (count=2)
        assert weights[0] < weights[3]

    def test_inverse_class_frequency_balanced(self):
        y = np.array([0, 0, 1, 1, -1, -1])
        weights = compute_sample_weights(y, method="inverse_class_frequency")
        # All classes have equal count → equal weights
        assert np.allclose(weights, 1.0)

    def test_all_weights_positive(self):
        rng = np.random.RandomState(42)
        y = rng.randint(-1, 2, 200)
        pnl = rng.randn(200) * 0.01
        for method in ("none", "return_magnitude", "inverse_class_frequency"):
            weights = compute_sample_weights(y, pnl=pnl, method=method)
            assert np.all(weights > 0), f"Method {method} produced non-positive weights"
            assert np.all(np.isfinite(weights)), f"Method {method} produced non-finite weights"


class TestProfitFactorApprox:
    """Tests for profit_factor_approx."""

    def test_all_profits(self):
        y_true = np.array([1.0, 1.0, 1.0])
        y_pred = np.array([0.9, 0.8, 0.7])
        result = profit_factor_approx(y_true, y_pred)
        assert result > 0

    def test_all_losses(self):
        y_true = np.array([0.0, 0.0, 0.0])
        y_pred = np.array([0.9, 0.8, 0.7])
        result = profit_factor_approx(y_true, y_pred)
        # When directions are wrong, returns are negative
        assert isinstance(result, float)

    def test_with_pnl(self):
        y_true = np.array([1.0, 0.0, 1.0])
        y_pred = np.array([0.9, 0.2, 0.8])
        pnl = np.array([0.05, -0.02, 0.03])
        result = profit_factor_approx(y_true, y_pred, pnl=pnl)
        assert isinstance(result, float)
        assert result > 0

    def test_no_losses(self):
        y_true = np.array([1.0, 1.0])
        y_pred = np.array([0.9, 0.8])
        pnl = np.array([0.05, 0.03])
        result = profit_factor_approx(y_true, y_pred, pnl=pnl)
        assert result == 100.0  # No losses → high profit factor
