import json
from pathlib import Path

from core.deployment.environment_config import EnvironmentConfig
from core.deployment.replay_isolation import ReplayEnvironment
from core.deployment.service_container import ServiceContainer
from core.feedback.performance_analytics import PerformanceAnalytics


class BacktestRunner:
    """Structured backtesting engine.

    Accepts a sequence of historical triggers, runs them through
    the full decision pipeline in replay mode, simulates fills,
    and produces performance analytics.
    """

    def __init__(
        self,
        config: EnvironmentConfig | None = None,
        container: ServiceContainer | None = None,
        initial_equity: float = 100000.0,
    ):
        self._initial_equity = initial_equity
        self._container = container
        self._config = config
        self._analytics = PerformanceAnalytics(initial_equity=initial_equity)

    def run(self, scenarios: list[dict], *, base_dir: str | None = None) -> "BacktestResult":
        if self._container is None:
            cfg = self._config or EnvironmentConfig.development(
                base_dir or "backtest_data",
                enable_idempotency=False,
                enable_audit_log=False,
            )
            self._container = ServiceContainer(cfg).build()

        replay = ReplayEnvironment(self._container)
        replay.activate()

        orch = self._container.build_orchestrator()
        trades = []
        decisions = []
        errors = []

        for i, scenario in enumerate(scenarios):
            trigger = scenario.get("trigger", {"symbol": scenario.get("symbol", "UNKNOWN")})
            feature_source = scenario.get("features", {})

            try:
                outcome = orch.run_cycle(trigger, feature_source)
                decision_info = {
                    "index": i,
                    "trigger": trigger,
                    "verdict_allowed": False,
                    "action": None,
                    "symbol": trigger.get("symbol"),
                }

                if outcome.decision_result:
                    intent = outcome.decision_result.intent
                    verdict = outcome.decision_result.verdict
                    decision_info["verdict_allowed"] = verdict.is_allowed()
                    decision_info["action"] = (
                        intent.action.value
                        if hasattr(intent.action, "value")
                        else str(intent.action)
                    )
                    decision_info["side"] = (
                        intent.side.value if hasattr(intent.side, "value") else str(intent.side)
                    )
                    decision_info["conviction"] = intent.conviction

                    if verdict.is_allowed() and scenario.get("simulated_fill"):
                        fill = scenario["simulated_fill"]
                        trade = {
                            "entry_price": fill["entry_price"],
                            "exit_price": fill["exit_price"],
                            "side": decision_info.get("side", "long"),
                            "quantity": fill.get("quantity", 1.0),
                            "entry_time": scenario.get("entry_time"),
                            "exit_time": scenario.get("exit_time"),
                            "symbol": trigger.get("symbol"),
                            "scenario_index": i,
                        }
                        trades.append(trade)

                decisions.append(decision_info)

            except Exception as exc:  # BLE001:REVIEWED
                errors.append({"index": i, "error": str(exc)})

        replay.deactivate()

        analytics_result = self._analytics.analyze(trades)
        replay_summary = replay.get_replay_summary()

        return BacktestResult(
            trades=trades,
            decisions=decisions,
            errors=errors,
            analytics=analytics_result,
            replay_summary=replay_summary,
            scenarios_count=len(scenarios),
        )


class BacktestResult:
    """Holds backtest output and provides report generation."""

    def __init__(self, trades, decisions, errors, analytics, replay_summary, scenarios_count):
        self.trades = trades
        self.decisions = decisions
        self.errors = errors
        self.analytics = analytics
        self.replay_summary = replay_summary
        self.scenarios_count = scenarios_count

    def summary(self) -> dict:
        return {
            "scenarios": self.scenarios_count,
            "decisions": len(self.decisions),
            "trades_executed": len(self.trades),
            "errors": len(self.errors),
            "allowed_count": sum(1 for d in self.decisions if d.get("verdict_allowed")),
            "blocked_count": sum(1 for d in self.decisions if not d.get("verdict_allowed")),
            "total_pnl": self.analytics["total_pnl"],
            "win_rate": self.analytics["win_rate"],
            "sharpe_ratio": self.analytics["sharpe_ratio"],
            "max_drawdown_pct": self.analytics["max_drawdown_pct"],
            "profit_factor": self.analytics["profit_factor"],
            "final_equity": self.analytics["final_equity"],
            "total_return_pct": self.analytics["total_return_pct"],
        }

    def to_dict(self) -> dict:
        return {
            "summary": self.summary(),
            "analytics": self.analytics,
            "decisions": self.decisions,
            "trades": self.trades,
            "errors": self.errors,
            "replay_summary": self.replay_summary,
        }

    def save(self, path: str) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()
        data["analytics"].pop("equity_curve", None)
        data["analytics"].pop("pnl_series", None)
        p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return str(p)
