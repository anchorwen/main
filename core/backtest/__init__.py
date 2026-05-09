"""Event-driven backtest framework.

Reuses existing execution contracts (OrderState, Fill, ExecutionQualityMetric)
and financial metrics to provide a unified backtest experience.
"""

from core.backtest.data_feed import Bar, DataFeed
from core.backtest.engine import BacktestEngine, BacktestResult
from core.backtest.execution_simulator import ExecutionSimulator
from core.backtest.metrics import compute_backtest_metrics
from core.backtest.portfolio import VirtualPortfolio, VirtualPosition

__all__ = [
    "DataFeed",
    "Bar",
    "ExecutionSimulator",
    "VirtualPortfolio",
    "VirtualPosition",
    "BacktestEngine",
    "BacktestResult",
    "compute_backtest_metrics",
]
