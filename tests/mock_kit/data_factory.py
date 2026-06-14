"""Synthetic market data generators for testing.

Extracted from tests/execution/conftest.py — the canonical OHLC bar generators,
feature vector factories, and regime info helpers.

All generators use deterministic seeds where applicable.
Use `create_synthetic_ohlc_bars()` as the unified entry point.
"""

from __future__ import annotations

import math
import random
from typing import Any

import numpy as np


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------
def create_synthetic_ohlc_bars(
    n: int = 50,
    *,
    mode: str = "trending",
    start_price: float = 2000.0,
    step: float = 0.5,
    noise: float = 0.15,
    center: float = 2000.0,
    amplitude: float = 5.0,
    sigma: float = 1.0,
    seed: int = 42,
) -> list[dict[str, float]]:
    """Generate synthetic OHLC bars in the specified mode.

    Args:
        n: Number of bars to generate.
        mode: "trending", "ranging", or "random_walk".
        start_price: Starting price for trending/random_walk modes.
        step: Trend strength per bar (trending mode).
        noise: Noise scale factor (trending mode).
        center: Center price for ranging mode.
        amplitude: Oscillation amplitude for ranging mode.
        sigma: Volatility for random_walk mode.
        seed: Random seed for reproducibility.

    Returns:
        List of dicts with keys: open, high, low, close.
    """
    if mode == "trending":
        return generate_trending_bars(n, start_price, step, noise, seed=seed)
    elif mode == "ranging":
        return generate_ranging_bars(n, center, amplitude)
    elif mode == "random_walk":
        return generate_random_walk_bars(n, start_price, sigma, seed=seed)
    else:
        raise ValueError(f"Unknown mode: {mode}. Use 'trending', 'ranging', or 'random_walk'.")


# ---------------------------------------------------------------------------
# Individual generators
# ---------------------------------------------------------------------------
def generate_trending_bars(
    n: int = 50,
    start_price: float = 2000.0,
    step: float = 0.5,
    noise: float = 0.15,
    *,
    seed: int = 42,
) -> list[dict[str, float]]:
    """Generate synthetic OHLC bars in a strong uptrend with realistic noise."""
    rng = random.Random(seed)
    bars: list[dict[str, float]] = []
    price = start_price
    for _ in range(n):
        jitter = rng.gauss(0.0, noise)
        o = price + jitter * 0.3
        h = price + 2.0 + abs(jitter) * 0.5
        l = price - 0.3 - abs(jitter) * 0.5
        c = price + 1.5 + jitter
        bars.append({"open": o, "high": h, "low": l, "close": c})
        price += step + jitter * 0.1
    return bars


def generate_ranging_bars(
    n: int = 50, center: float = 2000.0, amplitude: float = 5.0
) -> list[dict[str, float]]:
    """Generate synthetic OHLC bars in a ranging/mean-reverting pattern."""
    bars: list[dict[str, float]] = []
    for i in range(n):
        offset = math.sin(i * 0.3) * amplitude
        price = center + offset
        bars.append(
            {
                "open": price - 0.2,
                "high": price + 1.0,
                "low": price - 1.0,
                "close": price + 0.2,
            }
        )
    return bars


def generate_random_walk_bars(
    n: int = 50,
    start_price: float = 2000.0,
    sigma: float = 1.0,
    *,
    seed: int = 42,
) -> list[dict[str, float]]:
    """Generate synthetic OHLC bars following a random walk."""
    rng = random.Random(seed)
    bars: list[dict[str, float]] = []
    price = start_price
    for _ in range(n):
        o = price
        change = rng.gauss(0, sigma)
        c = price + change
        h = max(o, c) + abs(change) * 0.5
        l = min(o, c) - abs(change) * 0.5
        bars.append({"open": o, "high": h, "low": l, "close": c})
        price = c
    return bars


# ---------------------------------------------------------------------------
# Feature vectors
# ---------------------------------------------------------------------------
def create_mock_feature_vector(dim: int = 40, *, seed: int = 42) -> np.ndarray:
    """Create a deterministic mock feature vector of specified dimension.

    Args:
        dim: Feature dimension (40 for institutional V9, 9 for micro).
        seed: Random seed.

    Returns:
        numpy float32 array of shape (dim,).
    """
    rng = np.random.default_rng(seed)
    return rng.normal(0, 1, (dim,)).astype(np.float32)


# ---------------------------------------------------------------------------
# Regime info
# ---------------------------------------------------------------------------
def create_mock_regime_info(
    *,
    regime: str = "normal",
    adx: float = 25.0,
    atr: float = 5.0,
    trend_direction: str = "long",
    trend_strength: float = 0.3,
) -> dict[str, Any]:
    """Build a regime info dict matching RegimeGate.classify() output.

    Args:
        regime: "trending", "ranging", or "normal".
        adx: ADX value.
        atr: ATR value.
        trend_direction: "long" or "short".
        trend_strength: Trend strength [0, 1].

    Returns:
        Dict with all fields expected by downstream consumers.
    """
    return {
        "regime": regime,
        "adx": adx,
        "di_plus": 30.0,
        "di_minus": 20.0,
        "atr": atr,
        "trend_direction": trend_direction,
        "trend_strength": trend_strength,
        "h1_trend_direction": trend_direction,
        "h1_trend_strength": trend_strength,
        "primary_trend": trend_direction,
        "strategy_gates": {
            "barrier_12bar": "full",
            "micro_3bar": "full",
            "statarb_dynamic": "full",
        },
    }
