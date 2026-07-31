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

import contextlib
import json
from typing import Any

from core.schemas.trading_contracts import ConsensusResult, DegradedResult, Direction

# ── Contract group definitions ────────────────────────────────────────────

# Group 1: Trained on survival-barrier contract (3.0×ATR SL, 1.5×ATR TP, 12-bar M5)
# FIX-20260524-036: SL/TP updated from 2.0/3.5 → 3.0/1.5 after calibration surface rebuild.
# Meta_Stage1_Huber_V1 frozen (regression collapse with corrected costs).
# Meta_Stage1_Binary_Cls_V1 active as lightgbm_v1 but vote_weight=0.0 (shadow monitoring).
BARRIER_GROUP: dict[str, Any] = {
    "name": "barrier_12bar",
    "horizon_cycles": 12,
    "brain_types": {
        "lightgbm_v1",
        "xgboost_v9",  # FIX-20260530: restored xgb_barrier_12bar_xgboost_v3 config
        # onnx_v9 (CRT) and online_sgd (Online_MLP) evicted 2026-05-22.
    },
    "contract": "survival_barrier_3.0sl_1.5tp_12bar",
    "description": "Shadow monitoring: Binary_Cls_V1 vote_weight=0.0. Huber frozen. Strategy dormant until voting brain passes quality gates.",
}

# Group 2a: Micro M5 (5-bar, 1.5×ATR SL, 2.5×ATR TP, ~25 min)
# Uses Union Ensemble: any brain detecting TP → signal, maximizing TP recall
MICRO_GROUP: dict[str, Any] = {
    "name": "micro_3bar",
    "horizon_cycles": 8,
    "brain_types": {
        "xgboost_v4.5",
        "transformer_v4.3",
        "transformer_v5",
    },
    "contract": "label-micro-barrier-1.0.0",
    "voting_mode": "union",
    "description": "M5 micro-barrier prediction (5 bar / 25 min horizon)",
}

# Group 2b: Micro M15 (5-bar M15, 1.5×ATR SL, 2.5×ATR TP, ~75 min)
# Uses Union Ensemble: XGBoost+Transformer union — TP=46% (vs 21% solo XGBoost)
MICRO_M15_GROUP: dict[str, Any] = {
    "name": "micro_m15",
    "horizon_cycles": 5,
    "brain_types": {
        "xgboost_v4.5_m15",
        "transformer_v5_m15",
    },
    "contract": "label-micro-barrier-1.0.0-M15",
    "voting_mode": "union",
    "description": "M15 micro-barrier prediction (5 bar / 75 min horizon)",
}

# Group 2c: Micro H1 (4-bar H1, 1.8×ATR SL, 2.8×ATR TP, ~4h)
# Uses Union Ensemble: XGBoost+Transformer union
MICRO_H1_GROUP: dict[str, Any] = {
    "name": "micro_h1",
    "horizon_cycles": 4,
    "brain_types": {
        "xgboost_v4.5_h1",
        "transformer_v5_h1",
    },
    "contract": "label-micro-barrier-1.0.0-H1",
    "voting_mode": "union",
    "description": "H1 micro-barrier prediction (4 bar / 4h horizon)",
}

# Group 2d: Micro H4 (3-bar H4, 2.0×ATR SL, 3.0×ATR TP, ~12h) — gate-only, no signals
MICRO_H4_GROUP: dict[str, Any] = {
    "name": "micro_h4",
    "horizon_cycles": 3,
    "brain_types": {
        "xgboost_v4.5_h4",
        "transformer_v5_h4",
    },
    "contract": "label-micro-barrier-1.0.0-H4",
    "description": "H4 micro-barrier prediction (3 bar / 12h horizon) — trend gate only",
}

# Group 3: OU mean-reversion (dynamic half-life, no fixed horizon)
ARB_GROUP: dict[str, Any] = {
    "name": "statarb_dynamic",
    "horizon_cycles": 0,  # dynamic, determined by OU half-life
    "brain_types": {
        "ou_params_v6",
    },
    "contract": "ou_mean_reversion_zscore",
    "description": "Mean-reversion signal based on OU process Z-score",
}

