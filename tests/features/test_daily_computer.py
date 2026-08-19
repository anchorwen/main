"""Tests for core.features.computers.daily_computer — pure indicator functions (Phase 3a).

Covers: _rolling_mean, _rolling_std, _ema_vectorized, _compute_true_range,
_compute_atr_array, _compute_rsi_array, _compute_macd_array,
_compute_bollinger_width_array, _compute_adx_array, _compute_momentum_array,
_build_h4_alignment.
"""

from __future__ import annotations

import numpy as np
import pytest

from core.features.computers.daily_computer import (
    _build_h4_alignment,
    _compute_adx_array,
    _compute_atr_array,
    _compute_bollinger_width_array,
    _compute_macd_array,
    _compute_momentum_array,
    _compute_rsi_array,
    _compute_true_range,
    _ema_vectorized,
    _rolling_mean,
    _rolling_std,
)

# ═══════════════════════════════════════════════════════════════════════════
# _rolling_mean
# ═══════════════════════════════════════════════════════════════════════════


def test_rolling_mean_basic():
    """Simple rolling mean with known values."""
    arr = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    result = _rolling_mean(arr, 3)
    assert np.isnan(result[0])
    assert np.isnan(result[1])
    assert result[2] == pytest.approx(2.0)  # mean of [1,2,3]
    assert result[3] == pytest.approx(3.0)  # mean of [2,3,4]
    assert result[4] == pytest.approx(4.0)  # mean of [3,4,5]


def test_rolling_mean_window_larger_than_array():
    """Window > array length → all NaN."""
    arr = np.array([1.0, 2.0])
    result = _rolling_mean(arr, 5)
    assert np.all(np.isnan(result))


def test_rolling_mean_single_element():
    """Single element → all NaN (window=2 > 1)."""
    arr = np.array([42.0])
    result = _rolling_mean(arr, 2)
    assert len(result) == 1
    assert np.isnan(result[0])


# ═══════════════════════════════════════════════════════════════════════════
# _rolling_std
# ═══════════════════════════════════════════════════════════════════════════


def test_rolling_std_basic():
    """Standard deviation over a known window."""
    arr = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    result = _rolling_std(arr, 3)
    assert np.isnan(result[0])
    assert np.isnan(result[1])
    # Population std of [1,2,3] = sqrt(2/3) ≈ 0.816
    assert result[2] == pytest.approx(0.816, abs=0.01)
    assert result[3] == pytest.approx(0.816, abs=0.01)  # std of [2,3,4] same
    assert result[4] == pytest.approx(0.816, abs=0.01)  # std of [3,4,5] same


def test_rolling_std_insufficient_data():
    """Too few data points → all NaN."""
    arr = np.array([1.0, 2.0])
    result = _rolling_std(arr, 5)
    assert np.all(np.isnan(result))


# ═══════════════════════════════════════════════════════════════════════════
# _ema_vectorized
# ═══════════════════════════════════════════════════════════════════════════


def test_ema_vectorized_basic():
    """EMA of simple series."""
    arr = np.array([1.0, 2.0, 2.0, 2.0])
    result = _ema_vectorized(arr, 3)
    assert result[0] == 1.0
    assert result[1] == 1.5  # 0.5*2 + 0.5*1
    assert len(result) == 4


def test_ema_vectorized_empty():
    """Empty array returns empty."""
    result = _ema_vectorized(np.array([]), 5)
    assert len(result) == 0


def test_ema_approaches_constant():
    """EMA of constant series approaches the constant."""
    arr = np.ones(100)
    result = _ema_vectorized(arr, 10)
    assert abs(result[-1] - 1.0) < 0.01


# ═══════════════════════════════════════════════════════════════════════════
# _compute_true_range
# ═══════════════════════════════════════════════════════════════════════════


def test_true_range_basic():
    """TR = max(H-L, |H-C_prev|, |L-C_prev|)."""
    highs = np.array([10.0, 12.0, 11.0])
    lows = np.array([8.0, 9.0, 8.0])
    closes = np.array([9.0, 11.0, 9.0])
    tr = _compute_true_range(highs, lows, closes)
    assert tr[0] == 2.0  # H-L = 10-8
    # tr[1] = max(12-9=3, |12-9|=3, |9-9|=0) = 3
    assert tr[1] == pytest.approx(3.0)


def test_true_range_same_prices():
    """All same → TR = 0."""
    arr = np.ones(10) * 5.0
    tr = _compute_true_range(arr, arr, arr)
    assert tr[0] == 0.0
    assert np.all(tr[1:] == 0.0)


# ═══════════════════════════════════════════════════════════════════════════
# _compute_atr_array
# ═══════════════════════════════════════════════════════════════════════════


def test_atr_array_basic():
    """ATR array has correct length and initial values."""
    n = 50
    highs = 100 + np.random.randn(n).cumsum() * 0.5 + 2
    lows = highs - np.abs(np.random.randn(n)) * 1.5
    closes = (highs + lows) / 2
    atr = _compute_atr_array(highs, lows, closes, period=14)
    assert len(atr) == n
    # First 14 values should be 0 (burn-in)
    assert atr[13] >= 0


def test_atr_array_insufficient_data():
    """Too few bars → all zeros."""
    atr = _compute_atr_array(
        np.array([10.0, 11.0]), np.array([9.0, 10.0]), np.array([9.5, 10.5]), period=14
    )
    assert np.all(atr == 0.0)


# ═══════════════════════════════════════════════════════════════════════════
# _compute_rsi_array
# ═══════════════════════════════════════════════════════════════════════════


