"""Unit tests for trend_isolation_gates.py — multi-gate trend isolation filter.

Covers:
  - Pass-through: non-statarb/non-swing strategies bypass gates
  - Gate 4aa: Direction-aware counter-trend (statarb, strong trend + counter signal)
  - Gate 4b: Multi-TF hard filter (swing strategies, H1 vs H4 divergence)
  - Gate 4c: Counter-trend gate (ADX-based, strategy-specific thresholds)
  - Gate 4d: Z-score inflection gate (statarb/OU strategies)
  - Gate 4e: Spatial z-score gate (swing family — Long hard-block / Short degrade)
  - Edge: None regime_info, missing keys, neutral directions
"""

from __future__ import annotations

import numpy as np

from core.execution.trend_isolation_gates import (
    apply_spatial_zscore_gate,
    apply_trend_isolation_gates,
    extract_h1_price_zscore,
)

# ── Mock config ───────────────────────────────────────────────────────────


class MockConfig:
    magic: int = 90410
    adx_trending_threshold: float = 25.0
    adx_mild_trend_threshold: float = 20.0


# ═══════════════════════════════════════════════════════════════════════════
# Pass-through cases
# ═══════════════════════════════════════════════════════════════════════════


class TestPassthrough:
    """Strategies not subject to any gate pass through unchanged."""

    def test_non_statarb_non_swing_passes(self) -> None:
        """btc_swing passes all gates (not statarb, not in swing list for 4b)."""
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="btc_swing",
            direction="long",
            confidence=0.8,
            entry_z_score=0.0,
            regime_info={"regime_gate": {"h1_adx": 30.0, "h1_trend_direction": "short"}},
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        assert result is None

    def test_none_regime_info_passes(self) -> None:
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="statarb_dynamic",
            direction="long",
            confidence=0.8,
            entry_z_score=0.0,
            regime_info=None,
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        assert result is None

    def test_neutral_direction_passes(self) -> None:
        """Gate 4aa counter-trend check ignores neutral direction."""
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="statarb_dynamic",
            direction="neutral",
            confidence=0.5,
            entry_z_score=0.0,
            regime_info={"regime_gate": {"h1_adx": 30.0, "primary_trend": "long"}},
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# Gate 4aa: Direction-aware counter-trend (statarb)
# ═══════════════════════════════════════════════════════════════════════════