# Group 3b: OU mean-reversion M15 (same brain type, wider timeframe)
STATARB_M15_GROUP: dict[str, Any] = {
    "name": "statarb_m15",
    "horizon_cycles": 0,
    "brain_types": {
        "ou_params_v6",
    },
    "contract": "ou_mean_reversion_zscore",
    "description": "OU mean-reversion on M15 bars (wider SL/TP, fewer signals)",
}

# Group 4: D1 daily swing (5-bar D1, 2.0×ATR SL, 3.5×ATR TP, ~1 week)
DAILY_SWING_GROUP: dict[str, Any] = {
    "name": "daily_swing",
    "horizon_cycles": 1440,  # 5 D1 bars × 288 M5 cycles/day
    "brain_types": {"xgboost_v9", "lightgbm_v1"},
    "contract": "d1_swing_5d",
    "voting_mode": "weighted",
    "description": "D1 daily swing — 5-bar (~1 week) barrier, SL=2.0xATR, TP=3.5xATR",
}

# Group 4b: M15 intraday swing (24-bar M15, 1.5×ATR SL, 3.0×ATR TP, ~6h)
M15_SWING_GROUP: dict[str, Any] = {
    "name": "m15_swing",
    "horizon_cycles": 72,  # 24 M15 bars × 3 M5 cycles/M15
    "brain_types": {"xgboost_v9"},  # FIX-20260602-060: was lightgbm_v1, actual brain is xgboost_v9
    "contract": "m15_swing_24bar",
    "voting_mode": "weighted",
    "description": "M15 intraday swing — 24-bar (~6h) barrier, SL=1.5xATR, TP=3.0xATR",
}

# Group 4c: M30 intraday swing (12-bar M30, 1.5×ATR SL, 3.0×ATR TP, ~6h)
M30_SWING_GROUP: dict[str, Any] = {
    "name": "m30_swing",
    "horizon_cycles": 36,  # 12 M30 bars × 3 M5 cycles/M30 (M30≈6 M5 bars)
    "brain_types": {"xgboost_v9", "lightgbm_v1"},
    "contract": "m30_swing_12bar",
    "voting_mode": "weighted",
    "description": "M30 intraday swing — 12-bar (~6h) barrier, SL=1.5xATR, TP=3.0xATR",
}

# Group 4d: H1 daily swing (24-bar H1, 2.0×ATR SL, 3.5×ATR TP, ~1d)
H1_SWING_GROUP: dict[str, Any] = {
    "name": "h1_swing",
    "horizon_cycles": 288,  # 24 H1 bars × 12 M5 cycles/H1
    "brain_types": {"xgboost_v9"},
    "contract": "h1_swing_24bar",
    "voting_mode": "weighted",
    "description": "H1 daily swing — 24-bar (~1d) barrier, SL=2.0xATR, TP=3.5xATR",
}

# Group 5: Meta-Labeling barrier_12bar (OU-triggered binary classification)
# Unlike BARRIER_GROUP (unconditional barrier prediction on every bar),
# this group ONLY evaluates OU signals — the meta-labeling binary classifier
# answers: "Given this OU signal (z_score, half_life, theta) + V9 features,
# will SL or TP hit first at 12-bar horizon?"
# Vote weight 0.0 by default — promoted to probation when OU feature bridge is active.
BARRIER_12BAR_META_GROUP: dict[str, Any] = {
    "name": "barrier_12bar_meta",
    "horizon_cycles": 12,
    "brain_types": {
        "lightgbm_v1",  # Meta_Stage1_MetaLabel_Binary_V1 — OU signal meta-labeler
    },
    "contract": "barrier_12bar_meta_binary_cls",
    "voting_mode": "weighted",
    "description": "OU-triggered meta-labeling: binary classifier on OU signal bars, SL=3.0/TP=1.5, 12-bar M5 horizon",
}

# Group 4e: H4 multi-day swing (18-bar H4, 2.0×ATR SL, 4.0×ATR TP, ~3d)
H4_SWING_GROUP: dict[str, Any] = {
    "name": "h4_swing",
    "horizon_cycles": 864,  # 18 H4 bars × 48 M5 cycles/H4
    "brain_types": {"xgboost_v9"},
    "contract": "h4_swing_18bar",
    "voting_mode": "weighted",
    "description": "H4 multi-day swing — 18-bar (~3d) barrier, SL=2.0xATR, TP=4.0xATR",
}

