"""Tests for shadow_decision_recorder module."""

import json
from datetime import UTC, datetime
from pathlib import Path

# ── _result_to_proposal tests ──


def test_result_to_proposal_ok():
    from scripts.shadow_decision_recorder import _result_to_proposal

    result = {
        "brain_id": "TestBrain",
        "brain_type": "onnx_v9",
        "status": "ok",
        "runtime_ms": 12.5,
        "direction_bias": "long",
        "up_probability": 0.75,
        "down_probability": 0.25,
        "confidence": 0.82,
        "backend": "onnxruntime",
    }
    now = datetime.now(UTC).replace(tzinfo=None)
    prop = _result_to_proposal(result, snapshot_id="s1", event_time=now)
    assert prop is not None
    assert prop.brain_id == "TestBrain"
    assert prop.prediction["direction_bias"] == "long"
    assert prop.prediction["confidence"] == 0.82
    assert prop.brain_status == "shadow"
    assert prop.vote_weight == 1.0


def test_result_to_proposal_error_status():
    from scripts.shadow_decision_recorder import _result_to_proposal

    result = {
        "brain_id": "BadBrain",
        "status": "error",
        "error": "load_failed",
    }
    now = datetime.now(UTC).replace(tzinfo=None)
    prop = _result_to_proposal(result, snapshot_id="s1", event_time=now)
    assert prop is None


def test_result_to_proposal_missing_status_is_none():
    from scripts.shadow_decision_recorder import _result_to_proposal

    result = {"brain_id": "X", "direction_bias": "long"}
    now = datetime.now(UTC).replace(tzinfo=None)
    prop = _result_to_proposal(result, snapshot_id="s1", event_time=now)
    assert prop is None


# ── _derive_action / _derive_side tests ──


def test_derive_action_long_short_are_open():
    from scripts.shadow_decision_recorder import _derive_action

    assert _derive_action("long") == "OPEN"
    assert _derive_action("short") == "OPEN"


def test_derive_action_neutral_split_are_abstain():
    from scripts.shadow_decision_recorder import _derive_action

    assert _derive_action("neutral") == "ABSTAIN"
    assert _derive_action("split") == "ABSTAIN"
    assert _derive_action("unknown") == "ABSTAIN"


def test_derive_side_mapping():
    from scripts.shadow_decision_recorder import _derive_side

    assert _derive_side("long") == "LONG"
    assert _derive_side("short") == "SHORT"
    assert _derive_side("neutral") == "FLAT"
    assert _derive_side("split") == "FLAT"


# ── record_shadow_from_ensemble tests ──


def test_record_shadow_from_ensemble(tmp_path: Path):
    from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
    from scripts.shadow_decision_recorder import record_shadow_from_ensemble

    store = JsonlLedgerStore(str(tmp_path))
    results = [
        {
            "brain_id": "Brain_A",
            "brain_type": "onnx_v9",
            "status": "ok",
            "direction_bias": "long",
            "up_probability": 0.8,
            "down_probability": 0.2,
            "confidence": 0.85,
        },
        {
            "brain_id": "Brain_B",
            "brain_type": "xgboost",
            "status": "ok",
            "direction_bias": "long",
            "up_probability": 0.7,
            "down_probability": 0.3,
            "confidence": 0.70,
        },
        {
            "brain_id": "Brain_C",
            "brain_type": "params",
            "status": "error",
            "error": "timeout",
        },
    ]
    consensus = {
        "consensus": "long",
        "total_brains": 2,
        "long_count": 2,
        "short_count": 0,
        "neutral_count": 0,
        "agreement_score": 1.0,
    }

    result = record_shadow_from_ensemble(
        results=results,
        consensus=consensus,
        symbol="XAUUSD",
        store=store,
    )

    assert result["written"] is True
    assert result["brain_count"] == 2  # only 2 valid (Brain_C is error)
    assert result["record_id"].startswith("record_")

    # Read back the written file
    date_key = datetime.now(UTC).replace(tzinfo=None).date().isoformat()
    file_path = tmp_path / "decisions" / date_key / "XAUUSD.decisions.jsonl"
    assert file_path.exists()

    lines = file_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["schema_version"] == "decision_record.v1"
    assert record["execution"]["dispatch_status"] == "shadow"
    assert record["trace"]["source"] == "shadow_ensemble"
    assert record["trace"]["brain_count"] == 2
    assert record["context"]["symbol"] == "XAUUSD"
    assert record["context"]["venue"] == "shadow"
    assert "Brain_A" in record["attribution"]["supporting_brains"]
    assert "Brain_B" in record["attribution"]["supporting_brains"]


