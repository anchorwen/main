"""H1-scale directional features computed from M5 mid-price buffer.

DQAF-20260707-003: The 41-dim btc_macro_enhanced schema lacks H1-timescale
directional signal — Wasserstein=0.0084 proved the model cannot distinguish
LONG from SHORT.  These 7 features capture 1-4 hour momentum, volatility,
and multi-scale divergence from the existing M5 price buffer.

Pure function contract: zero I/O, zero global state, same input → same output.
Mirrors the pattern of ``compute_tf_ou_hurst()`` — TF-specific features
computed from rolling close prices.

Feature design (7 features):
  H1_Ret_1       — Return over ~1 hour (12 M5 bars)
  H1_Ret_2       — Return over ~2 hours (24 M5 bars)
  H1_Ret_4       — Return over ~4 hours (48 M5 bars)
  H1_Realized_Vol — Std of 12-bar returns (hourly vol proxy)
  H1_Ret_Accel    — Return acceleration: Ret_1 - Ret_2
  H1_MeanRev      — Z-score distance from 24-bar moving average
  H1_M5_Div       — Multi-scale divergence: |H1_Ret - M5_Ret|/sum
"""

from __future__ import annotations

import numpy as np

# ── Feature names (order matches schema) ──────────────────────────────────
H1_DIRECTIONAL_FEATURE_NAMES: list[str] = [
    "H1_Ret_1",
    "H1_Ret_2",
    "H1_Ret_4",
    "H1_Realized_Vol",
    "H1_Ret_Accel",
    "H1_MeanRev",
    "H1_M5_Div",
]

N_H1_FEATURES: int = len(H1_DIRECTIONAL_FEATURE_NAMES)  # 7


def compute_h1_directional_features(
    mid_prices: list[float] | np.ndarray,
) -> dict[str, float]:
    """Compute 7 H1-scale directional features from a buffer of M5 mid prices.

    Requires at least 50 M5 bars (~4.2 hours) for the 48-bar Ret_4 horizon
    plus one extra for the current bar.

    Args:
        mid_prices: Chronological M5 mid prices, most recent last.
                    Minimum 49 elements (48 history + 1 current).

    Returns:
        Dict mapping feature name → float value.  Returns zeros when the
        buffer is too short (cold-start / bootstrap guard).
    """
    if mid_prices is None or len(mid_prices) < 49:
        return {k: 0.0 for k in H1_DIRECTIONAL_FEATURE_NAMES}

    prices = np.asarray(mid_prices, dtype=np.float64)
    current = prices[-1]

    # ── Price returns at H1 horizons ──
    # 12 M5 bars ≈ 1 hour, 24 ≈ 2 hours, 48 ≈ 4 hours
    p_12 = prices[-13]  # 12 bars before current
    p_24 = prices[-25]  # 24 bars before current
    p_48 = prices[-49]  # 48 bars before current

    h1_ret_1 = float((current - p_12) / p_12) if p_12 > 0 else 0.0
    h1_ret_2 = float((current - p_24) / p_24) if p_24 > 0 else 0.0
    h1_ret_4 = float((current - p_48) / p_48) if p_48 > 0 else 0.0

    # ── Realized volatility (12-bar returns std) ──
    rets_12 = np.diff(prices[-13:]) / prices[-14:-1]
    h1_realized_vol = float(np.std(rets_12)) if len(rets_12) > 1 else 0.0

    # ── Return acceleration ──
    h1_ret_accel = h1_ret_1 - h1_ret_2

    # ── Mean reversion: z-score from 24-bar MA ──
    ma_24 = float(np.mean(prices[-25:]))
    std_24 = float(np.std(prices[-25:]))
    if std_24 > 0 and ma_24 > 0:
        h1_mean_rev = float((current - ma_24) / std_24)
    else:
        h1_mean_rev = 0.0

    # ── Multi-scale divergence: short (1h) vs longer (4h) momentum ──
    # DQAF-20260707-003v2: Changed from H1-vs-M5 (requiring M5 granularity)
    # to H1-vs-H4 divergence so training (H1 bars) and serving (M5 buffer)
    # compute the same conceptual feature.
    denom = abs(h1_ret_1) + abs(h1_ret_4) + 1e-10
    h1_m5_div = float(abs(h1_ret_1 - h1_ret_4) / denom)

    # ── NaN guard ──
    result = {
        "H1_Ret_1": h1_ret_1,
        "H1_Ret_2": h1_ret_2,
        "H1_Ret_4": h1_ret_4,
        "H1_Realized_Vol": h1_realized_vol,
        "H1_Ret_Accel": h1_ret_accel,
        "H1_MeanRev": h1_mean_rev,
        "H1_M5_Div": h1_m5_div,
    }

    for k in result:
        v = result[k]
        if v is None or not np.isfinite(float(v)):
            result[k] = 0.0
        else:
            result[k] = round(float(v), 8)

    return result
