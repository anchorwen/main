"""Standardized extreme market data generators for institutional stress testing.

Covers 9 toxicity patterns sourced from FIX_REGISTRY historical failures
and industry extreme events (Flash Crash 2010, SNB 2015, COVID 2020).

Each generator returns (DataFrame, stress_metadata) where stress_metadata
contains `expected_failure_mode` — the toxicity type this scenario is designed to trigger.
Tests assert that the system either handles the scenario correctly or fails
in the expected way (never silently).

Hypothesis integration: each generator's companion `_strategy()` function
exposes the scenario for @hypothesis.given() property-based testing.

Covered toxicity patterns:
  1. flash_crash      — Price → 0 instant (divide-by-zero, margin spiral)
  2. liquidity_vacuum  — Zero volume (VWAP NaN, liquidity metric collapse)
  3. nan_cascade       — Consecutive NaN bars (feature computation contamination)
  4. spread_explosion  — 1000× spread (entry cost calculation, pre-trade guard)
  5. price_reversal    — V-reversal without retry (trailing SL, exit watchdog)
  6. regime_snap       — ADX 10→80 instant (regime gate transition)
  7. zero_atr          — ATR → 0 (SL distance division, trail freeze)
  8. duplicate_timestamps — Out-of-order + duplicate timestamps (look-ahead bias)
  9. extreme_gap       — 15% weekend gap (gap protection, position close)
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# hypothesis integration (optional — graceful if hypothesis not installed)
# ---------------------------------------------------------------------------
try:
    from hypothesis import strategies as st
    from hypothesis.extra.numpy import arrays as hnp_arrays
    from hypothesis.extra.pandas import data_frames, column, range_indexes

    _HYPOTHESIS_AVAILABLE = True
except ImportError:
    _HYPOTHESIS_AVAILABLE = False


# ============================================================================
# 1. Flash Crash
# ============================================================================
def generate_flash_crash(
    bars: int = 100, crash_at: int = 80, *, seed: int = 42
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Price crashes to near-zero at bar N, then recovers partially.

    Toxicity: divide-by-zero in margin calculation, PnL infinity,
    position liquidation at absurd prices.
    """
    rng = np.random.default_rng(seed)
    base_price = 2000.0
    timestamps = pd.date_range("2026-01-01", periods=bars, freq="5min")

    opens = np.full(bars, np.nan, dtype=np.float64)
    highs = np.full(bars, np.nan, dtype=np.float64)
    lows = np.full(bars, np.nan, dtype=np.float64)
    closes = np.full(bars, np.nan, dtype=np.float64)
    volumes = np.full(bars, np.nan, dtype=np.float64)

    for i in range(crash_at):
        o = base_price + rng.normal(0, 2)
        c = o + rng.normal(0, 3)
        opens[i] = o
        highs[i] = max(o, c) + abs(rng.normal(0, 1))
        lows[i] = min(o, c) - abs(rng.normal(0, 1))
        closes[i] = c
        volumes[i] = abs(rng.normal(100, 20))

    # Crash bar
    opens[crash_at] = closes[crash_at - 1]
    lows[crash_at] = 0.01
    highs[crash_at] = closes[crash_at - 1]
    closes[crash_at] = 0.05
    volumes[crash_at] = abs(rng.normal(10000, 2000))

    # Post-crash
    partial_recovery = 800.0
    for i in range(crash_at + 1, bars):
        o = partial_recovery + rng.normal(0, 5)
        c = o + rng.normal(0, 10)
        opens[i] = o
        highs[i] = max(o, c) + abs(rng.normal(0, 3))
        lows[i] = min(o, c) - abs(rng.normal(0, 3))
        closes[i] = c
        volumes[i] = abs(rng.normal(200, 50))
        partial_recovery = c

    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "spread": np.full(bars, 0.5, dtype=np.float64),
        }
    )

    metadata = {
        "scenario": "flash_crash",
        "expected_failure_mode": "divide_by_zero_or_margin_spiral",
        "crash_bar_index": crash_at,
        "pre_crash_price": base_price,
        "crash_price": 0.05,
        "description": "Price collapses to near-zero in a single bar — "
        "tests margin check, PnL calculation, and liquidation logic.",
    }
    return df, metadata


