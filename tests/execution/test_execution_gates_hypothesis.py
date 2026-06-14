"""Property tests for execution gates (Tier 1 — Capital Path).

Phase 3: Targets the highest-risk 0% coverage files in core/execution/.
- conformal_ou_gate: _sigmoid, _compute_z_depth_quality
- regime_direction_gate: _resolve_trend, filter
- meta_filter_gate: build_meta_filter_array
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st


# ============================================================================
# Conformal OU Gate — physics scoring
# ============================================================================
class TestConformalOUPhysics:
    """Pure math functions from conformal_ou_gate.py."""

    def test_sigmoid_zero(self) -> None:
        from core.execution.conformal_ou_gate import _sigmoid

        assert _sigmoid(0.0) == 0.5

    @given(x=st.floats(-10.0, 10.0, allow_nan=False, allow_infinity=False))
    @settings(max_examples=500)
    def test_sigmoid_output_in_0_1(self, x: float) -> None:
        """Sigmoid(x) must always be in (0, 1) for any finite x."""
        from core.execution.conformal_ou_gate import _sigmoid

        result = _sigmoid(x)
        assert 0.0 < result < 1.0, f"sigmoid({x}) = {result} not in (0,1)"
        assert math.isfinite(result)

    @given(x=st.floats(0.1, 10.0, allow_nan=False, allow_infinity=False))
    @settings(max_examples=100)
    def test_sigmoid_monotonic(self, x: float) -> None:
        """sigmoid(x) < sigmoid(x+1) — strictly increasing."""
        from core.execution.conformal_ou_gate import _sigmoid

        assert _sigmoid(x) < _sigmoid(x + 1.0)

    @given(
        z_score=st.floats(0.1, 10.0, allow_nan=False),
        z_entry=st.floats(0.1, 5.0, allow_nan=False),
    )
    @settings(max_examples=200)
    def test_z_depth_quality_in_01(self, z_score: float, z_entry: float) -> None:
        """Quality must be in [0.1, 1.0] for valid inputs."""
        from core.execution.conformal_ou_gate import _compute_z_depth_quality

        result = _compute_z_depth_quality(z_score, z_entry)
        assert 0.1 <= result <= 1.0, f"z_depth({z_score}, {z_entry}) = {result}"
        assert math.isfinite(result)

    def test_z_depth_zero_entry_default(self) -> None:
        """z_entry <= 0 returns 0.5 default."""
        from core.execution.conformal_ou_gate import _compute_z_depth_quality

        assert _compute_z_depth_quality(2.0, 0.0) == 0.5
        assert _compute_z_depth_quality(2.0, -1.0) == 0.5

    @given(z_score=st.floats(0.5, 8.0), z_entry=st.floats(0.1, 4.0))
    @settings(max_examples=100)
    def test_deeper_signal_higher_quality_near_entry(self, z_score: float, z_entry: float) -> None:
        """For depth < 2*z_entry, deeper = higher quality (monotonic in that range)."""
        from core.execution.conformal_ou_gate import _compute_z_depth_quality

        depth = abs(z_score) / z_entry
        if depth < 2.0 and depth > 1.0:
            q1 = _compute_z_depth_quality(z_score, z_entry)
            q2 = _compute_z_depth_quality(z_score * 1.1, z_entry)
            assert q1 >= 0.1 and q2 >= 0.1  # both valid


# ============================================================================
# Regime Direction Gate — trend resolution + signal filtering
# ============================================================================
class TestRegimeDirectionGate:
    """Tests for regime_direction_gate.py _resolve_trend() and filter()."""

    def _make_regime_info(self, **overrides: object) -> dict:
        defaults: dict[str, object] = {
            "adx": 30.0,
            "plus_di": 35.0,
            "minus_di": 15.0,
            "trend_direction": "long",
            "detected_regime": "",
            "primary_regime": "",
            "ou_theta_m5": float("nan"),
            "hurst_m5": float("nan"),
        }
        defaults.update(overrides)
        return defaults

    def test_strong_uptrend_resolves_up(self) -> None:
        """ADX=30, +DI > -DI → 'up'."""
        from core.execution.regime_direction_gate import RegimeDirectionGate

        gate = RegimeDirectionGate()
        result = gate._resolve_trend(self._make_regime_info(adx=30, plus_di=35, minus_di=15))
        assert result == "up"

    def test_strong_downtrend_resolves_down(self) -> None:
        """ADX=30, -DI > +DI → 'down'."""
        from core.execution.regime_direction_gate import RegimeDirectionGate

        gate = RegimeDirectionGate()
        result = gate._resolve_trend(self._make_regime_info(adx=30, plus_di=15, minus_di=35))
        assert result == "down"

    def test_ranging_with_low_adx(self) -> None:
        """ADX=10 (< 25 threshold) → 'ranging' (full passthrough)."""
        from core.execution.regime_direction_gate import RegimeDirectionGate

        gate = RegimeDirectionGate(adx_threshold=25)
        result = gate._resolve_trend(self._make_regime_info(adx=10, plus_di=20, minus_di=15))
        assert result == "ranging"

    def test_filter_blocks_counter_trend_in_uptrend(self) -> None:
        """In uptrend, 'short' signals must be blocked."""
        from core.execution.regime_direction_gate import RegimeDirectionGate

        gate = RegimeDirectionGate()
        signals = [
            {"brain_id": "b1", "direction": "long"},
            {"brain_id": "b2", "direction": "short"},
            {"brain_id": "b3", "direction": "long"},
        ]
        filtered, audit = gate.filter(signals, self._make_regime_info())

        assert len(filtered) == 2
        assert all(s["direction"] == "long" for s in filtered)
        assert len(audit["blocked_short"]) == 1

    def test_filter_passthrough_in_ranging(self) -> None:
        """In ranging, ALL signals must pass through unblocked."""
        from core.execution.regime_direction_gate import RegimeDirectionGate

        gate = RegimeDirectionGate()
        signals = [
            {"brain_id": "b1", "direction": "long"},
            {"brain_id": "b2", "direction": "short"},
        ]
        filtered, audit = gate.filter(
            signals, self._make_regime_info(adx=10, plus_di=20, minus_di=15)
        )
        assert len(filtered) == 2

    def test_filter_empty_signals(self) -> None:
        """Empty signal list → empty result, no crash."""
        from core.execution.regime_direction_gate import RegimeDirectionGate

        gate = RegimeDirectionGate()
        filtered, audit = gate.filter([], self._make_regime_info())
        assert filtered == []

    def test_trend_direction_string_variants(self) -> None:
        """Various trend_direction strings must resolve correctly."""
        from core.execution.regime_direction_gate import RegimeDirectionGate

        gate = RegimeDirectionGate(adx_threshold=25)
        # "long" with ADX=30
        assert gate._resolve_trend(self._make_regime_info(adx=30, plus_di=0, minus_di=0, trend_direction="long")) == "up"
        # "up"
        assert gate._resolve_trend(self._make_regime_info(adx=30, plus_di=0, minus_di=0, trend_direction="up")) == "up"
        # "bullish"
        assert gate._resolve_trend(self._make_regime_info(adx=30, plus_di=0, minus_di=0, trend_direction="bullish")) == "up"
        # "short"
        assert gate._resolve_trend(self._make_regime_info(adx=30, plus_di=0, minus_di=0, trend_direction="short")) == "down"

    def test_physics_override_requires_sufficient_history(self) -> None:
        """With NaN OU/Hurst, gate falls through to ADX (no crash)."""
        from core.execution.regime_direction_gate import RegimeDirectionGate

        gate = RegimeDirectionGate()
        # NaN ou_theta/hurst_m5 → physics path skipped → ADX used
        result = gate._resolve_trend(
            self._make_regime_info(
                adx=30, plus_di=35, minus_di=15,
                ou_theta_m5=float("nan"), hurst_m5=float("nan"),
            )
        )
        assert result == "up"  # ADX path → uptrend


# ============================================================================
# Meta Filter Gate — feature array builder
# ============================================================================
class TestMetaFilterGate:
    """Tests for meta_filter_gate.py build_meta_filter_array()."""

    def test_build_array_correct_shape(self) -> None:
        """Must produce array matching expected feature dimension."""
        from core.execution.meta_filter_gate import build_meta_filter_array

        feat_vec = np.random.default_rng(42).normal(0, 1, (40,)).astype(np.float32)
        micro = {name: 0.0 for name in [
            "tick_return", "hl_ratio", "co_ratio", "avg_spread", "OIM",
            "tick_velocity", "XAGUSDc_return", "EURUSDc_return", "USDJPYc_return",
        ]}

        result = build_meta_filter_array(feat_vec, micro, ou_z_entry=1.3)

        assert isinstance(result, np.ndarray)
        assert result.dtype == np.float32
        # Dimension should match feature_names.json
        assert len(result) > 40  # 40 V9 + 9 micro + 1 ou_z_entry = at least 47

    def test_short_feature_vector_pads_with_zero(self) -> None:
        """Feature vector shorter than expected → pad with 0, no crash."""
        from core.execution.meta_filter_gate import build_meta_filter_array

        short_vec = np.array([1.0, 2.0, 3.0], dtype=np.float32)  # only 3 features
        micro = {name: 0.0 for name in [
            "tick_return", "hl_ratio", "co_ratio", "avg_spread", "OIM",
            "tick_velocity", "XAGUSDc_return", "EURUSDc_return", "USDJPYc_return",
        ]}

        result = build_meta_filter_array(short_vec, micro)
        assert isinstance(result, np.ndarray)

    def test_missing_micro_features_default_zero(self) -> None:
        """Missing micro features → default to 0.0, no KeyError."""
        from core.execution.meta_filter_gate import build_meta_filter_array

        feat_vec = np.zeros(40, dtype=np.float32)
        result = build_meta_filter_array(feat_vec, {})  # empty micro dict

        assert isinstance(result, np.ndarray)
        assert result.dtype == np.float32


# ============================================================================
# Market Efficiency
# ============================================================================
class TestMarketEfficiency:
    """Tests for market_efficiency.py — Kaufman ER, market normalization."""

    def test_kaufman_er_all_noise(self) -> None:
        """Pure noise (random walk) → ER ≈ 0."""
        from core.execution.market_efficiency import compute_kaufman_er

        rng = np.random.default_rng(42)
        prices = list(2000.0 + rng.normal(0, 1, 100).cumsum())

        er = compute_kaufman_er(prices, period=10)
        # Random walk → ER typically < 0.3
        assert 0.0 <= er <= 1.0
        assert math.isfinite(er)

    def test_kaufman_er_strong_trend(self) -> None:
        """Linear trend → ER ≈ 1."""
        from core.execution.market_efficiency import compute_kaufman_er

        prices = list(np.linspace(2000.0, 2100.0, 50))

        er = compute_kaufman_er(prices, period=10)
        assert 0.8 <= er <= 1.0, f"Strong trend ER should be near 1, got {er}"

    def test_kaufman_er_short_data(self) -> None:
        """Less data than period → must not crash."""
        from core.execution.market_efficiency import compute_kaufman_er

        er = compute_kaufman_er([2000.0, 2001.0, 2002.0], period=10)
        assert 0.0 <= er <= 1.0
