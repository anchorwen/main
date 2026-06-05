"""Tests for TrailStopEngine — extracted from position_manager (Phase A+B).

FIX-010: TrailStopEngine created as standalone class with TrailPolicy
dataclass.  Removed confidence-based trail adjustments (Phase A) and
switched from delta-based vol adjustment (+0.8/-0.3) to capped additive
adjustment (FIX-071: -0.5 for vol>1.5, +0.5 for vol<0.7).

Historically, trail stop bugs were the #2 recurring category:
- FIX-064: trail activation watermark (don't trail before 1.0x ATR profit)
- FIX-071: quadratic explosion fix (inverted vol adjustment)
- FIX-084: dynamic floor skip breakeven when RR<1.0
- FIX-010: Phase A+B separation + TrailPolicy per-strategy
- FIX-026: per-strategy trail params (atr_mult_low/high)
"""

from __future__ import annotations

import pytest

from core.execution.position_manager import ActivePosition, ActivePositionManager
from core.execution.trail_stop_engine import TrailPolicy, TrailStopEngine


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def default_policy() -> TrailPolicy:
    return TrailPolicy(
        trail_atr_mult=2.0,
        trail_atr_mult_low=1.5,
        trail_atr_mult_high=3.0,
        breakeven_threshold_atr=1.0,
        trail_activation_atr=1.0,
        min_trail_mult=0.8,
        max_lock_atr=1.5,
    )


@pytest.fixture
def engine(default_policy) -> TrailStopEngine:
    return TrailStopEngine(default_policy=default_policy)


@pytest.fixture
def long_pos() -> ActivePosition:
    pm = ActivePositionManager()
    return pm.register_position(
        ticket=1,
        side="long",
        entry_price=2500.0,
        volume=0.01,
        initial_sl=2490.0,
        initial_tp=2535.0,
        entry_atr=5.0,
        entry_cycle=0,
        entry_consensus={"aggregated_bias": "long", "consensus_score": 0.65},
        supporting_brain_ids=["brain_a", "brain_b"],
        current_high=2500.0,
    )


@pytest.fixture
def short_pos() -> ActivePosition:
    pm = ActivePositionManager()
    return pm.register_position(
        ticket=2,
        side="short",
        entry_price=2500.0,
        volume=0.01,
        initial_sl=2510.0,
        initial_tp=2465.0,
        entry_atr=5.0,
        entry_cycle=0,
        entry_consensus={"aggregated_bias": "short", "consensus_score": 0.65},
        supporting_brain_ids=["brain_c"],
    )


# ── Trail Activation Watermark (FIX-064) ───────────────────────────────────


def test_trail_not_activated_before_watermark(engine, long_pos):
    """Trail returns None until unrealized profit exceeds activation_atr × entry_atr."""
    long_pos.highest_high = 2503.0  # +3pts, activation = 1.0 × 5.0 = 5.0
    long_pos.trail_multiplier = 2.0
    result = engine.compute_trail_stop(long_pos, current_atr=5.0)
    assert result is None  # < 1.0 ATR profit — trail stays at initial SL


def test_trail_activated_after_watermark(engine, long_pos):
    """Trail activates once unrealized profit crosses the watermark."""
    long_pos.highest_high = 2510.0  # +10pts >= 5.0 activation
    long_pos.trail_multiplier = 2.0
    long_pos.breakeven_triggered = True
    result = engine.compute_trail_stop(long_pos, current_atr=5.0)
    assert result is not None
    # candidate = max(2490, 2510-10) = 2500, breakeven floor = max(2500, 2500) = 2500
    assert result >= 2490.0  # never below original SL


def test_activation_watermark_short(engine, short_pos):
    """Short trail activation checks entry - lowest_low."""
    short_pos.lowest_low = 2493.0  # +7pts, activation = 5.0
    short_pos.trail_multiplier = 2.0
    result = engine.compute_trail_stop(short_pos, current_atr=5.0)
    assert result is not None
    # candidate = min(2510, 2493+10) = 2503
    assert result <= short_pos.initial_sl


# ── Volatility Adjustment (FIX-071) ────────────────────────────────────────


def test_high_vol_tightens_trail(engine, long_pos):
    """vol_ratio > 1.5 → tighten K by -0.5. Extreme vol = climax, ATR provides room."""
    long_pos.trail_multiplier = 2.0
    long_pos.entry_atr = 5.0
    long_pos.highest_high = 2550.0
    engine.adjust_trail_for_regime(long_pos, current_atr=10.0, regime_info={"regime": "normal"})
    # vol_ratio = 2.0 → vol_adj = -0.5 → k = 2.0-0.5 = 1.5
    assert long_pos.trail_multiplier == 1.5


def test_low_vol_widens_trail(engine, long_pos):
    """vol_ratio < 0.7 → widen K by +0.5. Tight trails kill positions in quiet markets."""
    long_pos.trail_multiplier = 2.0
    long_pos.entry_atr = 5.0
    long_pos.highest_high = 2550.0
    engine.adjust_trail_for_regime(long_pos, current_atr=1.0, regime_info={"regime": "normal"})
    # vol_ratio = 0.2 → vol_adj = +0.5 → k = 2.0+0.5 = 2.5
    assert long_pos.trail_multiplier == 2.5


