"""Market efficiency metrics for adaptive circuit breaker.

Kaufman Efficiency Ratio (ER) quantifies whether price movement is
directional (trending) or noisy (mean-reverting / ranging):

    ER = |close[t] - close[t-N]| / sum(|close[i] - close[i-1]|)

    ER > 0.6 → strong trend (toxic for mean-reversion strategies)
    ER < 0.3 → mean-reverting / ranging (safe for entry)
    0.3 ≤ ER ≤ 0.6 → transitional

Reference: Kaufman, P. "Trading Systems and Methods" (5th ed, 2013), Ch 6.
"""

from __future__ import annotations

import numpy as np


def compute_kaufman_er(prices: list[float] | np.ndarray, period: int = 10) -> float:
    """Compute Kaufman Efficiency Ratio over the last `period` bars.

    Args:
        prices: Sequential close/mid prices, most recent last.
        period: Lookback window. Must be >= 2 and <= len(prices).

    Returns:
        ER in [0.0, 1.0]. Returns 0.0 if insufficient data.
    """
    if len(prices) < max(period, 2):
        return 0.0

    p = np.asarray(prices[-period:], dtype=np.float64)
    if len(p) < 2:
        return 0.0

    direction = abs(p[-1] - p[0])
    volatility = float(np.sum(np.abs(np.diff(p))))
    if volatility < 1e-10:
        return 0.0

    er = direction / volatility
    return float(np.clip(er, 0.0, 1.0))


def check_market_normalized(
    *,
    current_atr: float,
    rolling_atr_mean: float,
    rolling_atr_std: float,
    kaufman_er: float,
    atr_threshold: float = 1.2,
    er_threshold: float = 0.5,
) -> tuple[bool, str]:
    """Check if market conditions have normalized after a circuit-breaker trip.

    Returns (is_normalized, reason).
    """
    atr_ratio = current_atr / max(rolling_atr_mean, 1e-8)
    vol_ok = atr_ratio < atr_threshold
    er_ok = kaufman_er < er_threshold

    if vol_ok:
        return True, f"vol_normalized_atr_ratio_{atr_ratio:.2f}"
    if er_ok:
        return True, f"er_safe_{kaufman_er:.2f}"
    return False, f"toxic_vol_{atr_ratio:.2f}_er_{kaufman_er:.2f}"
