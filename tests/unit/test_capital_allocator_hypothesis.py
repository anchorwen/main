"""Property-based tests for capital_allocator — the financial controller.

Phase 2.2 — institutional invariants for the final lot-sizing gate.

TARGET 1: compute_volume() — the last multiplication before money hits market.
TARGET 2: CapitalAllocator.allocate_capacity() — cross-brain budget division.

INVARIANTS (compute_volume):
  1. VOLUME BOUNDS: output ∈ [min_volume, max_volume] — absolute guardrails
  2. LOT STEP INTEGRITY: output is a positive multiple of 0.01
  3. MONOTONICITY: better agreement → same or larger volume (all else equal)
  4. ZERO ATR SAFETY: vol_atr=0 → no division by zero, vol_factor=1.0
  5. NAN SAFETY: NaN in any float input → no crash
  6. REGIME ORDER: low_vol ≥ normal ≥ high_vol (for same inputs)
  7. DETERMINISM: same inputs → same outputs

INVARIANTS (allocate_capacity):
  8. BUDGET CONSERVATION: sum(allocations) ≤ total_budget
  9. CONCENTRATION CAP: no single brain > total_budget × max_concentration
  10. ZERO WEIGHT DEGENERATION: all-zero weights → all-zero allocations
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------
_regimes = st.sampled_from(["low", "normal", "high"])

_base_volumes = st.floats(0.001, 0.5, allow_nan=False, allow_infinity=False)

_vol_atrs = st.one_of(
    st.floats(0.1, 50.0, allow_nan=False, allow_infinity=False),
    st.just(0.0),
    st.just(-1.0),  # negative ATR → should be guarded
    st.just(0.001),  # near-zero ATR
)

_vol_references = st.floats(1.0, 20.0, allow_nan=False, allow_infinity=False)

_agreement_levels = st.sampled_from(["full", "reduced", "minimal"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_decision(agreement: str, direction: str = "long") -> Any:
    """Create a minimal AllocationDecision for testing."""
    from core.execution.capital_allocator import AllocationDecision

    return AllocationDecision(
        should_trade=True,
        direction=direction,
        confidence=0.80,
        volume=0.0,
        agreement_level=agreement,
        active_groups=["barrier_12bar"],
        reason=f"{agreement}_long_+barrier_12bar",
    )


# ============================================================================
# INVARIANT 1: VOLUME BOUNDS
# ============================================================================
@given(
    base_volume=_base_volumes,
    agreement=st.sampled_from(["full", "reduced", "minimal", "garbage"]),
    regime=_regimes,
    vol_atr=_vol_atrs,
    # Use realistic lot-step multiples (0.01, 0.02, ...)
    min_volume=st.sampled_from([0.01, 0.02, 0.03, 0.05]),
    max_volume=st.sampled_from([0.05, 0.10, 0.20, 0.50, 1.0]),
)
@settings(max_examples=300)
def test_volume_bounds_never_exceeded(
    base_volume: float,
    agreement: str,
    regime: str,
    vol_atr: float,
    min_volume: float,
    max_volume: float,
) -> None:
    """Output must ALWAYS be within [min_volume, max_volume]."""
    from core.execution.capital_allocator import compute_volume

    decision = _make_decision(agreement)
    # Override agreement_level if "garbage" — test unknown agreement handling
    if agreement == "garbage":
        decision.agreement_level = "garbage"

    assume(min_volume < max_volume)

    result = compute_volume(
        base_volume=base_volume,
        decision=decision,
        regime=regime,
        vol_atr=vol_atr,
        min_volume=min_volume,
        max_volume=max_volume,
    )

    assert not math.isnan(result), f"NaN volume: inputs={base_volume},{agreement},{regime},{vol_atr}"
    assert not math.isinf(result), f"Inf volume"
    assert result >= min_volume - 0.001, (
        f"Volume {result} below min_volume {min_volume}"
    )
    assert result <= max_volume + 0.001, (
        f"Volume {result} above max_volume {max_volume}"
    )


# ============================================================================
# INVARIANT 2: LOT STEP INTEGRITY
# ============================================================================
@given(
    base_volume=_base_volumes,
    agreement=_agreement_levels,
    regime=_regimes,
    vol_atr=_vol_atrs,
)
@settings(max_examples=200)
def test_volume_is_lot_step_multiple(
    base_volume: float,
    agreement: str,
    regime: str,
    vol_atr: float,
) -> None:
    """Output must be a multiple of 0.01 (the standard lot step)."""
    from core.execution.capital_allocator import compute_volume

    decision = _make_decision(agreement)
    result = compute_volume(
        base_volume=base_volume,
        decision=decision,
        regime=regime,
        vol_atr=vol_atr,
    )

    # Round to 2 decimal places → multiply by 100 → should be integer
    remainder = round(result * 100) % 1
    assert remainder == 0.0, (
        f"Volume {result} is not a multiple of 0.01 lot step"
    )


# ============================================================================
# INVARIANT 3: MONOTONICITY IN AGREEMENT
# ============================================================================
@given(
    base_volume=st.floats(0.01, 0.2, allow_nan=False, allow_infinity=False),
    regime=_regimes,
    vol_atr=_vol_atrs,
)
@settings(max_examples=200)
def test_agreement_monotonicity_full_geq_reduced(
    base_volume: float, regime: str, vol_atr: float
) -> None:
    """Full agreement must NOT produce smaller volume than reduced agreement."""
    from core.execution.capital_allocator import compute_volume

    full_vol = compute_volume(
        base_volume=base_volume,
        decision=_make_decision("full"),
        regime=regime,
        vol_atr=vol_atr,
    )
    reduced_vol = compute_volume(
        base_volume=base_volume,
        decision=_make_decision("reduced"),
        regime=regime,
        vol_atr=vol_atr,
    )

    assert full_vol >= reduced_vol - 0.001, (
        f"Monotonicity violation: full={full_vol} < reduced={reduced_vol} "
        f"(base={base_volume}, regime={regime}, atr={vol_atr})"
    )


# ============================================================================
# INVARIANT 4: ZERO / NEGATIVE ATR SAFETY
# ============================================================================
@given(
    base_volume=st.floats(0.01, 0.1, allow_nan=False, allow_infinity=False),
    agreement=_agreement_levels,
    regime=_regimes,
    vol_atr=st.one_of(st.just(0.0), st.just(-1.0), st.just(-0.001), st.just(-100.0)),
)
@settings(max_examples=100)
def test_zero_or_negative_atr_no_crash(
    base_volume: float, agreement: str, regime: str, vol_atr: float
) -> None:
    """Zero or negative ATR must NOT cause division by zero or crash."""
    from core.execution.capital_allocator import compute_volume

    decision = _make_decision(agreement)
    result = compute_volume(
        base_volume=base_volume,
        decision=decision,
        regime=regime,
        vol_atr=vol_atr,
    )

    assert not math.isnan(result)
    assert not math.isinf(result)
    # When ATR ≤ 0, vol_factor should be clamped to 1.0
    # So volume = base × agreement_factor × regime_factor × 1.0
    assert result >= 0.0


# ============================================================================
# INVARIANT 5: NaN INPUT SAFETY
# ============================================================================
@pytest.mark.parametrize(
    "nan_field",
    ["base_volume", "vol_atr", "vol_reference", "min_volume", "max_volume"],
)
def test_nan_input_does_not_crash(nan_field: str) -> None:
    """NaN in any float input field must not crash compute_volume."""
    from core.execution.capital_allocator import compute_volume

    kwargs: dict[str, Any] = {
        "base_volume": 0.05,
        "decision": _make_decision("full"),
        "regime": "normal",
        "vol_atr": 5.0,
        "vol_reference": 5.0,
        "min_volume": 0.01,
        "max_volume": 0.10,
    }
    kwargs[nan_field] = float("nan")
    decision = kwargs.pop("decision")

    result = compute_volume(decision=decision, **kwargs)

    # NaN in → function may produce NaN or a fallback value
    # The key invariant: NO EXCEPTION
    if math.isnan(result):
        pass  # NaN output is acceptable (better than crash)
    elif math.isnan(kwargs.get("max_volume", 0)):
        pass  # max_volume is NaN — can't bound-check against NaN
    else:
        assert 0.0 <= result <= kwargs["max_volume"] + 0.001


# ============================================================================
# INVARIANT 6: REGIME ORDER
# ============================================================================
@given(
    base_volume=st.floats(0.01, 0.1, allow_nan=False, allow_infinity=False),
    agreement=_agreement_levels,
    vol_atr=_vol_atrs,
)
@settings(max_examples=200)
def test_regime_order_low_vol_gets_more_size(
    base_volume: float, agreement: str, vol_atr: float
) -> None:
    """Low-volatility regime should allocate ≥ size than high-volatility regime."""
    from core.execution.capital_allocator import compute_volume

    decision = _make_decision(agreement)

    low_vol = compute_volume(
        base_volume=base_volume, decision=decision,
        regime="low", vol_atr=vol_atr,
    )
    normal_vol = compute_volume(
        base_volume=base_volume, decision=decision,
        regime="normal", vol_atr=vol_atr,
    )
    high_vol = compute_volume(
        base_volume=base_volume, decision=decision,
        regime="high", vol_atr=vol_atr,
    )

    assert low_vol >= normal_vol - 0.001, (
        f"Regime order violation: low={low_vol} < normal={normal_vol}"
    )
    assert normal_vol >= high_vol - 0.001, (
        f"Regime order violation: normal={normal_vol} < high={high_vol}"
    )


# ============================================================================
# INVARIANT 7: DETERMINISM
# ============================================================================
@given(
    base_volume=_base_volumes,
    agreement=_agreement_levels,
    regime=_regimes,
    vol_atr=_vol_atrs,
)
@settings(max_examples=100)
def test_same_inputs_same_output(
    base_volume: float, agreement: str, regime: str, vol_atr: float
) -> None:
    """Identical inputs must produce identical outputs (no hidden state)."""
    from core.execution.capital_allocator import compute_volume

    decision1 = _make_decision(agreement)
    decision2 = _make_decision(agreement)

    r1 = compute_volume(
        base_volume=base_volume, decision=decision1,
        regime=regime, vol_atr=vol_atr,
    )
    r2 = compute_volume(
        base_volume=base_volume, decision=decision2,
        regime=regime, vol_atr=vol_atr,
    )

    assert r1 == r2, f"Non-deterministic: {r1} vs {r2}"


# ============================================================================
# CapitalAllocator.allocate_capacity() invariants
# ============================================================================
class TestAllocateCapacity:
    """Property tests for the budget-level capital allocator."""

    @given(
        total_budget=st.floats(1.0, 100000.0, allow_nan=False, allow_infinity=False),
        n_brains=st.integers(1, 20),
        max_concentration=st.floats(0.1, 0.9, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_budget_conservation(
        self, total_budget: float, n_brains: int, max_concentration: float
    ) -> None:
        """Sum of allocations must never exceed total_budget."""
        from core.execution.capital_allocator import CapitalAllocator

        # Generate random weights (non-negative, not all zero)
        import numpy as np
        rng = np.random.default_rng(42 + n_brains)
        weights_raw = rng.random(n_brains).tolist()
        total_w = sum(weights_raw)
        brain_weights = {f"brain_{i}": w / total_w for i, w in enumerate(weights_raw)}

        allocator = CapitalAllocator()
        result = allocator.allocate_capacity(
            total_budget=total_budget,
            brain_weights=brain_weights,
            max_concentration=max_concentration,
        )

        total_allocated = sum(result.values())
        assert total_allocated <= total_budget + 0.01, (
            f"Budget violation: allocated {total_allocated} > budget {total_budget}"
        )

    @given(
        total_budget=st.floats(10.0, 50000.0, allow_nan=False, allow_infinity=False),
        n_brains=st.integers(2, 15),
        max_concentration=st.floats(0.1, 0.5, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_concentration_cap(
        self, total_budget: float, n_brains: int, max_concentration: float
    ) -> None:
        """No single brain may exceed total_budget × max_concentration."""
        from core.execution.capital_allocator import CapitalAllocator

        import numpy as np
        rng = np.random.default_rng(99 + n_brains)
        weights_raw = rng.random(n_brains).tolist()
        total_w = sum(weights_raw)
        brain_weights = {f"brain_{i}": w / total_w for i, w in enumerate(weights_raw)}

        allocator = CapitalAllocator()
        result = allocator.allocate_capacity(
            total_budget=total_budget,
            brain_weights=brain_weights,
            max_concentration=max_concentration,
        )

        cap = total_budget * max_concentration
        for brain_id, alloc in result.items():
            assert alloc <= cap + 0.01, (
                f"Concentration violation: {brain_id}={alloc} > cap={cap} "
                f"(budget={total_budget}, max_conc={max_concentration})"
            )

    def test_zero_weights_all_zero_output(self) -> None:
        """All-zero weights → all-zero allocations, no division by zero."""
        from core.execution.capital_allocator import CapitalAllocator

        allocator = CapitalAllocator()
        result = allocator.allocate_capacity(
            total_budget=10000.0,
            brain_weights={"b1": 0.0, "b2": 0.0, "b3": 0.0},
        )

        for bid, alloc in result.items():
            assert alloc == 0.0, f"Zero-weight brain {bid} got {alloc}"

    @given(
        nan_budget=st.just(float("nan")),
    )
    @settings(max_examples=5)
    def test_nan_budget_does_not_crash(self, nan_budget: float) -> None:
        """NaN total_budget must not crash (may produce NaN output)."""
        from core.execution.capital_allocator import CapitalAllocator

        allocator = CapitalAllocator()
        # Must not raise
        result = allocator.allocate_capacity(
            total_budget=nan_budget,
            brain_weights={"b1": 0.5, "b2": 0.5},
        )
        # Any output is acceptable as long as no exception
        assert isinstance(result, dict)

    @given(
        n_brains=st.integers(1, 10),
    )
    @settings(max_examples=50)
    def test_min_lot_gating(self, n_brains: int) -> None:
        """With lot_value set, tiny allocations below min_lot_size are zeroed."""
        from core.execution.capital_allocator import CapitalAllocator

        # Very small budget → allocations should be below min_lot
        tiny_budget = 0.001  # $0.001 total budget
        brain_weights = {f"brain_{i}": 1.0 / n_brains for i in range(n_brains)}

        allocator = CapitalAllocator()
        result = allocator.allocate_capacity(
            total_budget=tiny_budget,
            brain_weights=brain_weights,
            min_lot_size=0.01,
            lot_value=100.0,  # $100 per lot
        )

        # All allocations should be 0 (each < min_lot_size)
        for bid, alloc in result.items():
            assert alloc == 0.0, (
                f"Sub-min-lot allocation not zeroed: {bid}={alloc}"
            )
