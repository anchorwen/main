"""Tests for contract_groups.py — per-group consensus computation."""

from dataclasses import dataclass, field
from typing import Any

from core.parliament.contract_groups import (
    ALL_GROUPS,
    ARB_GROUP,
    BARRIER_GROUP,
    MICRO_GROUP,
    MICRO_H1_GROUP,
    MICRO_M15_GROUP,
    ConsensusResult,
    ContractGroupConsensus,
    compute_all_group_signals,
    get_group_for_brain_type,
    get_group_for_proposal,
)

# ── Fake proposal for testing ──


@dataclass
class FakeProposal:
    """Minimal BrainSignal-compatible stand-in for tests.

    Uses the same attribute names as BrainSignal so that consensus
    methods consume this identically — no dict nesting.
    """

    brain_id: str = "B1"
    direction: str = "neutral"
    confidence: float = 0.5
    raw_score: float = 0.0
    fallback: bool = False
    runtime_ms: float = 0.0
    vote_weight: float = 1.0

    # Legacy attributes for backward-compat tests
    brain_type: str = ""
    prediction: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)


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
        direction=direction,
        confidence=conf,
        raw_score=max(up, down),
        fallback=fallback,
        vote_weight=weight,
        prediction={
            "up_probability": up,
            "down_probability": down,
            "confidence": conf,
            "direction_bias": direction,
        },
        health={"fallback_used": fallback},
    )


# ── Group definitions ──


def test_barrier_group_definition():
    assert BARRIER_GROUP["name"] == "barrier_12bar"
    assert BARRIER_GROUP["horizon_cycles"] == 12
    assert "lightgbm_v1" in BARRIER_GROUP["brain_types"]
    # Dictator Protocol (2026-05-22): onnx_v9 + online_sgd evicted.
    # FIX-20260530-073: xgboost_v9 restored for barrier brain recovery.
    assert "onnx_v9" not in BARRIER_GROUP["brain_types"]
    assert "online_sgd" not in BARRIER_GROUP["brain_types"]
    assert "xgboost_v9" in BARRIER_GROUP["brain_types"]


def test_micro_group_definition():
    assert MICRO_GROUP["name"] == "micro_3bar"
    assert MICRO_GROUP["horizon_cycles"] == 8  # extended to match widened SL (1.0→2.0 ATR)
    assert "xgboost_v4.5" in MICRO_GROUP["brain_types"]
    assert "transformer_v4.3" in MICRO_GROUP["brain_types"]


def test_arb_group_definition():
    assert ARB_GROUP["name"] == "statarb_dynamic"
    assert ARB_GROUP["horizon_cycles"] == 0
    assert "ou_params_v6" in ARB_GROUP["brain_types"]


def test_all_groups_disjoint():
    """Brain types may appear in multiple groups (e.g. ou_params_v6 in both
    statarb_dynamic M5 and statarb_m15 M15).  Legacy _TYPE_TO_GROUP lookup
    returns the first match, but routing now uses contract_group."""
    # Verify no unexpected duplicates (same brain_type in same group name)
    seen_pairs: set[tuple[str, str]] = set()
    for g in ALL_GROUPS:
        for bt in g["brain_types"]:
            pair = (bt, g["name"])
            assert pair not in seen_pairs, f"{bt} duplicated within {g['name']}"
            seen_pairs.add(pair)


def test_get_group_for_brain_type_known():
    # xgboost_v9 and lightgbm_v1 appear in multiple groups; _TYPE_TO_GROUP
    # returns last-write-wins.  Preferred: contract_group + get_group_for_contract_group().
    g = get_group_for_brain_type("xgboost_v9")
    assert g is not None
    assert g["name"] in (
        "barrier_12bar",
        "btc_swing",
        "daily_swing",
        "m30_swing",
        "h1_swing",
        "h1_directional",
        "h4_swing",
    )
    g45 = get_group_for_brain_type("xgboost_v4.5")
    assert g45 is not None
    assert g45["name"] == "micro_3bar"
    # Legacy _TYPE_TO_GROUP returns last-write-wins (statarb_m15 overwrites statarb_dynamic
    # for ou_params_v6).  Preferred: use contract_group field + get_group_for_contract_group().
    g_ou = get_group_for_brain_type("ou_params_v6")
    assert g_ou is not None
    assert g_ou["name"] in ("statarb_dynamic", "statarb_m15")


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
        brain_type: str = "xgboost_v9"

    @dataclass
    class Prop:
        source: Any = None
        brain_type: str = ""
        metadata: dict[str, Any] = field(default_factory=dict)

    p = Prop(source=Source(), brain_type="")
    g = get_group_for_proposal(p)
    assert g is not None
    # brain_type="xgboost_v9" appears in multiple groups; _TYPE_TO_GROUP
    # returns last-write-wins.  Prefer contract_group for disambiguation.
    assert g["name"] in (
        "barrier_12bar",
        "btc_swing",
        "daily_swing",
        "m30_swing",
        "h1_swing",
        "h1_directional",
        "h4_swing",
    )


