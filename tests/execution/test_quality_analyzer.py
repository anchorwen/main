"""Tests for VWAP benchmark in core/execution/quality_analyzer.py."""

from __future__ import annotations

import pytest

from core.execution.quality_analyzer import compute_vwap


class TestComputeVWAP:
    def test_basic_two_fills(self):
        fills = [
            {"price": 2000.0, "volume": 1.0},
            {"price": 2002.0, "volume": 2.0},
        ]
        result = compute_vwap(fills)
        # (2000*1 + 2002*2) / 3 = 6004/3 = 2001.333333
        assert result == pytest.approx(2001.333333, rel=1e-5)

    def test_single_fill(self):
        fills = [{"price": 2015.75, "volume": 0.5}]
        result = compute_vwap(fills)
        assert result == pytest.approx(2015.75)

    def test_empty_fills_returns_none(self):
        assert compute_vwap([]) is None

    def test_zero_volume_ignored(self):
        fills = [
            {"price": 2000.0, "volume": 0.0},
            {"price": 2002.0, "volume": 2.0},
        ]
        result = compute_vwap(fills)
        assert result == pytest.approx(2002.0)

    def test_zero_price_ignored(self):
        fills = [
            {"price": 0.0, "volume": 1.0},
            {"price": 2000.0, "volume": 3.0},
        ]
        result = compute_vwap(fills)
        assert result == pytest.approx(2000.0)

    def test_all_zero_returns_none(self):
        fills = [{"price": 0.0, "volume": 0.0}]
        assert compute_vwap(fills) is None

    def test_mixed_string_values(self):
        fills = [
            {"price": "2000.5", "volume": "2"},
            {"price": 2001.5, "volume": 1},
        ]
        result = compute_vwap(fills)
        assert result == pytest.approx(2000.833333, rel=1e-5)

    def test_equal_weights(self):
        fills = [
            {"price": 1990.0, "volume": 1.0},
            {"price": 2010.0, "volume": 1.0},
        ]
        result = compute_vwap(fills)
        assert result == pytest.approx(2000.0)
