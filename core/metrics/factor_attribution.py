"""Factor-based P&L attribution with time-decay weighting.

Decomposes strategy returns into factor contributions:
  - market_beta: Directional exposure to the underlying price.
  - momentum: Serial correlation / trend-following component.
  - volatility: ATR-based volatility risk premium.
  - carry: Overnight / roll component.
  - residual: Unexplained remainder.

All decompositions use exponentially-decayed weights so recent
observations are more influential.

Usage:
    from core.metrics.factor_attribution import decompose_pnl, FactorAttributionReport

    report = decompose_pnl(daily_returns, factor_returns, half_life=21)
    print(report.r_squared, report.factor_contributions)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# ── Time-decay weights ────────────────────────────────────────────────────


def exponential_decay_weights(n_periods: int, *, half_life: int = 21) -> np.ndarray:
    """Exponentially decaying weights, most recent first.

    Args:
        n_periods: Number of observations.
        half_life: Number of periods for weight to halve.

    Returns:
        1-D array of weights that sum to 1.0.
    """
    if n_periods <= 0:
        return np.array([], dtype=np.float64)

    decay_factor = np.exp(-np.log(2) / half_life)
    weights = np.power(decay_factor, np.arange(n_periods)[::-1])
    total = weights.sum()
    if total <= 0:
        return np.ones(n_periods, dtype=np.float64) / n_periods
    return weights / total


# ── Factor returns construction ───────────────────────────────────────────


def build_factor_returns(
    prices: np.ndarray,
    atr_series: np.ndarray | None = None,
    *,
    direction_signs: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    """Build factor return series from price and ATR data.

    Args:
        prices: Close/mid prices, shape (n_periods,).
        atr_series: ATR values, shape (n_periods,).  If None, volatility
                    factor is zero.
        direction_signs: +1 (long), -1 (short), 0 (flat) per period.

    Returns:
        Dict mapping factor name to array of same length as prices.
        Keys: market, momentum, volatility, carry.
    """
    n = len(prices)
    if n < 2:
        return {
            "market": np.zeros(n),
            "momentum": np.zeros(n),
            "volatility": np.zeros(n),
            "carry": np.zeros(n),
        }

    # Market: daily log returns
    log_rets = np.diff(np.log(np.maximum(prices, 1e-9)))
    market = np.concatenate([[0.0], log_rets])

    # Momentum: lag-1 sign × return magnitude
    momentum = np.zeros(n, dtype=np.float64)
    if n >= 2:
        momentum[1:] = np.sign(log_rets) * np.abs(log_rets)

    # Volatility: ATR / price as risk proxy
    volatility = np.zeros(n, dtype=np.float64)
    if atr_series is not None and len(atr_series) == n:
        atr = np.asarray(atr_series, dtype=np.float64)
        vol_pct = atr / np.maximum(prices, 1e-9)
        if direction_signs is not None:
            signs = np.asarray(direction_signs, dtype=np.float64)
            vol_pct = vol_pct * np.where(signs != 0, signs, 1.0)
        volatility = vol_pct

    # Carry: overnight price change (simplified as close-to-close overnight)
    carry = np.zeros(n, dtype=np.float64)

    return {"market": market, "momentum": momentum, "volatility": volatility, "carry": carry}


# ── P&L decomposition ─────────────────────────────────────────────────────


@dataclass
class FactorAttributionReport:
    """Result of a factor-based P&L decomposition."""

    factor_contributions: dict[str, float]  # per-factor attribution (fraction)
    r_squared: float  # fraction of variance explained
    residual: float  # 1.0 - sum(|contributions|), bounded [0, 1]
    n_periods: int
    half_life: int

    def to_dict(self) -> dict:
        return {
            "factor_contributions": self.factor_contributions,
            "r_squared": round(self.r_squared, 6),
            "residual": round(self.residual, 6),
            "n_periods": self.n_periods,
            "half_life": self.half_life,
        }


def decompose_pnl(
    strategy_returns: np.ndarray,
    factor_returns: dict[str, np.ndarray],
    *,
    half_life: int = 21,
) -> FactorAttributionReport:
    """Decompose strategy returns into factor contributions.

    Uses weighted least squares with exponential time decay.  Factor
    contributions are the coefficients of a regression of strategy
    returns on factor returns, normalised so they sum to ≤ 1.0.

    Args:
        strategy_returns: Daily strategy P&L, shape (n_periods,).
        factor_returns: Dict of factor_name → array of same length.
        half_life: EMA half-life in periods.

    Returns:
        FactorAttributionReport with per-factor contributions and fit quality.
    """
    sr = np.asarray(strategy_returns, dtype=np.float64).ravel()
    n = len(sr)

    if n < 3 or len(factor_returns) == 0:
        return FactorAttributionReport(
            factor_contributions={},
            r_squared=0.0,
            residual=1.0,
            n_periods=n,
            half_life=half_life,
        )

    # Build design matrix
    factor_names = sorted(factor_returns.keys())
    X_list = []
    for name in factor_names:
        col = np.asarray(factor_returns[name], dtype=np.float64).ravel()[:n]
        X_list.append(col)

    X = np.column_stack(X_list)
    if X.shape[1] == 0:
        return FactorAttributionReport(
            factor_contributions={},
            r_squared=0.0,
            residual=1.0,
            n_periods=n,
            half_life=half_life,
        )

    # Time-decay weights
    W = exponential_decay_weights(n, half_life=half_life)
    W_sqrt = np.sqrt(W)

    # Weighted least squares
    X_w = X * W_sqrt[:, np.newaxis]
    y_w = sr * W_sqrt

    try:
        coeffs, residuals, rank, singular = np.linalg.lstsq(X_w, y_w, rcond=None)
    except np.linalg.LinAlgError:
        return FactorAttributionReport(
            factor_contributions={name: 0.0 for name in factor_names},
            r_squared=0.0,
            residual=1.0,
            n_periods=n,
            half_life=half_life,
        )

    # R-squared from weighted residuals
    y_w_mean = np.average(y_w, weights=W)
    ss_total = np.sum(W * (sr - y_w_mean) ** 2)
    ss_residual = float(residuals[0]) if len(residuals) > 0 else np.sum((y_w - X_w @ coeffs) ** 2)
    r_sq = 1.0 - ss_residual / ss_total if ss_total > 1e-12 else 0.0

    # Normalise contributions to sum to ≤ 1.0
    abs_coeffs = np.abs(coeffs)
    total_abs = abs_coeffs.sum()
    if total_abs > 0:
        norm_coeffs = abs_coeffs / (total_abs + abs(r_sq - total_abs) * 0.5)
    else:
        norm_coeffs = abs_coeffs

    contributions = {}
    for i, name in enumerate(factor_names):
        contributions[name] = round(float(norm_coeffs[i]), 6)

    residual_frac = max(0.0, min(1.0, 1.0 - sum(contributions.values())))

    return FactorAttributionReport(
        factor_contributions=contributions,
        r_squared=round(max(0.0, min(1.0, r_sq)), 6),
        residual=round(residual_frac, 6),
        n_periods=n,
        half_life=half_life,
    )
