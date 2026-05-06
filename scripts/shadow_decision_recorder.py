"""Shadow decision recorder: persist inference results to ledger for brain leaderboard.

Writes DecisionRecord entries to data/decisions/{date}/XAUUSD.decisions.jsonl
so that brain_leaderboard and feedback_loop can consume them.

Usage:
  from scripts.shadow_decision_recorder import record_shadow_from_ensemble
  store = JsonlLedgerStore("data")
  record_shadow_from_ensemble(results, consensus, symbol="XAUUSD", store=store)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal
from core.contracts.domain.decision_record import DecisionRecord
from core.contracts.enums import BrainRole, BrainStatus
from core.contracts.ids import (
    new_intent_id,
    new_proposal_id,
    new_record_id,
    new_snapshot_id,
    new_verdict_id,
)
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore

SCHEMA_VERSION = "decision_record.v1"


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _direction_to_side(direction: str) -> str:
    """Normalize direction strings to canonical decision_side: LONG/SHORT/FLAT."""
    d = direction.lower()
    if d in ("up", "long"):
        return "LONG"
    if d in ("down", "short"):
        return "SHORT"
    return "FLAT"


def _derive_action(consensus: str | dict[str, Any]) -> str:
    """Derive decision_action: OPEN when direction is long/short, ABSTAIN otherwise."""
    if isinstance(consensus, dict):
        c = consensus.get("consensus") or consensus.get("aggregated_bias", "no_results")
    else:
        c = str(consensus)
    if c in ("split", "no_results", "neutral"):
        return "ABSTAIN"
    if c in ("long", "short"):
        return "OPEN"
    return "ABSTAIN"


def _derive_side(consensus: str | dict[str, Any]) -> str:
    """Derive decision_side from consensus direction."""
    if isinstance(consensus, dict):
        c = consensus.get("consensus") or consensus.get("aggregated_bias", "no_results")
    else:
        c = str(consensus)
    return _direction_to_side(c)


def _result_to_proposal(
    result: dict[str, Any], snapshot_id: str, event_time: datetime
) -> BrainDecisionProposal | None:
    """Convert a shadow result dict → BrainDecisionProposal. Returns None if status != 'ok'."""
    if result.get("status") != "ok":
        return None

    return BrainDecisionProposal(
        schema_version="brain_decision_proposal.v1",
        proposal_id=new_proposal_id(),
        snapshot_id=snapshot_id,
        brain_id=result.get("brain_id", "unknown"),
        brain_role=BrainRole.ALPHA,
        brain_status=BrainStatus.SHADOW,
        model_version=result.get("brain_type", "unknown"),
        event_time=event_time,
        generated_at=event_time,
        prediction={
            "direction_bias": result.get("direction_bias", "neutral"),
            "up_probability": result.get("up_probability", 0.5),
            "down_probability": result.get("down_probability", 0.5),
            "confidence": result.get("confidence", 0.0),
        },
        health={"runtime_ms": result.get("runtime_ms", 0.0)},
        vote_weight=1.0,
    )


def _group_by_direction(results: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Partition brain_ids into supporting (majority) and opposing (minority) groups."""
    counts: dict[str, list[str]] = {}
    for r in results:
        if r.get("status") != "ok":
            continue
        d = _direction_to_side(r.get("direction_bias", "neutral"))
        counts.setdefault(d, []).append(r["brain_id"])

    if not counts:
        return [], []

    majority_dir = max(
        counts,
        key=lambda k: (
            len(counts[k]),
            {"LONG": 2, "SHORT": 1, "FLAT": 0}.get(k, 0),
        ),
    )
    supporting = counts[majority_dir]
    opposing = [bid for d, bids in counts.items() if d != majority_dir for bid in bids]
    return supporting, opposing


def record_shadow_from_ensemble(
    results: list[dict[str, Any]],
    consensus: dict[str, Any],
    symbol: str = "XAUUSD",
    store: JsonlLedgerStore | None = None,
) -> dict[str, Any]:
    """Persist shadow ensemble results as DecisionRecords.

    Creates one DecisionRecord per brain result. Writes to data/decisions/{date}/.

    Args:
        results: List of per-brain result dicts from live_shadow_ensemble.
        consensus: Consensus dict from _compare_directions.
        symbol: Trading symbol.
        store: JsonlLedgerStore; creates one if None.

    Returns:
        Dict with written count, path, and per-brain record_ids.
    """
    ok_results = [r for r in results if r.get("status") == "ok"]
    errored = [r for r in results if r.get("status") != "ok"]
    if errored:
        print(
            "[shadow_decision_recorder] dropping non-OK results: "
            + ", ".join(
                f"{r.get('brain_id', '?')}: {r.get('error', r.get('status', 'unknown'))}"
                for r in errored
            ),
            flush=True,
        )
    supporting, opposing = _group_by_direction(ok_results)

    if not ok_results:
        return {
            "written": False,
            "brain_count": 0,
            "reason": "no_valid_proposals",
        }

    snapshot_id = new_snapshot_id()
    event_time = _utc_now()
    date_key = event_time.strftime("%Y-%m-%d")

    if store is None:
        store = JsonlLedgerStore("data")

    # Write one combined record per ensemble run (not per brain)
    proposal_ids = [new_proposal_id() for _ in ok_results]
    record_id = new_record_id()

    record = DecisionRecord(
        schema_version=SCHEMA_VERSION,
        record_id=record_id,
        snapshot_id=snapshot_id,
        intent_id=new_intent_id(),
        verdict_id=new_verdict_id(),
        event_time=event_time,
        recorded_at=_utc_now(),
        context={"symbol": symbol, "venue": "shadow"},
        inputs={
            "proposal_ids": proposal_ids,
            "proposal_count": len(ok_results),
        },
        execution={
            "dispatch_status": "shadow",
            "venue": "shadow",
        },
        outcome={},
        attribution={
            "supporting_brains": supporting,
            "opposing_brains": opposing,
            "consensus": consensus,
        },
        labels={
            "decision_action": _derive_action(consensus),
            "decision_side": _derive_side(consensus),
        },
        trace={
            "source": "shadow_ensemble",
            "brain_count": len(ok_results),
            "total_results": len(results),
        },
    )
    target_file = store.append_record(date_key, symbol, record)

    return {
        "written": True,
        "brain_count": len(ok_results),
        "record_id": record_id,
        "snapshot_id": snapshot_id,
        "date_key": date_key,
        "path": str(target_file),
        "proposal_ids": proposal_ids,
    }


