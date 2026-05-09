"""Event-driven backtest engine.

Connects DataFeed → Strategy → ExecutionSimulator → VirtualPortfolio
in a chronological event loop.  Supports pluggable strategy callables
and SL/TP bar-level checking.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.backtest.data_feed import Bar, DataFeed
from core.backtest.execution_simulator import ExecutionSimulator
from core.backtest.portfolio import VirtualPortfolio


@dataclass
class BacktestResult:
    """Structured result from a backtest run."""

    trades: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    total_pnl: float = 0.0
    total_cost: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    bars_processed: int = 0
    start_time: str = ""
    end_time: str = ""


# Strategy signature: (Bar, VirtualPortfolio, dict) → dict | None
# Returns a trade signal dict: {direction, confidence, volume, sl_atr_mult, tp_atr_mult}
# Returns None for no trade.
StrategyFn = Callable[[Bar, VirtualPortfolio, dict[str, Any]], dict[str, Any] | None]


class BacktestEngine:
    """Event-driven backtest loop.

    Usage:
        feed = DataFeed.from_csv("data/ohlcv.csv")
        engine = BacktestEngine(feed, strategy_fn, initial_cash=10000)
        result = engine.run()
    """

    def __init__(
        self,
        data_feed: DataFeed,
        strategy: StrategyFn,
        *,
        initial_cash: float = 10000.0,
        simulator: ExecutionSimulator | None = None,
        portfolio: VirtualPortfolio | None = None,
        max_positions: int = 1,
        cooldown_bars: int = 0,
        sl_atr_mult: float = 2.0,
        tp_atr_mult: float = 3.5,
        atr_lookback: int = 14,
        strategy_context: dict[str, Any] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ):
        self.data_feed = data_feed
        self.strategy = strategy
        self.simulator = simulator or ExecutionSimulator()
        self.portfolio = portfolio or VirtualPortfolio(initial_cash=initial_cash)
        self.max_positions = max_positions
        self.cooldown_bars = cooldown_bars
        self.sl_atr_mult = sl_atr_mult
        self.tp_atr_mult = tp_atr_mult
        self.atr_lookback = atr_lookback
        self.strategy_context = strategy_context or {}
        self.progress_callback = progress_callback

        # Internal state
        self._bars_since_last_trade: int = 999
        self._atr_values: list[float] = []
        self._bar_index: int = 0

    def _compute_atr(self, bars: list[Bar]) -> float:
        """Simple rolling ATR(14) from bar ranges."""
        if len(bars) < 2:
            return 0.0
        tr_values: list[float] = []
        for i in range(1, min(len(bars), self.atr_lookback + 1)):
            b0 = bars[-i]
            b1 = bars[-i - 1]
            tr = max(
                b0.high - b0.low,
                abs(b0.high - b1.close),
                abs(b0.low - b1.close),
            )
            tr_values.append(tr)
        return sum(tr_values) / len(tr_values) if tr_values else 0.0

    def run(self) -> BacktestResult:
        """Execute the event-driven backtest loop."""
        bars = self.data_feed.bars
        if len(bars) < self.atr_lookback + 1:
            return BacktestResult(bars_processed=0)

        start_bar = self.atr_lookback
        bar_count = len(bars) - start_bar

        for i in range(start_bar, len(bars)):
            self._bar_index = i
            bar = bars[i]
            previous_bars = bars[: i + 1]

            # Compute current ATR
            current_atr = self._compute_atr(previous_bars)
            self.strategy_context["current_atr"] = current_atr

            # Check SL/TP for open positions (bar-level granularity)
            for pos in list(self.portfolio.positions):
                if pos.is_stopped(bar.low, bar.high):
                    exit_price = pos.exit_price_from_bar(bar.low, bar.high)
                    fill = self.simulator.execute_market(
                        timestamp=bar.timestamp,
                        symbol=bar.symbol,
                        side="sell" if pos.side == "buy" else "buy",
                        quantity=pos.quantity,
                        mid_price=exit_price,
                    )
                    self.portfolio.close_position(
                        pos,
                        exit_price=fill.price,
                        exit_time=bar.timestamp,
                        reason="sl_tp_hit",
                        commission=fill.commission,
                    )
                    self._bars_since_last_trade = 0

            # Run strategy
            if len(self.portfolio.positions) < self.max_positions:
                if self.cooldown_bars <= 0 or self._bars_since_last_trade >= self.cooldown_bars:
                    signal = self.strategy(bar, self.portfolio, self.strategy_context)
                    if signal and signal.get("direction") not in (None, "neutral"):
                        self._execute_signal(bar, signal, current_atr)
                        self._bars_since_last_trade = 0
                    else:
                        self._bars_since_last_trade += 1
                else:
                    self._bars_since_last_trade += 1

            # Record equity at each bar
            self.portfolio.record_equity(bar.timestamp, bar.close)

            if self.progress_callback and i % 100 == 0:
                self.progress_callback(i - start_bar, bar_count)

        return self._build_result(bars, start_bar)

    def _execute_signal(self, bar: Bar, signal: dict[str, Any], current_atr: float) -> None:
        """Execute a strategy signal."""
        direction = signal.get("direction", "neutral")
        _confidence = signal.get("confidence", 0.5)
        volume = signal.get("volume", 0.01)
        sl_mult = signal.get("sl_atr_mult", self.sl_atr_mult)
        tp_mult = signal.get("tp_atr_mult", self.tp_atr_mult)
        magic = signal.get("magic", 0)

        side = "buy" if direction == "long" else "sell"

        fill = self.simulator.execute_market(
            timestamp=bar.timestamp,
            symbol=bar.symbol,
            side=side,
            quantity=volume,
            mid_price=bar.close,
        )

        # Compute SL/TP levels
        sl_distance = current_atr * sl_mult
        tp_distance = current_atr * tp_mult
        if side == "buy":
            sl = round(fill.price - sl_distance, 5) if sl_distance > 0 else 0.0
            tp = round(fill.price + tp_distance, 5) if tp_distance > 0 else 0.0
        else:
            sl = round(fill.price + sl_distance, 5) if sl_distance > 0 else 0.0
            tp = round(fill.price - tp_distance, 5) if tp_distance > 0 else 0.0

        self.portfolio.open_position(
            symbol=bar.symbol,
            side=side,
            quantity=volume,
            price=fill.price,
            timestamp=bar.timestamp,
            stop_loss=sl,
            take_profit=tp,
            magic=magic,
            commission=fill.commission,
        )

    def _build_result(self, bars: list[Bar], start_bar: int) -> BacktestResult:
        """Compute final metrics from portfolio state."""
        # Force-close any remaining positions at last bar close
        last_bar = bars[-1]
        for pos in list(self.portfolio.positions):
            self.portfolio.close_position(
                pos,
                exit_price=last_bar.close,
                exit_time=last_bar.timestamp,
                reason="end_of_run",
                commission=0.0,
            )

        # Compute holding duration for each trade
        for t in self.portfolio.closed_trades:
            _entry_dt = datetime.fromisoformat(t["entry_time"])
            _exit_dt = datetime.fromisoformat(t["exit_time"])
            t["holding_bars"] = 1  # real bar count needs bar-index tracking

        # Max drawdown from equity curve
        max_dd = 0.0
        peak = 0.0
        for point in self.portfolio.equity_curve:
            eq = point["equity"]
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (peak - eq) / peak
                if dd > max_dd:
                    max_dd = dd

        # Sharpe from equity returns (approximate)
        eq_values = [p["equity"] for p in self.portfolio.equity_curve]
        returns: list[float] = []
        for j in range(1, len(eq_values)):
            if eq_values[j - 1] > 0:
                returns.append((eq_values[j] - eq_values[j - 1]) / eq_values[j - 1])

        import math

        mean_ret = sum(returns) / len(returns) if returns else 0.0
        std_ret = 0.0
        if len(returns) > 1:
            variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
            std_ret = math.sqrt(variance)
        sharpe = (mean_ret / std_ret * math.sqrt(252 * 288)) if std_ret > 0 else 0.0

        # Profit factor
        gross_profit = sum(t["pnl"] for t in self.portfolio.closed_trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in self.portfolio.closed_trades if t["pnl"] < 0))
        profit_factor = (
            gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        )

        return BacktestResult(
            trades=self.portfolio.closed_trades,
            equity_curve=self.portfolio.equity_curve,
            total_pnl=round(self.portfolio.total_pnl, 2),
            total_cost=round(self.simulator.total_cost, 2),
            win_rate=round(self.portfolio.win_rate, 4),
            total_trades=self.portfolio.total_trades,
            max_drawdown_pct=round(max_dd, 4),
            sharpe_ratio=round(sharpe, 4),
            profit_factor=round(profit_factor, 2),
            bars_processed=len(bars) - start_bar,
            start_time=bars[start_bar].timestamp.isoformat() if bars else "",
            end_time=bars[-1].timestamp.isoformat() if bars else "",
        )