class TestGate4aaCounterTrend:
    """Block statarb when trend is strong and signal is counter-trend."""

    def test_blocks_short_when_h1_trend_is_long(self) -> None:
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="statarb_dynamic",
            direction="short",
            confidence=0.7,
            entry_z_score=0.0,
            regime_info={
                "regime_gate": {
                    "h1_adx": 30.0,
                    "h4_trend_strength": 1.0,
                    "m5_trend_strength": 1.0,
                    "h1_trend_direction": "long",
                    "primary_trend": "long",
                    "primary_trend_source": "h1",
                }
            },
            config=config,
            brain_ids=["b1"],
            support_count=2,
            total_count=3,
            regime_gate_mode="active",
        )
        assert result is not None
        assert result.should_trade is False
        assert result.direction == "short"
        assert result.volume == 0.0
        assert "counter_trend_blocked" in result.reason

    def test_blocks_long_when_h1_trend_is_short(self) -> None:
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="statarb_m15",
            direction="long",
            confidence=0.7,
            entry_z_score=0.0,
            regime_info={
                "regime_gate": {
                    "h1_adx": 30.0,
                    "h4_trend_strength": 1.0,
                    "m5_trend_strength": 1.0,
                    "h1_trend_direction": "short",
                    "primary_trend": "short",
                    "primary_trend_source": "h1",
                }
            },
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        assert result is not None
        assert result.should_trade is False
        assert "counter_trend_blocked" in result.reason

    def test_passes_when_direction_matches_trend(self) -> None:
        """Long signal with long trend → no block."""
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="statarb_dynamic",
            direction="long",
            confidence=0.7,
            entry_z_score=0.0,
            regime_info={
                "regime_gate": {
                    "h1_adx": 30.0,
                    "h4_trend_strength": 1.0,
                    "m5_trend_strength": 1.0,
                    "h1_trend_direction": "long",
                    "primary_trend": "long",
                    "primary_trend_source": "h1",
                }
            },
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        assert result is None

    def test_passes_when_trend_weak(self) -> None:
        """ADX low + not MTF consensus → pass through 4aa, then 4c blocks? No.

        ADX values are normalized (0-1 scale). h1_adx=0.40 (40% of range) is
        below the 0.55 counter-trend block threshold AND below 25.0 for 4aa.
        With primary_trend=neutral → 4c counter-trend check is skipped.
        """
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="statarb_dynamic",
            direction="short",
            confidence=0.7,
            entry_z_score=0.0,
            regime_info={
                "regime_gate": {
                    "h1_adx": 0.40,  # Normalized: 0.40 < 0.55 block threshold
                    "h4_trend_strength": 0.3,  # ≤ 0.5 → not MTF consensus
                    "m5_trend_strength": 0.3,
                    "primary_trend": "neutral",  # neutral → 4c skip
                    "h1_trend_direction": "long",
                }
            },
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        assert result is None

    def test_mtf_consensus_with_moderate_adx(self) -> None:
        """ADX > 20 + H4_TS > 0.5 + M5_TS > 0.5 → MTF consensus blocks."""
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="statarb_dynamic",
            direction="short",
            confidence=0.7,
            entry_z_score=0.0,
            regime_info={
                "regime_gate": {
                    "h1_adx": 22.0,  # > 20
                    "h4_trend_strength": 0.8,  # > 0.5
                    "m5_trend_strength": 0.8,  # > 0.5
                    "h1_trend_direction": "long",
                    "primary_trend": "long",
                    "primary_trend_source": "h4",
                }
            },
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        assert result is not None


# ═══════════════════════════════════════════════════════════════════════════
# Gate 4b: Multi-TF hard filter (swing strategies)
# ═══════════════════════════════════════════════════════════════════════════


class TestGate4bHardMTF:
    """Block swing strategies when H1 and H4 trend directions diverge."""

    def test_blocks_swing_when_h1_h4_diverge(self) -> None:
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="m15_swing",
            direction="long",
            confidence=0.7,
            entry_z_score=0.0,
            regime_info={
                "regime_gate": {
                    "h4_trend_direction": "short",
                    "h1_trend_direction": "long",
                }
            },
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        assert result is not None
        assert result.should_trade is False
        assert "hard_trend_filter" in result.reason

    def test_passes_when_h1_h4_agree(self) -> None:
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="m15_swing",
            direction="long",
            confidence=0.7,
            entry_z_score=0.0,
            regime_info={
                "regime_gate": {
                    "h4_trend_direction": "long",
                    "h1_trend_direction": "long",
                }
            },
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        assert result is None

    def test_passes_when_h4_neutral(self) -> None:
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="m30_swing",
            direction="long",
            confidence=0.7,
            entry_z_score=0.0,
            regime_info={
                "regime_gate": {
                    "h4_trend_direction": "neutral",
                    "h1_trend_direction": "short",
                }
            },
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        assert result is None

    def test_non_swing_statarb_not_subject_to_4b(self) -> None:
        """statarb_dynamic is not a swing strategy → 4b doesn't apply."""
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="statarb_dynamic",
            direction="long",
            confidence=0.7,
            entry_z_score=0.0,
            regime_info={
                "regime_gate": {
                    "h4_trend_direction": "short",
                    "h1_trend_direction": "long",
                }
            },
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        # statarb only goes through 4aa (counter-trend) which needs strong trend
        # With no ADX → 0.0 → no trend → passes 4aa → reaches 4b which skips it → passes
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# Gate 4c: Counter-trend action (ADX-based)
# ═══════════════════════════════════════════════════════════════════════════


