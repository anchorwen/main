"""Multi-brain joint decision weight contract tests."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

# ── _resolve_consensus_side tests ──


def test_resolve_consensus_side_long():
    from scripts.live_intent_loop import _resolve_consensus_side

    consensus = {"aggregated_bias": "long", "consensus_score": 0.65}
    assert _resolve_consensus_side(consensus, 0.55) == "long"


def test_resolve_consensus_side_short():
    from scripts.live_intent_loop import _resolve_consensus_side

    consensus = {"aggregated_bias": "short", "consensus_score": 0.72}
    assert _resolve_consensus_side(consensus, 0.55) == "short"


def test_resolve_consensus_side_neutral():
    from scripts.live_intent_loop import _resolve_consensus_side

    consensus = {"aggregated_bias": "neutral", "consensus_score": 0.80}
    assert _resolve_consensus_side(consensus, 0.55) is None


def test_resolve_consensus_side_low_confidence():
    from scripts.live_intent_loop import _resolve_consensus_side

    consensus = {"aggregated_bias": "long", "consensus_score": 0.45}
    assert _resolve_consensus_side(consensus, 0.55) is None


def test_resolve_consensus_side_custom_threshold():
    from scripts.live_intent_loop import _resolve_consensus_side

    consensus = {"aggregated_bias": "short", "consensus_score": 0.60}
    assert _resolve_consensus_side(consensus, 0.70) is None
    assert _resolve_consensus_side(consensus, 0.50) == "short"


# ── _load_brain_entries_from_dir tests ──


def test_load_brain_entries_from_dir(tmp_path: Path):
    from scripts.live_intent_loop import _load_brain_entries_from_dir

    for bid in ("Brain_A", "Brain_B"):
        entry = {
            "schema_version": "brain_registry_entry.v1",
            "brain_id": bid,
            "brain_type": "onnx_v9",
            "brain_role": "alpha_brain",
            "model_version": "v1.0",
            "status": "shadow",
            "artifact_path": str(tmp_path / f"{bid}.onnx"),
            "feature_schema_id": "v9_institutional_40",
        }
        (tmp_path / f"{bid}.json").write_text(json.dumps(entry), encoding="utf-8")

    entries = _load_brain_entries_from_dir(str(tmp_path))
    assert len(entries) == 2
    assert {e["brain_id"] for e in entries} == {"Brain_A", "Brain_B"}


def test_load_brain_entries_from_dir_skips_normalization(tmp_path: Path):
    from scripts.live_intent_loop import _load_brain_entries_from_dir

    entry = {
        "schema_version": "brain_registry_entry.v1",
        "brain_id": "Test",
        "brain_type": "onnx_v9",
        "brain_role": "alpha_brain",
        "model_version": "v1.0",
        "status": "shadow",
        "artifact_path": str(tmp_path / "m.onnx"),
        "feature_schema_id": "v9",
    }
    (tmp_path / "Test.json").write_text(json.dumps(entry), encoding="utf-8")
    (tmp_path / "Test.normalization.json").write_text(
        json.dumps({"mean": [0.0], "std": [1.0]}), encoding="utf-8"
    )

    entries = _load_brain_entries_from_dir(str(tmp_path))
    assert len(entries) == 1


def test_load_brain_entries_from_dir_not_found():
    from scripts.live_intent_loop import _load_brain_entries_from_dir

    with pytest.raises(FileNotFoundError):
        _load_brain_entries_from_dir("/nonexistent/brains")


def test_load_brain_entries_from_dir_empty(tmp_path: Path):
    from scripts.live_intent_loop import _load_brain_entries_from_dir

    with pytest.raises(FileNotFoundError, match="brain_registry_entry"):
        _load_brain_entries_from_dir(str(tmp_path))


# ── BrainDecisionProposal vote_weight tests ──


def test_vote_weight_proposal_roundtrip():
    from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal

    now = datetime.now(UTC).replace(tzinfo=None)
    proposal = BrainDecisionProposal(
        schema_version="brain_decision_proposal.v1",
        proposal_id="p1",
        snapshot_id="s1",
        brain_id="TestBrain",
        brain_role="alpha_brain",
        brain_status="shadow",
        model_version="v1.0",
        event_time=now,
        generated_at=now,
        prediction={"direction_bias": "long", "confidence": 0.72},
        vote_weight=2.0,
    )
    d = proposal.to_dict()
    assert d["vote_weight"] == 2.0
    assert d["brain_id"] == "TestBrain"


def test_vote_weight_defaults_to_one():
    from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal

    now = datetime.now(UTC).replace(tzinfo=None)
    proposal = BrainDecisionProposal(
        schema_version="brain_decision_proposal.v1",
        proposal_id="p2",
        snapshot_id="s2",
        brain_id="TestBrain",
        brain_role="alpha_brain",
        brain_status="shadow",
        model_version="v1.0",
        event_time=now,
        generated_at=now,
    )
    assert proposal.vote_weight == 1.0
    d = proposal.to_dict()
    assert d["vote_weight"] == 1.0


# ── ParliamentService _compute_consensus vote_weight tests ──


def _make_proposal(brain_id, direction, up, down, confidence, vote_weight=1.0, fallback=False):
    from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal

    now = datetime.now(UTC).replace(tzinfo=None)
    return BrainDecisionProposal(
        schema_version="brain_decision_proposal.v1",
        proposal_id=f"p_{brain_id}",
        snapshot_id="s",
        brain_id=brain_id,
        brain_role="alpha_brain",
        brain_status="shadow",
        model_version="v1",
        event_time=now,
        generated_at=now,
        prediction={
            "direction_bias": direction,
            "up_probability": up,
            "down_probability": down,
            "confidence": confidence,
        },
        health={"fallback_used": fallback, "risk_score": 0.3},
        vote_weight=vote_weight,
    )


def test_vote_weight_scales_influence():
    from core.parliament.parliament_service import ParliamentService

    ps = ParliamentService()
    # Brain A: vote_weight=3.0, strong long
    # Brain B: vote_weight=1.0, strong short
    proposals = [
        _make_proposal("A", "long", 0.8, 0.2, 0.9, vote_weight=3.0),
        _make_proposal("B", "short", 0.2, 0.8, 0.9, vote_weight=1.0),
    ]
    consensus = ps._compute_consensus(proposals)
    assert consensus["aggregated_bias"] == "long"
    assert consensus["voter_count"] == 2


def test_vote_weight_equal_weights_follows_confidence():
    from core.parliament.parliament_service import ParliamentService

    ps = ParliamentService()
    # Both equal weight, B has higher confidence for short
    proposals = [
        _make_proposal("A", "long", 0.7, 0.3, 0.6, vote_weight=1.0),
        _make_proposal("B", "short", 0.2, 0.8, 0.9, vote_weight=1.0),
    ]
    consensus = ps._compute_consensus(proposals)
    assert consensus["aggregated_bias"] == "short"


def test_vote_weight_fallback_penalty():
    from core.parliament.parliament_service import ParliamentService

    ps = ParliamentService()
    # Brain A: weight=2.0, strong long, but fallback
    # Brain B: weight=1.0, moderate short, healthy
    proposals = [
        _make_proposal("A", "long", 0.9, 0.1, 0.9, vote_weight=2.0, fallback=True),
        _make_proposal("B", "short", 0.3, 0.7, 0.7, vote_weight=1.0),
    ]
    consensus = ps._compute_consensus(proposals)
    # A effective weight: 2.0 * 0.9 * 0.5 = 0.9
    # B effective weight: 1.0 * 0.7 * 1.0 = 0.7
    # A still wins despite fallback penalty due to higher static weight
    assert consensus["aggregated_bias"] == "long"


def test_vote_weight_default_uses_one():
    from core.parliament.parliament_service import ParliamentService

    ps = ParliamentService()
    # Two proposals without explicit vote_weight (should default to 1.0)
    now = datetime.now(UTC).replace(tzinfo=None)
    from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal

    p1 = BrainDecisionProposal(
        schema_version="v1",
        proposal_id="p1",
        snapshot_id="s",
        brain_id="A",
        brain_role="alpha",
        brain_status="shadow",
        model_version="v1",
        event_time=now,
        generated_at=now,
        prediction={
            "direction_bias": "long",
            "up_probability": 0.7,
            "down_probability": 0.3,
            "confidence": 0.6,
        },
    )
    p2 = BrainDecisionProposal(
        schema_version="v1",
        proposal_id="p2",
        snapshot_id="s",
        brain_id="B",
        brain_role="alpha",
        brain_status="shadow",
        model_version="v1",
        event_time=now,
        generated_at=now,
        prediction={
            "direction_bias": "short",
            "up_probability": 0.3,
            "down_probability": 0.7,
            "confidence": 0.8,
        },
    )
    consensus = ps._compute_consensus([p1, p2])
    assert consensus["voter_count"] == 2
    assert consensus["aggregated_bias"] == "short"  # higher confidence wins


def test_multi_brain_consensus_e2e():
    from core.parliament.parliament_service import ParliamentService

    ps = ParliamentService()
    proposals = [
        _make_proposal("V9", "long", 0.75, 0.25, 0.82, vote_weight=1.0),
        _make_proposal("XGB", "long", 0.60, 0.40, 0.65, vote_weight=0.8),
        _make_proposal("OU", "short", 0.40, 0.60, 0.55, vote_weight=0.5),
    ]
    consensus = ps._compute_consensus(proposals)
    assert consensus["voter_count"] == 3
    assert consensus["aggregated_bias"] == "long"
    assert consensus["consensus_score"] > 0.5


def test_multi_brain_consensus_neutral_when_empty():
    from core.parliament.parliament_service import ParliamentService

    ps = ParliamentService()
    consensus = ps._compute_consensus([])
    assert consensus["aggregated_bias"] == "neutral"
    assert consensus["voter_count"] == 0


# ── CLI flag tests ──


def test_multi_brain_cli_flag():
    from scripts.live_intent_loop import build_parser

    p = build_parser()
    args = p.parse_args(["--mt5-terminal-path", "/t", "--multi-brain"])
    assert args.multi_brain is True
    assert args.brains_dir == "configs/brains"


def test_multi_brain_cli_default():
    from scripts.live_intent_loop import build_parser

    p = build_parser()
    args = p.parse_args(["--mt5-terminal-path", "/t"])
    assert args.multi_brain is False


def test_multi_brain_cli_brains_dir():
    from scripts.live_intent_loop import build_parser

    p = build_parser()
    args = p.parse_args(["--mt5-terminal-path", "/t", "--brains-dir", "/custom/brains"])
    assert args.brains_dir == "/custom/brains"
