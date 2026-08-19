"""Property-based tests for StructuralSwingV1 (Tier 1 — Capital Path).

Phase 3: Pure rule-based strategy, no ML, no I/O.
All indicator functions are static methods with numpy-only math.
Target: ≥85% line coverage on core/strategies/structural_swing_v1.py.
"""

from __future__ import annotations

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

from core.strategies.structural_swing_v1 import StructuralSwingV1


# ============================================================================
# EMA tests
# ============================================================================
@given(
    array=hnp.arrays(
        dtype=np.float64,
        shape=st.integers(5, 200),
        elements=st.floats(1000.0, 3000.0, allow_nan=False, allow_infinity=False),
    ),
    period=st.integers(2, 50),
)
@settings(max_examples=200)
def test_ema_output_shape_and_finite(array: np.ndarray, period: int) -> None:
    """EMA output must have same shape as input, and no NaN beyond warmup."""
    result = StructuralSwingV1._ema(array, period)

    assert result.shape == array.shape
    assert result.dtype == np.float64

    # First (period-1) values may be NaN (warmup)
    if len(array) >= period:
        assert np.all(np.isnan(result[: period - 1]))
        # After warmup, all values must be finite
        assert np.all(np.isfinite(result[period - 1 :]))


def test_ema_short_data_returns_all_nan() -> None:
    """EMA with data shorter than period must return all NaN."""
    data = np.array([100.0, 200.0, 300.0], dtype=np.float64)
    result = StructuralSwingV1._ema(data, period=10)
    assert np.all(np.isnan(result))


@given(period=st.integers(5, 20))
@settings(max_examples=50)
def test_ema_constant_input_produces_constant_output(period: int) -> None:
    """EMA of constant values must converge to the constant."""
    n = period * 5
    data = np.full(n, 1000.0, dtype=np.float64)
    result = StructuralSwingV1._ema(data, period)

    # After warmup, EMA should be very close to 1000.0
    tail = result[period * 2 :]  # give it time to converge
    assert np.all(np.isfinite(tail))
    np.testing.assert_allclose(tail, 1000.0, atol=0.01)


# ============================================================================
# ATR tests
# ============================================================================
@given(
    n=st.integers(20, 100),
    period=st.integers(2, 14),
)
@settings(max_examples=100)
def test_atr_output_shape(n: int, period: int) -> None:
    """ATR output must have same shape as input."""
    rng = np.random.default_rng(42)
    highs = 2000.0 + rng.normal(0, 5, n).cumsum()
    lows = highs - 5.0 - rng.random(n) * 2
    closes = lows + rng.random(n) * 5

    result = StructuralSwingV1._atr(highs, lows, closes, period)

    assert result.shape == closes.shape
    # After warmup, ATR must be positive
    valid = result[period + 1 :]
    if len(valid) > 0:
        assert np.all(valid > 0), f"ATR must be positive, got min={valid.min()}"


def test_atr_short_data() -> None:
    """ATR with data shorter than period+1 must return all NaN."""
    data = np.array([100.0, 200.0], dtype=np.float64)
    result = StructuralSwingV1._atr(data, data, data, period=14)
    assert np.all(np.isnan(result))


# ============================================================================
# Trend filter tests
# ============================================================================
def test_trend_filter_requires_minimum_data() -> None:
    """_check_trend with insufficient data returns (False, 'neutral', 0)."""
    strat = StructuralSwingV1(ema_slow=50)
    closes = np.ones(30, dtype=np.float64) * 2000.0
    atr = np.ones(30, dtype=np.float64)

    allowed, direction, diff = strat._check_trend(closes, atr, idx=29)

    assert allowed is False
    assert direction == "neutral"


@given(
    trend_strength=st.floats(0.1, 5.0, allow_nan=False),
    noise=st.floats(0.0, 3.0, allow_nan=False),
)
@settings(max_examples=100)
def test_strong_uptrend_detected_long(trend_strength: float, noise: float) -> None:
    """Strong uptrend must be detected as 'long'."""
    strat = StructuralSwingV1(ema_fast=5, ema_slow=20, ema_threshold_atr_mult=0.3)
    n = 100
    rng = np.random.default_rng(42)
    base = 2000.0 + np.arange(n, dtype=np.float64) * trend_strength
    closes = base + rng.normal(0, noise, n)
    atr = np.full(n, max(noise * 3, 1.0), dtype=np.float64)

    allowed, direction, _diff = strat._check_trend(closes, atr, idx=n - 1)

    # Strong uptrend should be detected
    if trend_strength > 0.5:
        assert allowed is True
        assert direction == "long"


