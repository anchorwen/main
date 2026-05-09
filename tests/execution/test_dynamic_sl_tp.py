"""Tests for core/execution/dynamic_sl_tp.py — volatility-normalized SL/TP."""

from __future__ import annotations

import pytest

from core.execution.dynamic_sl_tp import (
    DynamicSLTP,
    compute_dynamic_sl_tp,
    compute_sl_tp_levels,
)


class TestComputeDynamicSLTP:
    def test_default_atr_equals_ref_atr(self):
        """When current_atr == ref_atr, effective multipliers equal base."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=2.0, base_tp_mult=3.5, current_atr=5.0, ref_atr=5.0
        )
        assert result.sl_atr_mult == pytest.approx(2.0, rel=0.01)
        assert result.tp_atr_mult == pytest.approx(3.5, rel=0.01)
        assert result.vol_ratio == pytest.approx(1.0, rel=0.01)

    def test_high_vol_shrinks_multipliers(self):
        """current_atr > ref_atr → effective multipliers shrink."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=2.0, base_tp_mult=3.5, current_atr=10.0, ref_atr=5.0
        )
        # vol_ratio = 2.0 → mult = base / 2.0
        assert result.sl_atr_mult < 2.0
        assert result.tp_atr_mult < 3.5
        assert result.vol_ratio == pytest.approx(2.0, rel=0.01)

    def test_low_vol_expands_multipliers(self):
        """current_atr < ref_atr → effective multipliers expand."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=2.0, base_tp_mult=3.5, current_atr=2.5, ref_atr=5.0
        )
        # vol_ratio = 0.5 → mult = base / 0.5
        assert result.sl_atr_mult > 2.0
        assert result.tp_atr_mult > 3.5
        assert result.vol_ratio == pytest.approx(0.5, rel=0.01)

    def test_zero_atr_falls_back_to_ref(self):
        """current_atr=0 → uses ref_atr, vol_ratio=1.0."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=2.0, base_tp_mult=3.5, current_atr=0.0, ref_atr=5.0
        )
        assert result.vol_ratio == pytest.approx(1.0)
        assert result.sl_atr_mult == pytest.approx(2.0, rel=0.01)

    def test_negative_atr_falls_back_to_ref(self):
        """current_atr < 0 → falls back to ref_atr."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=2.0, base_tp_mult=3.5, current_atr=-3.0, ref_atr=5.0
        )
        assert result.vol_ratio == pytest.approx(1.0)

    def test_multiplier_clamping_min(self):
        """Effective multiplier never goes below min_sl_mult."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=2.0,
            base_tp_mult=3.5,
            current_atr=50.0,
            ref_atr=5.0,
            min_sl_mult=0.5,
        )
        # vol_ratio = 10.0 → mult = 2.0/10 = 0.2 → clamped to 0.5
        assert result.sl_atr_mult == pytest.approx(0.5)
        assert result.tp_atr_mult >= 0.5

    def test_multiplier_clamping_max(self):
        """Effective multiplier never exceeds max_sl_mult."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=2.0,
            base_tp_mult=3.5,
            current_atr=0.5,
            ref_atr=5.0,
            max_sl_mult=4.0,
        )
        # vol_ratio = 0.1 → mult = 2.0/0.1 = 20 → clamped to 4.0
        assert result.sl_atr_mult == pytest.approx(4.0)
        assert result.tp_atr_mult <= 4.0

    def test_hard_sl_is_sl_times_ratio(self):
        """hard_sl_distance = sl_distance * hard_sl_ratio."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=2.0,
            base_tp_mult=3.5,
            current_atr=5.0,
            ref_atr=5.0,
            hard_sl_ratio=1.5,
        )
        assert result.hard_sl_distance == pytest.approx(result.sl_distance * 1.5, rel=1e-4)

    def test_dataclass_fields_populated(self):
        """Returned DynamicSLTP has all expected fields."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=2.0, base_tp_mult=3.5, current_atr=5.0, ref_atr=5.0
        )
        assert isinstance(result, DynamicSLTP)
        assert result.sl_distance > 0
        assert result.tp_distance > 0
        assert result.hard_sl_distance > 0
        assert result.sl_atr_mult > 0
        assert result.tp_atr_mult > 0
        assert result.vol_ratio > 0


class TestComputeSLTPLevels:
    def test_levels_long(self):
        """For long, SL below entry, TP above."""
        dsl = DynamicSLTP(
            sl_distance=10.0,
            tp_distance=17.5,
            hard_sl_distance=15.0,
            sl_atr_mult=2.0,
            tp_atr_mult=3.5,
            vol_ratio=1.0,
        )
        levels = compute_sl_tp_levels("long", 2000.0, dsl)
        assert levels["stop_loss"] == pytest.approx(1990.0)
        assert levels["take_profit"] == pytest.approx(2017.5)
        assert levels["hard_sl"] == pytest.approx(1985.0)

    def test_levels_short(self):
        """For short, SL above entry, TP below."""
        dsl = DynamicSLTP(
            sl_distance=10.0,
            tp_distance=17.5,
            hard_sl_distance=15.0,
            sl_atr_mult=2.0,
            tp_atr_mult=3.5,
            vol_ratio=1.0,
        )
        levels = compute_sl_tp_levels("short", 2000.0, dsl)
        assert levels["stop_loss"] == pytest.approx(2010.0)
        assert levels["take_profit"] == pytest.approx(1982.5)
        assert levels["hard_sl"] == pytest.approx(2015.0)

    def test_levels_precision(self):
        """Levels are rounded to 5 decimal places."""
        dsl = DynamicSLTP(
            sl_distance=10.123456,
            tp_distance=17.654321,
            hard_sl_distance=15.0,
            sl_atr_mult=2.0,
            tp_atr_mult=3.5,
            vol_ratio=1.0,
        )
        levels = compute_sl_tp_levels("long", 2000.0, dsl)
        # Entry ± distances should be 5dp
        sl_str = f"{levels['stop_loss']:.5f}"
        assert sl_str == "1989.87654"