# Group 5e: BTC swing H1 Survival (SL=3.0/TP=2.0, magic=90411)
# DQAF-20260615-002: Dedicated line for V9_H1 — matches training SL/TP.
BTC_SWING_H1_GROUP: dict[str, Any] = {
    "name": "btc_swing_h1",
    "horizon_cycles": 144,
    "brain_types": {"lightgbm_v1"},
    "contract": "btc_swing_h1_survival",
    "voting_mode": "weighted",
    "description": "BTC H1 Survival — SL=3.0xATR, TP=2.0xATR, high-WR mode",
}

# Group 5f: BTC swing (M30 12-bar, isolated 90410 magic, crypto 24/7)
BTC_SWING_GROUP: dict[str, Any] = {
    "name": "btc_swing",
    "horizon_cycles": 36,
    "brain_types": {"xgboost_v9"},
    "contract": "btc_swing_12bar",
    "voting_mode": "weighted",
    "description": "BTC M30 swing — 12-bar barrier, SL=2.0xATR, TP=2.5xATR",
}

# Group 5h: BTC swing M15 (24-bar M15, SL=2.0×ATR, TP=2.0×ATR, ~6h)
# Shadow tracer bullet — zero vote_weight, zero live risk.
BTC_SWING_M15_GROUP: dict[str, Any] = {
    "name": "btc_swing_m15",
    "horizon_cycles": 72,  # 24 M15 bars × 3 M5 cycles/M15
    "brain_types": {"xgboost_v9"},
    "contract": "btc_swing_m15_24bar",
    "voting_mode": "weighted",
    "description": "BTC M15 swing — 24-bar (~6h) barrier, SL=2.0xATR, TP=2.0xATR, shadow tracer",
}

# Group 5i: BTC swing M30 (24-bar M30, SL=2.0×ATR, TP=2.5×ATR, ~12h)
# Shadow tracer bullet — zero vote_weight, zero live risk.
BTC_SWING_M30_GROUP: dict[str, Any] = {
    "name": "btc_swing_m30",
    "horizon_cycles": 144,  # 24 M30 bars × 6 M5 cycles/M30
    "brain_types": {"xgboost_v9"},
    "contract": "btc_swing_m30_24bar",
    "voting_mode": "weighted",
    "description": "BTC M30 swing — 24-bar (~12h) barrier, SL=2.0xATR, TP=2.5xATR, shadow tracer",
}

# Group 5j: BTC swing H1 V2 (24-bar H1, SL=2.0×ATR, TP=2.5×ATR, ~1d)
# 41-dim directional labels — Wasserstein 2.3× improvement over old 48-dim H1.
# Shadow tracer bullet — zero vote_weight, zero live risk.
BTC_SWING_H1_V2_GROUP: dict[str, Any] = {
    "name": "btc_swing_h1_v2",
    "horizon_cycles": 288,  # 24 H1 bars × 12 M5 cycles/H1
    "brain_types": {"lightgbm_v1"},
    "contract": "btc_swing_h1_v2_24bar",
    "voting_mode": "weighted",
    "description": "BTC H1 V2 swing — 24-bar (~1d) barrier, SL=2.0xATR, TP=2.5xATR, 41-dim directional, shadow tracer",
}

# Group 5k: BTC swing H4 (12-bar H4, SL=2.5×ATR, TP=3.0×ATR, ~2d)
# Shadow tracer bullet — zero vote_weight, zero live risk.
BTC_SWING_H4_GROUP: dict[str, Any] = {
    "name": "btc_swing_h4",
    "horizon_cycles": 576,  # 12 H4 bars × 48 M5 cycles/H4
    "brain_types": {"xgboost_v9"},
    "contract": "btc_swing_h4_12bar",
    "voting_mode": "weighted",
    "description": "BTC H4 swing — 12-bar (~2d) barrier, SL=2.5xATR, TP=3.0xATR, shadow tracer",
}

