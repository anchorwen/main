"""Tests for core.features.computers.microstructure_computer — Phase 3c coverage.

Covers: _safe_div, _mt5_timeframe, _resample_ohlc, _resample_closes,
_compute_returns, _fill_ohlc_defaults, _bar_to_features,
_compute_ohlc_features_from_row.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from core.features.computers.microstructure_computer import (
    CROSS_FEATURE_NAMES,
    CROSS_SYMBOLS,
    FEATURE_NAMES,
    MIN_M5_BARS,
    MIN_TICKS,
    MicrostructureFeatureComputer,
    _mt5_timeframe,
    _safe_div,
)

# ═══════════════════════════════════════════════════════════════════════════
# _compute_tick_features — bid/ask index regression lock (FIX-20260827-001)
# ═══════════════════════════════════════════════════════════════════════════


def test_bid_ask_not_swapped_avg_spread_positive() -> None:
    """FIX-20260827-001: bids=t[1], asks=t[2] → avg_spread must be positive.

    Pre-fix the computer read bids=t[2](ASK) and asks=t[1](BID), so
    ``spreads = asks - bids`` computed bid - ask → a NEGATIVE avg_spread
    (live mean -3939).  Regression lock: with bid consistently below ask,
    avg_spread is positive and exposes the tuple indices correctly.
    """
    c = MicrostructureFeatureComputer(MagicMock(), "XAUUSDc")
    base = 100.0
    ticks = [
        (float(i), base, base + 0.5, base + 0.2, 10, 1700000000000 + i, 2, 10.0) for i in range(3)
    ]  # (time, bid, ask, last, volume, time_msc, flags, volume_real)
    c._mt5.copy_ticks_from.return_value = ticks
    result: dict[str, float] = {}
    c._compute_tick_features(result)
    assert result["avg_spread"] == pytest.approx(0.5)  # ask - bid = 0.5, positive
    assert result["avg_spread"] > 0.0


# ═══════════════════════════════════════════════════════════════════════════
# _safe_div
# ═══════════════════════════════════════════════════════════════════════════


class TestSafeDiv:
    def test_normal_division(self) -> None:
        assert _safe_div(10.0, 2.0) == 5.0

    def test_zero_denominator_returns_fallback(self) -> None:
        assert _safe_div(10.0, 0.0, fallback=-1.0) == -1.0

    def test_nan_numerator_returns_fallback(self) -> None:
        assert _safe_div(float("nan"), 2.0) == 0.0

    def test_nan_denominator_returns_fallback(self) -> None:
        assert _safe_div(10.0, float("nan")) == 0.0

    def test_inf_numerator_returns_fallback(self) -> None:
        assert _safe_div(float("inf"), 2.0) == 0.0

    def test_inf_denominator_returns_fallback(self) -> None:
        assert _safe_div(10.0, float("inf")) == 0.0

    def test_negative_inf_returns_fallback(self) -> None:
        assert _safe_div(float("-inf"), 2.0) == 0.0

    def test_custom_fallback(self) -> None:
        assert _safe_div(float("nan"), 1.0, fallback=42.0) == 42.0

    def test_both_nan_returns_fallback(self) -> None:
        assert _safe_div(float("nan"), float("nan"), fallback=99.0) == 99.0

    def test_valid_zero_numerator(self) -> None:
        """0.0 / valid_denom = 0.0 (zero numerator is NOT nan/inf)."""
        assert _safe_div(0.0, 5.0) == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# _mt5_timeframe
# ═══════════════════════════════════════════════════════════════════════════


class TestMT5Timeframe:
    def test_m5(self) -> None:
        assert _mt5_timeframe("M5") == 5

    def test_m15(self) -> None:
        assert _mt5_timeframe("M15") == 15

    def test_h1(self) -> None:
        assert _mt5_timeframe("H1") == 16385

    def test_h4(self) -> None:
        assert _mt5_timeframe("H4") == 16388

    def test_unknown_falls_back_to_m5(self) -> None:
        assert _mt5_timeframe("UNKNOWN") == 5

    def test_empty_string_falls_back_to_m5(self) -> None:
        assert _mt5_timeframe("") == 5


# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════


class TestConstants:
    def test_feature_names_count(self) -> None:
        assert len(FEATURE_NAMES) == 9

    def test_cross_symbols_count(self) -> None:
        assert len(CROSS_SYMBOLS) == 3

    def test_cross_feature_names_count(self) -> None:
        assert len(CROSS_FEATURE_NAMES) == 3

    def test_min_m5_bars(self) -> None:
        assert MIN_M5_BARS == 4

    def test_min_ticks(self) -> None:
        assert MIN_TICKS == 10


# ═══════════════════════════════════════════════════════════════════════════
# _resample_ohlc (static method)
# ═══════════════════════════════════════════════════════════════════════════


class TestResampleOHLC:
    def test_no_resample_when_ratio_one(self) -> None:
        closes = np.array([1.0, 2.0, 3.0])
        opens = np.array([1.0, 2.0, 3.0])
        highs = np.array([1.5, 2.5, 3.5])
        lows = np.array([0.5, 1.5, 2.5])
        rc, ro, rh, rl = MicrostructureFeatureComputer._resample_ohlc(
            closes,
            opens,
            highs,
            lows,
            1,
        )
        assert np.array_equal(rc, closes)
        assert np.array_equal(ro, opens)
        assert np.array_equal(rh, highs)
        assert np.array_equal(rl, lows)

    def test_resample_ratio_3(self) -> None:
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        opens = np.array([0.5, 1.5, 2.5, 3.5, 4.5, 5.5])
        highs = np.array([1.2, 2.2, 3.2, 4.2, 5.2, 6.2])
        lows = np.array([0.8, 1.8, 2.8, 3.8, 4.8, 5.8])
        rc, ro, rh, rl = MicrostructureFeatureComputer._resample_ohlc(
            closes,
            opens,
            highs,
            lows,
            3,
        )
        assert len(rc) == 2  # 6 // 3
        assert ro[0] == 0.5  # first open of group 0
        assert ro[1] == 3.5  # first open of group 1
        assert rc[0] == 3.0  # last close of group 0
        assert rc[1] == 6.0  # last close of group 1
        assert rh[0] == pytest.approx(3.2)  # max of highs[0:3]
        assert rl[0] == pytest.approx(0.8)  # min of lows[0:3]


# ═══════════════════════════════════════════════════════════════════════════
# _resample_closes (static method)
# ═══════════════════════════════════════════════════════════════════════════


class TestResampleCloses:
    def test_no_resample_ratio_one(self) -> None:
        arr = np.array([1.0, 2.0, 3.0])
        result = MicrostructureFeatureComputer._resample_closes(arr, 1)
        assert np.array_equal(result, arr)

    def test_resample_ratio_2(self) -> None:
        arr = np.array([1.0, 2.0, 3.0, 4.0])
        result = MicrostructureFeatureComputer._resample_closes(arr, 2)
        assert len(result) == 2
        assert result[0] == 2.0  # last of [1,2]
        assert result[1] == 4.0  # last of [3,4]


# ═══════════════════════════════════════════════════════════════════════════
# _compute_returns (static method)
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeReturns:
    def test_returns_length(self) -> None:
        closes = np.array([100.0, 101.0, 102.0, 103.0, 104.0])
        returns = MicrostructureFeatureComputer._compute_returns(closes, 3)
        assert len(returns) == 3

    def test_returns_values(self) -> None:
        closes = np.array([100.0, 101.0, 102.0, 103.0])
        returns = MicrostructureFeatureComputer._compute_returns(closes, 2)
        # FIX-20260827-001: raw fraction, no ×100. (102-101)/101 = 0.0099009...
        assert returns[0] == pytest.approx(0.0099, abs=0.0001)
        # (103-102)/102 = 0.0098039...
        assert returns[1] == pytest.approx(0.0098, abs=0.0001)

    def test_returns_zero_on_zero_prev_close(self) -> None:
        closes = np.array([0.0, 100.0])
        returns = MicrostructureFeatureComputer._compute_returns(closes, 1)
        assert returns[0] == 0.0  # 0 denominator → 0.0 (safe)


# ═══════════════════════════════════════════════════════════════════════════
# _fill_ohlc_defaults (static method)
# ═══════════════════════════════════════════════════════════════════════════


class TestFillOHLCDefaults:
    def test_fills_defaults(self) -> None:
        result: dict = {}
        MicrostructureFeatureComputer._fill_ohlc_defaults(result)
        assert result["tick_return"] == 0.0
        assert result["hl_ratio"] == 0.0
        assert result["co_ratio"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# _bar_to_features (instance method, pure computation)
# ═══════════════════════════════════════════════════════════════════════════


class TestBarToFeatures:
    def make_computer(self) -> MicrostructureFeatureComputer:
        c = MicrostructureFeatureComputer(MagicMock(), "XAUUSDc")
        return c

    def test_basic_bar_computation(self) -> None:
        c = self.make_computer()
        bar = {"open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0}
        prev_close = 100.0
        tick_features = {"avg_spread": 0.02, "OIM": 0.5, "tick_velocity": 0.03}
        cross_returns = {
            "XAGUSDc_return": [0.001],
            "EURUSDc_return": [0.0005],
            "USDJPYc_return": [-0.0002],
        }
        row = c._bar_to_features(bar, prev_close, tick_features, cross_returns, 0)
        assert len(row) == 9
        # FIX-20260827-001: tick_return = (103-100)/100 = 0.03 (raw fraction, no ×100)
        assert row[0] == pytest.approx(0.03)
        # hl_ratio = (105-99)/103 = 0.05825...
        assert row[1] == pytest.approx(0.05825, abs=0.001)
        # co_ratio = 103/100 = 1.03
        assert row[2] == pytest.approx(1.03)
        assert row[3] == 0.02  # avg_spread
        assert row[4] == 0.5  # OIM
        assert row[5] == 0.03  # tick_velocity
        assert row[6] == 0.001  # XAGUSDc_return
        assert row[7] == 0.0005  # EURUSDc_return
        assert row[8] == -0.0002  # USDJPYc_return

    def test_missing_cross_returns_zero(self) -> None:
        c = self.make_computer()
        bar = {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}
        row = c._bar_to_features(bar, 100.0, {}, {}, 0)
        # Cross returns: falls back to 0.0 for all 3
        assert row[6] == 0.0
        assert row[7] == 0.0
        assert row[8] == 0.0

    def test_out_of_range_cross_index_zero(self) -> None:
        """Cross returns index out of range → IndexError (not safe-guarded).
        The method uses direct indexing on the list returned by .get()."""
        c = self.make_computer()
        bar = {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}
        cross = {"XAGUSDc_return": [0.01]}  # only 1 element
        with pytest.raises(IndexError):
            c._bar_to_features(bar, 100.0, {}, cross, 5)  # index 5 > 0

    def test_zero_prev_close_safe(self) -> None:
        """prev_close=0 → _safe_div returns fallback 0.0 for tick_return.
        hl_ratio and co_ratio still compute since close/open are non-zero."""
        c = self.make_computer()
        bar = {"open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0}
        row = c._bar_to_features(bar, 0.0, {}, {}, 0)
        assert row[0] == 0.0  # tick_return → 0 (safe_div fallback)
        # hl_ratio = (105-95)/100 = 0.1 → still computed fine
        assert row[1] == pytest.approx(0.1)
        # co_ratio = 100/100 = 1.0 → still computed fine
        assert row[2] == pytest.approx(1.0)


# ═══════════════════════════════════════════════════════════════════════════
# _compute_ohlc_features_from_row (instance method)
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeOHLCFeaturesFromRow:
    def make_computer(self) -> MicrostructureFeatureComputer:
        return MicrostructureFeatureComputer(MagicMock(), "XAUUSDc")

    def test_computes_three_features(self) -> None:
        c = self.make_computer()
        bar_row = (None, 100.0, 105.0, 99.0, 103.0, None, None, None)
        result: dict[str, float] = {}
        c._compute_ohlc_features_from_row(bar_row, 100.0, result)
        # FIX-20260827-001: tick_return = (103-100)/100 = 0.03 (raw fraction, no ×100)
        assert result["tick_return"] == pytest.approx(0.03)
        # hl_ratio = (105-99)/103 = 0.05825...
        assert result["hl_ratio"] == pytest.approx(0.05825, abs=0.001)
        # co_ratio = 103/100 = 1.03
        assert result["co_ratio"] == pytest.approx(1.03)

    def test_zero_prev_close_safe(self) -> None:
        c = self.make_computer()
        bar_row = (None, 100.0, 105.0, 99.0, 103.0, None, None, None)
        result: dict[str, float] = {}
        c._compute_ohlc_features_from_row(bar_row, 0.0, result)
        assert result["tick_return"] == 0.0  # safe_div fallback
        assert result["hl_ratio"] == pytest.approx(0.05825, abs=0.001)
        assert result["co_ratio"] == pytest.approx(1.03)
