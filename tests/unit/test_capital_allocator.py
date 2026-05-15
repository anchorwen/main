"""Tests for capital_allocator.py — conflict resolution and position sizing."""

import pytest

from core.execution.capital_allocator import (
    AllocationDecision,
    _count_directions,
    compute_volume,
    resolve_conflicts,
)
from core.parliament.contract_groups import GroupSignal

# ── Helpers ──


def _gs(name="test_group", direction="long", confidence=0.8, supporting=3, total=3):
    """Quick GroupSignal factory."""
    return GroupSignal(
        group_name=name,
        direction=direction,
        confidence=confidence,
        consensus_score=confidence,
        supporting_count=supporting,
        opposing_count=total - supporting,
        neutral_count=0,
        total_count=total,
        horizon_cycles=12,
    )


# ── AllocationDecision dataclass ──


def test_allocation_decision_defaults():
    d = AllocationDecision(
        should_trade=False,
        direction="neutral",
        confidence=0.0,
        volume=0.0,
        agreement_level="none",
    )
    assert d.active_groups == []
    assert d.dissenting_groups == []
    assert d.reason == ""


# ── _count_directions ──


def test_count_directions_all_long():
    signals = {
        "g1": _gs("g1", "long"),
        "g2": _gs("g2", "long"),
        "g3": _gs("g3", "long"),
    }
    result = _count_directions(signals)
    assert result["long"] == ["g1", "g2", "g3"]
    assert result["short"] == []
    assert result["neutral"] == []


def test_count_directions_mixed():
    signals = {
        "g1": _gs("g1", "long"),
        "g2": _gs("g2", "short"),
        "g3": _gs("g3", "neutral"),
    }
    result = _count_directions(signals)
    assert result["long"] == ["g1"]
    assert result["short"] == ["g2"]
    assert result["neutral"] == ["g3"]


def test_count_directions_skips_none():
    signals = {
        "g1": _gs("g1", "long"),
        "g2": None,
    }
    result = _count_directions(signals)
    assert result["long"] == ["g1"]
    assert result["short"] == []
    # g2 is skipped entirely, not counted as neutral


def test_count_directions_unknown_direction():
    """Directions not in 'long'/'short' go to neutral bucket."""

    class WeirdSignal:
        direction = "sideways"

    signals = {"g1": WeirdSignal()}
    result = _count_directions(signals)
    assert result["long"] == []
    assert result["short"] == []
    assert result["neutral"] == ["g1"]


def test_count_directions_empty():
    assert _count_directions({}) == {"long": [], "short": [], "neutral": []}


# ── resolve_conflicts ──


def test_resolve_empty_signals():
    result = resolve_conflicts({})
    assert result.should_trade is False
    assert result.reason == "no_active_groups"


def test_resolve_all_three_agree_long():
    signals = {
        "barrier": _gs("barrier", "long", confidence=0.85, supporting=3, total=3),
        "micro": _gs("micro", "long", confidence=0.72, supporting=2, total=2),
        "arb": _gs("arb", "long", confidence=0.68, supporting=1, total=1),
    }
    result = resolve_conflicts(signals)
    assert result.should_trade is True
    assert result.direction == "long"
    assert result.agreement_level == "full"
    assert result.confidence == pytest.approx(0.75, abs=0.05)  # (0.85+0.72+0.68)/3 * 1.0
    assert set(result.active_groups) == {"barrier", "micro", "arb"}


def test_resolve_all_three_agree_short():
    signals = {
        "barrier": _gs("barrier", "short", confidence=0.80),
        "micro": _gs("micro", "short", confidence=0.75),
        "arb": _gs("arb", "short", confidence=0.70),
    }
    result = resolve_conflicts(signals)
    assert result.should_trade is True
    assert result.direction == "short"
    assert result.agreement_level == "full"