# Group 5k: BTC Expected R M15 (24-bar M15, SL=1.5×ATR, TP=2.5×ATR, ~6h)
# V4 Two-Tower — asymmetric Huber regression, LONG tower (vote_weight=0.0)
# + SHORT tower (vote_weight=1.0).  Shadow deployment Aug 2026.
BTC_EXPECTED_R_M15_GROUP: dict[str, Any] = {
    "name": "btc_expected_r_m15",
    "horizon_cycles": 144,  # 24 M15 bars × 6 M5 cycles/M15
    "brain_types": {"expected_r_long", "expected_r_short"},
    "contract": "btc_expected_r_m15_24bar",
    "voting_mode": "weighted",
    "description": "BTC Expected R V4 M15 Two-Tower — regression E[R_long]/E[R_short], asymmetric Huber loss, shadow deployment",
}

# Group 5g: H1 directional XAU (24-bar H1, 2.0×ATR SL, 3.5×ATR TP, ~1d)
# Bidirectional XGBoost regression — predicts direction + magnitude on H1 bars.
# Swing_V10_H1_Directional (PF=81.10, +107.33R) — XAU's highest-performing brain.
H1_DIRECTIONAL_GROUP: dict[str, Any] = {
    "name": "h1_directional",
    "horizon_cycles": 288,  # 24 H1 bars × 12 M5 cycles/H1
    "brain_types": {"xgboost_v9"},
    "contract": "h1_directional_regression",
    "voting_mode": "weighted",
    "description": "H1 directional XGBoost — bidirectional regression, SL=2.0xATR, TP=3.5xATR, 24-bar horizon",
}

ALL_GROUPS: tuple[dict[str, Any], ...] = (
    BARRIER_GROUP,
    BARRIER_12BAR_META_GROUP,
    MICRO_GROUP,
    MICRO_M15_GROUP,
    MICRO_H1_GROUP,
    MICRO_H4_GROUP,
    ARB_GROUP,
    STATARB_M15_GROUP,
    DAILY_SWING_GROUP,
    M15_SWING_GROUP,
    M30_SWING_GROUP,
    H1_SWING_GROUP,
    H4_SWING_GROUP,
    BTC_SWING_GROUP,
    BTC_SWING_H1_GROUP,
    BTC_SWING_M15_GROUP,
    BTC_SWING_M30_GROUP,
    BTC_SWING_H1_V2_GROUP,
    BTC_SWING_H4_GROUP,
    BTC_EXPECTED_R_M15_GROUP,
    H1_DIRECTIONAL_GROUP,
)

# Primary lookup: contract_group name → group definition
_GROUP_BY_NAME: dict[str, dict[str, Any]] = {g["name"]: g for g in ALL_GROUPS}

# Legacy lookup: brain_type → group (deprecated — use _GROUP_BY_NAME via contract_group field)
_TYPE_TO_GROUP: dict[str, dict[str, Any]] = {}
for _g in ALL_GROUPS:
    for _bt in _g["brain_types"]:
        _TYPE_TO_GROUP[_bt] = _g


def get_group_for_contract_group(group_name: str) -> dict[str, Any] | None:
    """Return the contract group dict for a given contract group name."""
    return _GROUP_BY_NAME.get(group_name)


def get_group_for_brain_type(brain_type: str) -> dict[str, Any] | None:
    """Return the contract group dict for a given brain_type (legacy).

    Prefer ``get_group_for_contract_group()`` using the brain JSON
    ``contract_group`` field as the single source of truth.
    """
    return _TYPE_TO_GROUP.get(brain_type)


