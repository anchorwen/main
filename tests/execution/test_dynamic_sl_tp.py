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

    def test_high_vol_keeps_constant_multiplier(self):
        """current_atr > ref_atr → multiplier stays constant, distance scales with ATR."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=2.0, base_tp_mult=3.5, current_atr=10.0, ref_atr=5.0
        )
        # Multiplier stays at base (not shrunk), distance proportional to current ATR
        assert result.sl_atr_mult == pytest.approx(2.0, rel=0.01)
        assert result.tp_atr_mult == pytest.approx(3.5, rel=0.01)
        assert result.vol_ratio == pytest.approx(2.0, rel=0.01)
        assert result.sl_distance == pytest.approx(20.0, rel=0.01)  # 2.0 × 10
        assert result.tp_distance == pytest.approx(35.0, rel=0.01)  # 3.5 × 10

    def test_low_vol_keeps_constant_multiplier(self):
        """current_atr < ref_atr → multiplier stays constant, distance shrinks with ATR.

        At ATR=2.5: SL=2.0×2.5=5.0 (still 2 ATR). The multiplier doesn't expand because
        ATR itself defines the risk distance — lower ATR already means tighter stops.
        """
        result = compute_dynamic_sl_tp(
            base_sl_mult=2.0, base_tp_mult=3.5, current_atr=2.5, ref_atr=5.0
        )
        # Multiplier stays at base, distance is proportional to current ATR
        assert result.sl_atr_mult == pytest.approx(2.0, rel=0.01)
        assert result.tp_atr_mult == pytest.approx(3.5, rel=0.01)
        assert result.vol_ratio == pytest.approx(0.5, rel=0.01)
        assert result.sl_distance == pytest.approx(5.0, rel=0.01)  # 2.0 × 2.5
        assert result.tp_distance == pytest.approx(8.75, rel=0.01)  # 3.5 × 2.5

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
        """Effective multiplier never goes below min_sl_mult (safety floor)."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=0.1,  # Below min_sl_mult default (1.2)
            base_tp_mult=0.3,
            current_atr=5.0,
            ref_atr=5.0,
            min_sl_mult=0.5,
        )
        # base 0.1 → clamped to min_sl_mult 0.5
        assert result.sl_atr_mult == pytest.approx(0.5)
        assert result.tp_atr_mult >= 0.5

    def test_multiplier_clamping_max(self):
        """Both SL and TP multipliers respect their respective ceilings."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=5.0,  # Above max_sl_mult
            base_tp_mult=8.0,  # Above max_tp_mult
            current_atr=5.0,
            ref_atr=5.0,
            max_sl_mult=4.0,
            max_tp_mult=6.0,
        )
        # base 5.0 → clamped to max_sl_mult 4.0; base 8.0 → clamped to max_tp_mult 6.0
        assert result.sl_atr_mult == pytest.approx(4.0)
        assert result.tp_atr_mult == pytest.approx(6.0)

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