def _serialize_feature_vector(fv: Any) -> list[float] | None:
    """Convert a numpy array or list feature vector to a plain list of floats."""
    if fv is None:
        return None
    try:
        import numpy as np

        if isinstance(fv, np.ndarray):
            return fv.tolist()
    except ImportError:
        pass
    if isinstance(fv, list | tuple):
        return [float(x) for x in fv]
    return None


def _extract_vote_details(proposals: list[Any]) -> list[dict[str, Any]]:
    """Extract per-brain prediction details from proposals."""
    votes: list[dict[str, Any]] = []
    for p in proposals:
        pred = getattr(p, "prediction", {}) or {}
        votes.append(
            {
                "brain_id": getattr(p, "brain_id", "unknown"),
                "direction_bias": pred.get("direction_bias", "neutral"),
                "up_probability": round(float(pred.get("up_probability", 0.5)), 6),
                "down_probability": round(float(pred.get("down_probability", 0.5)), 6),
                "confidence": round(float(pred.get("confidence", 0.0)), 6),
            }
        )
    return votes


def record_shadow_from_proposals(
    proposals: list[Any],
    consensus: dict[str, Any],
    symbol: str = "XAUUSD",
    store: JsonlLedgerStore | None = None,
    dispatch_status: str = "shadow_verify",
    *,
    feature_vector: Any = None,
    regime_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist multi-brain proposals as DecisionRecords (used by live_intent_loop).

    Creates one DecisionRecord per proposal. Attributions are derived from
    proposal brain_ids grouped by direction matching against consensus.

    Args:
        proposals: List of BrainDecisionProposal objects.
        consensus: Consensus dict. If empty, one is synthesized.
        symbol: Trading symbol.
        store: JsonlLedgerStore; creates one if None.
        dispatch_status: Status for execution block (default: "shadow_verify").
        feature_vector: Optional numpy array or list of 40 feature values.
        regime_info: Optional market regime dict from RegimeDetector.

    Returns:
        Dict with written count and paths.
    """
    if not proposals:
        return {"written": False, "count": 0, "reason": "no_proposals"}

    # Synthesize consensus if not provided
    if not consensus:
        direction_counts: dict[str, int] = {}
        for p in proposals:
            d = _direction_to_side(p.prediction.get("direction_bias", "neutral"))
            direction_counts[d] = direction_counts.get(d, 0) + 1
        majority = max(direction_counts, key=direction_counts.get) if direction_counts else "FLAT"
        n = len(proposals)
        max_same = direction_counts.get(majority, 0)
        consensus = {
            "consensus": majority.lower(),
            "total_brains": n,
            "agreement_score": round(max_same / n, 4) if n > 0 else 0.0,
        }

    snapshot_id = new_snapshot_id()
    event_time = _utc_now()
    date_key = event_time.strftime("%Y-%m-%d")

    if store is None:
        store = JsonlLedgerStore("data")

    direction_brain_map: dict[str, list[str]] = {}
    for p in proposals:
        d = _direction_to_side(p.prediction.get("direction_bias", "neutral"))
        direction_brain_map.setdefault(d, []).append(p.brain_id)

    majority_dir = (
        max(
            direction_brain_map,
            key=lambda k: (
                len(direction_brain_map[k]),
                {"LONG": 2, "SHORT": 1, "FLAT": 0}.get(k, 0),
            ),
        )
        if direction_brain_map
        else "FLAT"
    )
    supporting = direction_brain_map.get(majority_dir, [])
    opposing = [bid for d, bids in direction_brain_map.items() if d != majority_dir for bid in bids]

    proposal_ids = [p.proposal_id for p in proposals]
    record_id = new_record_id()

    record = DecisionRecord(
        schema_version=SCHEMA_VERSION,
        record_id=record_id,
        snapshot_id=snapshot_id,
        intent_id=new_intent_id(),
        verdict_id=new_verdict_id(),
        event_time=event_time,
        recorded_at=_utc_now(),
        context={"symbol": symbol, "venue": dispatch_status},
        inputs={
            "proposal_ids": proposal_ids,
            "proposal_count": len(proposals),
        },
        execution={
            "dispatch_status": dispatch_status,
            "venue": dispatch_status,
        },
        outcome={},
        attribution={
            "supporting_brains": supporting,
            "opposing_brains": opposing,
            "consensus": consensus,
        },
        labels={
            "decision_action": _derive_action(consensus),
            "decision_side": _derive_side(consensus),
        },
        trace={
            "source": dispatch_status,
            "brain_count": len(proposals),
            "proposal_count": len(proposals),
        },
        extensions={
            "feature_vector": _serialize_feature_vector(feature_vector),
            "regime_info": regime_info or {},
            "vote_details": _extract_vote_details(proposals),
        },
    )
    target_file = store.append_record(date_key, symbol, record)

    return {
        "written": True,
        "brain_count": len(proposals),
        "record_id": record_id,
        "path": str(target_file),
        "date_key": date_key,
        "snapshot_id": snapshot_id,
    }