def get_group_for_proposal(proposal: Any) -> dict[str, Any] | None:
    """Return the contract group for a BrainDecisionProposal.

    Probes (in order): proposal.contract_group, proposal.brain_id via
    BrainRegistry, proposal.brain_type (legacy).
    """
    # 1. Direct contract_group attribute
    cg = getattr(proposal, "contract_group", None)
    if cg and cg in _GROUP_BY_NAME:
        return _GROUP_BY_NAME[cg]

    # 2. brain_id → BrainRegistry → contract_group
    bid = getattr(proposal, "brain_id", None)
    if bid:
        try:
            from core.brains.brain_registry import BrainRegistry

            registry = BrainRegistry.instance()
            entry = registry.get(bid)
            if entry and entry.contract_group in _GROUP_BY_NAME:
                return _GROUP_BY_NAME[entry.contract_group]

        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            pass
    # 3. Legacy brain_type probe
    brain_type = ""
    with contextlib.suppress(RuntimeError, ValueError, KeyError, TypeError, OSError):
        brain_type = getattr(proposal, "brain_type", "")
    if not brain_type:
        try:
            src = getattr(proposal, "source", None)
            if src is not None:
                brain_type = getattr(src, "brain_type", "")
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            pass
    if not brain_type:
        try:
            meta = getattr(proposal, "metadata", None) or {}
            brain_type = meta.get("model_type", "")
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            pass
    return _TYPE_TO_GROUP.get(brain_type) if brain_type else None


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
    ) -> ConsensusResult | None:
        """Produce a ConsensusResult from homogeneous proposals.

        Routes to union or weighted-average mode based on group's
        ``voting_mode`` key (default: weighted-average).

        Returns None if there are no valid proposals.
        """
        if not proposals:
            return None

        if self.group.get("voting_mode") == "union":
            return self._compute_union(proposals, dynamic_weighter)
        return self._compute_weighted(proposals, dynamic_weighter)

    def _compute_weighted(
        self,
        proposals: list[Any],
        dynamic_weighter: Any = None,
    ) -> ConsensusResult | None:
        """Direction-count voting weighted by confidence × vote_weight.

        Each BrainSignal casts a weighted vote for its decided direction.
        No probability averaging — the adapter already resolved direction.
        Weight = vote_weight × confidence × 0.5 (fallback) or 1.0 (healthy).
        """

        long_weight: float = 0.0
        short_weight: float = 0.0
        directions: list[str] = []
        brain_ids: list[str] = []
        total = 0

        for p in proposals:
            total += 1
            try:
                bid = getattr(p, "brain_id", "unknown")
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                bid = "unknown"
            brain_ids.append(bid)

            # Check for degraded signal
            if isinstance(p, DegradedResult):
                directions.append("neutral")
                continue

            direction_str = getattr(p, "direction", "neutral")
            conf = float(getattr(p, "confidence", 0.5))
            fallback = bool(getattr(p, "fallback", False))

            # ── FIX-20260607-011: Decouple base_weight (config permission)
            #    from dynamic_scale (PnL performance multiplier).
            #
            #    base_weight = config-level gate: 0.0=muted, 1.0=full voting rights
            #    dynamic_scale = PnL-driven adjustment from DynamicBrainWeighter
            #
            #    final = base_weight × dynamic_scale
            #    If base_weight=0, the brain is PHYSICALLY MUTED regardless
            #    of PnL performance.  This prevents shadow brains from
            #    accumulating collective weight to override voting brains.
            # DQAF-20260731-004: Preserve explicit vote_weight=0.0 (muted/observation-only).
            # The old "or 1.0" pattern silently upgraded 0.0→1.0 because 0.0 is falsy,
            # nullifying the base_weight <= 0.0 fail-fast gate below.
            _vw_raw = getattr(p, "vote_weight", None)
            base_weight = float(_vw_raw) if _vw_raw is not None else 1.0
            if base_weight <= 0.0:
                # ── Fail-Fast Gate: muted brain cannot vote ──
                directions.append("neutral")
                continue

            # dynamic_scale is pre-computed by DynamicBrainWeighter.apply_weights()
            # and stamped onto the proposal as p.dynamic_scale.
            # If unavailable (frozen object or no weighter), default to 1.0.
            dynamic_scale = float(getattr(p, "dynamic_scale", 1.0) or 1.0)

            vote_weight = base_weight * dynamic_scale
            weight = vote_weight * conf * (0.5 if fallback else 1.0)

            if direction_str == "long":
                long_weight += weight
                directions.append("long")
            elif direction_str == "short":
                short_weight += weight
                directions.append("short")
            else:
                directions.append("neutral")

        total_weight = long_weight + short_weight
        if total_weight < 1e-9:
            return None

        neutral_count = directions.count("neutral")
        long_count = directions.count("long")
        short_count = directions.count("short")

        # When every brain says neutral, the group has no directional signal.
        if neutral_count == total:
            return ConsensusResult(
                direction="neutral",
                confidence=0.0,
                supporting_brains=[],
                dissenting_brains=[],
                brain_ids=brain_ids,
                supporting_count=0,
                total_count=total,
            )

        # FIX-20260602-052 + FIX-20260603-062: self-normalization bug.
        # When ALL non-neutral votes agree on the same direction, the
        # weight/total_weight division collapses to 1.0 regardless of raw
        # confidence.  This happens for single-brain AND for unanimous
        # multi-brain (e.g. 2 brains both voting SHORT).
        # Use weighted-average confidence instead.
        _all_agree = (long_count > 0 and short_count == 0) or (short_count > 0 and long_count == 0)
        if _all_agree and (long_count + short_count) > 0:
            direction: Direction = "neutral"  # narrowed per branch below
            if long_count > 0:
                direction = "long"
                _total_conf = sum(
                    float(getattr(_p, "confidence", 0.5))
                    for _p in proposals
                    if getattr(_p, "direction", "neutral") == "long"
                )
                consensus_base = _total_conf / long_count
            else:
                direction = "short"
                _total_conf = sum(
                    float(getattr(_p, "confidence", 0.5))
                    for _p in proposals
                    if getattr(_p, "direction", "neutral") == "short"
                )
                consensus_base = _total_conf / short_count
            # Small bonus for each additional agreeing brain
            _agree_bonus = min(0.15, 0.05 * (max(long_count, short_count) - 1))
            consensus_base = min(1.0, consensus_base + _agree_bonus)
            # Neutral penalty
            if neutral_count > 0:
                neutral_ratio = neutral_count / total
                consensus_base *= max(0.35, 1.0 - neutral_ratio * 0.2)
            consensus_score = consensus_base
        else:
            # Mixed directions — weighted consensus (correct)
            if long_weight >= short_weight:
                direction = "long"
                consensus_base = long_weight / total_weight
            else:
                direction = "short"
                consensus_base = short_weight / total_weight

            # Neutral penalty
            if neutral_count > 0:
                neutral_ratio = neutral_count / total
                consensus_base *= max(0.35, 1.0 - neutral_ratio * 0.2)

            # Majority agreement boost
            majority_ratio = max(long_count, short_count) / max(total, 1)
            consensus_score = consensus_base * 0.5 + majority_ratio * 0.5

        # Build supporting/dissenting lists from parallel brain_ids + directions
        supporting_brains = [
            bid for bid, d in zip(brain_ids, directions, strict=False) if d == direction
        ]
        dissenting_brains = [
            bid
            for bid, d in zip(brain_ids, directions, strict=False)
            if d not in (direction, "neutral")
        ]

        return ConsensusResult(
            direction=direction,
            confidence=round(float(consensus_score), 4),
            supporting_brains=supporting_brains,
            dissenting_brains=dissenting_brains,
            brain_ids=brain_ids,
            supporting_count=max(long_count, short_count),
            total_count=total,
        )

    def _compute_union(
        self,
        proposals: list[Any],
        dynamic_weighter: Any = None,
    ) -> ConsensusResult | None:
        """Union ensemble voting: any brain detecting TP → signal.

        Designed for microstructure (barrier) groups where XGBoost excels
        at SL detection and Transformer excels at TP detection.  Union
        maximises TP recall — the cost is higher false positives, which
        are absorbed by the SL/TP barrier.

        Logic:
          - Any brain voting "long"  → group may signal long.
          - Any brain voting "short" → group may signal short.
          - If both directions present: confidence-weighted tie-break.
          - Confidence = max-voter confidence + small bonus per additional
            agreeing brain.
        """
        long_voters: list[tuple[str, float]] = []
        short_voters: list[tuple[str, float]] = []
        neutral_voters: list[str] = []
        all_brain_ids: list[str] = []

        for p in proposals:
            try:
                bid = getattr(p, "brain_id", "unknown")
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                bid = "unknown"
            all_brain_ids.append(bid)

            # Check for degraded signal
            if isinstance(p, DegradedResult):
                neutral_voters.append(bid)
                continue

            direction_str = getattr(p, "direction", "neutral")
            conf = float(getattr(p, "confidence", 0.5))

            if direction_str == "long":
                long_voters.append((bid, conf))
            elif direction_str == "short":
                short_voters.append((bid, conf))
            else:
                neutral_voters.append(bid)

        total = len(proposals)
        long_count = len(long_voters)
        short_count = len(short_voters)
        neutral_count = len(neutral_voters)

        # ── Union direction ──
        has_long = long_count > 0
        has_short = short_count > 0
        direction: Direction = "neutral"  # narrowed per branch below

        if has_long and has_short:
            # Tie-break: compare aggregate confidence of each side
            long_conf_sum = sum(c[1] for c in long_voters)
            short_conf_sum = sum(c[1] for c in short_voters)
            if long_conf_sum >= short_conf_sum:
                direction = "long"
                supporting = long_voters
                opposing = short_voters
            else:
                direction = "short"
                supporting = short_voters
                opposing = long_voters
        elif has_long:
            direction = "long"
            supporting = long_voters
            opposing = short_voters
        elif has_short:
            direction = "short"
            supporting = short_voters
            opposing = long_voters
        else:
            # All brains neutral: use avg brain confidence rather than 0.0
            # so confidence-drop exits are proportional to actual conviction
            # decline, not an artificial cliff from 0.90 → 0.00.
            avg_conf = sum(float(getattr(p, "confidence", 0.5)) for p in proposals) / max(total, 1)
            # Dampen: neutral consensus is inherently lower-confidence than
            # directional consensus, but not zero.
            neutral_confidence = round(avg_conf * 0.55, 4)
            return ConsensusResult(
                direction="neutral",
                confidence=neutral_confidence,
                supporting_brains=[],
                dissenting_brains=[],
                brain_ids=all_brain_ids,
                supporting_count=0,
                total_count=total,
            )

        # ── Union confidence ──
        # Base: max confidence among agreeing brains
        max_conf = max(c[1] for c in supporting) if supporting else 0.5
        # Multi-brain bonus: +0.06 per additional agreeing brain (max +0.18)
        agreement_bonus = min((len(supporting) - 1) * 0.06, 0.18)
        # Opposition penalty: if dissenting brains exist, discount
        opposition_penalty = 0.12 if len(opposing) > 0 else 0.0
        # Neutral drag: each neutral voter slightly reduces confidence
        neutral_drag = neutral_count * 0.04

        confidence = max(
            0.35, min(0.95, max_conf + agreement_bonus - opposition_penalty - neutral_drag)
        )

        return ConsensusResult(
            direction=direction,
            confidence=round(confidence, 4),
            supporting_brains=[bid for bid, _ in supporting],
            dissenting_brains=[bid for bid, _ in opposing],
            brain_ids=all_brain_ids,
            supporting_count=len(supporting),
            total_count=total,
        )


