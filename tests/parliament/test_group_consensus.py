"""Tests for core.parliament.group_consensus — contract-group consensus computer.

Covers compute_contract_group_consensus() with mocked sub-services.
Because all dependencies are imported inside the function body (lazy imports),
patches must target the DEFINITION modules, not the calling module.

Key paths:
  - Empty proposals → neutral
  - No trade allocation → neutral with reason
  - Normal consensus → direction + volume + extra
  - Capacity allocation path (budget > 0)
  - Regime info propagation
  - Short direction
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.parliament.group_consensus import compute_contract_group_consensus

# ── Test-data factories ─────────────────────────────────────────────────────


def _brain_info(brain_id="B1", contract_group="barrier_12bar", **kw):
    """Build a brain_info dict for a single brain."""
    return {"brain_id": brain_id, "contract_group": contract_group, **kw}


def _proposal(
    direction="long", confidence=0.8, brain_id="B1", contract_group="barrier_12bar", **kw
):
    """Build a mock proposal (SimpleNamespace) with relevant fields."""
    defaults = {
        "direction": direction,
        "confidence": confidence,
        "brain_id": brain_id,
        "contract_group": contract_group,
        "vote_weight": 1.0,
        "dynamic_scale": 1.0,
        "fallback": False,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ── Mock factories ──────────────────────────────────────────────────────────


def _mock_weighter_class():
    """Return a MagicMock for DynamicBrainWeighter that is safe to call."""
    mock = MagicMock()
    mock_instance = MagicMock()
    mock_instance.get_weights.return_value = {}
    mock.return_value = mock_instance
    return mock


def _mock_alloc_decision(
    should_trade=True,
    direction="long",
    confidence=0.7,
    agreement_level="majority",
    active_groups=None,
    dissenting_groups=None,
    reason="",
):
    """Build a mock AllocationDecision."""
    return SimpleNamespace(
        should_trade=should_trade,
        direction=direction,
        confidence=confidence,
        agreement_level=agreement_level,
        active_groups=active_groups or ["barrier_12bar"],
        dissenting_groups=dissenting_groups or [],
        reason=reason,
    )


def _mock_volume_fn(base_volume, decision, regime, vol_atr):
    """Default compute_volume mock — identity on base_volume."""
    return base_volume


# Patch targets (definition modules, not the calling module):
_WEIGHTER_PATH = "core.brains.services.dynamic_brain_weighter.DynamicBrainWeighter"
_ALL_GROUPS_PATH = "core.parliament.contract_groups.compute_all_group_signals"
_RESOLVE_PATH = "core.execution.capital_allocator.resolve_conflicts"
_VOLUME_PATH = "core.execution.capital_allocator.compute_volume"
_CAP_ALLOC_PATH = "core.execution.capital_allocator.CapitalAllocator"


# ── Tests: empty / trivial inputs ───────────────────────────────────────────


def test_empty_proposals_returns_neutral():
    """No proposals → neutral direction, 0 confidence, 0 volume."""
    with patch(_WEIGHTER_PATH, _mock_weighter_class()):
        result = compute_contract_group_consensus(
            raw_proposals=[],
            brains=[],
            tracker=SimpleNamespace(),
            pnl_ledger=SimpleNamespace(),
            correlation_tracker=None,
            base_volume=0.1,
            current_atr=5.0,
        )
    assert result["direction"] == "neutral"
    assert result["confidence"] == 0.0
    assert result["dynamic_volume"] == 0.0


# ── Tests: no-trade allocation ──────────────────────────────────────────────


def test_no_trade_returns_neutral_with_reason():
    """resolve_conflicts says should_trade=False → neutral with reason."""
    mock_gs = SimpleNamespace(
        direction="long",
        confidence=0.7,
        total_count=1,
        brain_ids=["B1"],
    )

    with (
        patch(_WEIGHTER_PATH, _mock_weighter_class()),
        patch(_ALL_GROUPS_PATH, return_value={"barrier_12bar": mock_gs}),
        patch(
            _RESOLVE_PATH,
            return_value=_mock_alloc_decision(
                should_trade=False,
                direction="neutral",
                confidence=0.0,
                agreement_level="none",
                reason="no_quorum",
            ),
        ),
    ):
        result = compute_contract_group_consensus(
            raw_proposals=[_proposal("long", 0.9, "B1")],
            brains=[_brain_info("B1")],
            tracker=SimpleNamespace(),
            pnl_ledger=SimpleNamespace(),
            correlation_tracker=None,
            base_volume=0.1,
            current_atr=3.0,
        )
    assert result["direction"] == "neutral"
    assert result["confidence"] == 0.0
    assert result["dynamic_volume"] == 0.0
    extra = result["consensus_extra"]
    assert extra["is_feasible"] is False
    assert extra["allocation"]["reason"] == "no_quorum"


# ── Tests: normal flow ─────────────────────────────────────────────────────


def test_normal_consensus_returns_direction():
    """Full flow with one proposal → direction=long, confidence=0.7."""
    mock_gs = SimpleNamespace(
        direction="long",
        confidence=0.7,
        total_count=1,
        brain_ids=["B1"],
    )

    with (
        patch(_WEIGHTER_PATH, _mock_weighter_class()),
        patch(_ALL_GROUPS_PATH, return_value={"barrier_12bar": mock_gs}),
        patch(
            _RESOLVE_PATH,
            return_value=_mock_alloc_decision(should_trade=True, direction="long", confidence=0.7),
        ),
        patch(_VOLUME_PATH, _mock_volume_fn),
    ):
        result = compute_contract_group_consensus(
            raw_proposals=[_proposal("long", 0.85, "B1", "barrier_12bar")],
            brains=[_brain_info("B1", "barrier_12bar")],
            tracker=SimpleNamespace(),
            pnl_ledger=SimpleNamespace(),
            correlation_tracker=None,
            base_volume=0.05,
            current_atr=4.5,
        )
    assert result["direction"] == "long"
    assert result["confidence"] == 0.7
    assert result["dynamic_volume"] == 0.05
    extra = result["consensus_extra"]
    assert extra["is_feasible"] is True
    assert extra["voter_count"] == 1
    assert extra["aggregated_bias"] == "long"
    assert extra["consensus_score"] == 0.7


def test_consensus_extra_includes_allocation_fields():
    """consensus_extra carries agreement_level, active_groups, dissenting_groups."""
    mock_gs = SimpleNamespace(
        direction="long",
        confidence=0.8,
        total_count=1,
        brain_ids=["B1"],
    )

    with (
        patch(_WEIGHTER_PATH, _mock_weighter_class()),
        patch(_ALL_GROUPS_PATH, return_value={"barrier_12bar": mock_gs}),
        patch(_RESOLVE_PATH, return_value=_mock_alloc_decision(agreement_level="majority")),
        patch(_VOLUME_PATH, _mock_volume_fn),
    ):
        result = compute_contract_group_consensus(
            raw_proposals=[_proposal("long", 0.8, "B1")],
            brains=[_brain_info("B1")],
            tracker=SimpleNamespace(),
            pnl_ledger=SimpleNamespace(),
            correlation_tracker=None,
            base_volume=0.05,
            current_atr=3.0,
        )
    extra = result["consensus_extra"]
    assert extra["allocation"]["agreement_level"] == "majority"
    assert "active_groups" in extra["allocation"]


def test_consensus_extra_counts_voters():
    """consensus_extra.voter_count reflects total voters from group signals."""
    mock_gs = SimpleNamespace(
        direction="long",
        confidence=0.75,
        total_count=3,
        brain_ids=["B1", "B2", "B3"],
    )

    with (
        patch(_WEIGHTER_PATH, _mock_weighter_class()),
        patch(_ALL_GROUPS_PATH, return_value={"barrier_12bar": mock_gs}),
        patch(_RESOLVE_PATH, return_value=_mock_alloc_decision()),
        patch(_VOLUME_PATH, _mock_volume_fn),
    ):
        result = compute_contract_group_consensus(
            raw_proposals=[
                _proposal("long", 0.8, "B1"),
                _proposal("long", 0.7, "B2"),
                _proposal("short", 0.6, "B3"),
            ],
            brains=[_brain_info(f"B{i}") for i in range(1, 4)],
            tracker=SimpleNamespace(),
            pnl_ledger=SimpleNamespace(),
            correlation_tracker=None,
            base_volume=0.1,
            current_atr=3.0,
        )
    extra = result["consensus_extra"]
    assert extra["voter_count"] == 3


# ── Tests: short direction ──────────────────────────────────────────────────


def test_consensus_short_direction():
    """Short direction propagates correctly."""
    mock_gs = SimpleNamespace(
        direction="short",
        confidence=0.75,
        total_count=1,
        brain_ids=["B1"],
    )

    with (
        patch(_WEIGHTER_PATH, _mock_weighter_class()),
        patch(_ALL_GROUPS_PATH, return_value={"barrier_12bar": mock_gs}),
        patch(
            _RESOLVE_PATH,
            return_value=_mock_alloc_decision(
                should_trade=True, direction="short", confidence=0.75, agreement_level="full"
            ),
        ),
        patch(_VOLUME_PATH, _mock_volume_fn),
    ):
        result = compute_contract_group_consensus(
            raw_proposals=[_proposal("short", 0.75, "B1")],
            brains=[_brain_info("B1")],
            tracker=SimpleNamespace(),
            pnl_ledger=SimpleNamespace(),
            correlation_tracker=None,
            base_volume=0.1,
            current_atr=3.5,
        )
    assert result["direction"] == "short"
    assert result["confidence"] == 0.75


# ── Tests: capacity allocation ──────────────────────────────────────────────


def test_total_budget_triggers_capacity_allocation():
    """When total_budget > 0, CapitalAllocator.allocate_capacity is called."""
    mock_gs = SimpleNamespace(
        direction="long",
        confidence=0.8,
        total_count=1,
        brain_ids=["B1"],
    )
    mock_alloc = MagicMock()
    mock_alloc.allocate_capacity.return_value = {"B1": 0.03}

    with (
        patch(_WEIGHTER_PATH, _mock_weighter_class()),
        patch(_ALL_GROUPS_PATH, return_value={"barrier_12bar": mock_gs}),
        patch(_RESOLVE_PATH, return_value=_mock_alloc_decision()),
        patch(_VOLUME_PATH, _mock_volume_fn),
        patch(_CAP_ALLOC_PATH, return_value=mock_alloc),
    ):
        result = compute_contract_group_consensus(
            raw_proposals=[_proposal("long", 0.8, "B1")],
            brains=[_brain_info("B1")],
            tracker=SimpleNamespace(),
            pnl_ledger=SimpleNamespace(),
            correlation_tracker=None,
            base_volume=0.05,
            current_atr=3.0,
            total_budget=5000.0,
            lot_value=100.0,
        )
    mock_alloc.allocate_capacity.assert_called_once()
    assert result["consensus_extra"]["capacity_allocations"] == {"B1": 0.03}


def test_zero_budget_skips_capacity_allocation():
    """total_budget=0 → CapitalAllocator is never instantiated."""
    mock_gs = SimpleNamespace(
        direction="long",
        confidence=0.8,
        total_count=1,
        brain_ids=["B1"],
    )

    with (
        patch(_WEIGHTER_PATH, _mock_weighter_class()),
        patch(_ALL_GROUPS_PATH, return_value={"barrier_12bar": mock_gs}),
        patch(_RESOLVE_PATH, return_value=_mock_alloc_decision()),
        patch(_VOLUME_PATH, _mock_volume_fn),
        patch(_CAP_ALLOC_PATH) as mock_cap_class,
    ):
        compute_contract_group_consensus(
            raw_proposals=[_proposal("long", 0.8, "B1")],
            brains=[_brain_info("B1")],
            tracker=SimpleNamespace(),
            pnl_ledger=SimpleNamespace(),
            correlation_tracker=None,
            base_volume=0.05,
            current_atr=3.0,
            total_budget=0.0,
            lot_value=100.0,
        )
    mock_cap_class.assert_not_called()


# ── Tests: regime_info propagation ──────────────────────────────────────────


def test_regime_info_propagated_to_volume():
    """regime_info dict is passed through to compute_volume."""
    mock_gs = SimpleNamespace(
        direction="long",
        confidence=0.8,
        total_count=1,
        brain_ids=["B1"],
    )
    captured_regime = []

    def _capture_volume(base_volume, decision, regime, vol_atr):
        captured_regime.append(regime)
        return base_volume

    with (
        patch(_WEIGHTER_PATH, _mock_weighter_class()),
        patch(_ALL_GROUPS_PATH, return_value={"barrier_12bar": mock_gs}),
        patch(_RESOLVE_PATH, return_value=_mock_alloc_decision()),
        patch(_VOLUME_PATH, _capture_volume),
    ):
        compute_contract_group_consensus(
            raw_proposals=[_proposal("long", 0.8, "B1")],
            brains=[_brain_info("B1")],
            tracker=SimpleNamespace(),
            pnl_ledger=SimpleNamespace(),
            correlation_tracker=None,
            base_volume=0.05,
            current_atr=3.0,
            regime_info={"regime": "trending", "strength": 0.9},
        )
    assert captured_regime[0] == "trending"


def test_regime_info_none_defaults_to_normal():
    """When regime_info is None or missing regime key, default='normal'."""
    mock_gs = SimpleNamespace(
        direction="long",
        confidence=0.8,
        total_count=1,
        brain_ids=["B1"],
    )
    captured_regime = []

    def _capture_volume(base_volume, decision, regime, vol_atr):
        captured_regime.append(regime)
        return base_volume

    with (
        patch(_WEIGHTER_PATH, _mock_weighter_class()),
        patch(_ALL_GROUPS_PATH, return_value={"barrier_12bar": mock_gs}),
        patch(_RESOLVE_PATH, return_value=_mock_alloc_decision()),
        patch(_VOLUME_PATH, _capture_volume),
    ):
        compute_contract_group_consensus(
            raw_proposals=[_proposal("long", 0.8, "B1")],
            brains=[_brain_info("B1")],
            tracker=SimpleNamespace(),
            pnl_ledger=SimpleNamespace(),
            correlation_tracker=None,
            base_volume=0.05,
            current_atr=3.0,
            regime_info=None,
        )
    assert captured_regime[0] == "normal"


# ── Tests: edge cases ───────────────────────────────────────────────────────


def test_more_proposals_than_brains_fills_empty():
    """If len(proposals) > len(brains), missing brain_info defaults to {}."""
    mock_gs = SimpleNamespace(
        direction="long",
        confidence=0.8,
        total_count=2,
        brain_ids=["B1", "B2"],
    )

    with (
        patch(_WEIGHTER_PATH, _mock_weighter_class()),
        patch(_ALL_GROUPS_PATH, return_value={"barrier_12bar": mock_gs}),
        patch(_RESOLVE_PATH, return_value=_mock_alloc_decision()),
        patch(_VOLUME_PATH, _mock_volume_fn),
    ):
        result = compute_contract_group_consensus(
            raw_proposals=[_proposal("long", 0.8, "B1"), _proposal("short", 0.6, "B2")],
            brains=[_brain_info("B1")],  # only 1 brain_info for 2 proposals
            tracker=SimpleNamespace(),
            pnl_ledger=SimpleNamespace(),
            correlation_tracker=None,
            base_volume=0.05,
            current_atr=3.0,
        )
    # Should not crash — missing brain_info defaults to {}
    assert result["direction"] == "long"
