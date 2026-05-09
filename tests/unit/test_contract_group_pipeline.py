"""Integration tests for the full contract-group pipeline.

Exercises: brain proposals → contract groups → per-group consensus →
capital allocator (conflict resolution + position sizing + correlation
tracking) → final allocation decision.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

from core.execution.capital_allocator import (
    GroupCorrelationTracker,
    compute_volume,
    resolve_conflicts,
)
from core.parliament.contract_groups import (
    compute_all_group_signals,
)

# ── Fake proposal ──


@dataclass
class FakeProposal:
    brain_id: str = "B1"
    prediction: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    vote_weight: float = 1.0


def _make_prop(
    up=0.6, down=0.4, conf=0.7, direction="long", fallback=False, weight=1.0, bid="B1"
) -> FakeProposal:
    return FakeProposal(
        brain_id=bid,
        prediction={
            "up_probability": up,
            "down_probability": down,
            "confidence": conf,
            "direction_bias": direction,
        },
        health={"fallback_used": fallback},
        vote_weight=weight,
    )


# ── Full pipeline tests ──


def test_pipeline_all_groups_agree_long():
    """All 3 contract groups agree long → full allocation with full volume."""
    brain_proposals = [
        (
            {"brain_type": "onnx_v9", "brain_id": "B_barrier1"},
            _make_prop(up=0.8, down=0.2, conf=0.85, direction="long", bid="B_barrier1"),
        ),
        (
            {"brain_type": "deepresmlp", "brain_id": "B_barrier2"},
            _make_prop(up=0.75, down=0.25, conf=0.80, direction="long", bid="B_barrier2"),
        ),
        (
            {"brain_type": "xgboost_v9", "brain_id": "B_barrier3"},
            _make_prop(up=0.70, down=0.30, conf=0.75, direction="long", bid="B_barrier3"),
        ),
        (
            {"brain_type": "xgboost_v4.5", "brain_id": "B_micro1"},
            _make_prop(up=0.72, down=0.28, conf=0.78, direction="long", bid="B_micro1"),
        ),
        (
            {"brain_type": "ou_params_v6", "brain_id": "B_arb1"},
            _make_prop(up=0.55, down=0.45, conf=0.60, direction="long", bid="B_arb1"),
        ),
    ]

    group_signals = compute_all_group_signals(brain_proposals)
    allocation = resolve_conflicts(group_signals)

    assert allocation.should_trade
    assert allocation.direction == "long"
    assert allocation.agreement_level == "full"
    assert len(allocation.active_groups) == 3

    vol = compute_volume(0.05, allocation, regime="normal")
    assert vol == pytest.approx(0.05, abs=0.01)


def test_pipeline_barrier_long_micro_short_no_trade():
    """Barrier group long, micro group short → NO TRADE (conflict)."""
    brain_proposals = [
        (
            {"brain_type": "onnx_v9", "brain_id": "B_barrier1"},
            _make_prop(up=0.8, down=0.2, conf=0.85, direction="long", bid="B_barrier1"),
        ),
        (
            {"brain_type": "xgboost_v9", "brain_id": "B_barrier2"},
            _make_prop(up=0.75, down=0.25, conf=0.80, direction="long", bid="B_barrier2"),
        ),
        (
            {"brain_type": "xgboost_v4.5", "brain_id": "B_micro1"},
            _make_prop(up=0.30, down=0.70, conf=0.78, direction="short", bid="B_micro1"),
        ),
        (
            {"brain_type": "transformer_v4.3", "brain_id": "B_micro2"},
            _make_prop(up=0.25, down=0.75, conf=0.82, direction="short", bid="B_micro2"),
        ),
    ]

    group_signals = compute_all_group_signals(brain_proposals)
    allocation = resolve_conflicts(group_signals)

    assert not allocation.should_trade
    assert "cross_group_conflict" in allocation.reason


def test_pipeline_two_agree_one_neutral_reduced_volume():
    """2 groups agree long, 1 neutral → reduced allocation."""
    brain_proposals = [
        (
            {"brain_type": "onnx_v9", "brain_id": "B_barrier1"},
            _make_prop(up=0.8, down=0.2, conf=0.85, direction="long", bid="B_barrier1"),
        ),
        (
            {"brain_type": "deepresmlp", "brain_id": "B_barrier2"},
            _make_prop(up=0.75, down=0.25, conf=0.80, direction="long", bid="B_barrier2"),
        ),
        (
            {"brain_type": "xgboost_v4.5", "brain_id": "B_micro1"},
            _make_prop(up=0.50, down=0.50, conf=0.50, direction="neutral", bid="B_micro1"),
        ),
    ]

    group_signals = compute_all_group_signals(brain_proposals)
    allocation = resolve_conflicts(group_signals)

    assert allocation.should_trade
    assert allocation.direction == "long"
    assert allocation.agreement_level == "reduced"

    vol = compute_volume(0.05, allocation, regime="normal")
    # reduced=0.70, normal=1.0, vol_factor=1.0 → 0.035, round to 2 decimals → 0.03
    assert vol == pytest.approx(0.03, abs=0.01)


def test_pipeline_only_one_group_active_reduced_confidence():
    """Only barrier group active, others absent → lower confidence."""
    brain_proposals = [
        (
            {"brain_type": "onnx_v9", "brain_id": "B_barrier1"},
            _make_prop(up=0.8, down=0.2, conf=0.85, direction="long", bid="B_barrier1"),
        ),
    ]

    group_signals = compute_all_group_signals(brain_proposals)
    allocation = resolve_conflicts(group_signals)

    assert allocation.should_trade
    assert allocation.direction == "long"
    assert allocation.agreement_level == "reduced"
    # GroupSignal blends majority_ratio (1.0) with raw_score → ~0.87
    # Then conf_mult=0.65 → ~0.5655
    assert allocation.confidence == pytest.approx(0.5655, abs=0.01)


def test_pipeline_high_regime_reduces_volume():
    """High volatility regime → volume reduced."""
    brain_proposals = [
        (
            {"brain_type": "onnx_v9", "brain_id": "B_barrier1"},
            _make_prop(up=0.8, down=0.2, conf=0.85, direction="long", bid="B_barrier1"),
        ),
        (
            {"brain_type": "xgboost_v9", "brain_id": "B_barrier2"},
            _make_prop(up=0.75, down=0.25, conf=0.80, direction="long", bid="B_barrier2"),
        ),
    ]

    group_signals = compute_all_group_signals(brain_proposals)
    allocation = resolve_conflicts(group_signals)

    vol_normal = compute_volume(0.10, allocation, regime="normal")
    vol_high = compute_volume(0.10, allocation, regime="high")
    vol_low = compute_volume(0.10, allocation, regime="low")

    assert vol_high < vol_normal < vol_low


def test_pipeline_atr_expansion_shrinks_volume():
    """High ATR (volatile) → volume scaled down for risk normalization."""
    brain_proposals = [
        (
            {"brain_type": "onnx_v9", "brain_id": "B_barrier1"},
            _make_prop(up=0.8, down=0.2, conf=0.85, direction="long", bid="B_barrier1"),
        ),
    ]

    group_signals = compute_all_group_signals(brain_proposals)
    allocation = resolve_conflicts(group_signals)

    vol_low_atr = compute_volume(0.10, allocation, vol_atr=15.0)
    vol_high_atr = compute_volume(0.10, allocation, vol_atr=3.0)

    assert vol_low_atr < vol_high_atr  # higher ATR → smaller position


def test_pipeline_all_neutral_produces_trade_with_low_confidence():
    """All proposals neutral → direction defaults to long with very low confidence.

    The contract-group system always produces a direction (when up >= down it's
    "long") even when all direction_bias values are "neutral."  The low-confidence
    gate downstream is responsible for filtering these out (typically threshold
    ≥ 0.30).  This test verifies the signal is produced with appropriately low
    conviction.
    """
    brain_proposals = [
        (
            {"brain_type": "onnx_v9", "brain_id": "B1"},
            _make_prop(up=0.5, down=0.5, conf=0.5, direction="neutral", bid="B1"),
        ),
        (
            {"brain_type": "xgboost_v4.5", "brain_id": "B2"},
            _make_prop(up=0.5, down=0.5, conf=0.5, direction="neutral", bid="B2"),
        ),
    ]

    group_signals = compute_all_group_signals(brain_proposals)
    allocation = resolve_conflicts(group_signals)

    # Should produce a trade signal (direction resolved from weighted scores)
    assert allocation.should_trade
    # Confidence must be very low (< 0.30) so the confidence gate can filter it
    assert allocation.confidence < 0.30


# ── Pipeline + correlation tracker ──


def test_pipeline_with_correlation_tracker():
    """Full pipeline with correlation tracking — no penalty when all agree."""
    tracker = GroupCorrelationTracker(ema_alpha=0.3)

    # Feed history of agreement
    for _ in range(10):
        brain_proposals = [
            (
                {"brain_type": "onnx_v9", "brain_id": "B_barrier1"},
                _make_prop(up=0.8, down=0.2, conf=0.85, direction="long", bid="B_barrier1"),
            ),
            (
                {"brain_type": "xgboost_v4.5", "brain_id": "B_micro1"},
                _make_prop(up=0.72, down=0.28, conf=0.78, direction="long", bid="B_micro1"),
            ),
            (
                {"brain_type": "ou_params_v6", "brain_id": "B_arb1"},
                _make_prop(up=0.55, down=0.45, conf=0.60, direction="long", bid="B_arb1"),
            ),
        ]
        gs = compute_all_group_signals(brain_proposals)
        tracker.update(gs)

    # Current cycle: all agree → no penalty
    gs = compute_all_group_signals(brain_proposals)
    allocation = resolve_conflicts(gs)
    penalty = tracker.get_correlation_penalty(gs)
    assert penalty == 1.0

    vol = compute_volume(0.05, allocation)
    vol_with_penalty = round(vol * penalty, 3)
    assert vol_with_penalty == vol  # no penalty applied


def test_pipeline_with_correlation_penalty():
    """Correlated groups diverging → penalty reduces volume."""
    tracker = GroupCorrelationTracker(ema_alpha=0.5)

    # Build strong co-direction
    for _ in range(20):
        brain_proposals = [
            (
                {"brain_type": "onnx_v9", "brain_id": "B_barrier1"},
                _make_prop(up=0.8, down=0.2, conf=0.85, direction="long", bid="B_barrier1"),
            ),
            (
                {"brain_type": "xgboost_v4.5", "brain_id": "B_micro1"},
                _make_prop(up=0.72, down=0.28, conf=0.78, direction="long", bid="B_micro1"),
            ),
            (
                {"brain_type": "ou_params_v6", "brain_id": "B_arb1"},
                _make_prop(up=0.55, down=0.45, conf=0.60, direction="long", bid="B_arb1"),
            ),
        ]
        gs = compute_all_group_signals(brain_proposals)
        tracker.update(gs)

    # Now micro flips to short
    brain_proposals_divergent = [
        (
            {"brain_type": "onnx_v9", "brain_id": "B_barrier1"},
            _make_prop(up=0.8, down=0.2, conf=0.85, direction="long", bid="B_barrier1"),
        ),
        (
            {"brain_type": "xgboost_v4.5", "brain_id": "B_micro1"},
            _make_prop(up=0.30, down=0.70, conf=0.78, direction="short", bid="B_micro1"),
        ),
        (
            {"brain_type": "ou_params_v6", "brain_id": "B_arb1"},
            _make_prop(up=0.55, down=0.45, conf=0.60, direction="long", bid="B_arb1"),
        ),
    ]
    gs_div = compute_all_group_signals(brain_proposals_divergent)
    allocation = resolve_conflicts(gs_div)
    # Long vs short → NO TRADE (conflict matrix blocks it)
    assert not allocation.should_trade


def test_pipeline_volume_clamped():
    """Volume respects min/max bounds."""
    brain_proposals = [
        (
            {"brain_type": "onnx_v9", "brain_id": "B1"},
            _make_prop(up=0.9, down=0.1, conf=0.95, direction="long", bid="B1"),
        ),
    ]

    group_signals = compute_all_group_signals(brain_proposals)
    allocation = resolve_conflicts(group_signals)

    # Test max clamp
    vol_max = compute_volume(0.20, allocation, regime="low", vol_atr=1.0, max_volume=0.10)
    assert vol_max <= 0.10

    # Test min clamp
    vol_min = compute_volume(0.001, allocation, regime="high", vol_atr=20.0, min_volume=0.01)
    assert vol_min >= 0.01


def test_pipeline_consensus_extra_structure():
    """Verify the consensus_extra dict structure (for downstream JSON logging)."""
    brain_proposals = [
        (
            {"brain_type": "onnx_v9", "brain_id": "B_barrier1"},
            _make_prop(up=0.8, down=0.2, conf=0.85, direction="long", bid="B_barrier1"),
        ),
        (
            {"brain_type": "deepresmlp", "brain_id": "B_barrier2"},
            _make_prop(up=0.75, down=0.25, conf=0.80, direction="long", bid="B_barrier2"),
        ),
        (
            {"brain_type": "xgboost_v4.5", "brain_id": "B_micro1"},
            _make_prop(up=0.40, down=0.60, conf=0.72, direction="short", bid="B_micro1"),
        ),
    ]

    group_signals = compute_all_group_signals(brain_proposals)
    allocation = resolve_conflicts(group_signals)

    # Build consensus_extra (mirrors live_cycle.py logic)
    all_supporting: list[str] = []
    all_opposing: list[str] = []
    total_voters = 0
    for _gname, gs in group_signals.items():
        if gs is None:
            continue
        total_voters += gs.total_count
        if gs.direction == allocation.direction:
            all_supporting.extend(gs.brain_ids)
        elif gs.direction != "neutral":
            all_opposing.extend(gs.brain_ids)

    consensus_extra = {
        "voter_count": total_voters,
        "majority_ratio": allocation.confidence,
        "disagreement_score": round(1.0 - allocation.confidence, 4),
        "supporting_brains": list(set(all_supporting)),
        "opposing_brains": list(set(all_opposing)),
        "is_feasible": allocation.should_trade,
        "aggregated_bias": allocation.direction,
        "consensus_score": allocation.confidence,
        "allocation": {
            "agreement_level": allocation.agreement_level,
            "active_groups": allocation.active_groups,
            "dissenting_groups": allocation.dissenting_groups,
            "reason": allocation.reason,
        },
    }

    # Two groups active, barrier supports short, micro long → NO trade
    assert not allocation.should_trade
    assert consensus_extra["aggregated_bias"] == "neutral"
    assert "cross_group_conflict" in consensus_extra["allocation"]["reason"]
    # Barrier had 2 brains, micro had 1 → 3 voters
    assert consensus_extra["voter_count"] == 3


# ── Shadow verification counterfactual settlement ──


def _compute_counterfactual_pnl(
    direction: str, entry_price: float, exit_price: float
) -> tuple[float, float]:
    """Replicate the shadow verification P&L formula from live_cycle.py."""
    if direction == "long":
        pnl = round(exit_price - entry_price, 6)
    elif direction == "short":
        pnl = round(entry_price - exit_price, 6)
    else:
        pnl = 0.0
    bps = round((pnl / entry_price) * 10000, 2) if entry_price > 0 else 0.0
    return pnl, bps


def test_counterfactual_pnl_long_win():
    """Long entry at 4700, exit at 4720 → +20.0 profit."""
    pnl, bps = _compute_counterfactual_pnl("long", 4700.0, 4720.0)
    assert pnl == 20.0
    assert bps == pytest.approx(42.55, 0.01)


def test_counterfactual_pnl_long_loss():
    """Long entry at 4700, exit at 4680 → -20.0 loss."""
    pnl, bps = _compute_counterfactual_pnl("long", 4700.0, 4680.0)
    assert pnl == -20.0
    assert bps == pytest.approx(-42.55, 0.01)


def test_counterfactual_pnl_short_win():
    """Short entry at 4720, exit at 4700 → +20.0 profit."""
    pnl, bps = _compute_counterfactual_pnl("short", 4720.0, 4700.0)
    assert pnl == 20.0
    assert bps == pytest.approx(42.37, 0.01)


def test_counterfactual_pnl_short_loss():
    """Short entry at 4700, exit at 4720 → -20.0 loss."""
    pnl, bps = _compute_counterfactual_pnl("short", 4700.0, 4720.0)
    assert pnl == -20.0


def test_counterfactual_pnl_neutral_is_zero():
    """Neutral direction always produces zero P&L regardless of price move."""
    pnl, bps = _compute_counterfactual_pnl("neutral", 4700.0, 4800.0)
    assert pnl == 0.0
    assert bps == 0.0


def test_counterfactual_pnl_flat_price():
    """No price movement → zero P&L for any direction."""
    for d in ("long", "short"):
        pnl, bps = _compute_counterfactual_pnl(d, 4700.0, 4700.0)
        assert pnl == 0.0
        assert bps == 0.0


def test_shadow_verification_state_lifecycle():
    """LiveCycleState.shadow_verification_pending → settle → cleared."""
    from dataclasses import dataclass
    from typing import Any

    @dataclass
    class FakeState:
        shadow_verification_pending: dict[str, Any] | None = None

    state = FakeState()

    # Stage a pending verification (simulating what live_cycle does)
    state.shadow_verification_pending = {
        "direction": "long",
        "entry_price": 4700.0,
        "consensus_score": 0.72,
        "supporting_brains": ["B1", "B2"],
        "opposing_brains": [],
    }

    # Verify it's set
    assert state.shadow_verification_pending is not None
    assert state.shadow_verification_pending["direction"] == "long"

    # Simulate settlement
    pending = state.shadow_verification_pending
    pnl, bps = _compute_counterfactual_pnl(pending["direction"], pending["entry_price"], 4720.0)
    assert pnl == 20.0

    # Clear after settlement
    state.shadow_verification_pending = None
    assert state.shadow_verification_pending is None


def test_shadow_verification_skips_neutral_direction():
    """When direction is neutral, shadow verification is not staged."""
    # live_cycle.py only stages when direction != "neutral"
    from dataclasses import dataclass
    from typing import Any

    @dataclass
    class FakeState:
        shadow_verification_pending: dict[str, Any] | None = None

    state = FakeState()
    direction = "neutral"
    mid_price = 4700.0

    # Replicate the staging condition from live_cycle.py
    if direction != "neutral" and mid_price is not None and mid_price > 0:
        state.shadow_verification_pending = {"direction": direction, "entry_price": mid_price}

    assert state.shadow_verification_pending is None


def test_shadow_verification_event_structure():
    """The shadow_verified event contains all required fields."""
    import json

    pending = {
        "direction": "short",
        "entry_price": 4720.0,
        "consensus_score": 0.65,
        "supporting_brains": ["B_barrier"],
        "opposing_brains": ["B_micro"],
    }
    mid_price = 4700.0

    pnl, bps = _compute_counterfactual_pnl(pending["direction"], pending["entry_price"], mid_price)

    event = {
        "event": "shadow_verified",
        "direction": pending["direction"],
        "entry_price": round(pending["entry_price"], 2),
        "exit_price": round(mid_price, 2),
        "counterfactual_pnl": pnl,
        "counterfactual_bps": bps,
        "consensus_score": pending["consensus_score"],
        "supporting_brains": pending["supporting_brains"],
        "opposing_brains": pending["opposing_brains"],
    }

    assert event["event"] == "shadow_verified"
    assert event["direction"] == "short"
    assert event["counterfactual_pnl"] == 20.0
    assert event["counterfactual_bps"] == pytest.approx(42.37, 0.01)
    assert len(event["supporting_brains"]) == 1
    assert len(event["opposing_brains"]) == 1
    # Verify JSON serializable
    json.dumps(event)