class TestGate4cCounterTrendADX:
    """Block counter-trend for strategies with ADX thresholds."""

    def test_blocks_statarb_when_h1_adx_above_block(self) -> None:
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="statarb_dynamic",
            direction="short",
            confidence=0.7,
            entry_z_score=0.0,
            regime_info={
                "regime_gate": {
                    "h1_adx": 30.0,  # raw ADX > 25.0 — 4aa fires before 4c
                    "h1_trend_direction": "long",
                    "primary_trend": "long",
                }
            },
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        assert result is not None
        # h1_adx=30.0 → 4aa fires (_trend_strength=30.0 > 25.0)
        assert "counter_trend_blocked" in result.reason
        assert "ts=30.0" in result.reason  # 4aa format

    def test_passes_when_h1_adx_below_block(self) -> None:
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="statarb_m15",
            direction="short",
            confidence=0.7,
            entry_z_score=0.0,
            regime_info={
                "regime_gate": {
                    "h1_adx": 20.0,  # raw ADX < 25.0 block threshold
                    "h1_trend_direction": "long",
                    "primary_trend": "long",
                }
            },
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        # Falls through 4aa (trend_strength=20.0 → no block) → 4b (not swing) → 4c
        # h1_adx=20.0 < 25.0 → not blocked by h1. No h4 in dict → h4_adx=0 → not blocked
        assert result is None

    def test_blocks_m15_swing_with_h1_adx_threshold(self) -> None:
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="m15_swing",
            direction="short",
            confidence=0.7,
            entry_z_score=0.0,
            regime_info={
                "regime_gate": {
                    "h1_adx": 30.0,  # raw ADX >= 25.0
                    "h1_trend_direction": "long",
                    "primary_trend": "long",
                }
            },
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        # m15_swing → swing list for 4b, but if h4_trend_direction not in dict → "" or "neutral"
        # 4b: needs both non-neutral and divergent → "neutral" blocks this
        # Then 4c: h1_adx >= 25.0 blocks
        assert result is not None


# ═══════════════════════════════════════════════════════════════════════════
# Gate 4d: Z-score inflection
# ═══════════════════════════════════════════════════════════════════════════


