from datetime import UTC, datetime, timedelta

from apps.engine.backtest_runner import BacktestRunner
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer
from core.feedback.performance_analytics import PerformanceAnalytics


def _trades(count=10, base_entry=2000.0, step=1.0, win_rate=0.7):
    trades = []
    t0 = datetime(2026, 1, 1, 9, 0, 0)
    for i in range(count):
        is_win = (i % int(1 / win_rate if win_rate > 0 else 1)) != 0
        exit_price = base_entry + step if is_win else base_entry - step * 0.5
        trades.append(
            {
                "entry_price": base_entry,
                "exit_price": exit_price,
                "side": "long",
                "quantity": 1.0,
                "entry_time": t0 + timedelta(hours=i),
                "exit_time": t0 + timedelta(hours=i, minutes=30),
            }
        )
    return trades


class TestPerformanceAnalytics:
    def test_empty_trades(self):
        pa = PerformanceAnalytics()
        r = pa.analyze([])
        assert r["trade_count"] == 0
        assert r["final_equity"] == 100000.0

    def test_basic_metrics(self):
        pa = PerformanceAnalytics(initial_equity=10000.0)
        trades = [
            {"entry_price": 100, "exit_price": 110, "side": "long", "quantity": 10},
            {"entry_price": 110, "exit_price": 105, "side": "long", "quantity": 10},
            {"entry_price": 200, "exit_price": 190, "side": "short", "quantity": 5},
        ]
        r = pa.analyze(trades)
        assert r["trade_count"] == 3
        assert r["win_count"] == 2
        assert r["loss_count"] == 1
        assert r["total_pnl"] == 100 + (-50) + 50
        assert r["final_equity"] == 10000 + 100

    def test_win_rate(self):
        pa = PerformanceAnalytics()
        trades = _trades(10, win_rate=0.5)
        r = pa.analyze(trades)
        assert 0.0 < r["win_rate"] <= 1.0

    def test_drawdown(self):
        pa = PerformanceAnalytics(initial_equity=1000.0)
        trades = [
            {"entry_price": 100, "exit_price": 90, "side": "long", "quantity": 1},
            {"entry_price": 100, "exit_price": 85, "side": "long", "quantity": 1},
            {"entry_price": 100, "exit_price": 120, "side": "long", "quantity": 1},
        ]
        r = pa.analyze(trades)
        assert r["max_drawdown"] > 0
        assert r["max_drawdown_pct"] > 0

    def test_sharpe_and_sortino(self):
        pa = PerformanceAnalytics()
        trades = _trades(20)
        r = pa.analyze(trades)
        assert isinstance(r["sharpe_ratio"], float)
        assert isinstance(r["sortino_ratio"], float)

    def test_profit_factor(self):
        pa = PerformanceAnalytics()
        trades = [
            {"entry_price": 100, "exit_price": 120, "side": "long", "quantity": 1},
            {"entry_price": 100, "exit_price": 95, "side": "long", "quantity": 1},
        ]
        r = pa.analyze(trades)
        assert r["profit_factor"] == 20 / 5

    def test_all_wins(self):
        pa = PerformanceAnalytics()
        trades = [{"entry_price": 100, "exit_price": 110, "side": "long", "quantity": 1}]
        r = pa.analyze(trades)
        assert r["win_rate"] == 1.0
        assert r["profit_factor"] == float("inf")

    def test_short_trades(self):
        pa = PerformanceAnalytics(initial_equity=5000.0)
        trades = [
            {"entry_price": 200, "exit_price": 180, "side": "short", "quantity": 2},
        ]
        r = pa.analyze(trades)
        assert r["total_pnl"] == 40.0
        assert r["final_equity"] == 5040.0

    def test_equity_curve_length(self):
        pa = PerformanceAnalytics()
        trades = _trades(5)
        r = pa.analyze(trades)
        assert len(r["equity_curve"]) == 6
        assert len(r["pnl_series"]) == 5

    def test_duration_tracking(self):
        pa = PerformanceAnalytics()
        trades = [
            {
                "entry_price": 100,
                "exit_price": 110,
                "side": "long",
                "quantity": 1,
                "entry_time": datetime(2026, 1, 1, 9, 0),
                "exit_time": datetime(2026, 1, 1, 10, 30),
            }
        ]
        r = pa.analyze(trades)
        assert r["avg_duration_seconds"] == 5400.0


