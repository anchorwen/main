"""Property-based tests for ActivePositionManager (Tier 1 — Capital Path).

Phase 3 — Route 1: Strangler Fig assault on position_manager.py (763 lines, 42.3%).
Three institutional invariants:
  1. Registration: idempotency + orphan detection
  2. Trail Advancement: monotonic + NaN/zero immunity
  3. MIA Eviction: atomic removal from all lookup tables
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

from core.execution.position_manager import (
    ActivePosition,
    ActivePositionManager,
    TrailPolicy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_pm() -> ActivePositionManager:
    """Create a minimal ActivePositionManager for testing."""
    return ActivePositionManager(
        trail_atr_mult=2.0,
        breakeven_threshold_atr=1.0,
        trail_activation_atr=0.3,
    )


# ============================================================================
# INVARIANT 1: Registration
# ============================================================================
@given(
    ticket=st.integers(1_000_000, 99_999_999),
    side=st.sampled_from(["long", "short"]),
    entry_price=st.floats(1000.0, 10000.0, allow_nan=False, allow_infinity=False),
    volume=st.floats(0.01, 1.0, allow_nan=False, allow_infinity=False),
    initial_sl=st.floats(500.0, 9000.0, allow_nan=False, allow_infinity=False),
    initial_tp=st.floats(1500.0, 15000.0, allow_nan=False, allow_infinity=False),
    entry_atr=st.floats(1.0, 50.0, allow_nan=False, allow_infinity=False),
    entry_cycle=st.integers(0, 10000),
)
@settings(max_examples=200)
def test_register_position_creates_tracked_position(
    ticket, side, entry_price, volume, initial_sl, initial_tp, entry_atr, entry_cycle
) -> None:
    """register_position() must store the position and make it retrievable."""
    pm = _make_pm()
    pos = pm.register_position(
        ticket=ticket, side=side, entry_price=entry_price, volume=volume,
        initial_sl=initial_sl, initial_tp=initial_tp,
        entry_atr=entry_atr, entry_cycle=entry_cycle,
    )
    assert isinstance(pos, ActivePosition)
    assert pm.has_position(ticket)
    retrieved = pm.get_position(ticket)
    assert retrieved is not None
    assert retrieved.ticket == ticket
    assert retrieved.side == side


@given(
    ticket=st.integers(1_000_000, 99_999_999),
    entry_price=st.floats(1000.0, 5000.0, allow_nan=False, allow_infinity=False),
    volume=st.floats(0.01, 1.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100)
def test_register_position_idempotent(ticket, entry_price, volume) -> None:
    """Double-registration of the same ticket must not create duplicates."""
    pm = _make_pm()

    pm.register_position(
        ticket=ticket, side="long", entry_price=entry_price, volume=volume,
        initial_sl=entry_price * 0.95, initial_tp=entry_price * 1.05,
        entry_atr=5.0, entry_cycle=0,
    )
    # Second registration — same ticket
    pm.register_position(
        ticket=ticket, side="long", entry_price=entry_price + 10.0, volume=volume,
        initial_sl=entry_price * 0.94, initial_tp=entry_price * 1.06,
        entry_atr=6.0, entry_cycle=1,
    )

    positions = pm.get_all_positions()
    assert len(positions) == 1, f"Double reg should not create duplicate, got {len(positions)}"
    # Second registration should refresh entry data
    assert positions[0].entry_price == entry_price + 10.0


@given(
    ticket=st.integers(1_000_000, 99_999_999),
    magic=st.integers(1, 99999),
)
@settings(max_examples=50)
def test_orphan_unknown_magic_handled(ticket, magic) -> None:
    """Registration with unknown magic number should not crash the system.

    ActivePositionManager does NOT have a magic registry (that's in strategy_line).
    It accepts any ticket regardless of magic — the magic validation happens
    upstream. This test verifies the manager doesn't crash on unknown contexts.
    """
    pm = _make_pm()
    # Register with any magic — manager should accept it
    pos = pm.register_position(
        ticket=ticket, side="long", entry_price=2000.0, volume=0.01,
        initial_sl=1900.0, initial_tp=2100.0,
        entry_atr=5.0, entry_cycle=0,
    )
    assert pos is not None
    assert pm.has_position(ticket)


# ============================================================================
# INVARIANT 2: Trail Advancement (Monotonic + NaN Immunity)
# ============================================================================
@given(
    initial_price=st.floats(1500.0, 3000.0, allow_nan=False, allow_infinity=False),
    atr=st.floats(1.0, 30.0, allow_nan=False, allow_infinity=False),
    n_steps=st.integers(5, 50),
    trend_strength=st.floats(0.1, 5.0, allow_nan=False),
)
@settings(max_examples=100)
def test_trail_monotonic_never_widens(
    initial_price, atr, n_steps, trend_strength
) -> None:
    """For a LONG position in uptrend, current_sl must never decrease.

    Trail can stay flat (not enough movement) or move up — but NEVER down.
    """
    pm = ActivePositionManager(trail_atr_mult=2.0, trail_activation_atr=0.3)
    ticket = 1_000_001
    sl = initial_price - atr * 2.0
    tp = initial_price + atr * 1.5

    pm.register_position(
        ticket=ticket, side="long", entry_price=initial_price, volume=0.01,
        initial_sl=sl, initial_tp=tp, entry_atr=atr, entry_cycle=0,
    )

    prev_sl = sl
    rng = np.random.default_rng(42)
    price = initial_price
    for i in range(n_steps):
        price += rng.normal(trend_strength * 0.5, atr * 0.3)
        high = price + atr * 0.3
        low = price - atr * 0.2
        current_atr_val = float(atr + rng.normal(0, atr * 0.05))

        pm.update_prices(
            mid=price, bid=price - 0.5, ask=price + 0.5,
            current_atr=current_atr_val, cycle_count=i + 1, ticket=ticket,
            m5_high=high, m5_low=low, m5_spread_points=30,
        )
        pos = pm.get_position(ticket)
        if pos:
            # Trail must never move backwards (widen) for LONG
            assert pos.current_sl >= prev_sl - 1e-9, (
                f"[{i}] SL widened: {prev_sl:.2f} → {pos.current_sl:.2f} "
                f"(price={price:.2f}, trail_mult={pos.trail_multiplier})"
            )
            prev_sl = pos.current_sl


def test_nan_atr_does_not_move_trail() -> None:
    """NaN ATR must not update the trail stop."""
    pm = ActivePositionManager(trail_atr_mult=2.0, trail_activation_atr=0.3)
    ticket = 1_000_002
    entry = 2000.0
    pm.register_position(
        ticket=ticket, side="long", entry_price=entry, volume=0.01,
        initial_sl=entry - 20.0, initial_tp=entry + 30.0,
        entry_atr=10.0, entry_cycle=0,
    )
    pos = pm.get_position(ticket)
    assert pos is not None
    original_sl = pos.current_sl

    # Inject NaN ATR
    pm.update_prices(
        mid=2005.0, bid=2005.0, ask=2005.5,
        current_atr=float("nan"), cycle_count=1, ticket=ticket,
        m5_spread_points=30,
    )

    # Trail should NOT have moved
    pos = pm.get_position(ticket)
    assert pos is not None
    assert pos.current_sl == original_sl, (
        f"NaN ATR should not move SL: {original_sl} → {pos.current_sl}"
    )


def test_zero_price_does_not_crash_trail() -> None:
    """Flash crash to zero price must not crash update_prices."""
    pm = _make_pm()
    ticket = 1_000_003
    entry = 2000.0
    pm.register_position(
        ticket=ticket, side="long", entry_price=entry, volume=0.01,
        initial_sl=entry - 30.0, initial_tp=entry + 30.0,
        entry_atr=10.0, entry_cycle=0,
    )

    # Simulate flash crash: price → 0.05
    pm.update_prices(
        mid=0.05, bid=0.04, ask=0.06,
        current_atr=500.0, cycle_count=1, ticket=ticket,
        m5_high=0.06, m5_low=0.01, m5_spread_points=30,
    )

    # Should not crash — position still exists
    assert pm.has_position(ticket)


# ============================================================================
# INVARIANT 3: MIA Eviction (Atomic Removal)
# ============================================================================
@given(
    n_positions=st.integers(1, 20),
    ticket_base=st.integers(1_000_000, 90_000_000),
)
@settings(max_examples=50)
def test_clear_position_is_atomic(n_positions, ticket_base) -> None:
    """clear_position(ticket) must remove from ALL internal dicts atomically."""
    pm = _make_pm()
    tickets = list(range(ticket_base, ticket_base + n_positions))

    for i, t in enumerate(tickets):
        pm.register_position(
            ticket=t, side="long" if i % 2 == 0 else "short",
            entry_price=2000.0 + i * 10, volume=0.01,
            initial_sl=1900.0 + i * 10, initial_tp=2100.0 + i * 10,
            entry_atr=5.0, entry_cycle=0,
        )
        pm.mark_pending_close(t, 100)

    # Pick middle ticket to clear
    target = tickets[n_positions // 2]
    pm.clear_position(target)

    # Verify atomic removal
    assert not pm.has_position(target), "has_position must return False after clear"
    assert pm.get_position(target) is None, "get_position must return None after clear"
    assert not pm.is_pending_close(target, 200), "pending_close must be cleared"

    # Verify OTHER tickets are untouched
    for t in tickets:
        if t != target:
            assert pm.has_position(t), f"Ticket {t} was incorrectly cleared"
    assert len(pm.get_all_positions()) == n_positions - 1


@given(n_positions=st.integers(2, 15))
@settings(max_examples=50)
def test_clear_all_positions_atomic(n_positions) -> None:
    """clear_position() with no ticket must remove ALL positions atomically."""
    pm = _make_pm()

    for i in range(n_positions):
        pm.register_position(
            ticket=1_000_000 + i, side="long", entry_price=2000.0, volume=0.01,
            initial_sl=1900.0, initial_tp=2100.0, entry_atr=5.0, entry_cycle=0,
        )

    pm.clear_position()  # clear all

    assert pm.get_all_positions() == []
    assert pm.get_position(1_000_000) is None
    # primary_ticket must be reset
    assert pm._primary_ticket is None


def test_pending_close_flood_guard() -> None:
    """After FLOOD_THRESHOLD close attempts, is_pending_close should be permanent."""
    pm = _make_pm()
    ticket = 1_000_004
    pm.register_position(
        ticket=ticket, side="long", entry_price=2000.0, volume=0.01,
        initial_sl=1900.0, initial_tp=2100.0, entry_atr=5.0, entry_cycle=0,
    )

    # Mark close 3 times (hits threshold)
    for i in range(pm.PENDING_CLOSE_FLOOD_THRESHOLD):
        pm.mark_pending_close(ticket, 100 + i)

    # After flood threshold, the lock should be permanent
    # is_pending_close returns True even after expiry
    far_future = 100 + pm.PENDING_CLOSE_MAX_CYCLES + 50
    still_pending = pm.is_pending_close(ticket, far_future)
    # Should remain locked (flood guard triggered)
    assert still_pending, "Flood guard should permanently lock after threshold"


# ============================================================================
# Edge cases
# ============================================================================
def test_get_position_nonexistent_returns_none() -> None:
    """get_position for unknown ticket must return None, not crash."""
    pm = _make_pm()
    assert pm.get_position(999999) is None
    assert not pm.has_position(999999)


def test_clear_nonexistent_position_noop() -> None:
    """clear_position for unknown ticket must not crash."""
    pm = _make_pm()
    pm.clear_position(999999)  # must not raise


@given(
    ticket=st.integers(1, 999999),
    volume=st.floats(0.001, 0.01),
    atr=st.floats(0.1, 0.5),
)
@settings(max_examples=50)
def test_register_zero_atr_position(ticket, volume, atr) -> None:
    """Position with very low ATR must still register successfully."""
    pm = _make_pm()
    pos = pm.register_position(
        ticket=ticket, side="long", entry_price=2000.0, volume=volume,
        initial_sl=1999.0, initial_tp=2001.0,
        entry_atr=atr, entry_cycle=0,
    )
    assert pos is not None
    assert pos.entry_atr == atr