if _HYPOTHESIS_AVAILABLE:

    def flash_crash_strategy() -> st.SearchStrategy[pd.DataFrame]:
        """Hypothesis strategy: random crash location and pre-crash price."""
        return st.builds(
            lambda crash_at, base_price: generate_flash_crash(
                bars=100, crash_at=max(5, min(95, crash_at)), seed=42
            )[0],
            crash_at=st.integers(5, 95),
            base_price=st.floats(100.0, 5000.0),
        )


# ============================================================================
# 2. Liquidity Vacuum
# ============================================================================
def generate_liquidity_vacuum(
    bars: int = 100, *, seed: int = 42
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Consecutive bars with zero volume — simulates market halt or delisting scare.

    Toxicity: VWAP → NaN, liquidity metrics divide-by-zero,
    ATR-based sizing collapses.
    """
    rng = np.random.default_rng(seed)
    timestamps = pd.date_range("2026-01-01", periods=bars, freq="5min")
    price = 2000.0

    opens = np.full(bars, np.nan, dtype=np.float64)
    highs = np.full(bars, np.nan, dtype=np.float64)
    lows = np.full(bars, np.nan, dtype=np.float64)
    closes = np.full(bars, np.nan, dtype=np.float64)
    volumes = np.full(bars, np.nan, dtype=np.float64)

    for i in range(bars):
        change = rng.normal(0, 0.5)
        opens[i] = price
        closes[i] = price + change
        highs[i] = max(opens[i], closes[i]) + 0.1
        lows[i] = min(opens[i], closes[i]) - 0.1
        # Zero volume for bars 20-60
        volumes[i] = 0.0 if 20 <= i <= 60 else abs(rng.normal(100, 20))
        price = closes[i]

    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "spread": np.full(bars, 0.5, dtype=np.float64),
        }
    )

    metadata = {
        "scenario": "liquidity_vacuum",
        "expected_failure_mode": "nan_propagation_from_zero_volume",
        "zero_volume_bars": 41,  # bars 20-60 inclusive
        "description": "40+ consecutive bars with zero volume — "
        "tests VWAP, liquidity metrics, and volume-weighted calculations.",
    }
    return df, metadata


if _HYPOTHESIS_AVAILABLE:

    def liquidity_vacuum_strategy() -> st.SearchStrategy[pd.DataFrame]:
        return st.builds(
            lambda bars: generate_liquidity_vacuum(bars=bars, seed=42)[0],
            bars=st.integers(20, 200),
        )


# ============================================================================
# 3. NaN Cascade
# ============================================================================
def generate_nan_cascade(
    bars: int = 100, *, seed: int = 42
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Consecutive bars with NaN OHLC — simulates data feed corruption.

    Toxicity: feature computation contamination — one NaN bar
    can propagate NaN through rolling windows, exponential MAs,
    and all downstream features.
    """
    rng = np.random.default_rng(seed)
    timestamps = pd.date_range("2026-01-01", periods=bars, freq="5min")
    price = 2000.0

    opens = np.full(bars, np.nan, dtype=np.float64)
    highs = np.full(bars, np.nan, dtype=np.float64)
    lows = np.full(bars, np.nan, dtype=np.float64)
    closes = np.full(bars, np.nan, dtype=np.float64)

    nan_start = 30
    nan_end = 45

    for i in range(bars):
        if nan_start <= i <= nan_end:
            opens[i] = np.nan
            highs[i] = np.nan
            lows[i] = np.nan
            closes[i] = np.nan
        else:
            change = rng.normal(0, 1.0)
            opens[i] = price
            closes[i] = price + change
            highs[i] = max(opens[i], closes[i]) + abs(rng.normal(0, 0.5))
            lows[i] = min(opens[i], closes[i]) - abs(rng.normal(0, 0.5))
            price = closes[i]

    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.normal(100, 20, bars),
            "spread": np.full(bars, 0.5, dtype=np.float64),
        }
    )

    metadata = {
        "scenario": "nan_cascade",
        "expected_failure_mode": "nan_propagation_in_features",
        "nan_bar_range": (nan_start, nan_end),
        "nan_bar_count": nan_end - nan_start + 1,
        "description": "15 consecutive NaN OHLC bars — "
        "tests feature assembler resilience, rolling window NaN handling, "
        "and whether NaN propagates to brain inference.",
    }
    return df, metadata