def test_normal_vol_no_adjustment(engine, long_pos):
    """vol_ratio in [0.7, 1.2] — no adjustment."""
    long_pos.trail_multiplier = 2.0
    long_pos.entry_atr = 5.0
    long_pos.highest_high = 2550.0
    engine.adjust_trail_for_regime(long_pos, current_atr=5.0, regime_info={"regime": "normal"})
    assert long_pos.trail_multiplier == 2.0


def test_mild_high_vol_tightens_modestly(engine, long_pos):
    """vol_ratio > 1.2 but ≤ 1.5 → tighten by -0.2."""
    long_pos.trail_multiplier = 2.0
    long_pos.entry_atr = 5.0
    long_pos.highest_high = 2550.0
    engine.adjust_trail_for_regime(long_pos, current_atr=7.0, regime_info={"regime": "normal"})
    # vol_ratio = 1.4 → vol_adj = -0.2 → k = 1.8
    assert long_pos.trail_multiplier == 1.8


# ── Regime-Based Multiplier Selection ──────────────────────────────────────


def test_low_regime_uses_low_mult(engine, long_pos):
    """Regime 'low' selects trail_atr_mult_low."""
    long_pos.trail_multiplier = 2.0
    long_pos.entry_atr = 5.0
    long_pos.highest_high = 2550.0
    engine.adjust_trail_for_regime(long_pos, current_atr=5.0, regime_info={"regime": "low"})
    # base = 1.5, vol_ratio=1.0 → no adj → k=1.5
    assert long_pos.trail_multiplier == 1.5


def test_high_regime_uses_high_mult(engine, long_pos):
    """Regime 'high' selects trail_atr_mult_high."""
    long_pos.trail_multiplier = 2.0
    long_pos.entry_atr = 5.0
    long_pos.highest_high = 2550.0
    engine.adjust_trail_for_regime(long_pos, current_atr=5.0, regime_info={"regime": "high"})
    # base = 3.0, vol_ratio=1.0 → no adj → k=3.0
    assert long_pos.trail_multiplier == 3.0


# ── Breakeven ──────────────────────────────────────────────────────────────


def test_breakeven_not_triggered_below_threshold(engine, long_pos):
    """Should not breakeven when favorable move < threshold."""
    long_pos.highest_high = 2503.0  # +3 < 1.0 * 5.0 = 5.0
    assert not engine.should_breakeven(long_pos, current_atr=5.0)


def test_breakeven_triggered_above_threshold(engine, long_pos):
    """Should breakeven when move >= threshold."""
    long_pos.highest_high = 2507.0  # +7 >= 5.0
    assert engine.should_breakeven(long_pos, current_atr=5.0)


def test_breakeven_not_triggered_twice(engine, long_pos):
    """Already triggered → no repeat."""
    long_pos.highest_high = 2507.0
    long_pos.breakeven_triggered = True
    assert not engine.should_breakeven(long_pos, current_atr=5.0)


def test_breakeven_short(engine, short_pos):
    """Breakeven for short: entry - lowest_low >= threshold."""
    short_pos.lowest_low = 2493.0  # -7 >= 5.0
    assert engine.should_breakeven(short_pos, current_atr=5.0)


# ── Trail Stop Calculation ─────────────────────────────────────────────────


def test_trail_stop_long_advances(engine, long_pos):
    """Trail advances when price moves favorably."""
    long_pos.highest_high = 2520.0  # strong rally
    long_pos.trail_multiplier = 2.0
    long_pos.current_sl = 2490.0
    result = engine.compute_trail_stop(long_pos, current_atr=5.0)
    assert result is not None
    # candidate = max(2490, 2520-10) = 2510, cap = 2500+1.5*5=2507.5
    # But activation: (2520-2500)/5 = 4.0 >= 1.0 → passes
    assert result >= long_pos.initial_sl


def test_trail_stop_short_advances(engine, short_pos):
    """Trail advances for short when price drops."""
    short_pos.lowest_low = 2480.0
    short_pos.trail_multiplier = 2.0
    short_pos.current_sl = 2510.0
    result = engine.compute_trail_stop(short_pos, current_atr=5.0)
    assert result is not None
    # candidate = min(2510, 2480+10) = 2490, cap = 2500-1.5*5=2492.5
    assert result <= short_pos.initial_sl


# ── Per-Position TrailPolicy ───────────────────────────────────────────────


def test_per_position_policy_overrides_default(engine, long_pos):
    """Position-specific TrailPolicy takes precedence over engine default."""
    custom = TrailPolicy(
        trail_atr_mult=2.5,
        trail_atr_mult_low=2.0,
        trail_atr_mult_high=3.5,
        breakeven_threshold_atr=0.8,
        trail_activation_atr=0.5,
        min_trail_mult=0.5,
        max_lock_atr=2.0,
    )
    long_pos.trail_policy = custom
    long_pos.trail_multiplier = 2.0
    long_pos.entry_atr = 5.0
    long_pos.highest_high = 2550.0

    engine.adjust_trail_for_regime(long_pos, current_atr=5.0, regime_info={"regime": "low"})
    # Uses custom trail_atr_mult_low=2.0, not default 1.5
    assert long_pos.trail_multiplier == 2.0
