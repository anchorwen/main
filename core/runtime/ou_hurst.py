"""OU Theta + Hurst Exponent computation — pure functions extracted from live_cycle.py.

Strangler Fig #14: extracted from ``_compute_tf_ou_hurst()`` in live_cycle.py.
Pure function contract: zero I/O, zero global state, same input → same output.

Mirrors the training-side computation in
``scripts/training/build_swing_enhanced_dataset.py:_ou_theta()`` and ``_hurst()``.
Keeping inference and training in sync prevents train-serve skew for tree models.

Used by:
  - Management phase: inject OU/Hurst into meta-feature vector
  - Entry evaluation: OU Theta + Hurst for StatArb mean-reversion signals
  - Physics-based override (FIX-20260613-090): OU Theta > 0.5 AND Hurst < 0.48 → "ranging"

Related FIXes: FIX-20260529-028 (creation), FIX-20260613-090-Step1 (physics override)
"""

from __future__ import annotations

import math

import numpy as np


def compute_tf_ou_hurst(mid_prices: list[float]) -> tuple[float, float]:
    """Compute TF_OU_Theta and TF_Hurst from rolling M5 mid prices.

    Uses the most recent 21 M5 close prices (~105 min history).
    Returns (ou_theta, hurst) — defaults (0.0, 0.5) on insufficient data.

    OU Theta: AR(1) mean-reversion coefficient.
      - theta > 0: mean-reverting (stationary)
      - theta ≈ 0: random walk (unit root)
      - theta < 0: explosive (diverging — physically impossible for prices)

    Hurst exponent (R/S method on n=20 window):
      - H > 0.5: trending / persistent
      - H ≈ 0.5: Brownian motion
      - H < 0.5: mean-reverting / anti-persistent

    Args:
        mid_prices: List of mid prices, most recent last. Minimum 21 elements.

    Returns:
        (ou_theta, hurst) tuple. Defaults to (0.0, 0.5) when data is insufficient.
    """
    if len(mid_prices) < 21:
        return 0.0, 0.5
    window = np.array(mid_prices[-21:], dtype=np.float64)
    # OU Theta: AR(1) mean-reversion coefficient
    y = window[1:]
    x = window[:-1]
    x_mean = float(np.mean(x))
    y_mean = float(np.mean(y))
    beta_num = float(np.sum((x - x_mean) * (y - y_mean)))
    beta_den = float(np.sum((x - x_mean) ** 2))
    if beta_den == 0:
        ou_theta = 0.0
    else:
        beta = np.clip(beta_num / beta_den, 1e-8, 0.99999999)
        ou_theta = float(-math.log(beta))
    # Hurst: R/S exponent
    s = float(np.std(window))
    if s == 0:
        hurst = 0.5
    else:
        mean_v = float(np.mean(window))
        z = np.cumsum(window - mean_v)
        r = float(np.max(z) - np.min(z))
        rs = r / s
        hurst = float(math.log(rs) / math.log(20)) if rs > 0 else 0.5
    return ou_theta, hurst