if _HYPOTHESIS_AVAILABLE:

    def nan_cascade_strategy() -> st.SearchStrategy[pd.DataFrame]:
        return st.builds(
            lambda nan_start, nan_len: generate_nan_cascade(
                bars=max(nan_start + nan_len + 10, 50)
            )[0],
            nan_start=st.integers(5, 60),
            nan_len=st.integers(3, 30),
        )


# ============================================================================
# 4. Spread Explosion
# ============================================================================
def generate_spread_explosion(
    bars: int = 100, factor: float = 1000.0, *, seed: int = 42
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Normal market then spread instantly multiplies by factor.

    Toxicity: entry cost becomes absurd, pre-trade guards must reject.
    If guards fail, system opens positions with impossible costs.
    """
    rng = np.random.default_rng(seed)
    timestamps = pd.date_range("2026-01-01", periods=bars, freq="5min")
    price = 2000.0

    opens = np.full(bars, np.nan, dtype=np.float64)
    highs = np.full(bars, np.nan, dtype=np.float64)
    lows = np.full(bars, np.nan, dtype=np.float64)
    closes = np.full(bars, np.nan, dtype=np.float64)
    spreads = np.full(bars, np.nan, dtype=np.float64)

    explosion_start = 50

    for i in range(bars):
        change = rng.normal(0, 1.0)
        opens[i] = price
        closes[i] = price + change
        highs[i] = max(opens[i], closes[i]) + 0.5
        lows[i] = min(opens[i], closes[i]) - 0.5
        spreads[i] = 0.5 if i < explosion_start else 0.5 * factor
        price = closes[i]

    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.normal(100, 20, bars),
            "spread": spreads,
        }
    )

    metadata = {
        "scenario": "spread_explosion",
        "expected_failure_mode": "entry_cost_overflow_or_pre_trade_reject",
        "explosion_factor": factor,
        "explosion_start_bar": explosion_start,
        "normal_spread": 0.5,
        "exploded_spread": 0.5 * factor,
        "description": f"Spread instantaneously multiplies by {factor:.0f}× — "
        "tests pre-trade guard spread check, entry cost calculation, "
        "and margin allocation.",
    }
    return df, metadata


if _HYPOTHESIS_AVAILABLE:

    def spread_explosion_strategy() -> st.SearchStrategy[pd.DataFrame]:
        return st.builds(
            lambda factor: generate_spread_explosion(factor=factor, seed=42)[0],
            factor=st.floats(10.0, 10000.0),
        )


# ============================================================================
# 5. Price Reversal (V-Reversal, No Retry)
# ============================================================================
def generate_price_reversal_no_retry(
    bars: int = 100, *, seed: int = 42
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Strong uptrend then instantaneous V-reversal — trend followers trapped.

    Toxicity: trailing SL never activates (price gaps past it),
    exit watchdog must detect extreme adverse move and close.
    """
    rng = np.random.default_rng(seed)
    timestamps = pd.date_range("2026-01-01", periods=bars, freq="5min")
    price = 2000.0

    opens = np.full(bars, np.nan, dtype=np.float64)
    highs = np.full(bars, np.nan, dtype=np.float64)
    lows = np.full(bars, np.nan, dtype=np.float64)
    closes = np.full(bars, np.nan, dtype=np.float64)

    reversal_at = 60
    peak_price = price + reversal_at * 1.5  # ~2090

    # Uptrend phase
    for i in range(reversal_at):
        opens[i] = price
        closes[i] = price + abs(rng.normal(0.8, 0.5))
        highs[i] = closes[i] + abs(rng.normal(0.3, 0.1))
        lows[i] = opens[i] - abs(rng.normal(0.1, 0.05))
        price = closes[i]

    # Reversal bar (gap down past SL)
    gap_down_price = peak_price * 0.85
    opens[reversal_at] = closes[reversal_at - 1]
    closes[reversal_at] = gap_down_price
    highs[reversal_at] = opens[reversal_at]
    lows[reversal_at] = gap_down_price - 10.0

    price = gap_down_price

    # Downtrend continuation
    for i in range(reversal_at + 1, bars):
        opens[i] = price
        closes[i] = price - abs(rng.normal(0.5, 0.3))
        lows[i] = closes[i] - abs(rng.normal(0.5, 0.2))
        highs[i] = opens[i] + abs(rng.normal(0.1, 0.05))
        price = closes[i]

    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.normal(100, 20, bars),
            "spread": np.full(bars, 0.5, dtype=np.float64),
        }
    )

    metadata = {
        "scenario": "price_reversal_no_retry",
        "expected_failure_mode": "trailing_sl_bypassed_or_watchdog_miss",
        "reversal_bar": reversal_at,
        "peak_price": float(peak_price),
        "gap_pct": 15.0,
        "description": "V-reversal with 15% gap — "
        "tests trailing SL activation, exit watchdog response time, "
        "and whether the position survives an adverse gap.",
    }
    return df, metadata


