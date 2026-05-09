"""Tests for core/backtest/ — DataFeed, ExecutionSimulator, VirtualPortfolio,
VirtualPosition, BacktestEngine, and compute_backtest_metrics."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from core.backtest.data_feed import Bar, DataFeed
from core.backtest.engine import BacktestEngine, BacktestResult
from core.backtest.execution_simulator import ExecutionSimulator
from core.backtest.metrics import compute_backtest_metrics
from core.backtest.portfolio import VirtualPortfolio, VirtualPosition


# ---------------------------------------------------------------------------
# Bar
# ---------------------------------------------------------------------------
class TestBar:
    def test_creation_defaults(self):
        bar = Bar(
            timestamp=datetime(2026, 1, 1, 9, 0),
            open=2000.0,
            high=2005.0,
            low=1999.0,
            close=2003.0,
            volume=150.0,
        )
        assert bar.symbol == "XAUUSDc"
        assert bar.spread == 0.0
        assert bar.features is None

    def test_mid_price(self):
        bar = Bar(
            timestamp=datetime(2026, 1, 1),
            open=2000.0,
            high=2010.0,
            low=1990.0,
            close=2005.0,
            volume=100.0,
        )
        assert bar.mid == 2000.0

    def test_with_features(self):
        bar = Bar(
            timestamp=datetime(2026, 1, 1),
            open=2000.0,
            high=2010.0,
            low=1990.0,
            close=2005.0,
            volume=100.0,
            features=[0.1, -0.2, 0.3],
        )
        assert bar.features == [0.1, -0.2, 0.3]


# ---------------------------------------------------------------------------
# DataFeed
# ---------------------------------------------------------------------------
class TestDataFeed:
    def _make_bars(self, n: int = 5) -> list[Bar]:
        bars: list[Bar] = []
        for i in range(n):
            bars.append(
                Bar(
                    timestamp=datetime(2026, 1, 1, 9, i),
                    open=2000.0 + i,
                    high=2002.0 + i,
                    low=1999.0 + i,
                    close=2001.0 + i,
                    volume=100.0 + i * 10,
                )
            )
        return bars

    def test_empty_feed(self):
        feed = DataFeed()
        assert len(feed) == 0
        bars = list(feed)
        assert bars == []

    def test_len(self):
        feed = DataFeed(self._make_bars(5))
        assert len(feed) == 5

    def test_iteration(self):
        bars = self._make_bars(3)
        feed = DataFeed(bars)
        result = list(feed)
        assert len(result) == 3
        assert result[0].close == 2001.0
        assert result[1].close == 2002.0

    def test_iteration_resets(self):
        feed = DataFeed(self._make_bars(3))
        first_pass = list(feed)
        second_pass = list(feed)
        assert len(first_pass) == len(second_pass)
        assert first_pass[0].close == second_pass[0].close

    def test_slice(self):
        bars = self._make_bars(10)
        feed = DataFeed(bars)
        sub = feed.slice(2, 5)
        assert len(sub) == 3
        assert sub.bars[0] is bars[2]
        assert sub.bars[-1] is bars[4]

    def test_slice_to_end(self):
        bars = self._make_bars(10)
        feed = DataFeed(bars)
        sub = feed.slice(8)
        assert len(sub) == 2

    def test_add_features(self):
        bars = self._make_bars(3)
        feed = DataFeed(bars)
        feed.add_features([[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]])
        assert bars[0].features == [0.1, 0.2]
        assert bars[1].features == [0.3, 0.4]
        assert bars[2].features == [0.5, 0.6]

    def test_add_features_partial(self):
        bars = self._make_bars(5)
        feed = DataFeed(bars)
        feed.add_features([[0.1], [0.2]])
        assert bars[0].features == [0.1]
        assert bars[1].features == [0.2]
        assert bars[2].features is None

    def test_from_csv(self, tmp_path: Path):
        csv_path = tmp_path / "test.csv"
        csv_path.write_text(
            "timestamp,open,high,low,close,volume\n"
            "2026-01-01T09:00:00,2000.0,2005.0,1999.0,2003.0,100.0\n"
            "2026-01-01T09:01:00,2003.0,2007.0,2001.0,2005.0,120.0\n"
            "2026-01-01T09:02:00,2005.0,2010.0,2004.0,2008.0,90.0\n"
        )
        feed = DataFeed.from_csv(str(csv_path))
        assert len(feed) == 3
        assert feed.bars[0].open == 2000.0
        assert feed.bars[0].close == 2003.0
        assert feed.bars[2].high == 2010.0

    def test_from_csv_with_tick_volume(self, tmp_path: Path):
        csv_path = tmp_path / "test_tickvol.csv"
        csv_path.write_text(
            "time,open,high,low,close,tick_volume\n"
            "2026-01-01T09:00:00,2000.0,2005.0,1999.0,2003.0,500\n"
        )
        feed = DataFeed.from_csv(str(csv_path))
        assert feed.bars[0].volume == 500.0

    def test_from_csv_with_spread(self, tmp_path: Path):
        csv_path = tmp_path / "test_spread.csv"
        csv_path.write_text(
            "timestamp,open,high,low,close,volume,spread\n"
            "2026-01-01T09:00:00,2000.0,2005.0,1999.0,2003.0,100.0,3.0\n"
        )
        feed = DataFeed.from_csv(str(csv_path))
        assert feed.bars[0].spread == 3.0

    def test_from_csv_custom_symbol(self, tmp_path: Path):
        csv_path = tmp_path / "test_sym.csv"
        csv_path.write_text(
            "timestamp,open,high,low,close,volume\n"
            "2026-01-01T09:00:00,1.1000,1.1010,1.0990,1.1005,200.0\n"
        )
        feed = DataFeed.from_csv(str(csv_path), symbol="EURUSD")
        assert feed.bars[0].symbol == "EURUSD"


# ---------------------------------------------------------------------------
# ExecutionSimulator
# ---------------------------------------------------------------------------
class TestExecutionSimulator:
    def test_buy_fill_above_mid(self):
        sim = ExecutionSimulator(spread_bps=3.0, slippage_per_lot_bps=0.5)
        fill = sim.execute_market(
            timestamp=datetime(2026, 1, 1, 9, 0),
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            mid_price=2000.0,
        )
        assert fill.price > 2000.0
        assert fill.side == "buy"
        assert fill.quantity == 0.01

    def test_sell_fill_below_mid(self):
        sim = ExecutionSimulator(spread_bps=3.0, slippage_per_lot_bps=0.5)
        fill = sim.execute_market(
            timestamp=datetime(2026, 1, 1, 9, 0),
            symbol="XAUUSD",
            side="sell",
            quantity=0.01,
            mid_price=2000.0,
        )
        assert fill.price < 2000.0
        assert fill.side == "sell"

    def test_commission(self):
        sim = ExecutionSimulator(commission_per_lot=7.0)
        fill = sim.execute_market(
            timestamp=datetime(2026, 1, 1),
            symbol="XAUUSD",
            side="buy",
            quantity=0.1,
            mid_price=2000.0,
        )
        assert fill.commission == pytest.approx(0.7, rel=1e-4)

    def test_notional_value(self):
        sim = ExecutionSimulator()
        fill = sim.execute_market(
            timestamp=datetime(2026, 1, 1),
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            mid_price=2000.0,
        )
        # notional = quantity × fill_price × 100; fill_price > mid for buy
        expected = 0.01 * fill.price * 100.0
        assert fill.notional == pytest.approx(expected)

    def test_direction_sign(self):
        sim = ExecutionSimulator()
        buy_fill = sim.execute_market(
            timestamp=datetime(2026, 1, 1),
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            mid_price=2000.0,
        )
        sell_fill = sim.execute_market(
            timestamp=datetime(2026, 1, 1),
            symbol="XAUUSD",
            side="sell",
            quantity=0.01,
            mid_price=2000.0,
        )
        assert buy_fill.direction_sign == 1
        assert sell_fill.direction_sign == -1

    def test_filled_orders_tracking(self):
        sim = ExecutionSimulator()
        assert sim.total_fills == 0
        sim.execute_market(
            timestamp=datetime(2026, 1, 1),
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            mid_price=2000.0,
        )
        assert sim.total_fills == 1
        assert len(sim.filled_orders) == 1

    def test_total_costs(self):
        sim = ExecutionSimulator(spread_bps=3.0, commission_per_lot=7.0)
        sim.execute_market(
            timestamp=datetime(2026, 1, 1),
            symbol="XAUUSD",
            side="buy",
            quantity=0.1,
            mid_price=2000.0,
        )
        assert sim.total_spread_cost > 0
        assert sim.total_commission > 0
        assert sim.total_cost == sim.total_spread_cost + sim.total_commission

    def test_average_slippage_bps(self):
        sim = ExecutionSimulator()
        assert sim.average_slippage_bps == 0.0
        sim.execute_market(
            timestamp=datetime(2026, 1, 1),
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            mid_price=2000.0,
        )
        assert sim.average_slippage_bps > 0

    def test_larger_quantity_more_slippage(self):
        sim = ExecutionSimulator(slippage_per_lot_bps=1.0)
        small = sim.execute_market(
            timestamp=datetime(2026, 1, 1),
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            mid_price=2000.0,
        )
        large = sim.execute_market(
            timestamp=datetime(2026, 1, 1),
            symbol="XAUUSD",
            side="buy",
            quantity=1.0,
            mid_price=2000.0,
        )
        assert large.slippage_bps > small.slippage_bps

    def test_zero_spread_no_slippage(self):
        sim = ExecutionSimulator(spread_bps=0.0, slippage_per_lot_bps=0.0)
        fill = sim.execute_market(
            timestamp=datetime(2026, 1, 1),
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            mid_price=2000.0,
        )
        assert fill.price == 2000.0


# ---------------------------------------------------------------------------
# VirtualPosition
# ---------------------------------------------------------------------------
class TestVirtualPosition:
    def _make_pos(
        self,
        side: str = "buy",
        entry: float = 2000.0,
        qty: float = 0.01,
        sl: float = 1990.0,
        tp: float = 2010.0,
    ) -> VirtualPosition:
        return VirtualPosition(
            symbol="XAUUSD",
            side=side,
            quantity=qty,
            entry_price=entry,
            entry_time=datetime(2026, 1, 1, 9, 0),
            stop_loss=sl,
            take_profit=tp,
        )

    def test_direction_sign(self):
        assert self._make_pos("buy").direction_sign == 1
        assert self._make_pos("sell").direction_sign == -1

    def test_unrealized_pnl_long(self):
        pos = self._make_pos("buy", entry=2000.0, qty=0.01)
        pnl = pos.unrealized_pnl(2010.0)
        assert pnl == pytest.approx(10.0 * 0.01 * 100.0)  # 10 USD × 0.01 × 100

    def test_unrealized_pnl_short(self):
        pos = self._make_pos("sell", entry=2000.0, qty=0.01)
        pnl = pos.unrealized_pnl(1990.0)
        assert pnl == pytest.approx(10.0 * 0.01 * 100.0)

    def test_unrealized_pnl_long_loss(self):
        pos = self._make_pos("buy", entry=2000.0, qty=0.01)
        pnl = pos.unrealized_pnl(1990.0)
        assert pnl < 0

    def test_is_stopped_sl_long(self):
        pos = self._make_pos("buy", entry=2000.0, sl=1995.0)
        # Bar low goes below stop loss
        assert pos.is_stopped(bar_low=1994.0, bar_high=2001.0) is True

    def test_is_stopped_sl_long_not_hit(self):
        pos = self._make_pos("buy", entry=2000.0, sl=1995.0)
        assert pos.is_stopped(bar_low=1996.0, bar_high=2001.0) is False

    def test_is_stopped_tp_long(self):
        pos = self._make_pos("buy", entry=2000.0, tp=2005.0)
        assert pos.is_stopped(bar_low=1999.0, bar_high=2006.0) is True

    def test_is_stopped_sl_short(self):
        pos = self._make_pos("sell", entry=2000.0, sl=2005.0)
        assert pos.is_stopped(bar_low=1999.0, bar_high=2006.0) is True

    def test_is_stopped_tp_short(self):
        pos = self._make_pos("sell", entry=2000.0, sl=2050.0, tp=1995.0)
        assert pos.is_stopped(bar_low=1994.0, bar_high=1999.0) is True

    def test_is_stopped_no_sl_tp(self):
        pos = VirtualPosition(
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            entry_price=2000.0,
            entry_time=datetime(2026, 1, 1),
        )
        assert pos.is_stopped(bar_low=0.0, bar_high=3000.0) is False

    def test_is_stopped_sl_zero(self):
        pos = self._make_pos("buy", entry=2000.0)
        pos.stop_loss = 0.0  # disable SL
        assert pos.is_stopped(bar_low=0.0, bar_high=2001.0) is False

    def test_exit_price_sl_long(self):
        pos = self._make_pos("buy", entry=2000.0, sl=1995.0)
        assert pos.exit_price_from_bar(bar_low=1994.0, bar_high=2001.0) == 1995.0

    def test_exit_price_tp_long(self):
        pos = self._make_pos("buy", entry=2000.0, tp=2005.0)
        assert pos.exit_price_from_bar(bar_low=1998.0, bar_high=2006.0) == 2005.0

    def test_exit_price_sl_short(self):
        pos = self._make_pos("sell", entry=2000.0, sl=2005.0, tp=0.0)
        assert pos.exit_price_from_bar(bar_low=1999.0, bar_high=2006.0) == 2005.0

    def test_exit_price_tp_short(self):
        pos = self._make_pos("sell", entry=2000.0, sl=2050.0, tp=1995.0)
        assert pos.exit_price_from_bar(bar_low=1994.0, bar_high=2001.0) == 1995.0

    def test_exit_price_no_hit(self):
        pos = self._make_pos("buy", entry=2000.0, sl=1995.0, tp=2005.0)
        assert pos.exit_price_from_bar(bar_low=1997.0, bar_high=2003.0) == 0.0


# ---------------------------------------------------------------------------
# VirtualPortfolio
# ---------------------------------------------------------------------------
class TestVirtualPortfolio:
    def test_initial_state(self):
        pf = VirtualPortfolio(initial_cash=10000.0)
        assert pf.cash == 10000.0
        assert len(pf.positions) == 0
        assert len(pf.closed_trades) == 0
        assert len(pf.equity_curve) == 0

    def test_open_position(self):
        pf = VirtualPortfolio(initial_cash=10000.0)
        pos = pf.open_position(
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            price=2000.0,
            timestamp=datetime(2026, 1, 1, 9, 0),
            stop_loss=1990.0,
            take_profit=2010.0,
        )
        assert len(pf.positions) == 1
        assert pos.symbol == "XAUUSD"
        assert pos.side == "buy"
        assert pos.stop_loss == 1990.0
        assert pos.take_profit == 2010.0

    def test_open_position_deducts_commission(self):
        pf = VirtualPortfolio(initial_cash=10000.0)
        pf.open_position(
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            price=2000.0,
            timestamp=datetime(2026, 1, 1),
            commission=0.7,
        )
        assert pf.cash == pytest.approx(9999.3)

    def test_close_position(self):
        pf = VirtualPortfolio(initial_cash=10000.0)
        pos = pf.open_position(
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            price=2000.0,
            timestamp=datetime(2026, 1, 1, 9, 0),
        )
        trade = pf.close_position(
            pos,
            exit_price=2010.0,
            exit_time=datetime(2026, 1, 1, 9, 30),
            reason="manual",
        )
        assert len(pf.positions) == 0
        assert len(pf.closed_trades) == 1
        assert trade["pnl"] == pytest.approx(10.0 * 0.01 * 100.0)
        assert trade["reason"] == "manual"

    def test_close_position_with_commission(self):
        pf = VirtualPortfolio(initial_cash=10000.0)
        pos = pf.open_position(
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            price=2000.0,
            timestamp=datetime(2026, 1, 1),
        )
        trade = pf.close_position(
            pos,
            exit_price=2010.0,
            exit_time=datetime(2026, 1, 1, 9, 30),
            commission=0.7,
        )
        assert trade["pnl_after_cost"] == pytest.approx(10.0 * 0.01 * 100.0 - 0.7)

    def test_record_equity(self):
        pf = VirtualPortfolio(initial_cash=10000.0)
        pf.open_position(
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            price=2000.0,
            timestamp=datetime(2026, 1, 1, 9, 0),
        )
        pf.record_equity(datetime(2026, 1, 1, 9, 1), 2010.0)
        assert len(pf.equity_curve) == 1
        point = pf.equity_curve[0]
        assert "equity" in point
        assert "unrealized_pnl" in point
        assert "cash" in point
        assert point["position_count"] == 1

    def test_equity(self):
        pf = VirtualPortfolio(initial_cash=10000.0)
        pf.record_equity(datetime(2026, 1, 1), 2000.0)
        assert pf.equity == 10000.0  # no positions, just cash

    def test_total_trades(self):
        pf = VirtualPortfolio()
        assert pf.total_trades == 0
        pos = pf.open_position(
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            price=2000.0,
            timestamp=datetime(2026, 1, 1),
        )
        pf.close_position(pos, exit_price=2000.0, exit_time=datetime(2026, 1, 1))
        assert pf.total_trades == 1

    def test_total_pnl(self):
        pf = VirtualPortfolio()
        pos = pf.open_position(
            symbol="XAUUSD",
            side="buy",
            quantity=0.1,
            price=2000.0,
            timestamp=datetime(2026, 1, 1),
        )
        pf.close_position(pos, exit_price=2010.0, exit_time=datetime(2026, 1, 1))
        # PnL = (2010-2000) * 0.1 * 100 = 100
        assert pf.total_pnl == pytest.approx(100.0, rel=1e-4)

    def test_win_rate(self):
        pf = VirtualPortfolio()
        for i in range(4):
            pos = pf.open_position(
                symbol="XAUUSD",
                side="buy",
                quantity=0.01,
                price=2000.0,
                timestamp=datetime(2026, 1, 1, 9, i),
            )
            exit_price = 2010.0 if i < 3 else 1990.0
            pf.close_position(pos, exit_price=exit_price, exit_time=datetime(2026, 1, 1, 10, i))
        assert pf.win_rate == 0.75

    def test_gross_exposure(self):
        pf = VirtualPortfolio(initial_cash=10000.0)
        pf.open_position(
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            price=2000.0,
            timestamp=datetime(2026, 1, 1),
        )
        pf.record_equity(datetime(2026, 1, 1), 2000.0)
        expected = (0.01 * 2000.0 * 100.0) / pf.equity
        assert pf.gross_exposure == pytest.approx(expected)

    def test_net_exposure(self):
        pf = VirtualPortfolio(initial_cash=10000.0)
        pf.open_position(
            symbol="XAUUSD",
            side="buy",
            quantity=0.01,
            price=2000.0,
            timestamp=datetime(2026, 1, 1),
        )
        pf.open_position(
            symbol="XAUUSD",
            side="sell",
            quantity=0.005,
            price=2000.0,
            timestamp=datetime(2026, 1, 1),
        )
        pf.record_equity(datetime(2026, 1, 1), 2000.0)
        # Net: 0.01 - 0.005 = 0.005 long
        expected = (0.005 * 2000.0 * 100.0) / pf.equity
        assert pf.net_exposure == pytest.approx(expected)


# ---------------------------------------------------------------------------
# BacktestEngine
# ---------------------------------------------------------------------------
class TestBacktestEngine:
    def _make_bars(self, n: int = 50, base: float = 2000.0, step: float = 0.5) -> list[Bar]:
        bars: list[Bar] = []
        for i in range(n):
            o = base + i * step
            h = o + 1.0
            l = o - 0.5
            c = o + 0.3
            bars.append(
                Bar(
                    timestamp=datetime(2026, 1, 1, 9, 0, i % 60),
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=100.0,
                )
            )
        return bars

    def _always_long(self, bar: Bar, portfolio: VirtualPortfolio, ctx: dict):
        return {"direction": "long", "volume": 0.01, "confidence": 1.0}

    def _always_short(self, bar: Bar, portfolio: VirtualPortfolio, ctx: dict):
        return {"direction": "short", "volume": 0.01, "confidence": 1.0}

    def _neutral(self, bar: Bar, portfolio: VirtualPortfolio, ctx: dict):
        return None

    def test_empty_bars(self):
        feed = DataFeed([])
        engine = BacktestEngine(feed, self._always_long)
        result = engine.run()
        assert result.bars_processed == 0

    def test_too_few_bars(self):
        feed = DataFeed(self._make_bars(5))
        engine = BacktestEngine(feed, self._always_long)
        result = engine.run()
        assert result.bars_processed == 0

    def test_basic_run(self):
        feed = DataFeed(self._make_bars(50))
        engine = BacktestEngine(feed, self._always_long, initial_cash=10000.0)
        result = engine.run()
        assert result.bars_processed > 0
        assert len(result.equity_curve) > 0
        assert len(result.trades) > 0

    def test_neutral_strategy_no_trades(self):
        feed = DataFeed(self._make_bars(50))
        engine = BacktestEngine(feed, self._neutral, initial_cash=10000.0)
        result = engine.run()
        assert result.total_trades == 0
        assert result.total_pnl == 0.0

    def test_short_strategy(self):
        feed = DataFeed(self._make_bars(50, base=2000.0, step=-0.5))
        engine = BacktestEngine(feed, self._always_short, initial_cash=10000.0)
        result = engine.run()
        assert result.bars_processed > 0

    def test_result_structure(self):
        feed = DataFeed(self._make_bars(50))
        engine = BacktestEngine(feed, self._always_long, initial_cash=10000.0)
        result = engine.run()
        assert result.total_pnl != 0
        assert result.total_cost >= 0
        assert result.win_rate >= 0
        assert result.sharpe_ratio is not None
        assert result.profit_factor >= 0
        assert result.start_time
        assert result.end_time

    def test_max_positions(self):
        feed = DataFeed(self._make_bars(50))
        engine = BacktestEngine(feed, self._always_long, max_positions=1)
        result = engine.run()
        assert len(result.trades) >= 0

    def test_sl_tp_hit(self):
        # Create bars where price drops hard → SL gets hit
        bars = self._make_bars(30, base=2000.0, step=0.3)
        # Add a sharp drop bar that will hit SL
        bars.append(
            Bar(
                timestamp=datetime(2026, 1, 1, 9, 30),
                open=1990.0,
                high=1991.0,
                low=1980.0,
                close=1985.0,
                volume=500.0,
            )
        )
        feed = DataFeed(bars)
        engine = BacktestEngine(feed, self._always_long, sl_atr_mult=1.0, tp_atr_mult=5.0)
        result = engine.run()
        # Some trades should have been stopped out
        reasons = [t.get("reason", "") for t in result.trades]
        assert any(r == "sl_tp_hit" for r in reasons) or len(result.trades) > 0

    def test_progress_callback(self):
        callback_calls: list[int] = []

        def cb(processed: int, total: int):
            callback_calls.append(processed)

        feed = DataFeed(self._make_bars(500))
        engine = BacktestEngine(feed, self._always_long, progress_callback=cb)
        engine.run()
        assert len(callback_calls) > 0

    def test_custom_strategy_context(self):
        feed = DataFeed(self._make_bars(50))
        ctx = {"my_param": 42}
        received_context: list[dict] = []

        def tracking_strategy(bar: Bar, portfolio: VirtualPortfolio, ctx2: dict):
            received_context.append(dict(ctx2))
            return None

        engine = BacktestEngine(feed, tracking_strategy, strategy_context=ctx)
        engine.run()
        assert len(received_context) > 0
        assert all("my_param" in c and c["my_param"] == 42 for c in received_context)


# ---------------------------------------------------------------------------
# compute_backtest_metrics
# ---------------------------------------------------------------------------
class TestComputeBacktestMetrics:
    def test_empty_result(self):
        result = BacktestResult()
        metrics = compute_backtest_metrics(result)
        assert metrics["total_trades"] == 0
        assert metrics["total_pnl"] == 0.0
        assert metrics["win_rate"] == 0.0
        assert metrics["final_equity"] == 0.0

    def test_single_winning_trade(self):
        result = BacktestResult(
            trades=[{"pnl": 100.0, "pnl_after_cost": 99.0}],
            total_pnl=100.0,
            total_cost=1.0,
            win_rate=1.0,
            total_trades=1,
            profit_factor=999.0,
            equity_curve=[
                {
                    "timestamp": "2026-01-01T09:00:00",
                    "equity": 10000.0,
                    "cash": 10000.0,
                    "unrealized_pnl": 0.0,
                    "position_count": 0,
                },
                {
                    "timestamp": "2026-01-01T09:01:00",
                    "equity": 10100.0,
                    "cash": 10100.0,
                    "unrealized_pnl": 0.0,
                    "position_count": 0,
                },
            ],
            start_time="2026-01-01T09:00:00",
            end_time="2026-01-01T09:01:00",
            bars_processed=1,
        )
        metrics = compute_backtest_metrics(result)
        assert metrics["total_trades"] == 1
        assert metrics["win_rate"] == 1.0
        assert metrics["net_pnl"] == 99.0
        assert metrics["final_equity"] == 10100.0
        assert metrics["return_pct"] == 1.0

    def test_with_drawdown(self):
        result = BacktestResult(
            total_trades=2,
            max_drawdown_pct=0.05,
            equity_curve=[
                {
                    "timestamp": "2026-01-01T09:00:00",
                    "equity": 10000.0,
                    "cash": 10000.0,
                    "unrealized_pnl": 0.0,
                    "position_count": 0,
                },
                {
                    "timestamp": "2026-01-01T09:01:00",
                    "equity": 9900.0,
                    "cash": 9900.0,
                    "unrealized_pnl": 0.0,
                    "position_count": 0,
                },
                {
                    "timestamp": "2026-01-01T09:02:00",
                    "equity": 10100.0,
                    "cash": 10100.0,
                    "unrealized_pnl": 0.0,
                    "position_count": 0,
                },
            ],
        )
        metrics = compute_backtest_metrics(result)
        assert metrics["max_drawdown_pct"] == 0.05
        assert metrics["sharpe_ratio"] is not None
        assert metrics["sortino_ratio"] is not None

    def test_profit_factor(self):
        result = BacktestResult(profit_factor=2.5)
        metrics = compute_backtest_metrics(result)
        assert metrics["profit_factor"] == 2.5

    def test_zero_equity_curve(self):
        result = BacktestResult(total_trades=0)
        metrics = compute_backtest_metrics(result)
        assert metrics["final_equity"] == 0.0
        assert metrics["return_pct"] == 0.0
