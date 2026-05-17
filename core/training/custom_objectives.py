"""Custom training objectives that directly optimize trading metrics.

The manifold mismatch problem: models trained on logloss/accuracy are evaluated
on Sharpe/win-rate. These custom objectives bridge that gap by providing
differentiable approximations of trading-aligned metrics for XGBoost and
LightGBM.

Supports:
  - Sharpe ratio approximation (differentiable, gradient-friendly)
  - Profit factor approximation
  - Return-magnitude-weighted logloss (critical trades get higher gradient)

Usage:
    # XGBoost
    params = {
        "objective": xgboost_sharpe_obj,  # custom objective callable
        ...
    }
    # LightGBM
    params = {
        "objective": lightgbm_sharpe_obj,  # custom objective callable
        ...
    }
"""

from __future__ import annotations

from typing import Any

import numpy as np


def _sharpe_from_preds(
    y_true: np.ndarray, y_pred: np.ndarray, pnl: np.ndarray | None = None
) -> float:
    """Compute Sharpe ratio from predictions.

    Uses PnL directly if provided; otherwise estimates PnL from direction
    predictions × returns (approximated from labels for classification).
    """
    if pnl is not None and len(pnl) > 0:
        # Use actual PnL data if available
        # y_pred gives direction; pnl gives magnitude
        positions = np.where(
            y_pred > 0.5,
            1.0,
            np.where(y_pred < -0.5 if y_pred.min() < -0.1 else y_pred < 0.3, -1.0, 0.0),
        )
        daily_returns = positions * pnl
    else:
        # Fallback: use predictions as positions, labels as returns
        positions = (y_pred - 0.5) * 2.0
        daily_returns = positions * (y_true * 2 - 1)  # ±1 direction

    mean_ret = float(np.mean(daily_returns))
    std_ret = float(np.std(daily_returns))
    if std_ret < 1e-10:
        return 0.0
    return mean_ret / std_ret * np.sqrt(252)


def _stable_sigmoid(preds: np.ndarray) -> np.ndarray:
    """Numerically stable sigmoid that clamps extreme values."""
    preds = np.clip(np.asarray(preds, dtype=np.float64), -20.0, 20.0)
    return 1.0 / (1.0 + np.exp(-preds))


def make_xgb_sharpe_obj(pnl: np.ndarray | None = None):
    """Create an XGBoost custom objective that minimizes negative Sharpe.

    Returns a callable f(preds, dtrain) → (grad, hess).

    Gradient derivation:
      position = 2*sigmoid(preds) - 1
      return = position * pnl
      Sharpe = mean(ret) / std(ret)

      d(position)/d(preds) = 2 * sigmoid * (1-sigmoid)
      d(ret)/d(preds) = d(position)/d(preds) * pnl

      d(Sharpe)/d(preds) = d(ret)/dpreds * (σ² - μ*(ret-μ)) / (n * σ³)

    We negate this for minimization.
    """
    _pnl = np.asarray(pnl, dtype=np.float64) if pnl is not None else None

    def _obj(preds: np.ndarray, dtrain: Any) -> tuple[np.ndarray, np.ndarray]:
        y = dtrain.get_label().astype(np.float64)
        p = _stable_sigmoid(preds.astype(np.float64))
        dp = p * (1.0 - p)

        if _pnl is not None and len(_pnl) > 0:
            returns = (2.0 * p - 1.0) * _pnl
            # d(ret)/d(preds) = 2 * pnl * p * (1-p)
            dr_dpreds = 2.0 * _pnl * dp
        else:
            direction = 2.0 * y - 1.0
            returns = (2.0 * p - 1.0) * direction
            dr_dpreds = 2.0 * direction * dp

        n = float(len(y))
        mean_r = returns.mean()
        var_r = returns.var() + 1e-10
        std_r = np.sqrt(var_r)

        # d(-Sharpe)/d(preds) = -dr_dpreds * (var_r - mean_r*(ret-mean_r)) / (n * std_r^3)
        numerator = dr_dpreds * (var_r - mean_r * (returns - mean_r))
        grad = -numerator / (n * std_r**3)

        # Clip gradient magnitude for stability
        grad = np.clip(grad, -5.0, 5.0)

        # Hessian: use absolute gradient as a proxy (well-behaved for boosting)
        hess = np.abs(grad) + 0.01

        return grad.astype(np.float64), hess.astype(np.float64)

    return _obj