if _HYPOTHESIS_AVAILABLE:

    def price_reversal_strategy() -> st.SearchStrategy[pd.DataFrame]:
        return st.builds(
            lambda reversal_at: generate_price_reversal_no_retry(
                bars=max(reversal_at + 20, 50), seed=42
            )[0],
            reversal_at=st.integers(10, 80),
        )


# ============================================================================
# 6. Regime Snap
# ============================================================================
def generate_regime_snap(
    bars: int = 100, *, seed: int = 42
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Market instantly transitions from low-vol ranging to high-vol trending.

    Toxicity: regime gate doesn't detect transition fast enough,
    strategies optimized for old regime continue operating under wrong assumptions.
    """
    rng = np.random.default_rng(seed)
    timestamps = pd.date_range("2026-01-01", periods=bars, freq="5min")
    price = 2000.0

    opens = np.full(bars, np.nan, dtype=np.float64)
    highs = np.full(bars, np.nan, dtype=np.float64)
    lows = np.full(bars, np.nan, dtype=np.float64)
    closes = np.full(bars, np.nan, dtype=np.float64)

    snap_at = 50

    # Ranging phase: low volatility, mean-reverting
    for i in range(snap_at):
        oscillation = np.sin(i * 0.3) * 3.0
        opens[i] = price + oscillation
        closes[i] = price + oscillation + rng.normal(0, 0.5)
        highs[i] = max(opens[i], closes[i]) + 1.0
        lows[i] = min(opens[i], closes[i]) - 1.0
        price = closes[i]

    # Trending phase: strong directional moves with high volatility
    for i in range(snap_at, bars):
        trend = (i - snap_at) * 3.0
        volatility = 8.0
        opens[i] = price
        closes[i] = price + trend + rng.normal(0, volatility)
        highs[i] = closes[i] + abs(rng.normal(0, volatility * 0.5))
        lows[i] = opens[i] - abs(rng.normal(0, volatility * 0.3))
        price = closes[i]

    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.normal(100, 20, bars),
            "spread": np.full(bars, 0.5, dtype=np.float64),
        }
    )

    metadata = {
        "scenario": "regime_snap",
        "expected_failure_mode": "regime_gate_lag_or_strategy_mismatch",
        "snap_bar": snap_at,
        "pre_snap_mode": "ranging_low_vol",
        "post_snap_mode": "trending_high_vol",
        "description": "Instant regime transition from ranging to trending — "
        "tests regime gate detection latency, strategy re-optimization trigger, "
        "and whether stale regime assumptions cause wrong entries.",
    }
    return df, metadata


if _HYPOTHESIS_AVAILABLE:

    def regime_snap_strategy() -> st.SearchStrategy[pd.DataFrame]:
        return st.builds(
            lambda snap_at: generate_regime_snap(
                bars=max(snap_at + 20, 50), seed=42
            )[0],
            snap_at=st.integers(10, 80),
        )


# ============================================================================
# 7. Zero ATR
# ============================================================================
def generate_zero_atr(
    bars: int = 100, *, seed: int = 42
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Price freezes — identical OHLC for consecutive bars, ATR → 0.

    Toxicity: SL distance = k × ATR = 0 → trail never moves.
    This was the ROOT CAUSE of FIX-20260603-064 (trail activation watermark
    never triggered for $3 micro-bounces).
    """
    rng = np.random.default_rng(seed)
    timestamps = pd.date_range("2026-01-01", periods=bars, freq="5min")

    opens = np.full(bars, np.nan, dtype=np.float64)
    highs = np.full(bars, np.nan, dtype=np.float64)
    lows = np.full(bars, np.nan, dtype=np.float64)
    closes = np.full(bars, np.nan, dtype=np.float64)

    frozen_price = 2000.0
    freeze_start = 30
    freeze_end = 70

    for i in range(bars):
        if freeze_start <= i <= freeze_end:
            opens[i] = frozen_price
            highs[i] = frozen_price + 0.01
            lows[i] = frozen_price - 0.01
            closes[i] = frozen_price
        else:
            change = rng.normal(0, 1.5)
            opens[i] = frozen_price
            closes[i] = frozen_price + change
            highs[i] = max(opens[i], closes[i]) + abs(rng.normal(0, 0.5))
            lows[i] = min(opens[i], closes[i]) - abs(rng.normal(0, 0.5))
            frozen_price = closes[i]

    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.normal(100, 20, bars),
            "spread": np.full(bars, 0.5, dtype=np.float64),
        }
    )

    metadata = {
        "scenario": "zero_atr",
        "expected_failure_mode": "sl_distance_zero_or_trail_freeze",
        "freeze_bar_range": (freeze_start, freeze_end),
        "frozen_price": frozen_price,
        "description": "40 consecutive bars with near-identical OHLC — "
        "tests ATR calculation floor, SL distance minimum, "
        "and trailing SL activation (FIX-20260603-064 root cause).",
    }
    return df, metadata