def test_get_group_for_proposal_by_metadata():
    @dataclass
    class PropWithMeta:
        brain_type: str = ""
        contract_group: str = ""
        metadata: dict[str, Any] = field(default_factory=dict)

    # Preferred path: contract_group attribute
    p = PropWithMeta(contract_group="statarb_dynamic", metadata={"model_type": "ou_params_v6"})
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
    assert len(result.dissenting_brains) == 0
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
    assert len(result.dissenting_brains) == 0
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
    assert len(result.dissenting_brains) == 0
    # One neutral = 2 supporting + 0 dissenting out of 3 total
    assert result.supporting_count == 2
    assert result.total_count == 3
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


def test_compute_group_respects_different_groups():
    c = ContractGroupConsensus(BARRIER_GROUP)
    result = c.compute([_make_prop()])
    assert result is not None
    assert result.direction in ("long", "short", "neutral")

    c2 = ContractGroupConsensus(MICRO_GROUP)
    result2 = c2.compute([_make_prop()])
    assert result2 is not None
    assert result2.direction in ("long", "short", "neutral")


# ── compute_all_group_signals ──


def test_compute_all_group_signals_groups_correctly():
    """Proposals from different brain types land in correct groups."""
    brain_proposals = [
        (
            {
                "brain_type": "xgboost_v9",
                "contract_group": "barrier_12bar",
                "brain_id": "B_barrier",
            },
            _make_prop(up=0.7, down=0.3, conf=0.8, direction="long", bid="B_barrier"),
        ),
        (
            {"brain_type": "xgboost_v4.5", "contract_group": "micro_3bar", "brain_id": "B_micro"},
            _make_prop(up=0.4, down=0.6, conf=0.7, direction="short", bid="B_micro"),
        ),
        (
            {
                "brain_type": "ou_params_v6",
                "contract_group": "statarb_dynamic",
                "brain_id": "B_arb",
            },
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
            {"brain_type": "xgboost_v9", "contract_group": "barrier_12bar", "brain_id": "B1"},
            _make_prop(up=0.7, down=0.3, conf=0.8, direction="long", bid="B1"),
        ),
    ]
    result = compute_all_group_signals(brain_proposals)
    assert result["barrier_12bar"] is not None
    assert result["micro_3bar"] is None
    assert result["micro_m15"] is None
    assert result["micro_h1"] is None
    assert result["micro_h4"] is None
    assert result["statarb_dynamic"] is None


def test_compute_all_group_signals_unknown_contract_group_skipped():
    """Brains with unknown/unset contract_group are skipped (not silently routed)."""
    brain_proposals = [
        (
            {"brain_type": "unknown_type_123", "brain_id": "B_unknown"},
            _make_prop(up=0.7, down=0.3, conf=0.8, direction="long", bid="B_unknown"),
        ),
    ]
    result = compute_all_group_signals(brain_proposals)
    # All groups should be None since the unknown brain is skipped
    for g_name in ("barrier_12bar", "micro_3bar", "statarb_dynamic"):
        assert result[g_name] is None, f"{g_name} should be None for unknown brain"


def test_compute_all_group_signals_routes_by_contract_group():
    """Proposals are routed to the correct group via brain_info contract_group,
    not by brain_type fallback.  BrainSignal carries its own brain_id
    from the adapter — no stamping needed."""
    p = FakeProposal(
        brain_id="B_adaptive",  # brain_id already set by adapter
        direction="long",
        confidence=0.8,
        raw_score=0.7,
    )
    brain_info = {
        "brain_type": "onnx_v9",
        "contract_group": "barrier_12bar",
        "brain_id": "B_adaptive",
    }
    result = compute_all_group_signals([(brain_info, p)])
    assert result["barrier_12bar"] is not None
    assert result["barrier_12bar"].brain_ids == ["B_adaptive"]


def test_compute_all_group_signals_empty_list():
    result = compute_all_group_signals([])
    assert result["barrier_12bar"] is None
    assert result["micro_3bar"] is None
    assert result["micro_m15"] is None
    assert result["micro_h1"] is None
    assert result["micro_h4"] is None
    assert result["statarb_dynamic"] is None


