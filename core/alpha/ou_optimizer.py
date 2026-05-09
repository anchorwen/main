"""OU Process Parameter Optimizer — Optuna Bayesian search + Kalman filter + trend mute.

Replaces the 324-combination grid search in arb_trainer with:
1. Optuna TPE (Tree-structured Parzen Estimator) for Bayesian hyperparameter optimization
2. Kalman filter for dynamic half-life estimation with adaptive noise
3. ADX-based trend detection to auto-mute mean-reversion signals in strong trends

Protocol:
- optimize() → (best_params, best_metrics, study)
- Uses expected improvement acquisition function via Optuna sampler
- Kalman filter smooths OU theta estimates across rolling windows
- Trend mute: ADX(14) > 25 → suppress OU signals (trend ≠ mean-revert)
"""

from __future__ import annotations

from typing import Any

import numpy as np


def load_price_data(csv_path: str, max_points: int = 50000) -> np.ndarray:
    """Load price series from CSV, detecting the price column automatically."""
    import pandas as pd

    df = pd.read_csv(csv_path)
    price_col = None
    for col in ["close", "Close", "CLOSE"]:
        if col in df.columns:
            price_col = col
            break
    if price_col is None:
        for col in ["Bid", "bid", "Ask", "ask"]:
            if col in df.columns:
                price_col = col
                break
    if price_col is None:
        for col in df.columns:
            lower = col.lower()
            if "close" in lower or "bid" in lower or "mid" in lower:
                price_col = col
                break
    if price_col is None:
        raise ValueError(f"No price column found. Available: {list(df.columns)}")

    prices_all = df[price_col].dropna().values.astype(np.float64)
    if len(prices_all) < 200:
        raise ValueError(f"Insufficient data: {len(prices_all)} rows")

    if len(prices_all) > max_points:
        step = len(prices_all) // max_points
        prices = prices_all[::step][:max_points]
    else:
        prices = prices_all
    return prices


# ═══════════════════════════════════════════════════════════════════════
# OU Process estimation
# ═══════════════════════════════════════════════════════════════════════


def calc_ou_params(window_prices: np.ndarray) -> dict[str, float]:
    """Estimate OU parameters from a price window.

    Returns dict with theta, mu, half_life, z_score, sigma.
    """
    y = np.diff(window_prices)
    x = window_prices[:-1]
    n = len(x)
    if n < 2:
        mu = float(np.mean(window_prices))
        return {"theta": 0.0, "mu": mu, "half_life": float("inf"), "z_score": 0.0, "sigma": 0.0}

    x_mean = float(np.mean(x))
    y_mean = float(np.mean(y))
    denom = float(np.sum((x - x_mean) ** 2))
    if denom < 1e-12:
        mu = float(np.mean(window_prices))
        return {"theta": 0.0, "mu": mu, "half_life": float("inf"), "z_score": 0.0, "sigma": 0.0}

    beta = float(np.sum((x - x_mean) * (y - y_mean)) / denom)
    alpha = y_mean - beta * x_mean
    theta = -beta

    if theta <= 1e-6:
        mu = float(np.mean(window_prices))
        return {
            "theta": 0.0,
            "mu": mu,
            "half_life": float("inf"),
            "z_score": 0.0,
            "sigma": float(np.std(window_prices)),
        }

    mu = alpha / theta
    half_life = np.log(2) / theta
    current_price = float(window_prices[-1])
    sigma = float(np.std(window_prices))
    effective_std = max(sigma, 0.50)
    z_score = (current_price - mu) / effective_std if effective_std > 0 else 0.0

    if abs(mu - current_price) > effective_std * 10:
        mu = float(np.mean(window_prices))
        z_score = (current_price - mu) / effective_std if effective_std > 0 else 0.0

    return {"theta": theta, "mu": mu, "half_life": half_life, "z_score": z_score, "sigma": sigma}


# ═══════════════════════════════════════════════════════════════════════
# Kalman filter for dynamic half-life tracking
# ═══════════════════════════════════════════════════════════════════════


