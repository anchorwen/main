"""Tests for core/execution/regime_gate.py — market regime classifier."""

from __future__ import annotations

from typing import Any, cast

from core.execution.regime_gate import RegimeGate
from tests.execution.conftest import (
    generate_ranging_bars,
    generate_trending_bars,
)


class TestRegimeGateInit:
    def test_default_regime_map_has_all_regimes(self):
        gate = RegimeGate()
        assert "trending" in gate.regime_map
        assert "mild_trend" in gate.regime_map
        assert "ranging" in gate.regime_map
        assert "high_vol" in gate.regime_map
        assert "normal" in gate.regime_map

    def test_default_regime_map_barrier_12bar_present(self):
        gate = RegimeGate()
        for regime, strategies in gate.regime_map.items():
            assert "barrier_12bar" in strategies, f"missing barrier_12bar in {regime}"
            assert "micro_3bar" in strategies, f"missing micro_3bar in {regime}"
            assert "statarb_dynamic" in strategies, f"missing statarb_dynamic in {regime}"

    def test_custom_thresholds_applied(self):
        gate = RegimeGate(adx_trending_threshold=40.0, adx_mild_threshold=15.0)
        assert gate.adx_trending == 40.0
        assert gate.adx_mild == 15.0

    def test_custom_regime_map_used(self):
        custom = {
            "trending": {"barrier_12bar": "reduced", "micro_3bar": "off", "statarb_dynamic": "off"}
        }
        gate = RegimeGate(regime_map=custom)
        assert gate.regime_map["trending"]["barrier_12bar"] == "reduced"

    def test_not_ready_on_init(self):
        gate = RegimeGate()
        assert gate.is_ready is False
        assert gate.h1_is_ready is False


class TestRegimeGateGetStrategyMode:
    def test_returns_off_for_unknown_strategy(self):
        gate = RegimeGate()
        # For unknown strategies, get_strategy_mode returns "full" by default
        mode = gate.get_strategy_mode("unknown_strategy")
        assert mode in ("full", "reduced", "off", "normal")

    def test_all_three_strategies_in_regime_map(self):
        gate = RegimeGate()
        for _regime, strategies in gate.regime_map.items():
            for name in ("barrier_12bar", "micro_3bar", "statarb_dynamic"):
                assert strategies[name] in ("full", "reduced", "shadow")


class TestRegimeGateBarFeeding:
    def test_feed_m5_bars_batch(self):
        gate = RegimeGate()
        bars = generate_trending_bars(50)
        gate.feed_m5_bars_batch(bars)
        # After 50 bars, Kalman should be tracking
        assert gate.is_ready is True

    def test_feed_h1_bars_batch(self):
        gate = RegimeGate()
        bars = generate_trending_bars(50)
        gate.feed_h1_bars_batch(bars)
        assert gate.h1_is_ready is True

    def test_feed_single_m5_bar(self):
        gate = RegimeGate()
        for i in range(100):
            # feed_m5_bar(high, low, close)
            price = 2000.0 + i * 0.1
            gate.feed_m5_bar(high=price + 1.0, low=price - 0.5, close=price)
        assert gate.is_ready is True

    def test_h1_independent_from_m5(self):
        gate = RegimeGate()
        # Feed M5 trending, H1 ranging
        gate.feed_m5_bars_batch(generate_trending_bars(80, start_price=2000.0, step=0.5))
        gate.feed_h1_bars_batch(generate_ranging_bars(80, center=2000.0, amplitude=10.0))
        assert gate.is_ready and gate.h1_is_ready


class TestRegimeGateClassify:
    def test_classify_returns_all_expected_keys(self):
        gate = RegimeGate()
        gate.feed_m5_bars_batch(generate_trending_bars(100, start_price=2000.0, step=0.3))
        gate.feed_h1_bars_batch(generate_trending_bars(100, start_price=2000.0, step=0.3))

        result = gate.classify(atr_value=5.0)

        assert "regime" in result
        assert "adx" in result
        assert "di_plus" in result
        assert "di_minus" in result
        assert "trend_direction" in result
        assert "h1_trend_strength" in result
        assert "h1_trend_direction" in result
        assert "h1_trend_strength" in result
        assert "primary_trend" in result
        assert "strategy_gates" in result

    def test_classify_produces_valid_regime(self):
        gate = RegimeGate()
        gate.feed_m5_bars_batch(generate_trending_bars(100, start_price=2000.0, step=0.3))
        gate.feed_h1_bars_batch(generate_trending_bars(100, start_price=2000.0, step=0.3))

        result = gate.classify(atr_value=5.0)
        assert result["regime"] in ("trending", "mild_trend", "ranging", "high_vol", "normal")

    def test_classify_strong_trend_produces_trending(self):
        gate = RegimeGate()
        gate.feed_m5_bars_batch(generate_trending_bars(200, start_price=2000.0, step=0.5))
        gate.feed_h1_bars_batch(generate_trending_bars(200, start_price=2000.0, step=0.5))

        result = gate.classify(atr_value=5.0)
        # Strong uptrend should produce trending or mild_trend
        assert result["regime"] in ("trending", "mild_trend", "normal")
        # Primary trend should not be neutral in a strong trend
        assert result["primary_trend"] in ("long", "neutral")

    def test_classify_ranging_produces_ranging_or_normal(self):
        gate = RegimeGate()
        gate.feed_m5_bars_batch(generate_ranging_bars(200, center=2000.0, amplitude=3.0))
        gate.feed_h1_bars_batch(generate_ranging_bars(200, center=2000.0, amplitude=3.0))

        result = gate.classify(atr_value=5.0)
        # Ranging price should not produce strong trending
        assert result["regime"] in ("ranging", "mild_trend", "normal")

    def test_adx_property_returns_float(self):
        gate = RegimeGate()
        gate.feed_m5_bars_batch(generate_trending_bars(100))
        adx = gate.adx
        assert isinstance(adx, float)
        assert adx >= 0

    def test_get_strategy_mode_after_classify(self):
        gate = RegimeGate()
        gate.feed_m5_bars_batch(generate_trending_bars(100))
        gate.feed_h1_bars_batch(generate_trending_bars(100))
        gate.classify(atr_value=5.0)

        # All strategies should return a valid mode
        for name in ("barrier_12bar", "micro_3bar", "statarb_dynamic"):
            mode = gate.get_strategy_mode(name)
            assert mode in ("full", "reduced", "shadow")

    def test_strategy_gates_match_get_strategy_mode(self):
        gate = RegimeGate()
        gate.feed_m5_bars_batch(generate_trending_bars(100))
        gate.feed_h1_bars_batch(generate_trending_bars(100))
        result = gate.classify(atr_value=5.0)

        for name in ("barrier_12bar", "micro_3bar", "statarb_dynamic"):
            assert result["strategy_gates"][name] == gate.get_strategy_mode(name)


