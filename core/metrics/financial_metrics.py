"""Unified financial metrics for trading strategy evaluation.

Single source of truth for Sharpe, Sortino, Calmar, max drawdown, profit factor,
win rate, and directional accuracy. All functions are pure and accept numpy arrays
or plain lists — no dependency on PyTorch or any specific model framework.

Usage:
    from core.metrics import compute_metrics

    results = compute_metrics(
        returns=[0.01, -0.005, 0.02, ...],
        predictions=[1, 0, 2, ...],
        targets=[1, 1, 2, ...],
    )
    # → {"sharpe_ratio": 1.2, "sortino_ratio": 1.8, "max_drawdown_pct": 5.3, ...}
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def _as_array(values: list[float] | np.ndarray) -> np.ndarray:
    return np.asarray(values, dtype=np.float64)


# ═══════════════════════════════════════════════════════════════════════
# Return-based metrics
# ═══════════════════════════════════════════════════════════════════════


def max_drawdown(returns: list[float] | np.ndarray) -> tuple[float, float]:
    """Compute max drawdown from a return series.

    Args:
        returns: Period returns (fractional, e.g. 0.01 = 1%).

    Returns:
        (max_drawdown_absolute, max_drawdown_percent) where percent is 0-100.
    """
    r = _as_array(returns)
    equity = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / peak
    max_dd_pct = float(dd.max() * 100)
    max_dd_abs = float((peak - equity).max())
    return max_dd_abs, max_dd_pct


def annualized_sharpe(
    returns: list[float] | np.ndarray,
    periods_per_year: int = 252,
    risk_free_rate: float = 0.0,
) -> float:
    """Annualized Sharpe ratio.

    Args:
        returns: Period returns (daily returns → periods_per_year=252).
        periods_per_year: Scaling factor (252 for daily, 52 for weekly, 12 for monthly).
        risk_free_rate: Annual risk-free rate (default 0 for crypto/forex).
    """
    r = _as_array(returns)
    if len(r) < 2:
        return 0.0
    excess = r - risk_free_rate / periods_per_year
    mean_excess = float(excess.mean())
    std_excess = float(excess.std(ddof=1))
    if std_excess == 0:
        return 0.0
    return mean_excess / std_excess * math.sqrt(periods_per_year)


def annualized_sortino(
    returns: list[float] | np.ndarray,
    periods_per_year: int = 252,
    risk_free_rate: float = 0.0,
    target_return: float = 0.0,
) -> float:
    """Annualized Sortino ratio (downside-only volatility).

    Args:
        returns: Period returns.
        periods_per_year: Scaling factor.
        risk_free_rate: Annual risk-free rate.
        target_return: Minimum acceptable return per period (default 0).
    """
    r = _as_array(returns)
    if len(r) < 2:
        return 0.0
    excess = r - risk_free_rate / periods_per_year
    mean_excess = float(excess.mean())
    downside = excess[excess < target_return]
    if len(downside) == 0:
        return 0.0 if mean_excess <= 0 else float("inf")
    down_std = float(downside.std(ddof=1))
    if down_std == 0:
        return 0.0
    return mean_excess / down_std * math.sqrt(periods_per_year)


def calmar_ratio(
    returns: list[float] | np.ndarray,
    periods_per_year: int = 252,
) -> float:
    """Calmar ratio: annualized return / max drawdown %.

    Args:
        returns: Period returns.
        periods_per_year: Scaling factor.
    """
    r = _as_array(returns)
    if len(r) < 2:
        return 0.0
    annual_return = float(r.mean()) * periods_per_year
    _, max_dd_pct = max_drawdown(r)
    if max_dd_pct == 0:
        return 0.0 if annual_return <= 0 else float("inf")
    return annual_return / (max_dd_pct / 100.0)


def omega_ratio(
    returns: list[float] | np.ndarray,
    threshold: float = 0.0,
) -> float:
    """Omega ratio: probability-weighted gain / loss above threshold."""
    r = _as_array(returns)
    gains = r[r > threshold].sum()
    losses = abs(r[r < threshold].sum())
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return float(gains / losses)


# ═══════════════════════════════════════════════════════════════════════
# Trade-based metrics
# ═══════════════════════════════════════════════════════════════════════


def win_rate(pnls: list[float] | np.ndarray) -> float:
    """Fraction of trades with positive P&L."""
    p = _as_array(pnls)
    if len(p) == 0:
        return 0.0
    return float((p > 0).mean())


def profit_factor(wins: list[float], losses: list[float]) -> float:
    """Gross profit / gross loss."""
    total_wins = sum(wins) if wins else 0.0
    total_losses = abs(sum(losses)) if losses else 0.0
    if total_losses == 0:
        return float("inf") if total_wins > 0 else 0.0
    return total_wins / total_losses


def profit_factor_from_pnls(pnls: list[float] | np.ndarray) -> float:
    """Compute profit factor from raw P&L array."""
    p = _as_array(pnls)
    wins = p[p > 0]
    losses = p[p < 0]
    return profit_factor(wins.tolist(), losses.tolist())


def expectancy(pnls: list[float] | np.ndarray) -> float:
    """Average P&L per trade."""
    p = _as_array(pnls)
    if len(p) == 0:
        return 0.0
    return float(p.mean())


# ═══════════════════════════════════════════════════════════════════════
# Classification-based metrics
# ═══════════════════════════════════════════════════════════════════════


def directional_accuracy(predictions: np.ndarray, targets: np.ndarray) -> float:
    """Fraction of correct directional calls.

    Maps class labels to direction {-1, 0, +1} and compares signs.
    For 3-class {0,1,2}: maps 0→-1, 1→0, 2→+1.
    """
    p = np.asarray(predictions)
    t = np.asarray(targets)
    p_dir = np.where(p == 2, 1, np.where(p == 0, -1, 0))
    t_dir = np.where(t == 2, 1, np.where(t == 0, -1, 0))
    return float((p_dir == t_dir).mean())


def precision_recall_f1(
    predictions: np.ndarray, targets: np.ndarray, pos_label: int = 1
) -> dict[str, float]:
    tp = float(((predictions == pos_label) & (targets == pos_label)).sum())
    fp = float(((predictions == pos_label) & (targets != pos_label)).sum())
    fn = float(((predictions != pos_label) & (targets == pos_label)).sum())

    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0

    return {"precision": prec, "recall": rec, "f1": f1}


# ═══════════════════════════════════════════════════════════════════════
# Composite / unified
# ═══════════════════════════════════════════════════════════════════════


def compute_metrics(
    *,
    returns: list[float] | np.ndarray | None = None,
    pnls: list[float] | np.ndarray | None = None,
    predictions: np.ndarray | None = None,
    targets: np.ndarray | None = None,
    periods_per_year: int = 252,
) -> dict[str, Any]:
    """Compute all standard financial metrics from available inputs.

    At least one of `returns` or `pnls` should be provided.

    Args:
        returns: Period returns (fractional).
        pnls: Raw P&L values per trade/period.
        predictions: Model class predictions (for classification metrics).
        targets: True class labels.
        periods_per_year: Annualization factor.

    Returns:
        Dict of computed metrics. Missing metrics are 0.0.
    """
    metrics: dict[str, Any] = {}

    r = _as_array(returns) if returns is not None else None
    p = _as_array(pnls) if pnls is not None else None

    # Derive returns from P&L if needed (assume fixed equity base)
    if r is None and p is not None:
        equity = 100_000.0
        r = p / equity

    if r is not None and len(r) >= 2:
        dd_abs, dd_pct = max_drawdown(r)
        metrics["sharpe_ratio"] = round(annualized_sharpe(r, periods_per_year), 6)
        metrics["sortino_ratio"] = round(annualized_sortino(r, periods_per_year), 6)
        metrics["calmar_ratio"] = round(calmar_ratio(r, periods_per_year), 6)
        metrics["omega_ratio"] = round(omega_ratio(r), 6)
        metrics["max_drawdown"] = round(dd_abs, 4)
        metrics["max_drawdown_pct"] = round(dd_pct, 4)
    else:
        for key in (
            "sharpe_ratio",
            "sortino_ratio",
            "calmar_ratio",
            "omega_ratio",
            "max_drawdown",
            "max_drawdown_pct",
        ):
            metrics.setdefault(key, 0.0)

    if p is not None and len(p) > 0:
        metrics["win_rate"] = round(win_rate(p), 6)
        metrics["profit_factor"] = round(profit_factor_from_pnls(p), 6)
        metrics["expectancy"] = round(expectancy(p), 6)
        metrics["total_pnl"] = round(float(p.sum()), 4)
    else:
        for key in ("win_rate", "profit_factor", "expectancy", "total_pnl"):
            metrics.setdefault(key, 0.0)

    if predictions is not None and targets is not None:
        metrics["direction_accuracy"] = round(directional_accuracy(predictions, targets), 6)
        cls = precision_recall_f1(predictions, targets)
        metrics.update({k: round(v, 6) for k, v in cls.items()})
        metrics["accuracy"] = round(float((predictions == targets).mean()), 6)

    return metrics