def test_resolve_two_agree_one_neutral():
    signals = {
        "barrier": _gs("barrier", "long", confidence=0.80),
        "micro": _gs("micro", "long", confidence=0.70),
        "arb": _gs("arb", "neutral", confidence=0.50),
    }
    result = resolve_conflicts(signals)
    assert result.should_trade is True
    assert result.direction == "long"
    assert result.agreement_level == "reduced"
    # confidence = avg(0.80, 0.70) * 0.85 = 0.75 * 0.85 = 0.6375
    assert result.confidence == pytest.approx(0.6375, abs=0.01)


def test_resolve_two_agree_one_absent():
    """2 groups agree, 1 is None (not present)."""
    signals = {
        "barrier": _gs("barrier", "long", confidence=0.80),
        "micro": _gs("micro", "long", confidence=0.70),
        "arb": None,
    }
    result = resolve_conflicts(signals)
    assert result.should_trade is True
    assert result.direction == "long"
    assert result.agreement_level == "reduced"
    # All present (2) agree, total_present=2 → conf_mult=0.85
    assert result.confidence == pytest.approx(0.6375, abs=0.01)


def test_resolve_two_agree_one_oppose_no_trade():
    """Long vs short conflict → NO TRADE."""
    signals = {
        "barrier": _gs("barrier", "long", confidence=0.80),
        "micro": _gs("micro", "long", confidence=0.70),
        "arb": _gs("arb", "short", confidence=0.60),
    }
    result = resolve_conflicts(signals)
    assert result.should_trade is False
    assert "cross_group_conflict" in result.reason


def test_resolve_one_agree_rest_neutral():
    signals = {
        "barrier": _gs("barrier", "long", confidence=0.75),
        "micro": _gs("micro", "neutral", confidence=0.50),
        "arb": _gs("arb", "neutral", confidence=0.50),
    }
    result = resolve_conflicts(signals)
    assert result.should_trade is True
    assert result.direction == "long"
    assert result.agreement_level == "reduced"
    # 1 agrees, 2 neutral → n_neutral >= 2 → conf_mult = 0.65
    # confidence = 0.75 * 0.65 = 0.4875
    assert result.confidence == pytest.approx(0.4875, abs=0.01)


def test_resolve_one_agree_alone_no_neutrals():
    """Only 1 group present, no neutrals."""
    signals = {
        "barrier": _gs("barrier", "long", confidence=0.80, supporting=1, total=1),
    }
    result = resolve_conflicts(signals)
    assert result.should_trade is True
    assert result.direction == "long"
    # total_present=1, n_long+n_short=1, not 2+1neutral case, not >=3
    # falls into: n_neutral >= 2 OR n_long+n_short == 1 → conf_mult=0.65
    assert result.agreement_level == "reduced"
    assert result.confidence == pytest.approx(0.80 * 0.65, abs=0.01)


def test_resolve_all_neutral():
    signals = {
        "barrier": _gs("barrier", "neutral"),
        "micro": _gs("micro", "neutral"),
        "arb": _gs("arb", "neutral"),
    }
    result = resolve_conflicts(signals)
    assert result.should_trade is False
    assert result.reason == "all_groups_neutral"


def test_resolve_require_unanimous_blocks_neutrals():
    signals = {
        "barrier": _gs("barrier", "long", confidence=0.80),
        "micro": _gs("micro", "long", confidence=0.70),
        "arb": _gs("arb", "neutral", confidence=0.50),
    }
    result = resolve_conflicts(signals, require_unanimous=True)
    assert result.should_trade is False
    assert "require_unanimous" in result.reason


def test_resolve_require_unanimous_all_agree():
    signals = {
        "barrier": _gs("barrier", "long", confidence=0.80),
        "micro": _gs("micro", "long", confidence=0.70),
        "arb": _gs("arb", "long", confidence=0.60),
    }
    result = resolve_conflicts(signals, require_unanimous=True)
    assert result.should_trade is True
    assert result.agreement_level == "full"


def test_resolve_direction_derived_from_supporting():
    """When only shorts exist, direction is short."""
    signals = {
        "barrier": _gs("barrier", "short", confidence=0.75),
    }
    result = resolve_conflicts(signals)
    assert result.direction == "short"
    assert result.should_trade is True