if _HYPOTHESIS_AVAILABLE:

    def zero_atr_strategy() -> st.SearchStrategy[pd.DataFrame]:
        return st.builds(
            lambda freeze_len: generate_zero_atr(
                bars=max(freeze_len + 30, 50), seed=42
            )[0],
            freeze_len=st.integers(5, 60),
        )


# ============================================================================
# 8. Duplicate / Out-of-Order Timestamps
# ============================================================================
def generate_duplicate_timestamps(
    bars: int = 100, *, seed: int = 42
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Bars arrive with duplicate and out-of-order timestamps.

    Toxicity: look-ahead bias — if the system doesn't enforce temporal ordering,
    feature computation at time T may read data from time T+1.
    """
    rng = np.random.default_rng(seed)
    base_timestamps = pd.date_range("2026-01-01", periods=bars, freq="5min")

    # Inject duplicates and out-of-order entries
    timestamps = list(base_timestamps)
    # Duplicate bar 25
    timestamps.insert(26, timestamps[25])
    # Swap bars 60 and 61
    timestamps[60], timestamps[61] = timestamps[61], timestamps[60]
    # Duplicate bar 80
    timestamps.insert(81, timestamps[80])

    actual_bars = len(timestamps)
    price = 2000.0

    opens = np.full(actual_bars, np.nan, dtype=np.float64)
    highs = np.full(actual_bars, np.nan, dtype=np.float64)
    lows = np.full(actual_bars, np.nan, dtype=np.float64)
    closes = np.full(actual_bars, np.nan, dtype=np.float64)

    for i in range(actual_bars):
        change = rng.normal(0, 1.0)
        opens[i] = price
        closes[i] = price + change
        highs[i] = max(opens[i], closes[i]) + 0.5
        lows[i] = min(opens[i], closes[i]) - 0.5
        price = closes[i]

    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.normal(100, 20, actual_bars),
            "spread": np.full(actual_bars, 0.5, dtype=np.float64),
        }
    )

    metadata = {
        "scenario": "duplicate_timestamps",
        "expected_failure_mode": "lookahead_bias_or_duplicate_processing",
        "original_bars": bars,
        "actual_bars": actual_bars,
        "anomalies": "2 duplicates + 1 swap",
        "description": "Bar feed contains duplicate and out-of-order timestamps — "
        "tests temporal ordering enforcement, look-ahead bias guards, "
        "and duplicate bar deduplication.",
    }
    return df, metadata


if _HYPOTHESIS_AVAILABLE:

    def duplicate_timestamps_strategy() -> st.SearchStrategy[pd.DataFrame]:
        return st.builds(
            lambda bars: generate_duplicate_timestamps(bars=max(bars, 10), seed=42)[0],
            bars=st.integers(20, 200),
        )


# ============================================================================
# 9. Extreme Gap (Weekend / Opening Gap)
# ============================================================================
def generate_extreme_gap(
    bars: int = 100, gap_pct: float = 0.15, *, seed: int = 42
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Weekend-style gap: price opens 15% away from previous close.

    Toxicity: gap protection must prevent entry at post-gap price.
    Existing positions may be deep in profit/loss without SL/TP triggering.
    """
    rng = np.random.default_rng(seed)
    timestamps = pd.date_range("2026-01-01", periods=bars, freq="5min")
    price = 2000.0

    opens = np.full(bars, np.nan, dtype=np.float64)
    highs = np.full(bars, np.nan, dtype=np.float64)
    lows = np.full(bars, np.nan, dtype=np.float64)
    closes = np.full(bars, np.nan, dtype=np.float64)

    gap_at = 50
    gap_multiplier = 1.0 + gap_pct  # upward gap
    post_gap_price = price * gap_multiplier

    # Pre-gap: normal trading
    for i in range(gap_at):
        change = rng.normal(0, 1.0)
        opens[i] = price
        closes[i] = price + change
        highs[i] = max(opens[i], closes[i]) + 0.5
        lows[i] = min(opens[i], closes[i]) - 0.5
        price = closes[i]

    pre_gap_close = closes[gap_at - 1]

    # Gap bar: opens at post_gap_price (no touch of pre-gap close)
    opens[gap_at] = post_gap_price
    closes[gap_at] = post_gap_price + rng.normal(0, 2.0)
    highs[gap_at] = max(opens[gap_at], closes[gap_at]) + 1.0
    lows[gap_at] = post_gap_price - 1.0  # does NOT touch pre-gap close

    price = closes[gap_at]

    # Post-gap: normal trading at new level
    for i in range(gap_at + 1, bars):
        change = rng.normal(0, 1.0)
        opens[i] = price
        closes[i] = price + change
        highs[i] = max(opens[i], closes[i]) + 0.5
        lows[i] = min(opens[i], closes[i]) - 0.5
        price = closes[i]

    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.normal(100, 20, bars),
            "spread": np.full(bars, 0.5, dtype=np.float64),
        }
    )

    metadata = {
        "scenario": "extreme_gap",
        "expected_failure_mode": "gap_protection_bypass_or_unfilled_sltp",
        "gap_bar": gap_at,
        "gap_pct": gap_pct * 100,
        "pre_gap_close": float(pre_gap_close),
        "post_gap_price": float(post_gap_price),
        "description": f"{gap_pct*100:.0f}% opening gap — "
        "tests gap protection logic, SL/TP gap-through behavior, "
        "and whether positions survive unexecuted SL due to price skipping.",
    }
    return df, metadata