@given(
    drop_magnitude=st.floats(1.0, 10.0, allow_nan=False),
)
@settings(max_examples=100)
def test_strong_downtrend_detected_short(drop_magnitude: float) -> None:
    """Strong downtrend must be detected as 'short'."""
    strat = StructuralSwingV1(ema_fast=5, ema_slow=20, ema_threshold_atr_mult=0.3)
    n = 100
    rng = np.random.default_rng(42)
    base = 2000.0 - np.arange(n, dtype=np.float64) * drop_magnitude
    closes = base + rng.normal(0, 1.0, n)
    atr = np.full(n, max(drop_magnitude, 1.0), dtype=np.float64)

    allowed, direction, _diff = strat._check_trend(closes, atr, idx=n - 1)

    if drop_magnitude > 1.0:
        assert allowed is True
        assert direction == "short"


def test_nan_atr_blocks_trend() -> None:
    """NaN ATR must return neutral regardless of trend."""
    strat = StructuralSwingV1()
    closes = np.arange(100, dtype=np.float64) + 2000.0
    atr = np.full(100, np.nan, dtype=np.float64)

    allowed, direction, _diff = strat._check_trend(closes, atr, idx=99)
    assert allowed is False


# ============================================================================
# Barrier computation tests
# ============================================================================
@given(
    direction=st.sampled_from(["long", "short"]),
    ref_price=st.floats(1000.0, 5000.0, allow_nan=False, allow_infinity=False),
    atr_val=st.floats(0.5, 20.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=200)
def test_barrier_sl_tp_ordering(direction: str, ref_price: float, atr_val: float) -> None:
    """SL must always protect against adverse movement, TP in favorable direction."""
    strat = StructuralSwingV1(sl_atr_mult=3.0, tp_atr_mult=1.5)
    entry, sl, tp = strat._compute_barriers(direction, ref_price, atr_val)

    assert entry > 0
    assert sl > 0
    assert tp > 0

    if direction == "long":
        assert sl < entry, f"Long SL ({sl}) must be below entry ({entry})"
        assert tp > entry, f"Long TP ({tp}) must be above entry ({entry})"
    else:
        assert sl > entry, f"Short SL ({sl}) must be above entry ({entry})"
        assert tp < entry, f"Short TP ({tp}) must be below entry ({entry})"


@given(
    sl_mult=st.floats(1.0, 5.0),
    tp_mult=st.floats(0.5, 3.0),
    atr_val=st.floats(0.5, 20.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100)
def test_barrier_tp_minimum_floor(sl_mult: float, tp_mult: float, atr_val: float) -> None:
    """TP distance must never be less than 0.3 × SL distance."""
    strat = StructuralSwingV1(sl_atr_mult=sl_mult, tp_atr_mult=tp_mult)
    entry, sl, tp = strat._compute_barriers("long", 2000.0, atr_val)

    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)

    assert (
        tp_dist >= sl_dist * 0.3 - 1e-9
    ), f"TP dist ({tp_dist:.4f}) < 0.3 × SL dist ({sl_dist:.4f})"


# ============================================================================
# Full evaluate() tests
# ============================================================================
def test_evaluate_needs_sufficient_history() -> None:
    """evaluate() with < 50 bars must return None."""
    strat = StructuralSwingV1()
    n = 30
    arr = np.ones(n, dtype=np.float64) * 2000.0

    result = strat.evaluate(arr, arr, arr, arr, arr, bar_index=n - 1)
    assert result is None


@given(seed=st.integers(0, 1000))
@settings(max_examples=50)
def test_evaluate_with_random_data_never_crashes(seed: int) -> None:
    """evaluate() must never crash, even on random/wild price data."""
    rng = np.random.default_rng(seed)
    n = 200
    # Random walk with occasional extreme moves
    m5_close = 2000.0 + rng.normal(0, 2, n).cumsum()
    m5_open = m5_close + rng.normal(0, 0.5, n)
    m5_high = np.maximum(m5_open, m5_close) + rng.random(n) * 3
    m5_low = np.minimum(m5_open, m5_close) - rng.random(n) * 3
    h1_close = 2000.0 + rng.normal(0, 5, n).cumsum()

    strat = StructuralSwingV1()

    for idx in range(50, n):
        result = strat.evaluate(m5_open, m5_high, m5_low, m5_close, h1_close, bar_index=idx)
        # Must not crash. May return None or a signal.
        if result is not None:
            assert result.direction in ("long", "short")
            assert result.entry_price > 0
            assert result.stop_loss > 0
            assert result.take_profit > 0