def test_resolve_two_long_one_short_no_trade():
    """Even 2 vs 1 conflict → no trade (conservative)."""
    signals = {
        "barrier": _gs("barrier", "long", confidence=0.80),
        "micro": _gs("micro", "long", confidence=0.70),
        "arb": _gs("arb", "short", confidence=0.60),
    }
    result = resolve_conflicts(signals)
    assert result.should_trade is False


def test_resolve_dissenting_groups_reported():
    signals = {
        "barrier": _gs("barrier", "long"),
        "micro": _gs("micro", "short"),
    }
    result = resolve_conflicts(signals)
    assert result.should_trade is False
    assert "barrier" in result.dissenting_groups or "micro" in result.dissenting_groups


# ── compute_volume ──


def test_compute_volume_basic():
    decision = AllocationDecision(
        should_trade=True,
        direction="long",
        confidence=0.75,
        volume=0.0,
        agreement_level="full",
    )
    vol = compute_volume(0.05, decision)
    # full=1.0, normal=1.0, vol_factor=1.0 → 0.05
    assert vol == 0.05


def test_compute_volume_reduced_agreement():
    decision = AllocationDecision(
        should_trade=True,
        direction="long",
        confidence=0.60,
        volume=0.0,
        agreement_level="reduced",
    )
    vol = compute_volume(0.10, decision)
    # reduced=0.70, normal=1.0, vol_factor=1.0 → 0.07
    assert vol == pytest.approx(0.07, abs=0.001)


def test_compute_volume_minimal_agreement():
    decision = AllocationDecision(
        should_trade=True,
        direction="long",
        confidence=0.50,
        volume=0.0,
        agreement_level="minimal",
    )
    vol = compute_volume(0.10, decision)
    # minimal=0.45, normal=1.0 → 0.045, float gives 0.04500...001 → round(., 2) → 0.05
    assert vol == pytest.approx(0.05, abs=0.01)


def test_compute_volume_high_regime():
    decision = AllocationDecision(
        should_trade=True,
        direction="long",
        confidence=0.75,
        volume=0.0,
        agreement_level="full",
    )
    vol = compute_volume(0.10, decision, regime="high")
    # full=1.0, high=0.70 → 0.07
    assert vol == pytest.approx(0.07, abs=0.001)


def test_compute_volume_low_regime():
    decision = AllocationDecision(
        should_trade=True,
        direction="long",
        confidence=0.75,
        volume=0.0,
        agreement_level="full",
    )
    vol = compute_volume(0.05, decision, regime="low")
    # full=1.0, low=1.20 → 0.06
    assert vol == pytest.approx(0.06, abs=0.001)


def test_compute_volume_atr_low_boosts():
    """Low ATR (quiet market) → vol_factor > 1.0 → larger position."""
    decision = AllocationDecision(
        should_trade=True,
        direction="long",
        confidence=0.75,
        volume=0.0,
        agreement_level="full",
    )
    # vol_atr=2.5, vol_reference=5.0 → vol_factor = min(1.5, max(0.5, 5.0/2.5)) = 1.5
    # 0.05 * 1.0 * 1.0 * 1.5 = 0.075, round to 2 decimals → 0.07
    vol = compute_volume(0.05, decision, vol_atr=2.5)
    assert vol == pytest.approx(0.07, abs=0.01)


def test_compute_volume_atr_high_reduces():
    """High ATR (volatile market) → vol_factor < 1.0 → smaller position."""
    decision = AllocationDecision(
        should_trade=True,
        direction="long",
        confidence=0.75,
        volume=0.0,
        agreement_level="full",
    )
    # vol_atr=10.0, vol_reference=5.0 → vol_factor = max(0.5, 5.0/10.0) = 0.5
    # 0.05 * 1.0 * 1.0 * 0.5 = 0.025, round to 2 decimals → 0.03
    vol = compute_volume(0.05, decision, vol_atr=10.0)
    assert vol == pytest.approx(0.03, abs=0.01)