def test_record_shadow_from_ensemble_no_valid_results(tmp_path: Path):
    from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
    from scripts.shadow_decision_recorder import record_shadow_from_ensemble

    store = JsonlLedgerStore(str(tmp_path))
    results = [
        {"brain_id": "A", "status": "error", "error": "x"},
        {"brain_id": "B", "status": "error", "error": "y"},
    ]
    consensus = {"consensus": "no_results"}

    result = record_shadow_from_ensemble(
        results=results, consensus=consensus, symbol="XAUUSD", store=store
    )
    assert result["written"] is False
    assert result["brain_count"] == 0
    assert result["reason"] == "no_valid_proposals"


def test_record_shadow_from_ensemble_creates_default_store(tmp_path: Path, monkeypatch):
    """When store is None, creates one with 'data' dir."""
    from scripts.shadow_decision_recorder import record_shadow_from_ensemble

    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    results = [
        {
            "brain_id": "Brain_X",
            "status": "ok",
            "direction_bias": "short",
            "up_probability": 0.3,
            "down_probability": 0.7,
            "confidence": 0.65,
        },
    ]
    consensus = {"consensus": "short"}

    result = record_shadow_from_ensemble(results=results, consensus=consensus)
    assert result["written"] is True
    assert (cwd / "data").is_dir()


# ── record_shadow_from_proposals tests ──


def test_record_shadow_from_proposals(tmp_path: Path):
    from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal
    from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
    from scripts.shadow_decision_recorder import record_shadow_from_proposals

    store = JsonlLedgerStore(str(tmp_path))
    now = datetime.now(UTC).replace(tzinfo=None)

    p1 = BrainDecisionProposal(
        schema_version="brain_decision_proposal.v1",
        proposal_id="prop_1",
        snapshot_id="snap_1",
        brain_id="V9",
        brain_role="alpha_brain",
        brain_status="shadow",
        model_version="v9",
        event_time=now,
        generated_at=now,
        prediction={
            "direction_bias": "long",
            "up_probability": 0.75,
            "down_probability": 0.25,
            "confidence": 0.82,
        },
        health={"risk_score": 0.3, "fallback_used": False},
        vote_weight=1.0,
    )
    p2 = BrainDecisionProposal(
        schema_version="brain_decision_proposal.v1",
        proposal_id="prop_2",
        snapshot_id="snap_1",
        brain_id="XGB",
        brain_role="alpha_brain",
        brain_status="shadow",
        model_version="v4.5",
        event_time=now,
        generated_at=now,
        prediction={
            "direction_bias": "short",
            "up_probability": 0.4,
            "down_probability": 0.6,
            "confidence": 0.65,
        },
        health={"risk_score": 0.3, "fallback_used": False},
        vote_weight=0.8,
    )

    consensus = {
        "aggregated_bias": "long",
        "consensus_score": 0.75,
        "voter_count": 2,
        "majority_ratio": 0.5,
        "disagreement_score": 0.2,
    }

    result = record_shadow_from_proposals(
        proposals=[p1, p2],
        consensus=consensus,
        symbol="XAUUSD",
        store=store,
        dispatch_status="shadow_verify",
    )

    assert result["written"] is True
    assert result["brain_count"] == 2

    date_key = now.date().isoformat()
    file_path = tmp_path / "decisions" / date_key / "XAUUSD.decisions.jsonl"
    assert file_path.exists()

    record = json.loads(file_path.read_text(encoding="utf-8"))
    assert record["execution"]["dispatch_status"] == "shadow_verify"
    assert record["trace"]["source"] == "shadow_verify"
    assert record["attribution"]["supporting_brains"] == ["V9"]
    assert record["attribution"]["opposing_brains"] == ["XGB"]


def test_record_shadow_from_proposals_empty(tmp_path: Path):
    from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
    from scripts.shadow_decision_recorder import record_shadow_from_proposals

    store = JsonlLedgerStore(str(tmp_path))
    result = record_shadow_from_proposals(proposals=[], consensus={}, symbol="XAUUSD", store=store)
    assert result["written"] is False
    assert result["reason"] == "no_proposals"


def test_record_shadow_writes_to_correct_path(tmp_path: Path):
    from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal
    from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
    from scripts.shadow_decision_recorder import record_shadow_from_proposals

    store = JsonlLedgerStore(str(tmp_path))
    now = datetime.now(UTC).replace(tzinfo=None)

    p = BrainDecisionProposal(
        schema_version="v1",
        proposal_id="p1",
        snapshot_id="s1",
        brain_id="Test",
        brain_role="alpha",
        brain_status="shadow",
        model_version="v1",
        event_time=now,
        generated_at=now,
        prediction={
            "direction_bias": "long",
            "up_probability": 0.8,
            "down_probability": 0.2,
            "confidence": 0.7,
        },
    )

    result = record_shadow_from_proposals(
        proposals=[p],
        consensus={"aggregated_bias": "long", "consensus_score": 0.7},
        symbol="XAUUSD",
        store=store,
    )

    date_str = now.date().isoformat()
    expected_path = tmp_path / "decisions" / date_str / "XAUUSD.decisions.jsonl"
    assert expected_path.exists()
    assert result["path"] == str(expected_path)
    assert "record_id" in result