# ── ConsensusResult dataclass ──


def test_consensus_result_defaults():
    gs = ConsensusResult(
        direction="long",
        confidence=0.75,
        supporting_brains=["B1", "B2", "B3"],
        dissenting_brains=[],
        supporting_count=3,
        total_count=4,
    )
    assert gs.brain_ids == []
    assert gs.direction == "long"
    assert gs.supporting_brains == ["B1", "B2", "B3"]


# ── Union Ensemble voting ──────────────────────────────────────────────


def test_union_voting_mode_on_micro_groups():
    """All three active micro groups specify voting_mode: union."""
    assert MICRO_GROUP.get("voting_mode") == "union"
    assert MICRO_M15_GROUP.get("voting_mode") == "union"
    assert MICRO_H1_GROUP.get("voting_mode") == "union"


def test_union_barrier_still_uses_weighted():
    """Barrier group stays on weighted-average voting (no union key)."""
    assert "voting_mode" not in BARRIER_GROUP
    # Weighted-average is the default — verify it still works
    c = ContractGroupConsensus(BARRIER_GROUP)
    result = c.compute([_make_prop(up=0.7, down=0.3, conf=0.8, direction="long")])
    assert result is not None
    assert result.direction == "long"


def test_union_single_long_activates():
    """One long brain → group signals long."""
    c = ContractGroupConsensus(MICRO_M15_GROUP)
    result = c.compute([_make_prop(up=0.75, down=0.25, conf=0.8, direction="long", bid="XGB_M15")])
    assert result is not None
    assert result.direction == "long"
    assert result.supporting_count == 1
    assert len(result.dissenting_brains) == 0
    assert result.confidence >= 0.5


def test_union_single_short_activates():
    """One short brain → group signals short."""
    c = ContractGroupConsensus(MICRO_M15_GROUP)
    result = c.compute([_make_prop(up=0.25, down=0.75, conf=0.8, direction="short", bid="TF_M15")])
    assert result is not None
    assert result.direction == "short"


def test_union_xgboost_long_transformer_neutral():
    """XGBoost long + Transformer neutral → union signals long."""
    c = ContractGroupConsensus(MICRO_M15_GROUP)
    props = [
        _make_prop(up=0.70, down=0.30, conf=0.75, direction="long", bid="XGB_M15"),
        _make_prop(up=0.50, down=0.50, conf=0.45, direction="neutral", bid="TF_M15"),
    ]
    result = c.compute(props)
    assert result is not None
    assert result.direction == "long"
    assert result.supporting_count == 1
    assert len(result.dissenting_brains) == 0
    assert result.total_count == 2
    # One neutral = 1 supporting + 0 dissenting out of 2 total


def test_union_xgboost_short_transformer_long():
    """Conflicting directions: aggregate up/down breaks tie."""
    c = ContractGroupConsensus(MICRO_M15_GROUP)
    # XGBoost short 0.6, TF long 0.7 → aggregate up > down → long wins
    props = [
        _make_prop(up=0.30, down=0.70, conf=0.80, direction="short", bid="XGB_M15"),
        _make_prop(up=0.75, down=0.25, conf=0.85, direction="long", bid="TF_M15"),
    ]
    result = c.compute(props)
    assert result is not None
    # avg_up = (0.30+0.75)/2 = 0.525, avg_down = (0.70+0.25)/2 = 0.475 → long
    assert result.direction == "long"
    assert result.supporting_count == 1  # only TF_M15 agrees
    assert len(result.dissenting_brains) == 1  # XGB_M15 dissents


def test_union_all_neutral():
    """All neutral brains → neutral signal."""
    c = ContractGroupConsensus(MICRO_GROUP)
    props = [
        _make_prop(up=0.50, down=0.50, conf=0.4, direction="neutral", bid="XGB"),
        _make_prop(up=0.50, down=0.50, conf=0.4, direction="neutral", bid="TF"),
    ]
    result = c.compute(props)
    assert result is not None
    assert result.direction == "neutral"
    # All-neutral now returns dampened avg brain confidence (0.4 * 0.55 = 0.22)
    # rather than hardcoded 0.0, so confidence-drop exits are proportional.
    assert 0.0 < result.confidence < 0.35
    assert result.supporting_count == 0