class KalmanHalfLifeFilter:
    """1-D Kalman filter tracking the OU theta parameter across rolling windows.

    State: theta (mean-reversion speed)
    Observation: raw theta estimate from linear regression

    Adaptive process noise: increases when theta estimates are volatile,
    decreases when stable — balances responsiveness vs smoothness.
    """

    def __init__(
        self,
        initial_theta: float = 0.01,
        process_noise: float = 0.001,
        measurement_noise: float = 0.01,
    ):
        self.theta_est = initial_theta
        self.cov = 1.0
        self.Q = process_noise  # process noise
        self.R = measurement_noise  # measurement noise

    def update(self, observed_theta: float) -> float:
        # Predict
        self.cov += self.Q
        # Update
        K = self.cov / (self.cov + self.R)  # Kalman gain
        self.theta_est = self.theta_est + K * (observed_theta - self.theta_est)
        self.cov = (1 - K) * self.cov
        # Adaptive process noise: increase when innovation is large
        innovation = abs(observed_theta - self.theta_est)
        self.Q = 0.001 + 0.01 * min(innovation, 1.0)
        return self.theta_est

    @property
    def half_life(self) -> float:
        if self.theta_est <= 1e-6:
            return float("inf")
        return np.log(2) / self.theta_est


# ═══════════════════════════════════════════════════════════════════════
# Trend detection (ADX-based)
# ═══════════════════════════════════════════════════════════════════════


def compute_adx(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> np.ndarray:
    """Compute Average Directional Index (ADX) for trend strength.

    ADX > 25  → trending market (OU signals should be muted)
    ADX < 20  → ranging/mean-reverting market (OU signals active)
    """
    n = len(close)
    if n < period + 1:
        return np.full(n, 20.0)

    tr = np.maximum(high[1:] - low[1:], np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1]))
    up_move = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # Wilder's smoothing
    atr = np.zeros(n)
    atr[period] = np.mean(tr[:period])
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i - 1]) / period

    plus_di = np.zeros(n)
    minus_di = np.zeros(n)
    plus_di[period] = np.mean(plus_dm[:period]) / atr[period] * 100 if atr[period] > 0 else 0
    minus_di[period] = np.mean(minus_dm[:period]) / atr[period] * 100 if atr[period] > 0 else 0
    for i in range(period + 1, n):
        plus_di[i] = (
            (plus_di[i - 1] * (period - 1) + plus_dm[i - 1]) / period / atr[i] * 100
            if atr[i] > 0
            else plus_di[i - 1]
        )
        minus_di[i] = (
            (minus_di[i - 1] * (period - 1) + minus_dm[i - 1]) / period / atr[i] * 100
            if atr[i] > 0
            else minus_di[i - 1]
        )

    dx = np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-8) * 100
    adx = np.zeros(n)
    adx[period * 2 - 1] = np.mean(dx[period : period * 2])
    for i in range(period * 2, n):
        adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    return adx


def compute_trend_mute(close: np.ndarray, adx_threshold: float = 25.0) -> np.ndarray:
    """Compute trend-mute multipliers for each bar.

    Returns array of multipliers in [0, 1]:
      1.0 = full OU signal (ranging market)
      0.0 = fully muted (strong trend)
      Linear interpolation between ADX 20-25.
    """
    # Use close as proxy for high/low when only price series available
    high = close
    low = close
    adx = compute_adx(high, low, close)
    mute = np.ones(len(close))
    for i in range(len(close)):
        if adx[i] > adx_threshold:
            mute[i] = 0.0
        elif adx[i] > 20.0:
            mute[i] = 1.0 - (adx[i] - 20.0) / (adx_threshold - 20.0)
    return mute


# ═══════════════════════════════════════════════════════════════════════
# Backtest engine
# ═══════════════════════════════════════════════════════════════════════


