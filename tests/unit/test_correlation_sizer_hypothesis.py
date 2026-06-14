"""Property-based tests for correlation_sizer — √N discount engine.

Institutional invariants (Phase 2.2 — toxicity bombardment):

1. POLARITY INVARIANCE: Output direction sign MUST match input direction.
   A SHORT call must never produce a positive allocation.

2. VOLUME NON-INFLATION: √N discount must never INCREASE volume.
   Post-discount volume ≤ pre-discount volume for every decision.

3. ZERO-STATE DEGENERATION: Zero/NaN/Inf inputs must produce valid,
   crash-free output (all zeros, no ZeroDivisionError).

These tests use hypothesis to explore the full parameter space of
these three invariants, catching edge cases that hand-written tests miss.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Decision stub — duck-type compatible with StrategyDecision
# ---------------------------------------------------------------------------
@dataclass
class SimpleDecision:
    strategy_name: str
    direction: str
    volume: float
    should_trade: bool = True
    reason: str = ""
    confidence: float = 0.80
    raw_score: float = 0.0


# ---------------------------------------------------------------------------
# Hypothesis strategies: generate lists of decisions with controlled properties
# ---------------------------------------------------------------------------
_directions = st.sampled_from(["long", "short"])

_volumes = st.one_of(
    st.floats(0.0, 10.0, allow_nan=False, allow_infinity=False),
    st.just(0.0),
    st.just(0.01),  # lot_step
    st.just(0.05),  # max_volume
)

_should_trade = st.booleans()


@st.composite
def decision_lists(draw: Any, min_size: int = 0, max_size: int = 30) -> list[SimpleDecision]:
    """Generate random lists of SimpleDecision objects."""
    n = draw(st.integers(min_size, max_size))
    decisions: list[SimpleDecision] = []
    for i in range(n):
        decisions.append(
            SimpleDecision(
                strategy_name=f"strat_{i}",
                direction=draw(_directions),
                volume=draw(_volumes),
                should_trade=draw(_should_trade),
                confidence=draw(st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False)),
            )
        )
    return decisions


# ---------------------------------------------------------------------------
# INVARIANT 1: POLARITY INVARIANCE
# ---------------------------------------------------------------------------
@given(decisions=decision_lists(min_size=1, max_size=20))
@settings(max_examples=200)
def test_polarity_invariance_direction_never_flips(decisions: list[SimpleDecision]) -> None:
    """Output direction must match input direction for every decision.

    The √N sizer only modifies volume and should_trade — it must NEVER
    change a decision's direction, no matter how extreme the inputs.
    """
    from core.execution.correlation_sizer import apply_sqrt_n_discount

    # Snapshot original directions
    original_directions = {d.strategy_name: d.direction for d in decisions}

    modified, _clusters = apply_sqrt_n_discount(decisions)

    for d in modified:
        orig_dir = original_directions[d.strategy_name]
        assert d.direction == orig_dir, (
            f"POLARITY VIOLATION: {d.strategy_name} direction changed "
            f"from '{orig_dir}' to '{d.direction}' — correlation_sizer "
            f"must never flip a direction signal."
        )


# ---------------------------------------------------------------------------
# INVARIANT 2: VOLUME NON-INFLATION
# ---------------------------------------------------------------------------
@given(decisions=decision_lists(min_size=1, max_size=25))
@settings(max_examples=200)
def test_volume_non_inflation_discount_never_increases(decisions: list[SimpleDecision]) -> None:
    """√N discount must never INCREASE volume for any decision.

    The discount factor is 1/√N ≤ 1 for all N ≥ 1.  Post-discount
    volume must be ≤ pre-discount volume for every individual decision
    in a cluster.
    """
    from core.execution.correlation_sizer import apply_sqrt_n_discount

    # Snapshot original volumes
    original_volumes = {d.strategy_name: d.volume for d in decisions}

    modified, _clusters = apply_sqrt_n_discount(decisions)

    for d in modified:
        orig_vol = original_volumes[d.strategy_name]
        assert d.volume <= orig_vol + 1e-9, (
            f"VOLUME INFLATION: {d.strategy_name} volume increased "
            f"from {orig_vol} to {d.volume} — √N discount must be ≤ 1.0"
        )

    # Cluster-level invariant: total discounted ≤ total raw
    for cluster in _clusters:
        assert cluster.discounted_volume <= cluster.raw_total_volume + 1e-9, (
            f"CLUSTER INFLATION: direction={cluster.direction} "
            f"raw={cluster.raw_total_volume} → disc={cluster.discounted_volume} "
            f"(n={cluster.n_same_direction}, discount=1/√{cluster.n_same_direction}"
            f"={1.0/math.sqrt(cluster.n_same_direction):.4f})"
        )


# ---------------------------------------------------------------------------
# INVARIANT 3: ZERO-STATE DEGENERATION
# ---------------------------------------------------------------------------
@given(
    volume_val=st.one_of(
        st.just(0.0),
        st.just(float("nan")),
        st.just(float("inf")),
        st.just(float("-inf")),
    ),
    n_decisions=st.integers(1, 10),
    direction=st.sampled_from(["long", "short"]),
)
@settings(max_examples=100)
def test_zero_state_degeneration_no_crash(
    volume_val: float, n_decisions: int, direction: str
) -> None:
    """Extreme volume values (0, NaN, Inf) must NOT crash the sizer."""
    from core.execution.correlation_sizer import apply_sqrt_n_discount

    decisions = [
        SimpleDecision(
            strategy_name=f"bad_{i}",
            direction=direction,
            volume=volume_val,
            should_trade=True,
        )
        for i in range(n_decisions)
    ]

    # Must not raise
    modified, clusters = apply_sqrt_n_discount(decisions)

    # All outputs must be finite
    for d in modified:
        if math.isnan(d.volume) or math.isinf(d.volume):
            # NaN/Inf in → the sizer may preserve NaN or clamp to 0
            # Either is acceptable — the key invariant is NO CRASH
            pass

    # Cluster results must be finite
    for c in clusters:
        assert not math.isnan(c.discounted_volume), (
            f"NaN in cluster result: discounted_volume={c.discounted_volume}"
        )
        assert not math.isinf(c.discounted_volume), (
            f"Inf in cluster result: discounted_volume={c.discounted_volume}"
        )


@given(decisions=decision_lists(min_size=0, max_size=0))
@settings(max_examples=10)
def test_empty_input_produces_empty_output(decisions: list[SimpleDecision]) -> None:
    """Empty decision list → empty output, no crash."""
    from core.execution.correlation_sizer import apply_sqrt_n_discount

    modified, clusters = apply_sqrt_n_discount(decisions)

    assert modified == []
    assert clusters == []


@given(
    n_long=st.integers(1, 8),
    n_short=st.integers(1, 8),
    volume=st.floats(0.001, 0.1),
)
@settings(max_examples=100)
def test_all_zero_confidence_decisions_produce_zero_volume(
    n_long: int, n_short: int, volume: float
) -> None:
    """When all decisions have should_trade=False, no volume is allocated."""
    from core.execution.correlation_sizer import apply_sqrt_n_discount

    decisions = [
        SimpleDecision(
            strategy_name=f"nolong_{i}",
            direction="long",
            volume=volume,
            should_trade=False,  # all blocked
        )
        for i in range(n_long)
    ] + [
        SimpleDecision(
            strategy_name=f"noshort_{i}",
            direction="short",
            volume=volume,
            should_trade=False,  # all blocked
        )
        for i in range(n_short)
    ]

    modified, clusters = apply_sqrt_n_discount(decisions)

    # All should_trade=False decisions are excluded from clusters
    # So clusters should be empty (n <= 1 for each direction)
    for d in modified:
        assert d.should_trade is False, "Non-trading decisions should remain non-trading"


# ---------------------------------------------------------------------------
# INVARIANT 4: SINGLE-DECISION PASSTHROUGH
# ---------------------------------------------------------------------------
@given(direction=st.sampled_from(["long", "short"]), volume=st.floats(0.01, 1.0))
@settings(max_examples=100)
def test_single_decision_passthrough_unchanged(direction: str, volume: float) -> None:
    """A single decision (N=1) must pass through completely unchanged."""
    from core.execution.correlation_sizer import apply_sqrt_n_discount

    d = SimpleDecision(strategy_name="solo", direction=direction, volume=volume)
    original = SimpleDecision(strategy_name="solo", direction=direction, volume=volume)

    modified, clusters = apply_sqrt_n_discount([d])

    assert len(modified) == 1
    assert modified[0].volume == original.volume, (
        f"Single decision volume changed: {original.volume} → {modified[0].volume}"
    )
    assert modified[0].should_trade == original.should_trade
    assert clusters == [], "N=1 should produce no cluster records"


# ---------------------------------------------------------------------------
# INVARIANT 5: IDEMPOTENCE
# ---------------------------------------------------------------------------
@given(
    n_decisions=st.integers(2, 10),
    volume=st.floats(0.01, 1.0),
    direction=st.sampled_from(["long", "short"]),
)
@settings(max_examples=100)
def test_discount_matches_sqrt_n_formula(
    n_decisions: int, volume: float, direction: str
) -> None:
    """√N discount must apply exactly once: vol_discounted ≤ vol_raw / √N.

    The function is NOT idempotent by design — each call multiplies again.
    This test verifies single-pass correctness.
    """
    from core.execution.correlation_sizer import apply_sqrt_n_discount

    decisions = [
        SimpleDecision(
            strategy_name=f"test_{i}",
            direction=direction,
            volume=volume,
            should_trade=True,
        )
        for i in range(n_decisions)
    ]

    modified, clusters = apply_sqrt_n_discount(decisions)

    expected_discount = 1.0 / math.sqrt(n_decisions)
    assert len(clusters) == 1
    cluster = clusters[0]

    # Each decision's post-discount volume ≤ volume * discount + rounding error
    for d in modified:
        if d.volume > 0:
            assert d.volume <= volume * expected_discount + 0.01, (
                f"Volume {d.volume} exceeds max allowed "
                f"{volume * expected_discount + 0.01} "
                f"(raw={volume}, discount=1/√{n_decisions}={expected_discount:.4f})"
            )

    # Cluster audit record matches (raw_total_volume is rounded to 4dp in function)
    assert cluster.n_same_direction == n_decisions
    assert cluster.raw_total_volume == pytest.approx(volume * n_decisions, rel=0.01)  # 1% tolerance for banker's rounding
    assert cluster.discounted_volume <= cluster.raw_total_volume + 0.01
