"""Shadow decision recording for multi-brain proposals.

Extracted from scripts/shadow_decision_recorder.py to eliminate reverse
dependency (core → scripts). The original script now delegates to this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.contracts.domain.decision_record import DecisionRecord
from core.contracts.ids import (
    new_intent_id,
    new_record_id,
    new_snapshot_id,
    new_verdict_id,
)
from core.ledger.schema_versions import SCHEMA_VERSION
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _direction_to_side(direction: str) -> str:
    d = direction.upper()
    return d if d in ("LONG", "SHORT", "FLAT") else "FLAT"


def _derive_action(consensus: str | dict[str, Any]) -> str:
    if isinstance(consensus, dict):
        consensus = str(consensus.get("consensus", "hold"))
    mapping = {"long": "open_long", "short": "open_short", "flat": "hold", "hold": "hold"}
    return mapping.get(str(consensus).lower(), "hold")


def _derive_side(consensus: str | dict[str, Any]) -> str:
    if isinstance(consensus, dict):
        consensus = str(consensus.get("consensus", "flat"))
    mapping = {"long": "long", "short": "short", "flat": "flat", "hold": "flat"}
    return mapping.get(str(consensus).lower(), "flat")


def _serialize_feature_vector(fv: Any) -> list[float] | None:
    if fv is None:
        return None
    try:
        import numpy as np

        if isinstance(fv, np.ndarray):
            return fv.flatten().tolist()
    except ImportError:
        pass
    if isinstance(fv, list | tuple):
        result: list[float] = []
        for v in fv:
            try:
                result.append(float(v))
            except (TypeError, ValueError):
                result.append(0.0)
        return result
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

    if not consensus:
        direction_counts: dict[str, int] = {}
        for p in proposals:
            d = _direction_to_side(p.prediction.get("direction_bias", "neutral"))
            direction_counts[d] = direction_counts.get(d, 0) + 1
        majority = (
            max(direction_counts, key=lambda k: direction_counts[k]) if direction_counts else "FLAT"
        )
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


def record_brain_votes(
    proposals: list[Any],
    strategy_name: str,
    consensus_direction: str,
    consensus_confidence: float,
    symbol: str = "XAUUSDc",
    base_dir: str = "data",
    *,
    brain_status_map: dict[str, str] | None = None,
    cycle_iteration: int = 0,
) -> str:
    """Record per-brain voting direction for EVERY cycle — before approval gates.

    Unlike record_shadow_from_proposals which only fires when a trade is
    dispatched, this function runs on every strategy evaluation cycle so
    that individual brain voting behaviour is tracked continuously for
    governance, backtesting, and evolutionary analysis.

    Writes one JSONL line per brain to ``data/brain_votes/YYYY-MM-DD.jsonl``.
    """
    import json
    from pathlib import Path

    event_time = _utc_now()
    date_key = event_time.strftime("%Y-%m-%d")

    votes_dir = Path(base_dir) / "brain_votes"
    votes_dir.mkdir(parents=True, exist_ok=True)
    output_path = votes_dir / f"{date_key}.jsonl"

    brain_status_map = brain_status_map or {}
    lines: list[str] = []

    for p in proposals:
        pred = getattr(p, "prediction", {}) or {}
        bid = getattr(p, "brain_id", "unknown")
        bstatus = getattr(p, "brain_status", brain_status_map.get(bid, "unknown"))

        # Collect raw_outputs (z_score, half_life, etc.) for diagnostic transparency
        extensions = getattr(p, "extensions", {}) or {}
        raw_outputs = extensions.get("raw_outputs", {}) if isinstance(extensions, dict) else {}

        entry = {
            "recorded_at": event_time.isoformat(),
            "cycle": cycle_iteration,
            "symbol": symbol,
            "strategy": strategy_name,
            "brain_id": bid,
            "brain_status": bstatus,
            "direction": pred.get("direction_bias", "neutral"),
            "up_prob": round(float(pred.get("up_probability", 0.5)), 6),
            "down_prob": round(float(pred.get("down_probability", 0.5)), 6),
            "confidence": round(float(pred.get("confidence", 0.0)), 6),
            "consensus_direction": consensus_direction,
            "consensus_confidence": consensus_confidence,
            "raw_outputs": {
                k: round(float(v), 6)
                if isinstance(v, int | float) and not isinstance(v, bool)
                else v
                for k, v in raw_outputs.items()
            }
            if raw_outputs
            else None,
        }
        lines.append(json.dumps(entry, ensure_ascii=False) + "\n")

    with open(output_path, "a", encoding="utf-8") as f:
        f.writelines(lines)

    return str(output_path)