def test_union_multi_agreement_confidence_boost():
    """Two agreeing brains → confidence boost over single."""
    c = ContractGroupConsensus(MICRO_M15_GROUP)
    # Single brain baseline
    single = c.compute([_make_prop(up=0.70, down=0.30, conf=0.70, direction="long", bid="XGB")])
    assert single is not None
    # Two agreeing brains
    duo = c.compute(
        [
            _make_prop(up=0.70, down=0.30, conf=0.70, direction="long", bid="XGB"),
            _make_prop(up=0.65, down=0.35, conf=0.72, direction="long", bid="TF"),
        ]
    )
    assert duo is not None
    # Duo confidence should be higher (agreement bonus applied)
    assert duo.confidence > single.confidence
    assert duo.supporting_count == 2


def test_union_with_opposing_penalty():
    """One agreeing + one opposing → confidence lower than clean agreement."""
    c = ContractGroupConsensus(MICRO_M15_GROUP)
    # Clean agreement
    clean = c.compute(
        [
            _make_prop(up=0.70, down=0.30, conf=0.70, direction="long", bid="XGB"),
            _make_prop(up=0.65, down=0.35, conf=0.72, direction="long", bid="TF"),
        ]
    )
    # One long, one short → opposition penalty
    opposed = c.compute(
        [
            _make_prop(up=0.70, down=0.30, conf=0.70, direction="long", bid="XGB"),
            _make_prop(up=0.40, down=0.60, conf=0.72, direction="short", bid="TF"),
        ]
    )
    assert clean is not None
    assert opposed is not None
    assert opposed.confidence < clean.confidence


def test_union_confidence_bounded():
    """Union confidence stays within [0.35, 0.95]."""
    c = ContractGroupConsensus(MICRO_GROUP)
    # Very high confidence brains
    high = c.compute(
        [
            _make_prop(up=0.99, down=0.01, conf=0.99, direction="long", bid="XGB"),
            _make_prop(up=0.98, down=0.02, conf=0.98, direction="long", bid="TF"),
            _make_prop(up=0.97, down=0.03, conf=0.97, direction="long", bid="TF2"),
        ]
    )
    assert high is not None
    assert high.confidence <= 0.95

    # Very low confidence, all neutral
    low_neutral = c.compute(
        [
            _make_prop(up=0.50, down=0.50, conf=0.2, direction="neutral", bid="XGB"),
        ]
    )
    assert low_neutral is not None
    assert low_neutral.direction == "neutral"
    # All-neutral now returns dampened avg confidence (0.2 * 0.55 = 0.11)
    assert 0.0 < low_neutral.confidence < 0.35  # neutral → dampened, not zero

    # Low confidence but with direction
    low_dir = c.compute(
        [
            _make_prop(up=0.55, down=0.45, conf=0.30, direction="long", bid="XGB"),
        ]
    )
    assert low_dir is not None
    assert low_dir.confidence >= 0.35  # clamped floor


def test_compute_all_group_signals_micro_uses_union():
    """compute_all_group_signals routes micro groups through union voting."""
    brain_proposals = [
        (
            {
                "brain_type": "xgboost_v4.5_m15",
                "contract_group": "micro_m15",
                "brain_id": "XGB_M15",
            },
            _make_prop(up=0.75, down=0.25, conf=0.80, direction="long", bid="XGB_M15"),
        ),
        (
            {
                "brain_type": "transformer_v5_m15",
                "contract_group": "micro_m15",
                "brain_id": "TF_M15",
            },
            _make_prop(up=0.55, down=0.45, conf=0.50, direction="neutral", bid="TF_M15"),
        ),
    ]
    result = compute_all_group_signals(brain_proposals)
    # micro_m15 should have a union result (XGB long activates the group)
    assert result["micro_m15"] is not None
    assert result["micro_m15"].direction == "long"
    # XGB + 1 neutral
    assert result["micro_m15"].supporting_count == 1
    assert len(result["micro_m15"].dissenting_brains) == 0
    assert result["micro_m15"].total_count == 2  # 1 supporting + 1 neutral
    # Other groups empty
    assert result["barrier_12bar"] is None


def test_micro_m15_h1_have_voting_mode():
    """Verify new M15/H1 micro groups are defined correctly."""
    assert MICRO_M15_GROUP["name"] == "micro_m15"
    assert MICRO_M15_GROUP["horizon_cycles"] == 5
    assert "xgboost_v4.5_m15" in MICRO_M15_GROUP["brain_types"]
    assert "transformer_v5_m15" in MICRO_M15_GROUP["brain_types"]

    assert MICRO_H1_GROUP["name"] == "micro_h1"
    assert MICRO_H1_GROUP["horizon_cycles"] == 4
    assert "xgboost_v4.5_h1" in MICRO_H1_GROUP["brain_types"]
    assert "transformer_v5_h1" in MICRO_H1_GROUP["brain_types"]