def run_backtest(
    prices: np.ndarray,
    window: int = 100,
    z_entry: float = 2.0,
    z_exit: float = 0.5,
    max_half_life: float = 20.0,
    theta_min: float = 0.005,
    use_kalman: bool = True,
    use_trend_mute: bool = True,
) -> dict[str, Any]:
    """Run OU mean-reversion backtest.

    Args:
        use_kalman: Apply Kalman filter to smooth theta estimates across windows.
        use_trend_mute: Mute signals when ADX indicates strong trend.
    """
    position = 0
    entry_price = 0.0
    trades = []
    equity_curve = [0.0]
    n = len(prices)

    kf = KalmanHalfLifeFilter() if use_kalman else None
    trend_mute = compute_trend_mute(prices) if use_trend_mute else np.ones(n)

    for i in range(window, n - 1):
        window_prices = prices[i - window : i]
        current_price = prices[i]

        ou = calc_ou_params(window_prices)
        theta_raw = ou["theta"]
        ou["mu"]
        z_score = ou["z_score"]

        if kf is not None:
            theta_smooth = kf.update(theta_raw)
            half_life = kf.half_life
        else:
            theta_smooth = theta_raw
            half_life = ou["half_life"]

        # Trend mute multiplier at this bar
        mute_factor = trend_mute[i] if use_trend_mute else 1.0

        if position == 0:
            if half_life < max_half_life and theta_smooth > theta_min and mute_factor > 0.3:
                if z_score < -z_entry:
                    position = 1
                    entry_price = current_price
                elif z_score > z_entry:
                    position = -1
                    entry_price = current_price
        elif position == 1:
            if z_score > -z_exit or z_score > z_entry * 0.3:
                pnl = current_price - entry_price
                trades.append(pnl)
                equity_curve.append(equity_curve[-1] + pnl)
                position = 0
        elif position == -1:
            if z_score < z_exit or z_score < -z_entry * 0.3:
                pnl = entry_price - current_price
                trades.append(pnl)
                equity_curve.append(equity_curve[-1] + pnl)
                position = 0

    if position != 0:
        final_pnl = (prices[-1] - entry_price) * position
        trades.append(final_pnl)
        equity_curve.append(equity_curve[-1] + final_pnl)

    if len(trades) < 5:
        return {
            "total_trades": len(trades),
            "winrate": 0.0,
            "total_pnl": 0.0,
            "sharpe": -999.0,
            "max_drawdown_pct": 100.0,
            "profit_factor": 0.0,
        }

    trades_arr = np.array(trades)
    wins = trades_arr > 0
    losses = trades_arr < 0
    total_pnl = float(trades_arr.sum())
    winrate = float(wins.sum() / len(trades_arr))
    gross_profit = float(trades_arr[wins].sum()) if wins.any() else 0.0
    gross_loss = float(abs(trades_arr[losses].sum())) if losses.any() else 1e-8
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    returns = np.diff(np.array(equity_curve))
    if len(returns) > 1 and np.std(returns) > 0:
        # Annualize using trades per year (not bars per year)
        n_returns = len(returns)
        trading_days = max(n / 288, 1)  # estimate from M5 bars
        trades_per_year = n_returns / trading_days * 252
        sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(max(trades_per_year, 1)))
    else:
        sharpe = 0.0

    equity = np.array(equity_curve)
    peak = np.maximum.accumulate(equity)
    # Use absolute equity floor to avoid numerical explosion when equity ≈ 0
    denom = np.maximum(np.abs(peak), 1.0)
    dd = (peak - equity) / denom * 100
    max_dd = float(np.max(dd))

    return {
        "total_trades": len(trades),
        "winrate": winrate,
        "total_pnl": total_pnl,
        "sharpe": sharpe,
        "max_drawdown_pct": max_dd,
        "profit_factor": profit_factor,
    }


# ═══════════════════════════════════════════════════════════════════════
# Optuna optimization
# ═══════════════════════════════════════════════════════════════════════


def _make_objective(prices: np.ndarray, use_kalman: bool, use_trend_mute: bool):
    """Factory for Optuna objective function with closed-over prices."""

    def objective(trial) -> float:
        window = trial.suggest_int("window", 30, 300, step=10)
        z_entry = trial.suggest_float("z_entry", 1.0, 4.0, step=0.1)
        z_exit = trial.suggest_float("z_exit", 0.1, 1.5, step=0.05)
        max_half_life = trial.suggest_int("max_half_life", 4, 60, step=2)
        theta_min = trial.suggest_float("theta_min", 0.0005, 0.05, log=True)

        metrics = run_backtest(
            prices,
            window=window,
            z_entry=z_entry,
            z_exit=z_exit,
            max_half_life=max_half_life,
            theta_min=theta_min,
            use_kalman=use_kalman,
            use_trend_mute=use_trend_mute,
        )
        sharpe = metrics["sharpe"]
        winrate = metrics["winrate"]
        max_dd = metrics["max_drawdown_pct"]
        n_trades = metrics["total_trades"]
        profit_factor = metrics["profit_factor"]

        # Hard floor: need at least 30 trades for statistical significance
        if n_trades < 30:
            return -999.0 + n_trades * 0.1  # gradated so Optuna can climb

        # T-stat adjusted Sharpe
        t_stat = sharpe / max(1.0, np.sqrt(n_trades / 252))
        score = t_stat

        if winrate < 0.48:
            score -= 3.0
        if winrate < 0.52:
            score -= 1.0
        if winrate >= 0.55:
            score += 0.5
        if winrate >= 0.60:
            score += 1.0
        if max_dd > 30:
            score -= (max_dd - 30) * 0.2
        if max_dd > 50:
            score -= (max_dd - 50) * 0.5
        if profit_factor < 1.0:
            score -= (1.0 - profit_factor) * 5.0
        if profit_factor < 1.2:
            score -= (1.2 - profit_factor) * 2.0

        return float(score)

    return objective