class TestBacktestRunner:
    def test_basic_backtest(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path), enable_idempotency=False)
        c = ServiceContainer(cfg).build()
        runner = BacktestRunner(container=c, initial_equity=50000.0)

        now = datetime.now(UTC).replace(tzinfo=None)
        scenarios = [
            {
                "trigger": {"symbol": "XAUUSD"},
                "features": {"f": 1.0},
                "simulated_fill": {"entry_price": 2000, "exit_price": 2010, "quantity": 0.1},
                "entry_time": now,
                "exit_time": now + timedelta(minutes=30),
            },
            {
                "trigger": {"symbol": "XAUUSD"},
                "features": {"f": 0.5},
                "simulated_fill": {"entry_price": 2010, "exit_price": 2005, "quantity": 0.1},
                "entry_time": now + timedelta(hours=1),
                "exit_time": now + timedelta(hours=1, minutes=20),
            },
            {
                "trigger": {"symbol": "EURUSD"},
                "features": {"f": 0.8},
            },
        ]

        result = runner.run(scenarios, base_dir=str(tmp_path))

        assert result.scenarios_count == 3
        assert len(result.decisions) == 3
        assert len(result.errors) == 0

        s = result.summary()
        assert s["scenarios"] == 3
        assert s["trades_executed"] >= 0
        assert isinstance(s["total_pnl"], int | float)
        assert isinstance(s["sharpe_ratio"], int | float)

    def test_backtest_save_report(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path), enable_idempotency=False)
        c = ServiceContainer(cfg).build()
        runner = BacktestRunner(container=c)

        now = datetime.now(UTC).replace(tzinfo=None)
        scenarios = [
            {
                "trigger": {"symbol": "XAUUSD"},
                "features": {"f": 1.0},
                "simulated_fill": {"entry_price": 2000, "exit_price": 2015, "quantity": 0.5},
                "entry_time": now,
                "exit_time": now + timedelta(minutes=15),
            }
        ]

        result = runner.run(scenarios, base_dir=str(tmp_path))
        path = result.save(str(tmp_path / "reports" / "test_report.json"))

        import json

        with open(path) as f:
            data = json.load(f)
        assert "summary" in data
        assert "analytics" in data
        assert "equity_curve" not in data["analytics"]

    def test_backtest_to_dict(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path), enable_idempotency=False)
        c = ServiceContainer(cfg).build()
        runner = BacktestRunner(container=c)

        result = runner.run([{"trigger": {"symbol": "XAUUSD"}, "features": {}}])
        d = result.to_dict()
        assert "summary" in d
        assert "replay_summary" in d

    def test_empty_backtest(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path), enable_idempotency=False)
        c = ServiceContainer(cfg).build()
        runner = BacktestRunner(container=c)
        result = runner.run([])
        assert result.scenarios_count == 0
        assert result.analytics["trade_count"] == 0

    def test_multi_symbol_backtest(self, tmp_path):
        cfg = EnvironmentConfig.development(str(tmp_path), enable_idempotency=False)
        c = ServiceContainer(cfg).build()
        runner = BacktestRunner(container=c, initial_equity=100000.0)

        now = datetime.now(UTC).replace(tzinfo=None)
        scenarios = []
        for i, sym in enumerate(["XAUUSD", "EURUSD", "GBPUSD", "XAUUSD", "EURUSD"]):
            scenarios.append(
                {
                    "trigger": {"symbol": sym},
                    "features": {"f": float(i)},
                    "simulated_fill": {
                        "entry_price": 100 + i,
                        "exit_price": 100 + i + (1 if i % 2 == 0 else -0.5),
                        "quantity": 1.0,
                    },
                    "entry_time": now + timedelta(hours=i),
                    "exit_time": now + timedelta(hours=i, minutes=30),
                }
            )

        result = runner.run(scenarios, base_dir=str(tmp_path))
        s = result.summary()
        assert s["scenarios"] == 5
        assert s["trades_executed"] >= 0
