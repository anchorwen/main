"""Tests for core/execution/dynamic_sl_tp.py — volatility-normalized SL/TP."""

from __future__ import annotations

import pytest

from core.execution.dynamic_sl_tp import (
    MAX_SL_ATR,
    MAX_TP_ATR,
    MIN_SL_ATR,
    MIN_TP_ATR,
    DynamicSLTP,
    StrategyFamily,
    _compute_regime_factors,
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


# ── Phase 4: Asymmetric regime scaling ──────────────────────────────────


class TestRegimeFactors:
    """Tests for _compute_regime_factors — asymmetric SL/TP volatility response."""

    def test_empty_family_returns_noop(self):
        """Empty strategy_family → (1.0, 1.0) — backward compat."""
        sl_f, tp_f = _compute_regime_factors(vol_ratio=2.0, strategy_family="")
        assert sl_f == 1.0
        assert tp_f == 1.0

    def test_normal_vol_returns_noop(self):
        """At vol_ratio=1.0, all families return (1.0, 1.0)."""
        for fam in (StrategyFamily.TREND_FOLLOWING.value, StrategyFamily.MEAN_REVERSION.value):
            sl_f, tp_f = _compute_regime_factors(vol_ratio=1.0, strategy_family=fam)
            assert sl_f == pytest.approx(1.0)
            assert tp_f == pytest.approx(1.0)

    def test_trend_high_vol_widens_both(self):
        """Trend following: both SL and TP widen with sqrt scaling."""
        sl_f, tp_f = _compute_regime_factors(
            vol_ratio=2.0, strategy_family=StrategyFamily.TREND_FOLLOWING.value
        )
        assert sl_f > 1.0
        assert tp_f > 1.0
        assert sl_f == pytest.approx(tp_f)  # synchronous

    def test_mean_reversion_high_vol_widens_sl_tightens_tp(self):
        """Mean reversion: SL widens (sqrt), TP tightens (inverse 4th root)."""
        sl_f, tp_f = _compute_regime_factors(
            vol_ratio=2.0, strategy_family=StrategyFamily.MEAN_REVERSION.value
        )
        assert sl_f > 1.0  # sqrt(2) = 1.414
        assert tp_f < 1.0  # 2^-0.25 = 0.841

    def test_mean_reversion_low_vol_tightens_sl_widens_tp(self):
        """Mean reversion: low vol → SL tightens, TP widens."""
        sl_f, tp_f = _compute_regime_factors(
            vol_ratio=0.5, strategy_family=StrategyFamily.MEAN_REVERSION.value
        )
        assert sl_f < 1.0
        assert tp_f > 1.0

    def test_trend_low_vol_tightens_both(self):
        """Trend following: low vol → both tighten together."""
        sl_f, tp_f = _compute_regime_factors(
            vol_ratio=0.5, strategy_family=StrategyFamily.TREND_FOLLOWING.value
        )
        assert sl_f < 1.0
        assert tp_f < 1.0
        assert sl_f == pytest.approx(tp_f)

    def test_regime_factors_clamped(self):
        """Extreme values are clamped to bounds."""
        # Very high vol
        sl_f, tp_f = _compute_regime_factors(
            vol_ratio=100.0, strategy_family=StrategyFamily.TREND_FOLLOWING.value
        )
        assert sl_f <= 1.80  # SL_FACTOR_MAX
        assert tp_f <= 2.00  # TP_FACTOR_MAX

        # Very low vol
        sl_f, tp_f = _compute_regime_factors(
            vol_ratio=0.01, strategy_family=StrategyFamily.MEAN_REVERSION.value
        )
        assert sl_f >= 0.55  # SL_FACTOR_MIN
        assert tp_f >= 0.55  # TP_FACTOR_MIN


class TestPhase4DynamicSLTP:
    """Integration tests: compute_dynamic_sl_tp with strategy_family."""

    def test_default_empty_family_preserves_behavior(self):
        """Without strategy_family, output matches pre-Phase-4 behavior."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=2.0,
            base_tp_mult=3.5,
            current_atr=10.0,
            ref_atr=5.0,
            strategy_family="",  # default
        )
        # Multipliers unchanged at base (no regime factor applied)
        assert result.sl_atr_mult == pytest.approx(2.0, rel=0.01)
        assert result.tp_atr_mult == pytest.approx(3.5, rel=0.01)

    def test_trend_following_high_vol_applies_regime(self):
        """Trend following in high vol → both multipliers above base."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=2.0,
            base_tp_mult=3.5,
            current_atr=10.0,
            ref_atr=5.0,
            strategy_family=StrategyFamily.TREND_FOLLOWING.value,
        )
        # vol_ratio=2.0 → sl_factor=tp_factor ≈ 1.414 → 2.0*1.414=2.828, 3.5*1.414=4.95
        assert result.sl_atr_mult > 2.0
        assert result.tp_atr_mult > 3.5

    def test_mean_reversion_high_vol_sl_widens_tp_tightens(self):
        """Mean reversion in high vol → SL widens, TP tightens."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=1.5,
            base_tp_mult=3.0,
            current_atr=10.0,
            ref_atr=5.0,
            strategy_family=StrategyFamily.MEAN_REVERSION.value,
        )
        assert result.sl_atr_mult > 1.5  # widens
        assert result.tp_atr_mult < 3.0  # tightens

    def test_hard_clip_min_sl_atr(self):
        """SL multiplier never drops below MIN_SL_ATR (0.8)."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=0.3,  # very low base
            base_tp_mult=1.0,
            current_atr=5.0,
            ref_atr=5.0,
            strategy_family="",  # no regime factor → base=0.3 → clamped to 0.8
        )
        assert result.sl_atr_mult >= MIN_SL_ATR

    def test_hard_clip_max_sl_atr(self):
        """SL multiplier never exceeds MAX_SL_ATR (4.0)."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=6.0,  # very high base
            base_tp_mult=1.0,
            current_atr=5.0,
            ref_atr=5.0,
            strategy_family="",
        )
        assert result.sl_atr_mult <= MAX_SL_ATR

    def test_hard_clip_max_tp_atr(self):
        """TP multiplier never exceeds MAX_TP_ATR (6.0)."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=1.0,
            base_tp_mult=10.0,  # very high base
            current_atr=5.0,
            ref_atr=5.0,
            strategy_family="",
            max_tp_mult=MAX_TP_ATR,
        )
        assert result.tp_atr_mult <= MAX_TP_ATR

    def test_hard_clip_min_tp_atr(self):
        """TP multiplier never drops below MIN_TP_ATR (1.0)."""
        result = compute_dynamic_sl_tp(
            base_sl_mult=1.0,
            base_tp_mult=0.3,  # very low base
            current_atr=5.0,
            ref_atr=5.0,
            strategy_family="",  # no regime factor → base=0.3 → clamped to 1.0
        )
        assert result.tp_atr_mult >= MIN_TP_ATR
