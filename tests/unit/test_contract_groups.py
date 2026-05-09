"""Tests for contract_groups.py — per-group consensus computation."""

from dataclasses import dataclass, field
from typing import Any

from core.parliament.contract_groups import (
    ALL_GROUPS,
    ARB_GROUP,
    BARRIER_GROUP,
    MICRO_GROUP,
    ContractGroupConsensus,
    GroupSignal,
    compute_all_group_signals,
    get_group_for_brain_type,
    get_group_for_proposal,
)

# ── Fake proposal for testing ──


@dataclass
class FakeProposal:
    """Minimal BrainDecisionProposal stand-in for tests."""

    brain_id: str = "B1"
    prediction: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    vote_weight: float = 1.0
    brain_type: str = ""


def _make_prop(
    up=0.6,
    down=0.4,
    conf=0.7,
    direction="long",
    fallback=False,
    weight=1.0,
    bid="B1",
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


# ── Group definitions ──


def test_barrier_group_definition():
    assert BARRIER_GROUP["name"] == "barrier_12bar"
    assert BARRIER_GROUP["horizon_cycles"] == 12
    assert "onnx_v9" in BARRIER_GROUP["brain_types"]
    assert "xgboost_v9" in BARRIER_GROUP["brain_types"]


def test_micro_group_definition():
    assert MICRO_GROUP["name"] == "micro_3bar"
    assert MICRO_GROUP["horizon_cycles"] == 3
    assert "xgboost_v4.5" in MICRO_GROUP["brain_types"]
    assert "transformer_v4.3" in MICRO_GROUP["brain_types"]


def test_arb_group_definition():
    assert ARB_GROUP["name"] == "statarb_dynamic"
    assert ARB_GROUP["horizon_cycles"] == 0
    assert "ou_params_v6" in ARB_GROUP["brain_types"]


def test_all_groups_disjoint():
    """No brain type belongs to multiple groups."""
    all_types: set[str] = set()
    for g in ALL_GROUPS:
        for bt in g["brain_types"]:
            assert bt not in all_types, f"{bt} appears in multiple groups"
            all_types.add(bt)


def test_get_group_for_brain_type_known():
    assert get_group_for_brain_type("onnx_v9") is not None
    assert get_group_for_brain_type("onnx_v9")["name"] == "barrier_12bar"
    assert get_group_for_brain_type("xgboost_v4.5")["name"] == "micro_3bar"
    assert get_group_for_brain_type("ou_params_v6")["name"] == "statarb_dynamic"


def test_get_group_for_brain_type_unknown():
    assert get_group_for_brain_type("nonexistent_v42") is None


def test_get_group_for_proposal_by_brain_type_attr():
    p = _make_prop()
    p.brain_type = "xgboost_v4.5"
    g = get_group_for_proposal(p)
    assert g is not None
    assert g["name"] == "micro_3bar"


def test_get_group_for_proposal_by_source():
    @dataclass
    class Source:
        brain_type: str = "onnx_v9"

    @dataclass
    class Prop:
        source: Any = None
        brain_type: str = ""
        metadata: dict[str, Any] = field(default_factory=dict)

    p = Prop(source=Source(), brain_type="")
    g = get_group_for_proposal(p)
    assert g is not None
    assert g["name"] == "barrier_12bar"


def test_get_group_for_proposal_by_metadata():
    @dataclass
    class PropWithMeta:
        brain_type: str = ""
        metadata: dict[str, Any] = field(default_factory=dict)

    p = PropWithMeta(metadata={"model_type": "ou_params_v6"})
    g = get_group_for_proposal(p)
    assert g is not None
    assert g["name"] == "statarb_dynamic"


def test_get_group_for_proposal_unknown():
    p = FakeProposal()
    g = get_group_for_proposal(p)
    assert g is None


# ── ContractGroupConsensus.compute ──


def test_compute_empty_proposals():
    c = ContractGroupConsensus(BARRIER_GROUP)
    assert c.compute([]) is None


def test_compute_single_long():
    c = ContractGroupConsensus(BARRIER_GROUP)
    result = c.compute([_make_prop(up=0.8, down=0.2, conf=0.9, direction="long")])
    assert result is not None
    assert result.direction == "long"
    assert 0.0 < result.confidence <= 1.0
    assert result.supporting_count == 1
    assert result.opposing_count == 0
    assert result.total_count == 1


def test_compute_single_short():
    c = ContractGroupConsensus(BARRIER_GROUP)
    result = c.compute([_make_prop(up=0.2, down=0.8, conf=0.9, direction="short")])
    assert result is not None
    assert result.direction == "short"
    assert result.supporting_count == 1


def test_compute_unanimous_long():
    c = ContractGroupConsensus(BARRIER_GROUP)
    props = [
        _make_prop(up=0.75, down=0.25, conf=0.8, direction="long", bid="B1"),
        _make_prop(up=0.70, down=0.30, conf=0.75, direction="long", bid="B2"),
        _make_prop(up=0.80, down=0.20, conf=0.85, direction="long", bid="B3"),
    ]
    result = c.compute(props)
    assert result is not None
    assert result.direction == "long"
    assert result.supporting_count == 3
    assert result.opposing_count == 0
    assert result.confidence > 0.5


def test_compute_majority_long_with_minority_neutral():
    c = ContractGroupConsensus(BARRIER_GROUP)
    props = [
        _make_prop(up=0.7, down=0.3, conf=0.8, direction="long", bid="B1"),
        _make_prop(up=0.6, down=0.4, conf=0.7, direction="long", bid="B2"),
        _make_prop(up=0.5, down=0.5, conf=0.5, direction="neutral", bid="B3"),
    ]
    result = c.compute(props)
    assert result is not None
    assert result.direction == "long"
    assert result.neutral_count == 1
    # Neutral penalty should apply
    assert result.confidence < 0.85


def test_compute_split_direction_picks_higher_weighted():
    """When weighted_up > weighted_down, direction is long even if more voters say short."""
    c = ContractGroupConsensus(BARRIER_GROUP)
    # One high-weight, high-confidence long; two low-weight shorts
    props = [
        _make_prop(up=0.90, down=0.10, conf=0.95, direction="long", weight=3.0, bid="B1"),
        _make_prop(up=0.30, down=0.70, conf=0.5, direction="short", weight=0.5, bid="B2"),
        _make_prop(up=0.35, down=0.65, conf=0.5, direction="short", weight=0.5, bid="B3"),
    ]
    result = c.compute(props)
    assert result is not None
    # Weighted up ≈ 0.9*0.95*3 = 2.565, weighted down ≈ 0.7*0.5*0.5 + 0.65*0.5*0.5 = 0.3375
    # So direction should be long
    assert result.direction == "long"


def test_compute_fallback_reduces_weight():
    c = ContractGroupConsensus(BARRIER_GROUP)
    normal = _make_prop(up=0.8, down=0.2, conf=0.9, direction="long", fallback=False, bid="B1")
    fallen = _make_prop(up=0.2, down=0.8, conf=0.9, direction="short", fallback=True, bid="B2")
    # Normal weight: 1.0 * 0.9 * 1.0 = 0.9
    # Fallback weight: 1.0 * 0.9 * 0.5 = 0.45
    # So long should win
    result = c.compute([normal, fallen])
    assert result is not None
    assert result.direction == "long"


def test_compute_total_weight_zero():
    """When all confidences are 0, result should be None."""
    c = ContractGroupConsensus(BARRIER_GROUP)
    props = [
        _make_prop(up=0.6, down=0.4, conf=0.0, direction="long", bid="B1"),
        _make_prop(up=0.4, down=0.6, conf=0.0, direction="short", bid="B2"),
    ]
    assert c.compute(props) is None


def test_compute_brain_ids_collected():
    c = ContractGroupConsensus(BARRIER_GROUP)
    props = [
        _make_prop(up=0.7, down=0.3, conf=0.8, direction="long", bid="alpha"),
        _make_prop(up=0.6, down=0.4, conf=0.7, direction="long", bid="beta"),
    ]
    result = c.compute(props)
    assert result is not None
    assert set(result.brain_ids) == {"alpha", "beta"}


def test_compute_horizon_from_group():
    c = ContractGroupConsensus(BARRIER_GROUP)
    result = c.compute([_make_prop()])
    assert result is not None
    assert result.horizon_cycles == 12

    c2 = ContractGroupConsensus(MICRO_GROUP)
    result2 = c2.compute([_make_prop()])
    assert result2 is not None
    assert result2.horizon_cycles == 3


# ── compute_all_group_signals ──


def test_compute_all_group_signals_groups_correctly():
    """Proposals from different brain types land in correct groups."""
    brain_proposals = [
        (
            {"brain_type": "onnx_v9", "brain_id": "B_barrier"},
            _make_prop(up=0.7, down=0.3, conf=0.8, direction="long", bid="B_barrier"),
        ),
        (
            {"brain_type": "xgboost_v4.5", "brain_id": "B_micro"},
            _make_prop(up=0.4, down=0.6, conf=0.7, direction="short", bid="B_micro"),
        ),
        (
            {"brain_type": "ou_params_v6", "brain_id": "B_arb"},
            _make_prop(up=0.55, down=0.45, conf=0.6, direction="long", bid="B_arb"),
        ),
    ]
    result = compute_all_group_signals(brain_proposals)
    assert result["barrier_12bar"] is not None
    assert result["micro_3bar"] is not None
    assert result["statarb_dynamic"] is not None

    assert result["barrier_12bar"].direction == "long"
    assert result["micro_3bar"].direction == "short"
    assert result["statarb_dynamic"].direction == "long"


def test_compute_all_group_signals_empty_group_is_none():
    brain_proposals = [
        (
            {"brain_type": "onnx_v9", "brain_id": "B1"},
            _make_prop(up=0.7, down=0.3, conf=0.8, direction="long", bid="B1"),
        ),
    ]
    result = compute_all_group_signals(brain_proposals)
    assert result["barrier_12bar"] is not None
    assert result["micro_3bar"] is None
    assert result["statarb_dynamic"] is None


def test_compute_all_group_signals_unknown_brain_defaults_barrier():
    brain_proposals = [
        (
            {"brain_type": "unknown_type_123", "brain_id": "B_unknown"},
            _make_prop(up=0.7, down=0.3, conf=0.8, direction="long", bid="B_unknown"),
        ),
    ]
    result = compute_all_group_signals(brain_proposals)
    assert result["barrier_12bar"] is not None
    assert "B_unknown" in result["barrier_12bar"].brain_ids


def test_compute_all_group_signals_stamps_brain_id():
    """Proposals without brain_id get it stamped from brain_info."""
    p = FakeProposal(
        brain_id="",  # empty string triggers stamping
        prediction={
            "up_probability": 0.7,
            "down_probability": 0.3,
            "confidence": 0.8,
            "direction_bias": "long",
        },
    )
    brain_info = {"brain_type": "onnx_v9", "brain_id": "stamped_id"}
    result = compute_all_group_signals([(brain_info, p)])
    assert result["barrier_12bar"] is not None
    assert p.brain_id == "stamped_id"


def test_compute_all_group_signals_empty_list():
    result = compute_all_group_signals([])
    assert result["barrier_12bar"] is None
    assert result["micro_3bar"] is None
    assert result["statarb_dynamic"] is None


# ── GroupSignal dataclass ──


def test_group_signal_defaults():
    gs = GroupSignal(
        group_name="test",
        direction="long",
        confidence=0.75,
        consensus_score=0.75,
        supporting_count=3,
        opposing_count=0,
        neutral_count=1,
        total_count=4,
        horizon_cycles=12,
    )
    assert gs.brain_ids == []
    assert gs.direction == "long"
