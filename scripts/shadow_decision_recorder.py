"""Lightweight shadow decision recorder for persisting ensemble inference results.

Writes DecisionRecords to data/decisions/ via JsonlLedgerStore, using the same
format as the live RuntimeLoop so brain_leaderboard.py can consume both.

Used by:
  - live_shadow_ensemble.py  (plain dict results → DecisionRecord)
  - live_intent_loop.py      (multi-brain + --no-mt5, BrainDecisionProposal → DecisionRecord)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal
from core.contracts.domain.decision_record import DecisionRecord
from core.contracts.ids import (
    new_intent_id,
    new_proposal_id,
    new_record_id,
    new_snapshot_id,
    new_verdict_id,
)
from core.ledger.schema_versions import SCHEMA_DECISION_RECORD
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore

SCHEMA_VERSION = "shadow_decision_recorder.v1"


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _result_to_proposal(
    result: dict[str, Any],
    snapshot_id: str,
    event_time: datetime,
) -> BrainDecisionProposal | None:
    """Convert a shadow ensemble result dict to a BrainDecisionProposal domain object.

    Returns None if result status is not "ok".
    """
    if result.get("status") != "ok":
        return None

    now = _utc_now()
    return BrainDecisionProposal(
        schema_version="brain_decision_proposal.v1",
        proposal_id=new_proposal_id(),
        snapshot_id=snapshot_id,
        brain_id=str(result.get("brain_id", "unknown")),
        brain_role="alpha_brain",
        brain_status="shadow",
        model_version="v1",
        event_time=event_time,
        generated_at=now,
        prediction={
            "direction_bias": result.get("direction_bias", "neutral"),
            "up_probability": float(result.get("up_probability", 0.5)),
            "down_probability": float(result.get("down_probability", 0.5)),
            "confidence": float(result.get("confidence", 0.0)),
        },
        health={"risk_score": 0.3, "fallback_used": False},
        vote_weight=1.0,
    )


def _derive_action(direction: str) -> str:
    """Map consensus direction to decision action."""
    if direction in ("long", "short"):
        return "OPEN"
    return "ABSTAIN"


def _derive_side(direction: str) -> str:
    """Map consensus direction to decision side."""
    if direction == "long":
        return "LONG"
    if direction == "short":
        return "SHORT"
    return "FLAT"


def _build_decision_record(
    *,
    event_time: datetime,
    symbol: str,
    proposal_ids: list[str],
    supporting_brains: list[str],
    opposing_brains: list[str],
    consensus: dict[str, Any],
    dispatch_status: str,
    source: str,
) -> DecisionRecord:
    now = _utc_now()
    direction = consensus.get("consensus", consensus.get("aggregated_bias", "neutral"))

    return DecisionRecord(
        schema_version=SCHEMA_DECISION_RECORD,
        record_id=new_record_id(),
        snapshot_id=new_snapshot_id(),
        intent_id=new_intent_id(),
        verdict_id=new_verdict_id(),
        event_time=event_time,
        recorded_at=now,
        context={
            "symbol": symbol,
            "venue": "shadow",
        },
        inputs={
            "proposal_ids": proposal_ids,
            "proposal_count": len(proposal_ids),
        },
        execution={
            "dispatch_status": dispatch_status,
        },
        outcome={},
        attribution={
            "supporting_brains": supporting_brains,
            "opposing_brains": opposing_brains,
            "consensus": consensus,
        },
        labels={
            "decision_action": _derive_action(direction),
            "decision_side": _derive_side(direction),
        },
        trace={
            "source": source,
            "brain_count": len(proposal_ids),
        },
    )


def record_shadow_from_ensemble(
    results: list[dict[str, Any]],
    consensus: dict[str, Any],
    *,
    symbol: str = "XAUUSD",
    store: JsonlLedgerStore | None = None,
    event_time: datetime | None = None,
) -> dict[str, Any]:
    """Convert shadow ensemble results → DecisionRecord and write via JsonlLedgerStore.

    Args:
        results: List of result dicts from _run_single_brain.
        consensus: Dict from _compare_directions.
        symbol: Trading symbol (stored as-is in context).
        store: JsonlLedgerStore instance. Created with data/ dir if None.
        event_time: Inference time. Defaults to now.

    Returns:
        Summary: {"record_id": ..., "brain_count": N, "written": bool, "path": str}
    """
    if store is None:
        store = JsonlLedgerStore("data")

    now = event_time or _utc_now()
    snapshot_id = new_snapshot_id()

    proposals: list[BrainDecisionProposal] = []
    for r in results:
        prop = _result_to_proposal(r, snapshot_id=snapshot_id, event_time=now)
        if prop is not None:
            proposals.append(prop)

    if not proposals:
        return {
            "record_id": "",
            "brain_count": 0,
            "written": False,
            "reason": "no_valid_proposals",
        }

    proposal_ids = [p.proposal_id for p in proposals]
    direction = consensus.get("consensus", consensus.get("aggregated_bias", "neutral"))
    supporting = [
        r.get("brain_id", "?")
        for r in results
        if r.get("status") == "ok" and r.get("direction_bias") == direction
    ]
    opposing = [
        r.get("brain_id", "?")
        for r in results
        if r.get("status") == "ok"
        and r.get("direction_bias") != direction
        and r.get("direction_bias") != "neutral"
        and direction != "split"
    ]

    record = _build_decision_record(
        event_time=now,
        symbol=symbol,
        proposal_ids=proposal_ids,
        supporting_brains=supporting,
        opposing_brains=opposing,
        consensus=consensus,
        dispatch_status="shadow",
        source="shadow_ensemble",
    )

    date_key = now.date().isoformat()
    path = store.append_record(date_key, symbol, record)
    return {
        "record_id": record.record_id,
        "brain_count": len(proposals),
        "written": True,
        "path": str(path),
    }


def record_shadow_from_proposals(
    proposals: list[Any],
    consensus: dict[str, Any],
    *,
    symbol: str = "XAUUSD",
    store: JsonlLedgerStore | None = None,
    event_time: datetime | None = None,
    dispatch_status: str = "shadow_verify",
) -> dict[str, Any]:
    """Create a DecisionRecord from existing BrainDecisionProposal objects.

    Used by live_intent_loop.py multi-brain + --no-mt5 path.

    Args:
        proposals: List of BrainDecisionProposal from brain.get_signal().
        consensus: Dict from parliament._compute_consensus().
        symbol: Trading symbol.
        store: JsonlLedgerStore instance.
        event_time: Inference time.
        dispatch_status: "shadow_verify" for --no-mt5 dry-run.

    Returns:
        Summary dict with record_id, brain_count, written, path.
    """
    if store is None:
        store = JsonlLedgerStore("data")

    now = event_time or _utc_now()
    if not proposals:
        return {"record_id": "", "brain_count": 0, "written": False, "reason": "no_proposals"}

    proposal_ids = [p.proposal_id for p in proposals]
    bias = consensus.get("aggregated_bias", "neutral")
    supporting = [p.brain_id for p in proposals if p.prediction.get("direction_bias") == bias]
    opposing = [
        p.brain_id
        for p in proposals
        if p.prediction.get("direction_bias") != bias
        and p.prediction.get("direction_bias") != "neutral"
    ]

    record = _build_decision_record(
        event_time=now,
        symbol=symbol,
        proposal_ids=proposal_ids,
        supporting_brains=supporting,
        opposing_brains=opposing,
        consensus=consensus,
        dispatch_status=dispatch_status,
        source="shadow_verify",
    )

    date_key = now.date().isoformat()
    path = store.append_record(date_key, symbol, record)
    return {
        "record_id": record.record_id,
        "brain_count": len(proposals),
        "written": True,
        "path": str(path),
    }