# ── A/B test bridge ────────────────────────────────────────────────────────


class ABGroupRouter:
    """Deterministic A/B splitter for champion-vs-challenger within a group.

    When a contract group contains multiple brains of the same type (e.g.
    LightGBM_V1 vs LightGBM_V2_Retrained), this router assigns each cycle
    to either the control or treatment variant using a hash of the trade
    timestamp, ensuring consistent assignment without shared state.

    Usage:
        router = ABGroupRouter(control_brain_id="LightGBM_V1_Institutional",
                               treatment_brain_id="LightGBM_V2_Retrained")
        variant = router.assign()
        # filter proposals to only include the assigned brain
    """

    def __init__(
        self,
        *,
        control_brain_id: str,
        treatment_brain_id: str,
        control_weight: float = 0.5,
        treatment_weight: float = 0.5,
        salt: str = "",
    ):
        from core.brains.services.ab_test import TrafficSplitter

        self.control_id = control_brain_id
        self.treatment_id = treatment_brain_id
        self._splitter = TrafficSplitter(
            control_weight=control_weight,
            treatment_weight=treatment_weight,
            control_name=control_brain_id,
            treatment_names=[treatment_brain_id],
            salt=salt,
        )
        self._tracker: Any = None  # lazy init

    def assign(self, key: str | None = None) -> str:
        """Return the brain_id to use for this cycle.

        When ``key`` is None, uses the current UTC timestamp.
        """
        import time as _time

        key = key or str(int(_time.time()))
        return self._splitter.assign(key)

    def record_outcome(self, brain_id: str, metric: float) -> None:
        """Record a metric for the assigned brain."""
        if self._tracker is None:
            from core.brains.services.ab_test import ExperimentTracker

            self._tracker = ExperimentTracker(
                experiment_id=f"ab_{self.control_id}_vs_{self.treatment_id}",
                metric_direction="higher",
            )
        self._tracker.record(variant=brain_id, metric=metric)

    def evaluate(self) -> dict[str, Any] | None:
        """Evaluate the experiment if enough data is available."""
        if self._tracker is None:
            return None
        result = self._tracker.evaluate()
        return result.to_dict()