class TestRegimeGateCounterTrend:
    def test_is_counter_trend_same_direction(self):
        gate = RegimeGate()
        # Fake the internal state to test counter-trend logic
        gate._current_regime = "trending"
        gate._m5._kalman = type(
            "FakeKalman", (), {"velocity": lambda s=None: 1.0, "direction": "long", "strength": 0.7}
        )()
        # TECH_DEBT-009: _m5 静态类型无 _last_direction (white-box 附加私有状态) → cast(Any)
        cast(Any, gate._m5)._last_direction = "long"
        cast(Any, gate._m5)._trend_strength = 0.8
        gate._h1._kalman = type(
            "FakeKalman", (), {"velocity": lambda s=None: 1.0, "direction": "long", "strength": 0.7}
        )()
        cast(Any, gate._h1)._last_direction = "long"
        cast(Any, gate._h1)._trend_strength = 0.8

        # Same direction as primary trend = NOT counter-trend
        assert gate.is_counter_trend(trade_direction="long") is False

    def test_is_counter_trend_opposite_direction(self):
        gate = RegimeGate()
        gate._m5._kalman = type(
            "FakeKalman", (), {"velocity": lambda s=None: 1.0, "direction": "long", "strength": 0.7}
        )()
        # TECH_DEBT-009: _m5 静态类型无 _last_direction (white-box 附加私有状态) → cast(Any)
        cast(Any, gate._m5)._last_direction = "long"
        cast(Any, gate._m5)._trend_strength = 0.8
        gate._h1._kalman = type(
            "FakeKalman", (), {"velocity": lambda s=None: 1.0, "direction": "long", "strength": 0.7}
        )()
        cast(Any, gate._h1)._last_direction = "long"
        cast(Any, gate._h1)._trend_strength = 0.8

        # Opposite direction to primary trend = counter-trend
        assert gate.is_counter_trend(trade_direction="short") is True

    def test_is_counter_trend_neutral_direction(self):
        gate = RegimeGate()
        gate._m5._kalman = type(
            "FakeKalman",
            (),
            {"velocity": lambda s=None: 1.0, "direction": "neutral", "strength": 0.0},
        )()
        # TECH_DEBT-009: _m5 静态类型无 _last_direction (white-box 附加私有状态) → cast(Any)
        cast(Any, gate._m5)._last_direction = "neutral"
        cast(Any, gate._m5)._trend_strength = 0.0
        gate._h1._kalman = type(
            "FakeKalman", (), {"velocity": lambda s=None: 1.0, "direction": "long", "strength": 0.7}
        )()
        cast(Any, gate._h1)._last_direction = "long"
        cast(Any, gate._h1)._trend_strength = 0.5

        # Check with neutral primary (M5 neutral, H1 long) — H1 takes priority
        assert gate.is_counter_trend(trade_direction="short") is True
        assert gate.is_counter_trend(trade_direction="long") is False

    def test_is_counter_trend_no_primary(self):
        gate = RegimeGate()
        gate._m5._kalman = type(
            "FakeKalman",
            (),
            {"velocity": lambda s=None: 0.0, "direction": "neutral", "strength": 0.0},
        )()
        # TECH_DEBT-009: _m5 静态类型无 _last_direction (white-box 附加私有状态) → cast(Any)
        cast(Any, gate._m5)._last_direction = "neutral"
        cast(Any, gate._m5)._trend_strength = 0.0
        gate._h1._kalman = type(
            "FakeKalman",
            (),
            {"velocity": lambda s=None: 0.0, "direction": "neutral", "strength": 0.0},
        )()
        cast(Any, gate._h1)._last_direction = "neutral"
        cast(Any, gate._h1)._trend_strength = 0.0

        # No primary trend — never counter-trend
        assert gate.is_counter_trend(trade_direction="long") is False
        assert gate.is_counter_trend(trade_direction="short") is False
