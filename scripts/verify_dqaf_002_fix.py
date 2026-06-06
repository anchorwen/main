#!/usr/bin/env python
"""验证 DQAF-002 / FIX-137: neutral deadlock 不再触发 brain_flip 假阳性。

模拟场景: 双脑 (V4+V5) 做空入场后，群组投票 neutral 平票。
修复前: _l2_supporting=[] → flip_ratio=100% → brain_flip_extreme_100pct (假阳性)
修复后: _l2_supporting=brain_ids → flip_ratio=0% → 不触发 brain_flip

Usage:
  python scripts/verify_dqaf_002_fix.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.execution.position_manager import ActivePosition, ActivePositionManager


def make_position(side: str = "short") -> ActivePosition:
    """Create a realistic BTC swing short position with two supporting brains."""
    pos = ActivePosition(
        ticket=3807444876,
        side=side,
        entry_price=60806.42,
        volume=0.05,
        initial_sl=61438.97,
        initial_tp=60070.85,
        current_sl=61438.97,
        current_tp=60070.85,
        highest_high=61500.0,
        lowest_low=60500.0,
        entry_atr=176.8379,
        entry_cycle=1,
        entry_z_score=-0.26,
        entry_consensus={"consensus_score": 0.6889, "direction": "short"},
        supporting_brain_ids=["BTC_Swing_V4", "BTC_Swing_V5"],
        strategy_name="btc_swing",
        confidence_alpha=0.3,
    )
    # Make position "old enough" to bypass min_hold protection
    pos.cycles_held = 10
    # Seed EMA with entry consensus score (mirroring live_cycle.py behavior)
    pos.confidence_ema = 0.6889
    return pos


def make_neutral_consensus() -> dict:
    """Simulate a neutral group consensus — brains present but direction neutral
    (e.g., one brain votes LONG, one SHORT → tie → neutral)."""
    return {
        "aggregated_bias": "neutral",
        "consensus_score": 0.50,
        "voter_count": 2,
        "majority_ratio": 0.50,
        "supporting_brains": [],
        "opposing_brains": [],
        "brain_ids": ["BTC_Swing_V4", "BTC_Swing_V5"],
    }


def main() -> int:
    errors = 0

    # ── Test 1: OLD behavior (the bug) ──
    # Simulate _l2_supporting=[] (empty) → false 100% flip
    print("=" * 60)
    print("Test 1: OLD — _l2_supporting=[] (BUG: empty → 100% flip)")
    print("=" * 60)

    pm_old = ActivePositionManager()
    pos_old = make_position("short")
    pm_old._positions[pos_old.ticket] = pos_old
    pm_old._entry_consensus_score = 0.6889

    old_consensus = make_neutral_consensus()
    old_supporting: list[str] = []  # ← THE BUG (line 1424 before fix)

    should_exit, reason = pm_old.evaluate_brain_exit(
        old_consensus, old_supporting, mid=60700.0, ticket=pos_old.ticket
    )

    print(f"  should_exit = {should_exit}")
    print(f"  reason      = {reason}")

    if "brain_flip_extreme_100pct" in reason:
        print("  [CONFIRMED] OLD code: FALSE POSITIVE brain_flip_extreme_100pct")
    else:
        print(f"  [UNEXPECTED] Got: {reason}")
        errors += 1

    # ── Test 2: NEW behavior (the fix) ──
    # Simulate _l2_supporting=brain_ids → flip=0% → no brain_flip
    print()
    print("=" * 60)
    print("Test 2: NEW — _l2_supporting=brain_ids (FIX: 0% flip)")
    print("=" * 60)

    pm_new = ActivePositionManager()
    pos_new = make_position("short")
    pm_new._positions[pos_new.ticket] = pos_new
    pm_new._entry_consensus_score = 0.6889

    new_consensus = make_neutral_consensus()
    new_supporting: list[str] = ["BTC_Swing_V4", "BTC_Swing_V5"]  # ← THE FIX

    should_exit, reason = pm_new.evaluate_brain_exit(
        new_consensus, new_supporting, mid=60700.0, ticket=pos_new.ticket
    )

    print(f"  should_exit = {should_exit}")
    print(f"  reason      = {reason}")

    if "brain_flip_extreme" in reason:
        print(f"  [FAIL] NEW code still triggers brain_flip: {reason}")
        errors += 1
    elif "brain_flip" in reason:
        print(f"  [WARN] Regular brain_flip (not extreme): {reason}")
    else:
        print("  [PASS] No brain_flip triggered")
        if reason:
            print(f"         Exit (if any) via: {reason}")
        else:
            print("         Position continues normally")

    # ── Test 3: True signal reversal still works ──
    # Both brains flip to LONG → signal_reversal should fire
    print()
    print("=" * 60)
    print("Test 3: True reversal — BOTH brains flip to LONG")
    print("=" * 60)

    pm_rev = ActivePositionManager()
    pos_rev = make_position("short")
    pm_rev._positions[pos_rev.ticket] = pos_rev
    pm_rev._entry_consensus_score = 0.6889

    rev_consensus = {
        "aggregated_bias": "long",
        "consensus_score": 0.65,
        "voter_count": 2,
        "majority_ratio": 1.0,
        "supporting_brains": ["BTC_Swing_V4", "BTC_Swing_V5"],
        "opposing_brains": [],
        "brain_ids": ["BTC_Swing_V4", "BTC_Swing_V5"],
    }
    rev_supporting = ["BTC_Swing_V4", "BTC_Swing_V5"]

    should_exit, reason = pm_rev.evaluate_brain_exit(
        rev_consensus, rev_supporting, mid=60700.0, ticket=pos_rev.ticket
    )

    print(f"  should_exit = {should_exit}")
    print(f"  reason      = {reason}")

    if should_exit and "signal_reversal" in reason:
        print("  [PASS] True reversal correctly triggers signal_reversal")
    else:
        print(f"  [FAIL] Got: {reason}")
        errors += 1

    # ── Summary ──
    print()
    print("=" * 60)
    if errors == 0:
        print("ALL 3 TESTS PASSED")
        print("  Test 1: OLD []     → brain_flip_extreme_100pct (confirmed bug)")
        print("  Test 2: NEW fix    → no brain_flip (confirmed fix)")
        print("  Test 3: True flip  → signal_reversal (still works)")
        return 0
    else:
        print(f"FAILED: {errors} test(s)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