_ab_routers: dict[str, ABGroupRouter] = {}


def register_ab_router(
    group_name: str,
    control_brain_id: str,
    treatment_brain_id: str,
    *,
    control_weight: float = 0.5,
) -> ABGroupRouter:
    """Register an A/B router for a contract group's champion/challenger pair."""
    router = ABGroupRouter(
        control_brain_id=control_brain_id,
        treatment_brain_id=treatment_brain_id,
        control_weight=control_weight,
        treatment_weight=1.0 - control_weight,
        salt=group_name,
    )
    _ab_routers[group_name] = router
    return router


def get_ab_router(group_name: str) -> ABGroupRouter | None:
    """Return the registered A/B router for *group_name*, if any."""
    return _ab_routers.get(group_name)


def filter_proposals_for_ab(
    group_name: str,
    proposals: list[Any],
    brain_info_map: dict[str, dict[str, Any]],
) -> list[Any]:
    """Filter proposals to only include the A/B-assigned brain for this cycle.

    If no A/B router is registered for *group_name*, returns all proposals
    unchanged (identity pass-through).
    """
    router = _ab_routers.get(group_name)
    if router is None:
        return proposals

    assigned = router.assign()
    filtered = []
    for p in proposals:
        bid = getattr(p, "brain_id", "")
        info = brain_info_map.get(bid, {})
        _btype = info.get("brain_type", "")
        # Only filter if the brain type matches the A/B experiment
        if bid == assigned:
            filtered.append(p)
        elif bid != assigned and bid not in (router.control_id, router.treatment_id):
            # Keep brains that aren't part of the experiment
            filtered.append(p)
        # else: brain is the other arm of the experiment — skip it this cycle

    return filtered if filtered else proposals  # safety: never return empty


