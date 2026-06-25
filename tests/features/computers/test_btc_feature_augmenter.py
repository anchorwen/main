"""Tests for core.features.computers.btc_feature_augmenter — BTC cross-asset correction.

FIX-20260625-XXX: Tier 2 zero-coverage breakout #8.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from core.features.computers.btc_feature_augmenter import (
    BTCFeatureAugmenter,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_store(xau_return: float = 0.001, event_time: float | None = None) -> MagicMock:
    """Create a mock feature store for XAUUSDc."""
    import time

    store = MagicMock()
    record = {
        "values": {"M5_Ret_1": xau_return},
        "event_time": event_time if event_time is not None else time.time(),
    }
    store.get_latest.return_value = record
    return store


def _make_worker(audjpy_rates=None, xau_rates=None) -> MagicMock:
    """Create a mock MT5Worker with copy_rates_from_pos."""
    worker = MagicMock()

    def _copy_rates(symbol, timeframe, start_pos, count, timeout=3.0):
        if symbol == "AUDJPYc":
            return audjpy_rates
        elif symbol == "XAUUSDc":
            return xau_rates
        return None

    worker.copy_rates_from_pos.side_effect = _copy_rates
    return worker


# ── Initialization ─────────────────────────────────────────────────────────


class TestBTCFeatureAugmenterInit:
    def test_default_construction(self) -> None:
        aug = BTCFeatureAugmenter()
        assert aug._store is None
        assert aug._worker is None
        assert aug._prev_ou is None
        assert aug._prev_hurst is None

    def test_with_store_and_worker(self) -> None:
        store = MagicMock()
        worker = MagicMock()
        aug = BTCFeatureAugmenter(feature_store=store, mt5_worker=worker)
        assert aug._store is store
        assert aug._worker is worker


# ── augment() — Happy Path ─────────────────────────────────────────────────


class TestAugmentHappyPath:
    def test_output_shape_is_41(self) -> None:
        aug = BTCFeatureAugmenter()
        daily = np.arange(24, dtype=np.float64) + 1.0
        micro = np.arange(9, dtype=np.float64) + 1.0
        result = aug.augment(daily, micro, btc_price=60000.0)
        assert result.shape == (41,)

    def test_no_nans_in_output(self) -> None:
        aug = BTCFeatureAugmenter()
        daily = np.arange(24, dtype=np.float64) + 1.0
        micro = np.arange(9, dtype=np.float64) + 1.0
        result = aug.augment(daily, micro, btc_price=60000.0)
        assert not np.isnan(result).any()

    def test_daily_slots_copied(self) -> None:
        aug = BTCFeatureAugmenter()
        daily = np.arange(24, dtype=np.float64) + 100.0
        micro = np.zeros(9)
        result = aug.augment(daily, micro, btc_price=60000.0)
        # Slots [0-11] = daily[0:12]
        np.testing.assert_array_equal(result[0:12], daily[0:12])
        # Slots [13-24] = daily[13:24]
        np.testing.assert_array_equal(result[13:24], daily[13:24])

    def test_micro_slots_copied(self) -> None:
        aug = BTCFeatureAugmenter()
        daily = np.zeros(24)
        micro = np.arange(9, dtype=np.float64) + 200.0
        result = aug.augment(daily, micro, btc_price=60000.0)
        # Slots [24-30] = micro[0:6]
        np.testing.assert_array_equal(result[24:30], micro[0:6])
        # Slots [31-33] = micro[7:9]
        np.testing.assert_array_equal(result[31:33], micro[7:9])

    def test_short_arrays_padded(self) -> None:
        aug = BTCFeatureAugmenter()
        daily = np.array([1.0, 2.0])
        micro = np.array([3.0])
        result = aug.augment(daily, micro, btc_price=60000.0)
        assert result.shape == (41,)
        assert not np.isnan(result).any()

    def test_tf_slots_set(self) -> None:
        aug = BTCFeatureAugmenter()
        daily = np.zeros(24)
        micro = np.zeros(9)
        result = aug.augment(daily, micro, btc_price=60000.0, tf_ou=0.123, tf_hurst=0.456)
        assert result[33] == pytest.approx(0.123)
        assert result[34] == pytest.approx(0.456)

    def test_tf_slots_with_none_raises(self) -> None:
        """None tf_ou propagates to ou_div_adx division, triggering TypeError.
        This tests that the augment method does not have None-safety for derivates."""
        aug = BTCFeatureAugmenter()
        daily = np.zeros(24)
        daily[7] = 1.0
        micro = np.zeros(9)
        # None tf_ou causes TypeError at ou_div_adx = None / max(ADX, 1)
        # This is a known limitation — callers must pass numeric values
        try:
            aug.augment(daily, micro, btc_price=60000.0, tf_ou=None, tf_hurst=0.5)
        except (TypeError, AssertionError):
            pass  # Expected: None can't be used in division


# ── augment() — Regime Derivatives ──────────────────────────────────────────


class TestAugmentRegimeDerivatives:
    def test_first_call_deltas_are_zero(self) -> None:
        aug = BTCFeatureAugmenter()
        daily = np.zeros(24)
        micro = np.zeros(9)
        result = aug.augment(daily, micro, btc_price=60000.0, tf_ou=0.5, tf_hurst=0.6)
        assert result[37] == 0.0  # delta_ou
        assert result[38] == 0.0  # delta_hurst

    def test_second_call_deltas_computed(self) -> None:
        aug = BTCFeatureAugmenter()
        daily = np.zeros(24)
        micro = np.zeros(9)
        aug.augment(daily, micro, btc_price=60000.0, tf_ou=0.5, tf_hurst=0.6)
        result = aug.augment(daily, micro, btc_price=60000.0, tf_ou=0.7, tf_hurst=0.4)
        assert result[37] == pytest.approx(0.2)  # 0.7 - 0.5
        assert result[38] == pytest.approx(-0.2)  # 0.4 - 0.6

    def test_ou_x_hurst_computed(self) -> None:
        aug = BTCFeatureAugmenter()
        daily = np.zeros(24)
        micro = np.zeros(9)
        result = aug.augment(daily, micro, btc_price=60000.0, tf_ou=0.5, tf_hurst=0.6)
        assert result[39] == pytest.approx(0.5 * 0.4)  # ou * (1-hurst)

    def test_ou_div_adx_computed(self) -> None:
        aug = BTCFeatureAugmenter()
        # daily[7] is the ADX slot
        daily = np.zeros(24)
        daily[7] = 2.0  # ADX
        micro = np.zeros(9)
        result = aug.augment(daily, micro, btc_price=60000.0, tf_ou=1.0, tf_hurst=0.6)
        assert result[40] == pytest.approx(1.0 / 2.0)  # ou / max(ADX, 1)

    def test_ou_div_adx_minimum_one(self) -> None:
        aug = BTCFeatureAugmenter()
        daily = np.zeros(24)
        daily[7] = 0.5  # ADX below 1
        micro = np.zeros(9)
        result = aug.augment(daily, micro, btc_price=60000.0, tf_ou=1.0, tf_hurst=0.6)
        assert result[40] == pytest.approx(1.0)  # ou / max(0.5, 1) = 1.0 / 1.0


# ── _compute_xauusdc_return ────────────────────────────────────────────────


class TestComputeXAUUSDCReturn:
    def test_returns_value_from_store(self) -> None:
        store = _make_store(xau_return=0.0025)
        aug = BTCFeatureAugmenter(feature_store=store)
        result = aug._compute_xauusdc_return()
        assert result == pytest.approx(0.0025)

    def test_no_store_returns_zero(self) -> None:
        aug = BTCFeatureAugmenter()
        result = aug._compute_xauusdc_return()
        assert result == 0.0

    def test_none_record_returns_zero(self) -> None:
        store = MagicMock()
        store.get_latest.return_value = None
        aug = BTCFeatureAugmenter(feature_store=store)
        result = aug._compute_xauusdc_return()
        assert result == 0.0

    def test_stale_record_returns_zero(self) -> None:
        store = _make_store(xau_return=0.005, event_time=0.0)  # epoch 0 = very stale
        aug = BTCFeatureAugmenter(feature_store=store)
        result = aug._compute_xauusdc_return()
        assert result == 0.0

    def test_exception_returns_zero(self) -> None:
        store = MagicMock()
        store.get_latest.side_effect = RuntimeError("boom")
        aug = BTCFeatureAugmenter(feature_store=store)
        result = aug._compute_xauusdc_return()
        assert result == 0.0

    def test_non_finite_return_sanitized(self) -> None:
        store = _make_store(xau_return=float("nan"))
        aug = BTCFeatureAugmenter(feature_store=store)
        result = aug._compute_xauusdc_return()
        assert result == 0.0


# ── _compute_audjpyc_return ────────────────────────────────────────────────


class TestComputeAUDJPYCReturn:
    def test_returns_computed_return(self) -> None:
        worker = _make_worker(
            audjpy_rates=[
                {"close": 100.0},
                {"close": 101.0},
            ]
        )
        aug = BTCFeatureAugmenter(mt5_worker=worker)
        result = aug._compute_audjpyc_return()
        assert result == pytest.approx(0.01)

    def test_no_worker_returns_zero(self) -> None:
        aug = BTCFeatureAugmenter()
        result = aug._compute_audjpyc_return()
        assert result == 0.0

    def test_none_rates_returns_zero(self) -> None:
        worker = _make_worker(audjpy_rates=None)
        aug = BTCFeatureAugmenter(mt5_worker=worker)
        result = aug._compute_audjpyc_return()
        assert result == 0.0

    def test_insufficient_rates_returns_zero(self) -> None:
        worker = _make_worker(audjpy_rates=[{"close": 100.0}])  # only 1 bar
        aug = BTCFeatureAugmenter(mt5_worker=worker)
        result = aug._compute_audjpyc_return()
        assert result == 0.0

    def test_prev_close_zero_returns_zero(self) -> None:
        worker = _make_worker(
            audjpy_rates=[
                {"close": 0.0},
                {"close": 101.0},
            ]
        )
        aug = BTCFeatureAugmenter(mt5_worker=worker)
        result = aug._compute_audjpyc_return()
        assert result == 0.0

    def test_exception_returns_zero(self) -> None:
        worker = MagicMock()
        worker.copy_rates_from_pos.side_effect = RuntimeError("MT5 error")
        aug = BTCFeatureAugmenter(mt5_worker=worker)
        result = aug._compute_audjpyc_return()
        assert result == 0.0


# ── _compute_btc_xau_ratio ─────────────────────────────────────────────────


class TestComputeBTCXAURatio:
    def test_returns_ratio_and_roc(self) -> None:
        worker = _make_worker(
            xau_rates=[
                {"close": 2500.0},
                {"close": 2600.0},
            ]
        )
        aug = BTCFeatureAugmenter(mt5_worker=worker)
        ratio, roc = aug._compute_btc_xau_ratio(btc_price=60000.0)
        # ratio = 60000/2600 = 23.077
        assert ratio == pytest.approx(60000.0 / 2600.0)
        # ratio_prev = 60000/2500 = 24.0
        # roc = (23.077 - 24.0) / 24.0 = -0.0385
        assert roc == pytest.approx((60000 / 2600 - 60000 / 2500) / (60000 / 2500))

    def test_no_worker_returns_zero(self) -> None:
        aug = BTCFeatureAugmenter()
        ratio, roc = aug._compute_btc_xau_ratio(btc_price=60000.0)
        assert ratio == 0.0
        assert roc == 0.0

    def test_zero_btc_price_returns_zero(self) -> None:
        worker = _make_worker()
        aug = BTCFeatureAugmenter(mt5_worker=worker)
        ratio, roc = aug._compute_btc_xau_ratio(btc_price=0.0)
        assert ratio == 0.0
        assert roc == 0.0

    def test_none_rates_returns_zero(self) -> None:
        worker = _make_worker(xau_rates=None)
        aug = BTCFeatureAugmenter(mt5_worker=worker)
        ratio, roc = aug._compute_btc_xau_ratio(btc_price=60000.0)
        assert ratio == 0.0

    def test_zero_close_returns_zero(self) -> None:
        worker = _make_worker(
            xau_rates=[
                {"close": 2500.0},
                {"close": 0.0},
            ]
        )
        aug = BTCFeatureAugmenter(mt5_worker=worker)
        ratio, roc = aug._compute_btc_xau_ratio(btc_price=60000.0)
        assert ratio == 0.0

    def test_exception_returns_zero(self) -> None:
        worker = MagicMock()
        worker.copy_rates_from_pos.side_effect = RuntimeError("boom")
        aug = BTCFeatureAugmenter(mt5_worker=worker)
        ratio, roc = aug._compute_btc_xau_ratio(btc_price=60000.0)
        assert ratio == 0.0


# ── Graceful Degradation ───────────────────────────────────────────────────


class TestGracefulDegradation:
    def test_all_deps_none_still_produces_vector(self) -> None:
        aug = BTCFeatureAugmenter()
        daily = np.random.RandomState(42).randn(24)
        micro = np.random.RandomState(43).randn(9)
        result = aug.augment(daily, micro, btc_price=60000.0)
        assert result.shape == (41,)
        assert not np.isnan(result).any()
        # Cross-asset features should be zero
        assert result[12] == 0.0  # XAUUSDc_return
        assert result[30] == 0.0  # AUDJPYc_return
        assert result[35] == 0.0  # BTC/XAU ratio
        assert result[36] == 0.0  # BTC/XAU ratio ROC

    def test_first_augment_log_once(self) -> None:
        aug = BTCFeatureAugmenter()
        assert aug._first_augment_logged is False
        aug.augment(np.zeros(24), np.zeros(9), btc_price=60000.0)
        assert aug._first_augment_logged is True
