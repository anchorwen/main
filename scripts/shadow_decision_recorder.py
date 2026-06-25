"""Shadow decision recorder: persist inference results to ledger for brain leaderboard.

Writes DecisionRecord entries to data/decisions/{date}/XAUUSD.decisions.jsonl
so that brain_leaderboard and feedback_loop can consume them.

``record_shadow_from_proposals`` delegates to ``core.runtime.shadow_recorder``.
``record_shadow_from_ensemble`` is kept here because it accepts raw result dicts
rather than ``BrainDecisionProposal`` objects.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.contracts.domain.decision_record import DecisionRecord
from core.contracts.ids import (
    new_intent_id,
    new_proposal_id,
    new_record_id,
    new_snapshot_id,
    new_verdict_id,
)

if TYPE_CHECKING:
    from core.schemas.trading_contracts import BrainSignal
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.runtime.shadow_recorder import record_shadow_from_proposals  # noqa: F401 — re-export

SCHEMA_VERSION = "decision_record.v1"


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _direction_to_side(direction: str) -> str:
    d = direction.lower()
    if d in ("up", "long"):
        return "LONG"
    if d in ("down", "short"):
        return "SHORT"
    return "FLAT"


def _derive_action(consensus: str | dict[str, Any]) -> str:
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
    if isinstance(consensus, dict):
        c = consensus.get("consensus") or consensus.get("aggregated_bias", "no_results")
    else:
        c = str(consensus)
    return _direction_to_side(c)


def _result_to_proposal(
    result: dict[str, Any], snapshot_id: str, event_time: datetime
) -> BrainSignal | None:
    if result.get("status") != "ok":
        return None

    from core.schemas.trading_contracts import BrainSignal

    return BrainSignal(
        brain_id=result.get("brain_id", "unknown"),
        direction=result.get("direction_bias", "neutral"),
        confidence=result.get("confidence", 0.0),
        raw_score=result.get("raw_score", 0.0),
        fallback=result.get("fallback", False),
        runtime_ms=result.get("runtime_ms", 0.0),
        vote_weight=float(result.get("vote_weight", 1.0) or 1.0),
    )


def _group_by_direction(results: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
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