# ── Factory ───────────────────────────────────────────────────────────────


def compute_all_group_signals(
    brain_proposals: list[tuple[dict[str, Any], Any]],
    dynamic_weighter: Any = None,
) -> dict[str, ConsensusResult | None]:
    """Group brain proposals by contract type and compute per-group consensus.

    Args:
        brain_proposals: list of (brain_info_dict, BrainDecisionProposal) tuples.
        dynamic_weighter: optional DynamicBrainWeighter for vote weights.

    Returns:
        dict mapping group_name → ConsensusResult (or None if group had no proposals).
    """
    grouped: dict[str, list[Any]] = {g_name: [] for g_name in _GROUP_BY_NAME}

    for b_info, prop in brain_proposals:
        cg_name = b_info.get("contract_group", "")
        group = _GROUP_BY_NAME.get(cg_name)
        if group is None:
            print(
                json.dumps(
                    {
                        "event": "unknown_contract_group_warning",
                        "contract_group": cg_name,
                        "brain_id": b_info.get("brain_id", "unknown"),
                        "brain_type": b_info.get("brain_type", ""),
                        "routed_to": None,
                        "skipped": True,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            continue  # Skip brains with unrecognised contract_group

        # BrainSignal always carries brain_id from the adapter.
        # Stamping is no longer needed (and would fail on frozen objects).
        grouped[group["name"]].append(prop)

    ContractGroupConsensus({})
    result: dict[str, ConsensusResult | None] = {}
    for group_def in ALL_GROUPS:
        name = group_def["name"]
        props = grouped.get(name, [])
        if props:
            c = ContractGroupConsensus(group_def)
            result[name] = c.compute(props, dynamic_weighter)
        else:
            result[name] = None

    return result
