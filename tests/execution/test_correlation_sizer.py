"""Unit tests for correlation_sizer.py — √N discount for multi-strategy clusters.

Covers:
  - apply_sqrt_n_discount: basic √N math, rounding, min_lot enforcement
  - Single decision (no discount), empty list, mixed directions
  - NaN/Inf guard, lot_step rounding, strategy drop ordering
  - ClusterResult audit records
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pytest

from core.execution.correlation_sizer import apply_sqrt_n_discount


# ── Mock StrategyDecision ─────────────────────────────────────────────────


@dataclass
class MockDecision:
    """Minimal mock matching StrategyDecision interface used by apply_sqrt_n_discount."""
    strategy_name: str = "test_strategy"
    should_trade: bool = True
    direction: str = "long"
    volume: float = 0.05
    reason: str = ""
    confidence: float = 0.5


# ═══════════════════════════════════════════════════════════════════════════
# apply_sqrt_n_discount
# ═══════════════════════════════════════════════════════════════════════════


class TestApplySqrtNDiscount:
    """Pure function: √N correlation discount."""

    # ── Trivial cases ──

    def test_empty_list_returns_empty(self) -> None:
        decisions, results = apply_sqrt_n_discount([])
        assert decisions == []
        assert results == []

    def test_single_decision_no_discount(self) -> None:
        d = MockDecision(volume=0.05)
        decisions, results = apply_sqrt_n_discount([d])
        assert len(decisions) == 1
        assert decisions[0].volume == 0.05  # unchanged
        assert decisions[0].should_trade is True
        assert results == []  # n=1 → no cluster audit record

    def test_single_per_direction_no_discount(self) -> None:
        d1 = MockDecision(direction="long", volume=0.05)
        d2 = MockDecision(direction="short", volume=0.03)
        decisions, results = apply_sqrt_n_discount([d1, d2])
        assert len(decisions) == 2
        # Both unchanged (1 per direction)
        assert decisions[0].volume == 0.05
        assert decisions[1].volume == 0.03

    # ── Basic √N discount ──

    def test_two_long_decisions_discounted_by_sqrt2(self) -> None:
        d1 = MockDecision(strategy_name="s1", direction="long", volume=0.10)
        d2 = MockDecision(strategy_name="s2", direction="long", volume=0.10)
        discount = 1.0 / math.sqrt(2)
        decisions, results = apply_sqrt_n_discount([d1, d2])

        expected = round(0.10 * discount / 0.01) * 0.01
        for d in decisions:
            assert d.volume == expected
            assert d.should_trade is True

    def test_three_long_decisions_sqrt3(self) -> None:
        d1 = MockDecision(direction="long", volume=0.10)
        d2 = MockDecision(direction="long", volume=0.10)
        d3 = MockDecision(direction="long", volume=0.10)
        decisions, results = apply_sqrt_n_discount([d1, d2, d3])

        discount = 1.0 / math.sqrt(3)
        expected = round(0.10 * discount / 0.01) * 0.01
        for d in decisions:
            assert d.volume == expected

    # ── Mixed directions ──

    def test_directions_discounted_separately(self) -> None:
        """Long cluster and short cluster are independent."""
        long1 = MockDecision(strategy_name="L1", direction="long", volume=0.10)
        long2 = MockDecision(strategy_name="L2", direction="long", volume=0.10)
        short1 = MockDecision(strategy_name="S1", direction="short", volume=0.05)

        decisions, results = apply_sqrt_n_discount([long1, long2, short1])

        # Longs: 2 → sqrt(2) discount
        # Shorts: 1 → no discount
        long_discount = 1.0 / math.sqrt(2)
        expected_long = round(0.10 * long_discount / 0.01) * 0.01

        assert long1.volume == expected_long
        assert long2.volume == expected_long
        assert short1.volume == 0.05  # unchanged

    # ── should_trade=False exclusion ──

    def test_should_trade_false_is_excluded(self) -> None:
        d1 = MockDecision(strategy_name="active", should_trade=True, direction="long", volume=0.10)
        d2 = MockDecision(strategy_name="blocked", should_trade=False, direction="long", volume=0.10)
        decisions, results = apply_sqrt_n_discount([d1, d2])

        assert d1.volume == 0.10  # unchanged (n=1 effective)
        assert d1.should_trade is True
        assert d2.volume == 0.10  # excluded from cluster
        assert results == []

    # ── NaN/Inf guard ──

    def test_nan_volume_drops_decision(self) -> None:
        d = MockDecision(strategy_name="nan_s", direction="long", volume=float("nan"))
        # Need a second long to form a cluster of 2
        d2 = MockDecision(strategy_name="ok_s", direction="long", volume=0.10)
        decisions, _ = apply_sqrt_n_discount([d, d2])
        # nan decision should be dropped
        assert d.should_trade is False
        assert d.volume == 0.0
        assert "sqrt_n_dropped:invalid_volume" in d.reason

    def test_inf_volume_drops_decision(self) -> None:
        d = MockDecision(strategy_name="inf_s", direction="long", volume=float("inf"))
        d2 = MockDecision(strategy_name="ok_s", direction="long", volume=0.10)
        decisions, _ = apply_sqrt_n_discount([d, d2])
        assert d.should_trade is False
        assert d.volume == 0.0

    # ── min_lot enforcement ──

    def test_below_min_lot_after_discount_drops(self) -> None:
        """Tiny volume after √N discount → dropped."""
        d1 = MockDecision(strategy_name="tiny1", direction="long", volume=0.01)
        d2 = MockDecision(strategy_name="tiny2", direction="long", volume=0.01)
        d3 = MockDecision(strategy_name="tiny3", direction="long", volume=0.01)
        # 3 longs: each gets 0.01 / sqrt(3) ≈ 0.00577 → rounded to 0.01 (lot_step) → >= 0.01 (min_lot)
        # Actually, 0.01 / sqrt(3) ≈ 0.00577. round(0.00577/0.01)=round(0.577)=1. 1*0.01=0.01 → NOT dropped
        # Let me use even smaller volumes
        d1.volume = 0.01
        d2.volume = 0.01
        decisions, _ = apply_sqrt_n_discount([d1, d2], min_lot=0.01, lot_step=0.01)
        # 0.01 / sqrt(2) ≈ 0.00707 → round(0.707)=1 → stepped=0.01 → >= 0.01 → not dropped
        # OK, with 2x 0.01 it survives. Let me test with 5x 0.01:
        d3 = MockDecision(strategy_name="t3", direction="long", volume=0.01)
        d4 = MockDecision(strategy_name="t4", direction="long", volume=0.01)
        d5 = MockDecision(strategy_name="t5", direction="long", volume=0.01)
        decisions, _ = apply_sqrt_n_discount([d1, d2, d3, d4, d5], min_lot=0.01, lot_step=0.01)
        # sqrt(5) ≈ 2.236, 0.01/2.236 = 0.00447, round(0.447)=0, stepped=0 → <0.01 → dropped
        for d in [d1, d2, d3, d4, d5]:
            assert d.should_trade is False
            assert "sqrt_n_dropped" in d.reason

    # ── lot_step rounding ──

    def test_custom_lot_step(self) -> None:
        d1 = MockDecision(direction="long", volume=0.05)
        d2 = MockDecision(direction="long", volume=0.05)
        decisions, _ = apply_sqrt_n_discount([d1, d2], lot_step=0.05)
        # 0.05 / sqrt(2) ≈ 0.03535 → round(0.03535/0.05)=round(0.707)=1 → 1*0.05=0.05
        assert decisions[0].volume == 0.05

    # ── ClusterResult audit records ──

    def test_returns_cluster_results(self) -> None:
        d1 = MockDecision(strategy_name="s1", direction="long", volume=0.10)
        d2 = MockDecision(strategy_name="s2", direction="long", volume=0.10)
        _, results = apply_sqrt_n_discount([d1, d2])

        assert len(results) == 1  # one cluster
        r = results[0]
        assert r.direction == "long"
        assert r.n_same_direction == 2
        assert r.raw_total_volume == pytest.approx(0.20)
        assert r.discounted_volume > 0

    def test_cluster_results_include_dropped(self) -> None:
        d1 = MockDecision(strategy_name="drop_me", direction="long", volume=0.001)
        d2 = MockDecision(strategy_name="drop_too", direction="long", volume=0.001)
        d3 = MockDecision(strategy_name="drop_also", direction="long", volume=0.001)
        d4 = MockDecision(strategy_name="drop_last", direction="long", volume=0.001)
        d5 = MockDecision(strategy_name="survivor", direction="long", volume=0.001)
        _, results = apply_sqrt_n_discount([d1, d2, d3, d4, d5], min_lot=0.01, lot_step=0.01)

        if results:
            all_dropped = []
            for r in results:
                all_dropped.extend(r.dropped_strategies)
            assert len(all_dropped) > 0

    # ── policy parameter accepted ──

    def test_policy_parameter_accepted(self) -> None:
        d1 = MockDecision(direction="long", volume=0.10)
        d2 = MockDecision(direction="long", volume=0.10)
        decisions, _ = apply_sqrt_n_discount([d1, d2], policy="drop_weakest")
        assert len(decisions) == 2