def optimize(
    prices: np.ndarray,
    n_trials: int = 200,
    seed: int = 42,
    use_kalman: bool = True,
    use_trend_mute: bool = True,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Run Optuna Bayesian optimization for OU parameters.

    Returns {optimal_params, metrics, top_10_results, search_meta}.
    Falls back to grid search if optuna is not installed.
    """
    try:
        import optuna

        optuna.logging.set_verbosity(optuna.logging.WARNING)

        objective = _make_objective(prices, use_kalman, use_trend_mute)
        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=seed),
            pruner=optuna.pruners.MedianPruner(n_startup_trials=20, n_warmup_steps=5),
        )
        study.optimize(
            objective, n_trials=n_trials, timeout=timeout_seconds, show_progress_bar=False
        )

        best_params = study.best_params
        best_metrics = run_backtest(
            prices,
            window=best_params["window"],
            z_entry=best_params["z_entry"],
            z_exit=best_params["z_exit"],
            max_half_life=best_params["max_half_life"],
            theta_min=best_params["theta_min"],
            use_kalman=use_kalman,
            use_trend_mute=use_trend_mute,
        )

        # Collect top 10 trials
        top_trials = sorted(
            study.trials, key=lambda t: t.value if t.value is not None else -999, reverse=True
        )[:10]
        top_10 = [{**t.params, "sharpe": t.value if t.value else -999} for t in top_trials]

        return {
            "optimal_params": best_params,
            "metrics": best_metrics,
            "top_10_results": top_10,
            "search_meta": {
                "method": "optuna_tpe",
                "n_trials": len(study.trials),
                "best_value": study.best_value,
                "kalman_filter": use_kalman,
                "trend_mute": use_trend_mute,
            },
        }

    except ImportError:
        return _grid_search_fallback(prices, use_kalman, use_trend_mute)


def _grid_search_fallback(
    prices: np.ndarray, use_kalman: bool, use_trend_mute: bool
) -> dict[str, Any]:
    """Fallback grid search when optuna is unavailable (108 combinations)."""
    param_grid = [
        {"window": w, "z_entry": ze, "z_exit": zx, "max_half_life": mhl, "theta_min": tm}
        for w in [50, 100, 200]
        for ze in [1.5, 2.0, 2.5, 3.0]
        for zx in [0.3, 0.5, 0.8]
        for mhl in [8, 20, 40]
        for tm in [0.001, 0.005, 0.01]
    ]

    best_sharpe = -999.0
    best_params = None
    best_metrics = None
    results_log = []

    for params in param_grid:
        metrics = run_backtest(
            prices,
            window=params["window"],
            z_entry=params["z_entry"],
            z_exit=params["z_exit"],
            max_half_life=params["max_half_life"],
            theta_min=params["theta_min"],
            use_kalman=use_kalman,
            use_trend_mute=use_trend_mute,
        )
        results_log.append({**params, **metrics})
        if metrics["sharpe"] > best_sharpe and metrics["winrate"] >= 0.48:
            best_sharpe = metrics["sharpe"]
            best_params = params
            best_metrics = metrics

    if best_params is None:
        best_params = param_grid[0]
        best_metrics = results_log[0]

    return {
        "optimal_params": best_params,
        "metrics": best_metrics,
        "top_10_results": sorted(results_log, key=lambda x: x["sharpe"], reverse=True)[:10],
        "search_meta": {
            "method": "grid_search_fallback",
            "n_trials": len(param_grid),
            "best_value": best_sharpe,
            "kalman_filter": use_kalman,
            "trend_mute": use_trend_mute,
        },
    }
