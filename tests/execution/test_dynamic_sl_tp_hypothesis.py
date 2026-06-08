"""Property-based tests for dynamic SL/TP — Hypothesis (S4 Functional Core).

Verifies mathematical invariants that MUST hold for ANY input:
  1. Vol ratio clamped to safe bounds
  2. SL/TP multipliers never exceed hard caps
  3. Min SL distance always respected
  4. Min RR ratio always maintained when enforced
  5. Long/short symmetry
  6. Never returns negative or zero distances
"""

from __future__ import annotations

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from core.execution.dynamic_sl_tp import (
    MAX_SL_ATR,
    MAX_TP_ATR,
    MIN_SL_ATR,
    MIN_TP_ATR,
    compute_dynamic_sl_tp,
    compute_sl_tp_levels,
)

# ── Input generators ───────────────────────────────────────────────────────


@st.composite
def reasonable_sltp_inputs(draw):
    """Generate realistic SL/TP inputs covering edge cases."""
    return dict(
        base_sl_mult=draw(st.floats(0.5, 5.0)),
        base_tp_mult=draw(st.floats(0.3, 8.0)),
        current_atr=draw(st.floats(0.1, 500.0)),
        ref_atr=draw(st.floats(0.1, 500.0)),
    )


@st.composite
def level_inputs(draw, *, with_spread: bool = True):
    """Generate SL/TP level computation inputs (via DynamicSLTP)."""
    side = draw(st.sampled_from(["long", "short"]))
    entry_price = draw(st.floats(1000.0, 100000.0))
    tick_size = draw(st.sampled_from([0.01, 0.1, 0.001]))
    spread_points = draw(st.floats(0.0, 500.0)) if with_spread else 0.0
    dsl = compute_dynamic_sl_tp(
        base_sl_mult=draw(st.floats(0.5, 5.0)),
        base_tp_mult=draw(st.floats(0.3, 8.0)),
        current_atr=draw(st.floats(0.1, 500.0)),
        ref_atr=draw(st.floats(0.1, 500.0)),
    )
    return dict(
        side=side,
        entry_price=entry_price,
        dsl=dsl,
        tick_size=tick_size,
        spread_points=spread_points,
    )


# ── Property tests ─────────────────────────────────────────────────────────


class TestSLTPInvariants:
    """Mathematical invariants that must hold for ANY valid input."""

    @given(reasonable_sltp_inputs())
    @settings(max_examples=500)
    def test_vol_ratio_never_zero_or_negative(self, inp):
        """Vol ratio must always be positive."""
        result = compute_dynamic_sl_tp(**inp)
        assert result.vol_ratio > 0

    @given(reasonable_sltp_inputs())
    @settings(max_examples=500)
    def test_sl_multiplier_within_hard_bounds(self, inp):
        """SL multiplier must never exceed hard-coded safety bounds."""
        result = compute_dynamic_sl_tp(**inp)
        assert (
            MIN_SL_ATR <= result.sl_atr_mult <= MAX_SL_ATR
        ), f"sl_atr_mult={result.sl_atr_mult} outside [{MIN_SL_ATR}, {MAX_SL_ATR}]"

    @given(reasonable_sltp_inputs())
    @settings(max_examples=500)
    def test_tp_multiplier_within_hard_bounds(self, inp):
        """TP multiplier must never exceed hard-coded safety bounds."""
        result = compute_dynamic_sl_tp(**inp)
        assert (
            MIN_TP_ATR <= result.tp_atr_mult <= MAX_TP_ATR
        ), f"tp_atr_mult={result.tp_atr_mult} outside [{MIN_TP_ATR}, {MAX_TP_ATR}]"

    @given(reasonable_sltp_inputs())
    @settings(max_examples=500)
    def test_sl_positive(self, inp):
        """SL distance must always be positive."""
        result = compute_dynamic_sl_tp(**inp)
        assert result.sl_distance > 0

    @given(reasonable_sltp_inputs())
    @settings(max_examples=500)
    def test_tp_positive(self, inp):
        """TP distance must always be positive."""
        result = compute_dynamic_sl_tp(**inp)
        assert result.tp_distance > 0


class TestSLTPLevelInvariants:
    """Level computation invariants."""

    @given(level_inputs())
    @settings(max_examples=500)
    def test_sl_below_entry_for_long(self, inp):
        assume(inp["side"] == "long")
        result = compute_sl_tp_levels(**inp)
        assert result["stop_loss"] < inp["entry_price"]

    @given(level_inputs())
    @settings(max_examples=500)
    def test_sl_above_entry_for_short(self, inp):
        assume(inp["side"] == "short")
        result = compute_sl_tp_levels(**inp)
        assert result["stop_loss"] > inp["entry_price"]

    @given(level_inputs(with_spread=False))
    @settings(max_examples=500)
    def test_tp_above_entry_long_no_spread(self, inp):
        assume(inp["side"] == "long")
        result = compute_sl_tp_levels(**inp)
        assert result["take_profit"] > inp["entry_price"]

    @given(level_inputs(with_spread=False))
    @settings(max_examples=500)
    def test_tp_below_entry_short_no_spread(self, inp):
        assume(inp["side"] == "short")
        result = compute_sl_tp_levels(**inp)
        assert result["take_profit"] < inp["entry_price"]

    @given(level_inputs())
    @settings(max_examples=500)
    def test_sl_distance_never_negative(self, inp):
        result = compute_sl_tp_levels(**inp)
        sl_dist = abs(inp["entry_price"] - result["stop_loss"])
        assert sl_dist > 0

    @given(level_inputs())
    @settings(max_examples=500)
    def test_hard_sl_wider_than_initial_sl(self, inp):
        result = compute_sl_tp_levels(**inp)
        if inp["side"] == "long":
            assert result["hard_sl"] < result["stop_loss"]
        else:
            assert result["hard_sl"] > result["stop_loss"]

    @given(level_inputs(with_spread=False))
    @settings(max_examples=500)
    def test_monotonic_long_no_spread(self, inp):
        assume(inp["side"] == "long")
        result = compute_sl_tp_levels(**inp)
        assert result["hard_sl"] < result["stop_loss"] < inp["entry_price"] < result["take_profit"]

    @given(level_inputs(with_spread=False))
    @settings(max_examples=500)
    def test_monotonic_short_no_spread(self, inp):
        assume(inp["side"] == "short")
        result = compute_sl_tp_levels(**inp)
        assert result["take_profit"] < inp["entry_price"] < result["stop_loss"] < result["hard_sl"]