def test_compute_volume_atr_zero_falls_back():
    decision = AllocationDecision(
        should_trade=True,
        direction="long",
        confidence=0.75,
        volume=0.0,
        agreement_level="full",
    )
    vol = compute_volume(0.05, decision, vol_atr=0.0)
    # vol_factor=1.0 → 0.05
    assert vol == 0.05


def test_compute_volume_clamped_to_min():
    decision = AllocationDecision(
        should_trade=True,
        direction="long",
        confidence=0.30,
        volume=0.0,
        agreement_level="minimal",
    )
    vol = compute_volume(0.01, decision, regime="high", vol_atr=20.0, min_volume=0.01)
    # Everything pushes downward, but clamped to 0.01
    assert vol >= 0.01


def test_compute_volume_clamped_to_max():
    decision = AllocationDecision(
        should_trade=True,
        direction="long",
        confidence=0.95,
        volume=0.0,
        agreement_level="full",
    )
    vol = compute_volume(0.20, decision, regime="low", vol_atr=1.0, max_volume=0.10)
    assert vol == 0.10


def test_compute_volume_combined_factors():
    """All three factors together."""
    decision = AllocationDecision(
        should_trade=True,
        direction="long",
        confidence=0.75,
        volume=0.0,
        agreement_level="reduced",
    )
    vol = compute_volume(0.05, decision, regime="high", vol_atr=10.0)
    # reduced=0.70, high=0.70, vol_factor=0.5 → 0.05 * 0.70 * 0.70 * 0.5 = 0.01225 → round(., 2) → 0.01
    assert vol == pytest.approx(0.01, abs=0.01)


def test_compute_volume_returns_rounded():
    decision = AllocationDecision(
        should_trade=True,
        direction="long",
        confidence=0.75,
        volume=0.0,
        agreement_level="reduced",
    )
    vol = compute_volume(0.07, decision, regime="high", vol_atr=10.0)
    # Should be rounded to 3 decimal places
    assert vol == round(vol, 3)
    assert isinstance(vol, float)


# ── GroupCorrelationTracker ──

from core.execution.capital_allocator import GroupCorrelationTracker


def _gs_corr(name="test_group", direction="long", confidence=0.8, supporting=3, total=3):
    return GroupSignal(
        group_name=name,
        direction=direction,
        confidence=confidence,
        consensus_score=confidence,
        supporting_count=supporting,
        opposing_count=total - supporting,
        neutral_count=0,
        total_count=total,
        horizon_cycles=12,
    )


def test_correlation_tracker_initial_penalty_is_neutral():
    tracker = GroupCorrelationTracker(ema_alpha=0.05)
    signals = {
        "barrier_12bar": _gs_corr("barrier_12bar", "long"),
        "micro_3bar": _gs_corr("micro_3bar", "long"),
        "statarb_dynamic": _gs_corr("statarb_dynamic", "long"),
    }
    # No history → penalty = 1.0
    assert tracker.get_correlation_penalty(signals) == 1.0


def test_correlation_tracker_update_and_penalty_when_all_agree():
    tracker = GroupCorrelationTracker(ema_alpha=0.3)
    # Feed in many cycles of agreement
    for _ in range(10):
        signals = {
            "barrier_12bar": _gs_corr("barrier_12bar", "long"),
            "micro_3bar": _gs_corr("micro_3bar", "long"),
            "statarb_dynamic": _gs_corr("statarb_dynamic", "long"),
        }
        tracker.update(signals)
    # All agree + high correlation → concentration penalty applied
    penalty = tracker.get_correlation_penalty(signals)
    assert penalty < 1.0  # penalty due to same-direction concentration


