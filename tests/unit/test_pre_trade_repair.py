"""Tests for repair and freshness checks in pre_trade_guards.py."""

from __future__ import annotations

import time

import numpy as np
import pytest

from core.execution.pre_trade_guards import (
    check_feature_freshness,
    repair_feature_vector,
)


class TestRepairFeatureVector:
    def test_clean_vector_no_change(self):
        fv = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        repaired, log = repair_feature_vector(fv)
        assert np.allclose(repaired, fv)
        assert log["nan_filled"] == 0
        assert log["inf_filled"] == 0
        assert log["repaired"] is False

    def test_single_nan_forward_filled(self):
        fv = np.array([1.0, np.nan, 3.0], dtype=np.float64)
        repaired, log = repair_feature_vector(fv)
        assert repaired[1] == pytest.approx(1.0)  # forward-filled from 1.0
        assert log["nan_filled"] == 1

    def test_leading_nan_gets_zero(self):
        fv = np.array([np.nan, 2.0, 3.0], dtype=np.float64)
        repaired, log = repair_feature_vector(fv)
        assert repaired[0] == pytest.approx(0.0)  # no predecessor → 0
        assert log["nan_filled"] == 1

    def test_multiple_consecutive_nan(self):
        fv = np.array([5.0, np.nan, np.nan, 8.0], dtype=np.float64)
        repaired, log = repair_feature_vector(fv)
        assert repaired[1] == pytest.approx(5.0)
        assert repaired[2] == pytest.approx(5.0)  # still 5.0
        assert log["nan_filled"] == 2

    def test_inf_replaced_by_median(self):
        fv = np.array([1.0, np.inf, 3.0, 5.0], dtype=np.float64)
        repaired, log = repair_feature_vector(fv)
        assert np.isfinite(repaired[1])
        assert log["inf_filled"] == 1

    def test_empty_vector(self):
        fv = np.array([], dtype=np.float64)
        repaired, log = repair_feature_vector(fv)
        assert len(repaired) == 0


class TestCheckFeatureFreshness:
    def test_fresh_feature(self):
        now = time.time()
        result = check_feature_freshness(now - 10, max_age_seconds=60)
        assert result["fresh"] is True
        assert result["age_seconds"] == pytest.approx(10, abs=1)

    def test_stale_feature(self):
        now = time.time()
        result = check_feature_freshness(now - 120, max_age_seconds=60)
        assert result["fresh"] is False

    def test_none_timestamp(self):
        result = check_feature_freshness(None)
        assert result["fresh"] is False
        assert result["reason"] == "missing_timestamp"

    def test_zero_timestamp(self):
        result = check_feature_freshness(0.0)
        assert result["fresh"] is False
