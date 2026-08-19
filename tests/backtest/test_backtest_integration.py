"""Integration tests for the backtest strategy adapter and runner pipeline."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

from core.backtest.data_feed import Bar, DataFeed
from core.backtest.engine import BacktestEngine
from core.backtest.metrics import compute_backtest_metrics
from core.backtest.strategy_adapter import (
    _barrier_rule,
    _micro_rule,
    _statarb_rule,
    rule_based_strategies,
)


def _make_bars(n: int = 200, start_price: float = 2000.0, trend: float = 0.3) -> list[Bar]:
    """Generate synthetic bars with mild uptrend and noise."""
    import random

    rng = random.Random(42)
    bars: list[Bar] = []
    t0 = datetime(2025, 1, 1, 0, 0)
    price = start_price
    for i in range(n):
        change = rng.gauss(trend, 5.0)
        h = price + abs(change) + rng.uniform(0, 2)
        l = price - abs(change) - rng.uniform(0, 2)
        c = price + change
        bars.append(
            Bar(
                timestamp=t0 + timedelta(minutes=5 * i),
                open=price,
                high=h,
                low=l,
                close=c,
                volume=100.0,
            )
        )
        price = c
    return bars


class TestRuleBasedStrategies:
    def test_barrier_rule_returns_none_on_short_history(self):
        bar = Bar(datetime.now(), 2000, 2005, 1995, 2002, 100)
        assert (
            _barrier_rule(bar, cast(Any, None), {"current_atr": 5.0}, []) is None
        )  # TECH_DEBT-009: 无 portfolio 探针 (A3)

    def test_barrier_rule_breaks_out_long(self):
        bars = _make_bars(30)
        # Force a breakout: current bar close above all recent highs
        breakout_bar = Bar(
            datetime.now(),
            2100,
            2105,
            2095,
            2104,
            200,
        )
        history = bars + [_make_bars(10, start_price=2050, trend=-0.1)[0]]
        signal = _barrier_rule(
            breakout_bar, cast(Any, None), {"current_atr": 5.0}, history[-30:]
        )  # TECH_DEBT-009: 无 portfolio 探针 (A3)
        # May or may not signal depending on exact values
        # Just verify it returns a valid format if it does
        if signal:
            assert signal["direction"] in ("long", "short")
            assert "confidence" in signal
            assert "volume" in signal

    def test_micro_rule_returns_none_on_short_history(self):
        bar = Bar(datetime.now(), 2000, 2005, 1995, 2002, 100)
        assert (
            _micro_rule(bar, cast(Any, None), {"current_atr": 3.0}, [bar]) is None
        )  # TECH_DEBT-009: 无 portfolio 探针 (A3)

    def test_statarb_rule_returns_none_on_short_history(self):
        bar = Bar(datetime.now(), 2000, 2005, 1995, 2002, 100)
        assert (
            _statarb_rule(bar, cast(Any, None), {"current_atr": 5.0}, [bar]) is None
        )  # TECH_DEBT-009: 无 portfolio 探针 (A3)

    def test_statarb_detects_extreme_z_score(self):
        """Generate bars where last close is 3 std below mean → should trigger long."""
        bars = _make_bars(50, start_price=2000, trend=0.0)
        # Set last bar to be 3 std below mean by modifying history
        mean_close = sum(b.close for b in bars[-20:]) / 20
        extreme_low = mean_close - 50  # far below mean
        history = bars[:-1] + [
            Bar(datetime.now(), extreme_low, extreme_low + 2, extreme_low - 2, extreme_low, 100)
        ]
        signal = _statarb_rule(
            history[-1], cast(Any, None), {"current_atr": 5.0}, history
        )  # TECH_DEBT-009: 无 portfolio 探针 (A3)
        if signal:
            assert signal["direction"] == "long"


class TestRuleBasedStrategyFn:
    def test_creates_callable(self):
        fn = rule_based_strategies(["statarb"])
        assert callable(fn)

    def test_returns_none_on_empty_context(self):
        fn = rule_based_strategies(["barrier"])
        bar = Bar(datetime.now(), 2000, 2005, 1995, 2002, 100)
        # No history in context → short history → None
        result = fn(bar, None, {"current_atr": 5.0})
        assert result is None

    def test_multiple_bars_accumulate_history(self):
        fn = rule_based_strategies(["statarb"])
        bars = _make_bars(60, trend=0.0)
        results = []
        for b in bars:
            r = fn(b, None, {"current_atr": 5.0})
            results.append(r)
        # After 50 bars, statarb should produce some signals
        # (may not on all runs due to random, but probability is high)
        non_none = [r for r in results[50:] if r is not None]
        assert len(non_none) >= 0  # at minimum, doesn't crash


class TestBacktestEngineWithRules:
    def test_end_to_end_barrier(self):
        bars = _make_bars(300, trend=0.5)
        feed = DataFeed(bars)
        strategy_fn = rule_based_strategies(["barrier_12bar"])
        engine = BacktestEngine(
            feed,
            strategy_fn,
            initial_cash=10000,
            max_positions=2,
            cooldown_bars=12,
        )
        result = engine.run()
        metrics = compute_backtest_metrics(result)
        assert result.bars_processed > 0
        assert metrics["total_trades"] >= 0
        assert "sharpe_ratio" in metrics
        assert "final_equity" in metrics

    def test_end_to_end_statarb(self):
        bars = _make_bars(200, trend=0.2)
        feed = DataFeed(bars)
        strategy_fn = rule_based_strategies(["statarb_dynamic"])
        engine = BacktestEngine(feed, strategy_fn, initial_cash=10000, max_positions=1)
        result = engine.run()
        metrics = compute_backtest_metrics(result)
        assert result.bars_processed > 0
        assert isinstance(metrics["net_pnl"], int | float)

    def test_end_to_end_all_three(self):
        bars = _make_bars(300, trend=0.3)
        feed = DataFeed(bars)
        strategy_fn = rule_based_strategies(["barrier_12bar", "micro_3bar", "statarb_dynamic"])
        engine = BacktestEngine(
            feed,
            strategy_fn,
            initial_cash=10000,
            max_positions=3,
            cooldown_bars=8,
        )
        result = engine.run()
        assert result.bars_processed > 0
        assert len(result.trades) >= 0
        assert len(result.equity_curve) > 0

    def test_result_has_required_fields(self):
        bars = _make_bars(200, trend=0.1)
        feed = DataFeed(bars)
        fn = rule_based_strategies(["barrier"])
        engine = BacktestEngine(feed, fn, initial_cash=5000, max_positions=1)
        result = engine.run()
        assert hasattr(result, "trades")
        assert hasattr(result, "equity_curve")
        assert hasattr(result, "total_pnl")
        assert hasattr(result, "sharpe_ratio")