class TestGate4dZScoreInflection:
    """z-score inflection check for statarb/OU strategies."""

    def test_zero_entry_z_score_passes(self) -> None:
        """entry_z_score=0.0 → skip z-inflection check."""
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="statarb_dynamic",
            direction="long",
            confidence=0.7,
            entry_z_score=0.0,
            regime_info=None,
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        assert result is None

    def test_no_z_check_for_non_statarb_non_ou(self) -> None:
        """btc_swing doesn't trigger z-inflection even with non-zero z-score."""
        config = MockConfig()
        result = apply_trend_isolation_gates(
            name="btc_swing",
            direction="long",
            confidence=0.7,
            entry_z_score=13.0,  # Very high z-score
            regime_info=None,
            config=config,
            brain_ids=["b1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
        )
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# Gate 4e: Spatial z-score gate (FIX-20260807-002, non-asymmetric)
# ═══════════════════════════════════════════════════════════════════════════


class TestGate4eSpatialZScore:
    """Non-asymmetric price-position sanity for the swing family.

    LONG  H1_z > +1.5 → hard block (never buy the range top)
    SHORT H1_z < -1.5 → volume degrade only (sell-low stays viable)
    Ranging regime tightens both thresholds to ±1.0.
    """

    def test_long_hard_blocks_above_threshold(self) -> None:
        """m15_swing LONG at H1_z=+2.0 (range top) → hard block."""
        config = MockConfig()
        result = apply_spatial_zscore_gate(
            name="m15_swing",
            direction="long",
            h1_price_zscore=2.0,
            regime_info={"regime": "normal"},
            config=config,
        )
        assert result.blocked is True
        assert result.volume_mult == 1.0
        assert result.reason is not None
        assert "spatial_zscore_long_blocked" in result.reason

    def test_long_passes_below_threshold(self) -> None:
        """m15_swing LONG at H1_z=+1.0 (below 1.5) → pass-through."""
        config = MockConfig()
        result = apply_spatial_zscore_gate(
            name="m15_swing",
            direction="long",
            h1_price_zscore=1.0,
            regime_info={"regime": "normal"},
            config=config,
        )
        assert result.blocked is False
        assert result.volume_mult == 1.0

    def test_short_degrades_below_threshold(self) -> None:
        """m15_swing SHORT at H1_z=-2.0 (range bottom) → volume degrade only."""
        config = MockConfig()
        result = apply_spatial_zscore_gate(
            name="m15_swing",
            direction="short",
            h1_price_zscore=-2.0,
            regime_info={"regime": "normal"},
            config=config,
        )
        assert result.blocked is False
        assert result.volume_mult == 0.5
        assert result.reason is not None
        assert "spatial_zscore_short_degraded" in result.reason

    def test_short_passes_above_threshold(self) -> None:
        """m15_swing SHORT at H1_z=-1.0 (above -1.5) → no degradation."""
        config = MockConfig()
        result = apply_spatial_zscore_gate(
            name="m15_swing",
            direction="short",
            h1_price_zscore=-1.0,
            regime_info={"regime": "normal"},
            config=config,
        )
        assert result.blocked is False
        assert result.volume_mult == 1.0

    def test_ranging_tightens_long_block(self) -> None:
        """Ranging regime tightens threshold to ±1.0 — LONG at +1.2 now blocks."""
        config = MockConfig()
        result = apply_spatial_zscore_gate(
            name="m30_swing",
            direction="long",
            h1_price_zscore=1.2,
            regime_info={"regime": "ranging"},
            config=config,
        )
        assert result.blocked is True
        assert result.reason is not None
        assert "_ranging" in result.reason

    def test_ranging_tightens_short_degrade(self) -> None:
        """Ranging regime tightens short threshold to -1.0 — SHORT at -1.2 degrades."""
        config = MockConfig()
        result = apply_spatial_zscore_gate(
            name="h1_swing",
            direction="short",
            h1_price_zscore=-1.2,
            regime_info={"regime": "ranging"},
            config=config,
        )
        assert result.blocked is False
        assert result.volume_mult == 0.5
        assert result.reason is not None
        assert "_ranging" in result.reason

    def test_non_ranging_long_passes_at_1_2(self) -> None:
        """Normal regime at +1.2 → below 1.5 threshold → pass (ranging-coupling proof)."""
        config = MockConfig()
        result = apply_spatial_zscore_gate(
            name="m30_swing",
            direction="long",
            h1_price_zscore=1.2,
            regime_info={"regime": "trending"},
            config=config,
        )
        assert result.blocked is False
        assert result.volume_mult == 1.0

    def test_non_eligible_strategy_passes(self) -> None:
        """statarb_dynamic is NOT in the swing family → gate does not apply."""
        config = MockConfig()
        result = apply_spatial_zscore_gate(
            name="statarb_dynamic",
            direction="long",
            h1_price_zscore=3.0,
            regime_info={"regime": "normal"},
            config=config,
        )
        assert result.blocked is False
        assert result.volume_mult == 1.0

    def test_btc_swing_not_eligible(self) -> None:
        """btc_swing is excluded — audit evidence is XAU-only (8/19 红线)."""
        config = MockConfig()
        result = apply_spatial_zscore_gate(
            name="btc_swing",
            direction="long",
            h1_price_zscore=2.5,
            regime_info={"regime": "normal"},
            config=config,
        )
        assert result.blocked is False
        assert result.volume_mult == 1.0

    def test_none_zscore_passes(self) -> None:
        """Missing z-score → fail-open pass-through (data gap never mis-blocks)."""
        config = MockConfig()
        result = apply_spatial_zscore_gate(
            name="m15_swing",
            direction="long",
            h1_price_zscore=None,
            regime_info={"regime": "normal"},
            config=config,
        )
        assert result.blocked is False
        assert result.volume_mult == 1.0

    def test_neutral_direction_passes(self) -> None:
        """Neutral direction → gate never fires."""
        config = MockConfig()
        result = apply_spatial_zscore_gate(
            name="m15_swing",
            direction="neutral",
            h1_price_zscore=3.0,
            regime_info={"regime": "normal"},
            config=config,
        )
        assert result.blocked is False

    def test_config_override_long_block_threshold(self) -> None:
        """StrategyLineConfig can tighten long-block threshold below default 1.5."""

        class TightConfig(MockConfig):
            spatial_long_block_z: float = 0.8

        config = TightConfig()
        result = apply_spatial_zscore_gate(
            name="m15_swing",
            direction="long",
            h1_price_zscore=0.9,
            regime_info={"regime": "normal"},
            config=config,
        )
        assert result.blocked is True

    def test_config_override_short_degrade_mult(self) -> None:
        """StrategyLineConfig can tune the short degrade volume multiplier."""

        class ShyConfig(MockConfig):
            spatial_short_degrade_mult: float = 0.35

        config = ShyConfig()
        result = apply_spatial_zscore_gate(
            name="m15_swing",
            direction="short",
            h1_price_zscore=-2.0,
            regime_info={"regime": "normal"},
            config=config,
        )
        assert result.blocked is False
        assert result.volume_mult == 0.35

    def test_integration_with_full_gate_chain(self) -> None:
        """Full apply_trend_isolation_gates + spatial gate: swing LONG at top blocked.

        The 4e gate is a separate function (spatial result consumed by
        strategy_line.py) — this test locks the cooperative contract: 4aa-4d
        pass (no direction conflict), then 4e blocks on position.
        """
        config = MockConfig()
        trend_result = apply_trend_isolation_gates(
            name="h1_swing",
            direction="long",
            confidence=0.7,
            entry_z_score=0.0,
            regime_info={
                "regime": "trending",
                "regime_gate": {
                    "h4_trend_direction": "long",
                    "h1_trend_direction": "long",
                    "h1_adx": 18.0,
                    "primary_trend": "long",
                },
            },
            config=config,
            brain_ids=["b1"],
            support_count=2,
            total_count=3,
            regime_gate_mode="active",
        )
        assert trend_result is None  # 4aa-4d all pass
        spatial = apply_spatial_zscore_gate(
            name="h1_swing",
            direction="long",
            h1_price_zscore=2.1,
            regime_info={"regime": "trending", "regime_gate": {}},
            config=config,
        )
        assert spatial.blocked is True


# ═══════════════════════════════════════════════════════════════════════════
# H1_Price_ZScore extraction from the runtime v9_40 feature vector
# ═══════════════════════════════════════════════════════════════════════════


def _v9_vector(h1_z: float) -> np.ndarray:
    """Build a 40-dim v9 institutional vector with H1_Price_ZScore set."""
    from core.features.schemas.registry import get_schema_feature_names

    _names = get_schema_feature_names("v9_institutional_40")
    _idx = _names.index("H1_Price_ZScore")
    vec = np.zeros(len(_names), dtype=np.float64)
    vec[_idx] = h1_z
    return vec


class TestExtractH1PriceZScore:
    """extract_h1_price_zscore() — fail-open extraction from feature vector."""

    def test_extracts_from_v9_vector(self) -> None:
        vec = _v9_vector(-0.42)
        assert extract_h1_price_zscore(vec) == -0.42

    def test_none_vector_returns_none(self) -> None:
        assert extract_h1_price_zscore(None) is None

    def test_short_vector_returns_none(self) -> None:
        assert extract_h1_price_zscore(np.zeros(30, dtype=np.float64)) is None

    def test_nan_returns_none(self) -> None:
        from core.features.schemas.registry import get_schema_feature_names

        _idx = get_schema_feature_names("v9_institutional_40").index("H1_Price_ZScore")
        vec = np.zeros(40, dtype=np.float64)
        vec[_idx] = float("nan")
        assert extract_h1_price_zscore(vec) is None

    def test_list_input_works(self) -> None:
        from core.features.schemas.registry import get_schema_feature_names

        _idx = get_schema_feature_names("v9_institutional_40").index("H1_Price_ZScore")
        lst = [0.0] * 40
        lst[_idx] = 1.25
        assert extract_h1_price_zscore(lst) == 1.25