def test_correlation_tracker_no_penalty_on_disagreement():
    tracker = GroupCorrelationTracker(ema_alpha=0.5)
    # Build strong correlation between barrier and micro
    for _ in range(20):
        signals = {
            "barrier_12bar": _gs_corr("barrier_12bar", "long"),
            "micro_3bar": _gs_corr("micro_3bar", "long"),
            "statarb_dynamic": None,
        }
        tracker.update(signals)
    # Now barrier and micro disagree — disagreement is natural hedge, no penalty
    signals_divergent = {
        "barrier_12bar": _gs_corr("barrier_12bar", "long"),
        "micro_3bar": _gs_corr("micro_3bar", "short"),
        "statarb_dynamic": None,
    }
    penalty = tracker.get_correlation_penalty(signals_divergent)
    assert penalty == 1.0  # opposing directions → natural hedge


def test_correlation_tracker_no_penalty_uncorrelated_disagreement():
    tracker = GroupCorrelationTracker(ema_alpha=0.1)
    # Feed perfectly alternating signals → groups are anti-correlated
    for i in range(50):
        b_dir = "long" if i % 2 == 0 else "short"
        m_dir = "short" if i % 2 == 0 else "long"  # always opposite
        signals = {
            "barrier_12bar": _gs_corr("barrier_12bar", b_dir),
            "micro_3bar": _gs_corr("micro_3bar", m_dir),
            "statarb_dynamic": None,
        }
        tracker.update(signals)
    # EMA → near 0.0 (anti-correlated), so disagreement is "normal" → no penalty
    signals = {
        "barrier_12bar": _gs_corr("barrier_12bar", "long"),
        "micro_3bar": _gs_corr("micro_3bar", "short"),
        "statarb_dynamic": None,
    }
    penalty = tracker.get_correlation_penalty(signals)
    # Low EMA (<0.5) → no penalty (disagreement is the norm for these groups)
    assert penalty == 1.0


def test_correlation_tracker_neutral_excluded_but_same_direction_penalized():
    tracker = GroupCorrelationTracker(ema_alpha=0.5)
    for _ in range(10):
        signals = {
            "barrier_12bar": _gs_corr("barrier_12bar", "long"),
            "micro_3bar": _gs_corr("micro_3bar", "long"),
            "statarb_dynamic": _gs_corr("statarb_dynamic", "long"),
        }
        tracker.update(signals)
    # One group goes neutral, but barrier+statarb both still long + correlated → penalty
    signals = {
        "barrier_12bar": _gs_corr("barrier_12bar", "long"),
        "micro_3bar": _gs_corr("micro_3bar", "neutral"),
        "statarb_dynamic": _gs_corr("statarb_dynamic", "long"),
    }
    penalty = tracker.get_correlation_penalty(signals)
    assert penalty < 1.0  # barrier+statarb same-direction concentration penalized


def test_correlation_tracker_two_neutral_skips():
    tracker = GroupCorrelationTracker(ema_alpha=0.5)
    signals = {
        "barrier_12bar": _gs_corr("barrier_12bar", "neutral"),
        "micro_3bar": _gs_corr("micro_3bar", "neutral"),
        "statarb_dynamic": _gs_corr("statarb_dynamic", "long"),
    }
    tracker.update(signals)
    # Both neutral pairs skip, no crash
    penalty = tracker.get_correlation_penalty(signals)
    assert penalty == 1.0


def test_correlation_tracker_penalty_clamped():
    tracker = GroupCorrelationTracker(ema_alpha=0.5)
    # Build perfect correlation
    for _ in range(20):
        signals = {
            "barrier_12bar": _gs_corr("barrier_12bar", "long"),
            "micro_3bar": _gs_corr("micro_3bar", "long"),
            "statarb_dynamic": _gs_corr("statarb_dynamic", "long"),
        }
        tracker.update(signals)
    # Complete disagreement after perfect correlation
    signals = {
        "barrier_12bar": _gs_corr("barrier_12bar", "long"),
        "micro_3bar": _gs_corr("micro_3bar", "short"),
        "statarb_dynamic": _gs_corr("statarb_dynamic", "short"),
    }
    penalty = tracker.get_correlation_penalty(signals)
    assert 0.5 <= penalty <= 1.0  # always in valid range
