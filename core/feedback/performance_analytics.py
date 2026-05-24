import math
from datetime import datetime


class PerformanceAnalytics:
    """Computes trading performance metrics from a sequence of trades.

    Each trade is a dict with at least:
        entry_price, exit_price, side ("long"/"short"),
        quantity, entry_time, exit_time
    """

    def __init__(self, initial_equity: float = 100000.0, risk_free_rate: float = 0.0):
        self._initial_equity = initial_equity
        self._risk_free_rate = risk_free_rate

    def analyze(self, trades: list[dict]) -> dict:
        if not trades:
            return self._empty_result()

        pnls = [self._trade_pnl(t) for t in trades]
        equity_curve = self._build_equity_curve(pnls)
        returns = self._compute_returns(equity_curve)

        total_pnl = sum(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        # Annualization factor from actual trade time span (not hardcoded 252).
        # Returns are per-trade, not daily, so daily sqrt(252) would mis-scale.
        annual_factor = self._annual_factor(trades)

        max_dd, max_dd_pct = self._max_drawdown(equity_curve)
        sharpe = self._sharpe_ratio(returns, annual_factor)
        sortino = self._sortino_ratio(returns, annual_factor)
        profit_factor = self._profit_factor(wins, losses)
        expectancy = total_pnl / len(trades)

        durations = [self._trade_duration_seconds(t) for t in trades if t.get("exit_time")]

        return {
            "trade_count": len(trades),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": len(wins) / len(trades) if trades else 0,
            "total_pnl": round(total_pnl, 4),
            "avg_pnl": round(expectancy, 4),
            "max_win": round(max(pnls), 4) if pnls else 0,
            "max_loss": round(min(pnls), 4) if pnls else 0,
            "profit_factor": round(profit_factor, 4),
            "sharpe_ratio": round(sharpe, 4),
            "sortino_ratio": round(sortino, 4),
            "max_drawdown": round(max_dd, 4),
            "max_drawdown_pct": round(max_dd_pct, 4),
            "avg_win": round(sum(wins) / len(wins), 4) if wins else 0,
            "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0,
            "expectancy": round(expectancy, 4),
            "initial_equity": self._initial_equity,
            "final_equity": round(equity_curve[-1], 4),
            "total_return_pct": round((equity_curve[-1] / self._initial_equity - 1) * 100, 4),
            "avg_duration_seconds": round(sum(durations) / len(durations), 2) if durations else 0,
            "equity_curve": [round(e, 2) for e in equity_curve],
            "pnl_series": [round(p, 4) for p in pnls],
        }

    def _trade_pnl(self, trade: dict) -> float:
        entry = float(trade["entry_price"])
        exit_ = float(trade["exit_price"])
        qty = float(trade.get("quantity", 1.0))
        side = trade.get("side", "long")
        if side == "long":
            return (exit_ - entry) * qty
        return (entry - exit_) * qty

    def _build_equity_curve(self, pnls: list[float]) -> list[float]:
        curve = [self._initial_equity]
        for p in pnls:
            curve.append(curve[-1] + p)
        return curve

    def _compute_returns(self, equity_curve: list[float]) -> list[float]:
        returns = []
        for i in range(1, len(equity_curve)):
            if equity_curve[i - 1] != 0:
                returns.append(equity_curve[i] / equity_curve[i - 1] - 1)
            else:
                returns.append(0.0)
        return returns

    def _annual_factor(self, trades: list[dict]) -> float:
        """Derive annualization factor from actual trade time span.

        Returns the estimated number of trades per year.  Falls back to 1.0
        (no annualization) when timestamps are missing or span < 1 day.
        """
        timestamps = []
        for t in trades:
            for key in ("exit_time", "entry_time"):
                ts = t.get(key)
                if isinstance(ts, datetime):
                    timestamps.append(ts)
                    break
        if len(timestamps) < 2:
            return 1.0
        try:
            first = min(timestamps)
            last = max(timestamps)
            span_days = (last - first).total_seconds() / 86400.0
            if span_days < 1.0:
                return 1.0
            return len(trades) / span_days * 365.0
        except (TypeError, ValueError):
            return 1.0

    def _max_drawdown(self, equity_curve: list[float]) -> tuple[float, float]:
        peak = equity_curve[0]
        max_dd = 0.0
        max_dd_pct = 0.0
        for val in equity_curve[1:]:
            if val > peak:
                peak = val
            dd = peak - val
            dd_pct = dd / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
        return max_dd, max_dd_pct

    def _sharpe_ratio(self, returns: list[float], annual_factor: float = 1.0) -> float:
        if len(returns) < 2:
            return 0.0
        mean_r = sum(returns) / len(returns)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1))
        if std_r == 0:
            return 0.0
        # Risk-free rate scaled to per-trade period
        period_rf = self._risk_free_rate / max(annual_factor, 1.0)
        excess = mean_r - period_rf
        return excess / std_r * math.sqrt(annual_factor)

    def _sortino_ratio(self, returns: list[float], annual_factor: float = 1.0) -> float:
        if len(returns) < 2:
            return 0.0
        mean_r = sum(returns) / len(returns)
        downside = [r for r in returns if r < 0]
        if not downside:
            return 0.0 if mean_r <= 0 else float("inf")
        down_std = math.sqrt(sum(r**2 for r in downside) / len(downside))
        if down_std == 0:
            return 0.0
        period_rf = self._risk_free_rate / max(annual_factor, 1.0)
        excess = mean_r - period_rf
        return excess / down_std * math.sqrt(annual_factor)

    def _profit_factor(self, wins: list[float], losses: list[float]) -> float:
        total_wins = sum(wins)
        total_losses = abs(sum(losses))
        if total_losses == 0:
            return float("inf") if total_wins > 0 else 0.0
        return total_wins / total_losses

    def _trade_duration_seconds(self, trade: dict) -> float:
        entry = trade.get("entry_time")
        exit_ = trade.get("exit_time")
        if isinstance(entry, datetime) and isinstance(exit_, datetime):
            return (exit_ - entry).total_seconds()
        return 0.0

    def _empty_result(self) -> dict:
        return {
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": 0,
            "total_pnl": 0,
            "avg_pnl": 0,
            "max_win": 0,
            "max_loss": 0,
            "profit_factor": 0,
            "sharpe_ratio": 0,
            "sortino_ratio": 0,
            "max_drawdown": 0,
            "max_drawdown_pct": 0,
            "avg_win": 0,
            "avg_loss": 0,
            "expectancy": 0,
            "initial_equity": self._initial_equity,
            "final_equity": self._initial_equity,
            "total_return_pct": 0,
            "avg_duration_seconds": 0,
            "equity_curve": [self._initial_equity],
            "pnl_series": [],
        }
