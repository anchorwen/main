"""Signal pipeline — ensemble proposals, contract group consensus, strategy evaluation.

Extracted from live_cycle.py. The ensemble proposal logic and group constants
are self-contained; the larger orchestration functions remain in live_cycle.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np


def _ensemble_proposals(
    group: dict[str, Any],
    member_proposals: list[Any],
) -> Any:
    """Merge proposals from correlated brains into a single ensemble vote."""
    if len(member_proposals) <= 1:
        return member_proposals[0] if member_proposals else None

    from core.brains.schema_versions import SCHEMA_BRAIN_DECISION_PROPOSAL
    from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal
    from core.contracts.ids import new_proposal_id

    avg_up = float(np.mean([p.prediction.get("up_probability", 0.5) for p in member_proposals]))
    avg_down = float(np.mean([p.prediction.get("down_probability", 0.5) for p in member_proposals]))
    confidence = max(avg_up, avg_down)
    if confidence < 0.01:
        direction = "neutral"
    else:
        direction = "long" if avg_up >= avg_down else "short"

    worst_fallback = any(p.health.get("fallback_used", False) for p in member_proposals)
    worst_runtime = max(p.health.get("runtime_ms", 0.0) for p in member_proposals)
    max_risk = max(p.health.get("risk_score", 0.0) for p in member_proposals)
    backends = list({p.health.get("backend", "unknown") for p in member_proposals})

    return BrainDecisionProposal(
        schema_version=SCHEMA_BRAIN_DECISION_PROPOSAL,
        proposal_id=new_proposal_id(),
        snapshot_id="",
        brain_id=group["group_id"],
        brain_role=group.get("role", "alpha_brain"),
        brain_status="shadow",
        model_version="ensemble",
        event_time=datetime.now(UTC).replace(tzinfo=None),
        generated_at=datetime.now(UTC).replace(tzinfo=None),
        prediction={
            "direction_bias": direction,
            "up_probability": avg_up,
            "down_probability": avg_down,
            "confidence": confidence,
            "uncertainty": 1.0 - confidence,
            "expected_edge_bps": None,
            "expected_hold_seconds": None,
        },
        health={
            "input_ok": True,
            "fallback_used": worst_fallback,
            "runtime_ms": worst_runtime,
            "risk_score": max_risk,
            "volatility_score": 0.5,
            "backend": "+".join(sorted(backends)),
        },
        vote_weight=group.get("vote_weight", 1.0),
    )


ENSEMBLE_GROUPS: list[dict[str, Any]] = [
    {
        "group_id": "SurvivalAlpha_Ensemble",
        "label": "Survival Alpha (V9 + CRT)",
        "brain_ids": ["V9_Institutional_01", "CRT.sur.chlg.g2026.1"],
        "magic": 90005,
        "role": "alpha_brain",
        "vote_weight": 1.0,
    },
    {
        "group_id": "TreeAlpha_Ensemble",
        "label": "Tree Alpha (LightGBM Champ + XGBoost V9 Challenger)",
        "brain_ids": ["LightGBM_V1_Institutional", "XGBoost_V9_Institutional"],
        "magic": 90008,
        "role": "alpha_brain",
        "vote_weight": 0.9,
    },
]
