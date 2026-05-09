"""Contract-based brain grouping for valid within-group voting.

Each model was trained on a specific label contract (barrier, tick-bar
forward return, OU mean reversion).  Models trained on the SAME contract
answer the SAME question — their votes can be meaningfully averaged.
Models trained on DIFFERENT contracts answer DIFFERENT questions —
their votes are incommensurate and must NOT be mixed in a single average.

This module defines three contract groups and provides per-group
consensus computation that replaces the old cross-group ParliamentService
weighted average.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── Contract group definitions ────────────────────────────────────────────

# Group 1: Trained on survival-barrier contract (2.0×ATR SL, 3.5×ATR TP, 12-bar M5)
BARRIER_GROUP = {
    "name": "barrier_12bar",
    "horizon_cycles": 12,
    "brain_types": {
        "onnx_v9",
        "deepresmlp",
        "online_sgd",
        "xgboost_v9",
        "lightgbm_v1",
    },
    "contract": "survival_barrier_2.0sl_3.5tp_12bar",
    "description": "Predicts which barrier (SL/TP) is hit first in 60 min",
}

# Group 2: Trained on tick-bar forward return (5 tick-bars, ~3-15 min)
MICRO_GROUP = {
    "name": "micro_3bar",
    "horizon_cycles": 3,
    "brain_types": {
        "xgboost_v4.5",
        "transformer_v4.3",
        "transformer_v5",
    },
    "contract": "tick_bar_forward_return_5bars",
    "description": "Predicts directional return over ~5 tick-bars",
}

# Group 3: OU mean-reversion (dynamic half-life, no fixed horizon)
ARB_GROUP = {
    "name": "statarb_dynamic",
    "horizon_cycles": 0,  # dynamic, determined by OU half-life
    "brain_types": {
        "ou_params_v6",
    },
    "contract": "ou_mean_reversion_zscore",
    "description": "Mean-reversion signal based on OU process Z-score",
}

ALL_GROUPS = (BARRIER_GROUP, MICRO_GROUP, ARB_GROUP)

# Fast lookup: brain_type → group
_TYPE_TO_GROUP: dict[str, dict[str, Any]] = {}
for _g in ALL_GROUPS:
    for _bt in _g["brain_types"]:
        _TYPE_TO_GROUP[_bt] = _g


def get_group_for_brain_type(brain_type: str) -> dict[str, Any] | None:
    """Return the contract group dict for a given brain_type, or None."""
    return _TYPE_TO_GROUP.get(brain_type)


def get_group_for_proposal(proposal: Any) -> dict[str, Any] | None:
    """Return the contract group for a BrainDecisionProposal.

    Probes proposal.source.brain_type, proposal.metadata.model_type,
    or proposal.brain_type.
    """
    brain_type = ""
    try:
        brain_type = getattr(proposal, "brain_type", "")
    except Exception:
        pass
    if not brain_type:
        try:
            src = getattr(proposal, "source", None)
            if src is not None:
                brain_type = getattr(src, "brain_type", "")
        except Exception:
            pass
    if not brain_type:
        try:
            meta = getattr(proposal, "metadata", None) or {}
            brain_type = meta.get("model_type", "")
        except Exception:
            pass
    return _TYPE_TO_GROUP.get(brain_type) if brain_type else None


# ── GroupSignal dataclass ─────────────────────────────────────────────────


@dataclass
class GroupSignal:
    """Consensus output for a single contract group."""

    group_name: str
    direction: str  # "long", "short", or "neutral"
    confidence: float  # group-level consensus confidence [0, 1]
    consensus_score: float  # raw weighted score
    supporting_count: int
    opposing_count: int
    neutral_count: int
    total_count: int
    horizon_cycles: int
    brain_ids: list[str] = field(default_factory=list)


# ── Per-group consensus computer ──────────────────────────────────────────


class ContractGroupConsensus:
    """Compute a single-group consensus from proposals sharing the same
    training contract.

    Unlike the old ParliamentService._compute_consensus() which mixed
    incommensurate confidence values across contract types, this only
    averages proposals whose models were trained to answer the SAME
    prediction question.

    The weighted-average logic is identical to the ParliamentService
    (weight = vote_weight × confidence × runtime_factor), but the
    inputs are now contract-homogeneous.
    """

    def __init__(self, group_definition: dict[str, Any]) -> None:
        self.group = group_definition

    def compute(
        self,
        proposals: list[Any],
        dynamic_weighter: Any = None,
    ) -> GroupSignal | None:
        """Produce a GroupSignal from homogeneous proposals.

        Returns None if there are no valid proposals.
        """
        if not proposals:
            return None

        up_scores: list[float] = []
        down_scores: list[float] = []
        weights: list[float] = []
        directions: list[str] = []
        brain_ids: list[str] = []
        total = 0

        for p in proposals:
            total += 1
            try:
                bid = getattr(p, "brain_id", "unknown")
            except Exception:
                bid = "unknown"
            brain_ids.append(bid)

            pred = getattr(p, "prediction", None) or {}
            health = getattr(p, "health", None) or {}

            up = float(pred.get("up_probability", 0.5))
            down = float(pred.get("down_probability", 0.5))
            conf = float(pred.get("confidence", 0.5))
            runtime_ok = not health.get("fallback_used", False)

            vote_weight = float(getattr(p, "vote_weight", 1.0) or 1.0)
            if dynamic_weighter is not None:
                try:
                    summary = dynamic_weighter.get_summary(bid)
                    if summary:
                        vote_weight = dynamic_weighter._compute_weight(summary)
                except Exception:
                    pass

            weight = vote_weight * conf * (1.0 if runtime_ok else 0.5)
            up_scores.append(up * weight)
            down_scores.append(down * weight)
            weights.append(weight)

            bias = pred.get("direction_bias", "neutral")
            directions.append(bias if bias in ("long", "short") else "neutral")

        total_weight = sum(weights)
        if total_weight < 1e-9:
            return None

        weighted_up = sum(up_scores) / total_weight
        weighted_down = sum(down_scores) / total_weight

        # Determine direction
        if weighted_up >= weighted_down:
            direction = "long"
            raw_score = weighted_up
        else:
            direction = "short"
            raw_score = weighted_down

        # Neutral penalty: if neutrals dominate, scale down
        neutral_count = directions.count("neutral")
        if neutral_count > 0:
            neutral_ratio = neutral_count / total
            raw_score *= max(0.50, 1.0 - neutral_ratio * 0.30)

        # Majority agreement boost (within-group, so it's meaningful)
        long_count = directions.count("long")
        short_count = directions.count("short")
        majority_ratio = max(long_count, short_count) / max(total, 1)
        consensus_score = raw_score * 0.65 + majority_ratio * 0.35

        return GroupSignal(
            group_name=self.group["name"],
            direction=direction,
            confidence=round(float(consensus_score), 4),
            consensus_score=round(float(consensus_score), 4),
            supporting_count=max(long_count, short_count),
            opposing_count=min(long_count, short_count) if direction != "neutral" else 0,
            neutral_count=neutral_count,
            total_count=total,
            horizon_cycles=self.group["horizon_cycles"],
            brain_ids=brain_ids,
        )


# ── Factory ───────────────────────────────────────────────────────────────


def compute_all_group_signals(
    brain_proposals: list[tuple[dict[str, Any], Any]],
    dynamic_weighter: Any = None,
) -> dict[str, GroupSignal | None]:
    """Group brain proposals by contract type and compute per-group consensus.

    Args:
        brain_proposals: list of (brain_info_dict, BrainDecisionProposal) tuples.
        dynamic_weighter: optional DynamicBrainWeighter for vote weights.

    Returns:
        dict mapping group_name → GroupSignal (or None if group had no proposals).
    """
    grouped: dict[str, list[Any]] = {"barrier_12bar": [], "micro_3bar": [], "statarb_dynamic": []}

    for b_info, prop in brain_proposals:
        btype = b_info.get("brain_type", "")
        group = _TYPE_TO_GROUP.get(btype)
        if group is None:
            # Unknown brain type — map to barrier as safest default
            group = BARRIER_GROUP

        # Stamp brain_id onto proposal if not present
        try:
            if not getattr(prop, "brain_id", None):
                prop.brain_id = b_info.get("brain_id", "unknown")
        except Exception:
            pass

        grouped[group["name"]].append(prop)

    ContractGroupConsensus({})
    result: dict[str, GroupSignal | None] = {}
    for group_def in ALL_GROUPS:
        name = group_def["name"]
        props = grouped.get(name, [])
        if props:
            c = ContractGroupConsensus(group_def)
            result[name] = c.compute(props, dynamic_weighter)
        else:
            result[name] = None

    return result