def test_rsi_array_basic():
    """RSI of uptrend should be > 50."""
    n = 100
    closes = np.linspace(100, 130, n) + np.random.randn(n) * 0.3
    rsi = _compute_rsi_array(closes, period=14)
    assert len(rsi) == n
    # Uptrend: RSI should be elevated in the tail
    assert np.median(rsi[50:]) > 50


def test_rsi_array_downtrend():
    """RSI of downtrend should be < 50."""
    n = 100
    closes = np.linspace(130, 100, n) + np.random.randn(n) * 0.3
    rsi = _compute_rsi_array(closes, period=14)
    assert np.median(rsi[50:]) < 50


def test_rsi_array_insufficient_data():
    """Too few bars → all 50.0 (neutral)."""
    rsi = _compute_rsi_array(np.array([1.0, 2.0, 3.0]), period=14)
    assert np.all(rsi == 50.0)


def test_rsi_array_range():
    """All RSI values in [0, 100]."""
    n = 100
    closes = np.random.randn(n).cumsum() + 100
    rsi = _compute_rsi_array(closes, period=14)
    assert np.all(rsi >= 0)
    assert np.all(rsi <= 100)


# ═══════════════════════════════════════════════════════════════════════════
# _compute_macd_array
# ═══════════════════════════════════════════════════════════════════════════


def test_macd_array_basic():
    """MACD line = fast EMA - slow EMA."""
    n = 100
    closes = np.random.randn(n).cumsum() + 100
    macd = _compute_macd_array(closes)
    assert len(macd) == n


def test_macd_flat_line():
    """Flat prices → MACD ≈ 0."""
    closes = np.ones(100) * 50.0
    macd = _compute_macd_array(closes)
    # For flat prices, EMA_fast == EMA_slow → MACD ≈ 0
    assert abs(macd[-1]) < 0.01


# ═══════════════════════════════════════════════════════════════════════════
# _compute_bollinger_width_array
# ═══════════════════════════════════════════════════════════════════════════


def test_bollinger_width_basic():
    """BB width is 2*std/ma."""
    n = 100
    closes = np.random.randn(n).cumsum() + 100
    width = _compute_bollinger_width_array(closes, period=20)
    assert len(width) == n
    # First 19 values should be 0 (burn-in from rolling_mean)
    assert width[19] >= 0  # after burn-in, should be non-negative


def test_bollinger_width_flat():
    """Flat prices → near-zero width."""
    closes = np.ones(100) * 50.0
    width = _compute_bollinger_width_array(closes, period=20)
    # Flat prices: std ≈ 0 → width ≈ 0
    assert width[-1] < 0.01


# ═══════════════════════════════════════════════════════════════════════════
# _compute_adx_array
# ═══════════════════════════════════════════════════════════════════════════


def test_adx_array_shape():
    """ADX array has correct length."""
    n = 60
    highs = np.random.randn(n).cumsum() + 100 + 2
    lows = highs - np.abs(np.random.randn(n)) * 2
    closes = (highs + lows) / 2
    adx = _compute_adx_array(highs, lows, closes, period=14)
    assert len(adx) == n


def test_adx_array_insufficient_data():
    """Too few bars → all 20.0 (default)."""
    adx = _compute_adx_array(
        np.array([10.0, 11.0, 12.0]),
        np.array([9.0, 10.0, 11.0]),
        np.array([9.5, 10.5, 11.5]),
        period=14,
    )
    assert np.all(adx == 20.0)


# ═══════════════════════════════════════════════════════════════════════════
# _compute_momentum_array
# ═══════════════════════════════════════════════════════════════════════════


def test_momentum_array_basic():
    """Momentum = (price_t - price_{t-days}) / price_{t-days} * 100."""
    closes = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    mom = _compute_momentum_array(closes, days=3)
    assert len(mom) == 6
    assert mom[0] == 0.0  # not enough history
    assert mom[3] == pytest.approx(3.0)  # (103-100)/100 * 100


def test_momentum_insufficient_data():
    """Not enough data → all zeros."""
    closes = np.array([1.0, 2.0, 3.0])
    mom = _compute_momentum_array(closes, days=5)
    assert np.all(mom == 0.0)


# ═══════════════════════════════════════════════════════════════════════════
# _build_h4_alignment
# ═══════════════════════════════════════════════════════════════════════════


def test_h4_alignment_basic():
    """H4 bars map to the most recent H4 before each D1."""
    d1_times = ["2024-01-02T00:00:00Z", "2024-01-03T00:00:00Z"]
    h4_times = [
        "2024-01-01T00:00:00Z",
        "2024-01-01T04:00:00Z",
        "2024-01-01T08:00:00Z",
        "2024-01-02T00:00:00Z",
        "2024-01-02T04:00:00Z",
        "2024-01-02T08:00:00Z",
        "2024-01-03T00:00:00Z",
    ]
    aligned = _build_h4_alignment(d1_times, h4_times)
    assert len(aligned) == 2
    # D1[0] = 2024-01-02 → last H4 ≤ that is index 3 (2024-01-02T00:00:00Z)
    assert aligned[0] == 3
    # D1[1] = 2024-01-03 → last H4 ≤ that is index 6 (2024-01-03T00:00:00Z)
    assert aligned[1] == 6


def test_h4_alignment_no_preceding_h4():
    """D1 before any H4 → -1."""
    d1_times = ["2023-12-31T00:00:00Z"]
    h4_times = ["2024-01-01T00:00:00Z", "2024-01-01T04:00:00Z"]
    aligned = _build_h4_alignment(d1_times, h4_times)
    assert aligned[0] == -1


def test_h4_alignment_empty():
    """No timestamps → empty."""
    assert _build_h4_alignment([], []) == []
