"""Backtest metrics — wraps core.metrics.financial_metrics for backtest results."""

from __future__ import annotations

import math
from typing import Any

from core.backtest.engine import BacktestResult


def compute_backtest_metrics(result: BacktestResult) -> dict[str, Any]:
    """Compute comprehensive metrics from a BacktestResult.

    Returns a dictionary suitable for serialization and comparison.
    """
    equity = [p["equity"] for p in result.equity_curve]
    returns: list[float] = []
    for i in range(1, len(equity)):
        if equity[i - 1] > 0:
            returns.append((equity[i] - equity[i - 1]) / equity[i - 1])

    # Mean return and standard deviation
    mean_ret = sum(returns) / len(returns) if returns else 0.0
    std_ret = 0.0
    if len(returns) > 1:
        variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
        std_ret = math.sqrt(variance)

    # Annualized Sharpe (5-min bars → 288 per day, 252 trading days)
    ann_factor = math.sqrt(252 * 288)
    sharpe = (mean_ret / std_ret * ann_factor) if std_ret > 0 else 0.0

    # Sortino ratio (downside deviation only)
    downside = [r for r in returns if r < 0]
    downside_std = 0.0
    if len(downside) > 1:
        d_var = sum((r - mean_ret) ** 2 for r in downside) / (len(downside) - 1)
        downside_std = math.sqrt(d_var)
    sortino = (mean_ret / downside_std * ann_factor) if downside_std > 0 else 0.0

    # Calmar ratio
    calmar = (
        (mean_ret * 252 * 288) / result.max_drawdown_pct if result.max_drawdown_pct > 0 else 0.0
    )

    # Omega ratio (threshold=0)
    gains = sum(r for r in returns if r > 0)
    losses = abs(sum(r for r in returns if r < 0))
    omega = gains / losses if losses > 0 else (999.0 if gains > 0 else 1.0)

    return {
        "total_trades": result.total_trades,
        "total_pnl": result.total_pnl,
        "total_cost": result.total_cost,
        "net_pnl": round(result.total_pnl - result.total_cost, 2),
        "win_rate": result.win_rate,
        "profit_factor": result.profit_factor,
        "max_drawdown_pct": result.max_drawdown_pct,
        "sharpe_ratio": round(sharpe, 4),
        "sortino_ratio": round(sortino, 4),
        "calmar_ratio": round(calmar, 4),
        "omega_ratio": round(omega, 4),
        "mean_return": round(mean_ret, 8),
        "volatility": round(std_ret, 8),
        "bars_processed": result.bars_processed,
        "start_time": result.start_time,
        "end_time": result.end_time,
        "final_equity": round(equity[-1], 2) if equity else 0.0,
        "return_pct": round((equity[-1] / equity[0] - 1) * 100, 2)
        if len(equity) > 1 and equity[0] > 0
        else 0.0,
    }