def lightgbm_sharpe_obj(
    y_true: np.ndarray, y_pred: np.ndarray, *, pnl: np.ndarray | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """LightGBM custom objective: differentiable negative Sharpe.

    Unlike XGBoost, LightGBM passes (y_true, y_pred) directly.
    Returns (gradient, hessian).
    """
    y = y_true.astype(np.float64)
    p = _stable_sigmoid(y_pred.astype(np.float64))
    dp = p * (1.0 - p)

    if pnl is not None and len(pnl) > 0:
        _pnl = np.asarray(pnl, dtype=np.float64)
        returns = (2.0 * p - 1.0) * _pnl
        dr_dpreds = 2.0 * _pnl * dp
    else:
        direction = 2.0 * y - 1.0
        returns = (2.0 * p - 1.0) * direction
        dr_dpreds = 2.0 * direction * dp

    n = float(len(y))
    mean_r = float(np.mean(returns))
    var_r = float(np.var(returns)) + 1e-10
    std_r = np.sqrt(var_r)

    numerator = dr_dpreds * (var_r - mean_r * (returns - mean_r))
    grad = -numerator / (n * std_r**3)

    grad = np.clip(grad, -5.0, 5.0)
    hess = np.abs(grad) + 0.01

    return grad.astype(np.float64), hess.astype(np.float64)


def lightgbm_sharpe_eval(
    y_true: np.ndarray, y_pred: np.ndarray, *, pnl: np.ndarray | None = None
) -> tuple[str, float, bool]:
    """LightGBM evaluation metric: actual (non-differentiable) Sharpe ratio.

    Returns (name, value, higher_is_better).
    """
    p = _stable_sigmoid(y_pred.astype(np.float64))
    positions = 2.0 * p - 1.0

    if pnl is not None and len(pnl) > 0:
        returns = positions * np.asarray(pnl, dtype=np.float64)
    else:
        direction = 2.0 * y_true.astype(np.float64) - 1.0
        returns = positions * direction

    mean_r = float(np.mean(returns))
    std_r = float(np.std(returns))
    if std_r < 1e-10:
        return "sharpe", 0.0, True

    sharpe = mean_r / std_r * np.sqrt(252)
    return "sharpe", float(sharpe), True


def weighted_logloss(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    sample_weight: np.ndarray | None = None,
) -> float:
    """Binary cross-entropy with per-sample weights.

    Critical trades (large PnL magnitude) get higher gradient weight, causing
    the model to focus learning on the most important decisions.

    Args:
        y_true: Binary labels (0 or 1).
        y_pred: Predicted probabilities.
        sample_weight: Per-sample importance weights. If None, uniform.

    Returns:
        Scalar loss value.
    """
    eps = 1e-15
    y_pred = np.clip(y_pred, eps, 1 - eps)
    loss = -(y_true * np.log(y_pred) + (1 - y_true) * np.log(1 - y_pred))

    if sample_weight is not None:
        loss = loss * sample_weight

    return float(np.mean(loss))


def xgboost_weighted_logloss_obj(sample_weight: np.ndarray | None = None):
    """XGBoost custom objective: weighted binary logloss.

    Returns gradient and hessian for XGBoost's custom objective interface.
    """

    def _obj(preds: np.ndarray, dtrain: Any) -> tuple[np.ndarray, np.ndarray]:
        y = dtrain.get_label()
        probs = 1.0 / (1.0 + np.exp(-preds))
        grad = probs - y  # d(logloss)/d(pred)
        hess = probs * (1.0 - probs)  # d²(logloss)/d(pred)²

        if sample_weight is not None:
            grad = grad * sample_weight
            hess = hess * sample_weight

        return grad.astype(np.float64), hess.astype(np.float64)

    return _obj


def compute_sample_weights(
    y: np.ndarray,
    pnl: np.ndarray | None = None,
    method: str = "return_magnitude",
) -> np.ndarray:
    """Compute per-sample weights for training.

    Args:
        y: Label array (regression targets or classification labels).
        pnl: P&L array for return-magnitude weighting.
        method: Weighting strategy.
            - "return_magnitude": weight ∝ |pnl|, normalized to mean=1.
            - "abs_target": weight ∝ |y|, normalized to mean=1 (for regression).
            - "inverse_class_frequency": balanced class weights.
            - "none": uniform weights (all 1.0).

    Returns:
        Array of sample weights, same length as y.
    """
    n = len(y)

    if method == "none":
        return np.ones(n, dtype=np.float64)

    if method == "return_magnitude":
        if pnl is None or len(pnl) == 0:
            return np.ones(n, dtype=np.float64)
        abs_pnl = np.abs(pnl)
        abs_pnl = np.clip(abs_pnl, 1e-8, None)
        weights = abs_pnl / abs_pnl.mean()
        weights = np.clip(weights, 0.1, 5.0)
        return weights.astype(np.float64)

    if method == "abs_target":
        # Weight ∝ |y| — amplifies large-magnitude regression targets.
        # Useful for Huber regression to prevent prediction collapse:
        # small-noise samples that carry no signal get low weight,
        # large-return samples that carry real information get high weight.
        y_f64 = np.asarray(y, dtype=np.float64).ravel()
        abs_y = np.abs(y_f64)
        abs_y = np.clip(abs_y, 1e-8, None)
        weights = abs_y / abs_y.mean()
        weights = np.clip(weights, 0.1, 5.0)
        return weights.astype(np.float64)

    if method == "inverse_class_frequency":
        classes = np.unique(y)
        if len(classes) == 0:
            return np.ones(n, dtype=np.float64)
        class_counts = {c: int(np.sum(y == c)) for c in classes}
        total = sum(class_counts.values())
        n_classes = len(classes)
        weights = np.ones(n, dtype=np.float64)
        for c in classes:
            if class_counts[c] > 0:
                weights[y == c] = total / (n_classes * class_counts[c])
        return weights

    return np.ones(n, dtype=np.float64)


def profit_factor_approx(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    pnl: np.ndarray | None = None,
) -> float:
    """Approximate profit factor from predictions.

    Profit Factor = Gross Profit / Gross Loss.
    Returns a scalar that the model tries to maximize.
    """
    if pnl is not None and len(pnl) > 0:
        returns = y_pred * pnl
    else:
        direction = 2.0 * y_true - 1.0
        returns = y_pred * direction

    gross_profit = float(np.sum(returns[returns > 0]))
    gross_loss = float(np.abs(np.sum(returns[returns < 0])))

    if gross_loss < 1e-10:
        return 100.0  # No losses → very high profit factor

    return gross_profit / gross_loss