if _HYPOTHESIS_AVAILABLE:

    def extreme_gap_strategy() -> st.SearchStrategy[pd.DataFrame]:
        return st.builds(
            lambda gap_pct: generate_extreme_gap(gap_pct=gap_pct, seed=42)[0],
            gap_pct=st.floats(0.02, 0.30),
        )


# ============================================================================
# Convenience: all scenarios
# ============================================================================
def all_stress_scenarios() -> list[tuple[str, tuple[pd.DataFrame, dict[str, Any]]]]:
    """Return all 9 stress scenarios for bulk testing.

    Usage:
        for name, (df, meta) in all_stress_scenarios():
            print(f"Testing {name}: {meta['expected_failure_mode']}")
            # run your test with df
    """
    return [
        ("flash_crash", generate_flash_crash()),
        ("liquidity_vacuum", generate_liquidity_vacuum()),
        ("nan_cascade", generate_nan_cascade()),
        ("spread_explosion", generate_spread_explosion()),
        ("price_reversal_no_retry", generate_price_reversal_no_retry()),
        ("regime_snap", generate_regime_snap()),
        ("zero_atr", generate_zero_atr()),
        ("duplicate_timestamps", generate_duplicate_timestamps()),
        ("extreme_gap", generate_extreme_gap()),
    ]
