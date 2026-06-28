"""Contract-group consensus computation and capital allocation.

Extracted from live_cycle.py per the Strangler Fig pattern.
Computes per-group voting, applies dynamic weights, resolves conflicts,
and returns a unified consensus result.
"""

from __future__ import annotations

import contextlib
from typing import Any

from core.runtime.fault_handler import FaultLevel, FaultTolerantContext


def compute_contract_group_consensus(
    raw_proposals: list[Any],
    brains: list[dict[str, Any]],
    tracker: Any,
    pnl_ledger: Any,
    correlation_tracker: Any,
    base_volume: float,
    current_atr: float,
    regime_info: dict[str, Any] | None = None,
    total_budget: float = 0.0,
    lot_value: float | None = None,
) -> dict[str, Any]:
    """Compute contract-group consensus and capital allocation from raw proposals.

    Returns a dict with keys: direction, confidence, dynamic_volume, proposals,
    consensus_extra.
    """
    from core.brains.services.dynamic_brain_weighter import DynamicBrainWeighter
    from core.execution.capital_allocator import CapitalAllocator, compute_volume, resolve_conflicts
    from core.parliament.contract_groups import compute_all_group_signals

    # Build (brain_info, proposal) pairs for group assignment
    brain_proposal_pairs: list[tuple[dict[str, Any], Any]] = []
    for i, p in enumerate(raw_proposals):
        b_info = brains[i] if i < len(brains) else {}
        # BrainSignal always carries brain_id from the adapter.
        brain_proposal_pairs.append((b_info, p))

    # Apply dynamic vote weights (same weighter, but now used per-group)
    weighter = DynamicBrainWeighter(tracker, pnl_store=pnl_ledger)
    for b_info in brains:
        bid = b_info.get("brain_id", "")
        if bid:
            weighter.set_brain_metadata(
                bid,
                {
                    "contract_group": b_info.get("contract_group", ""),
                    "feature_schema": b_info.get("feature_schema", ""),
                },
            )
    weighter.apply_weights(raw_proposals)

    # ── Capacity-aware position sizing (P&L Phase 4) ──
    # FIX-20260629-172: Unified weighting contract — capacity allocation
    # must use base_weight × dynamic_scale (same contract as voting in
    # contract_groups.py).  Bare get_weights() returns PnL-driven
    # dynamic_scale only, which allows vote_weight=0 (shadow/muted)
    # brains to receive positive capacity allocations.
    capacity_allocations: dict[str, float] = {}
    if total_budget > 0:
        with FaultTolerantContext(
            level=FaultLevel.DEGRADE,
            component="CapitalAllocator:allocate_capacity",
        ):
            allocator = CapitalAllocator()
            # ── Build unified weights: base_weight × dynamic_scale ──
            # base_weight: config-level vote_weight from BrainSignal (SSOT).
            #   0.0 = muted (shadow/retired) — must receive 0 capacity.
            # dynamic_scale: PnL-driven performance multiplier from weighter.
            _base_vote_weights: dict[str, float] = {}
            for p in raw_proposals:
                _bid = getattr(p, "brain_id", "")
                if _bid:
                    _base_vote_weights[_bid] = float(getattr(p, "vote_weight", 1.0) or 1.0)
            _pnl_dynamic_weights = weighter.get_weights()
            brain_weights: dict[str, float] = {}
            for _bid, _pnl_w in _pnl_dynamic_weights.items():
                _base = _base_vote_weights.get(_bid, 1.0)
                brain_weights[_bid] = _base * _pnl_w
            # Brains present only in proposals (not yet in PnL tracker)
            for _bid, _base in _base_vote_weights.items():
                if _bid not in brain_weights:
                    brain_weights[_bid] = _base * 1.0
            capacity_allocations = allocator.allocate_capacity(
                total_budget=total_budget,
                brain_weights=brain_weights,
                lot_value=lot_value,
            )

    # Per-group consensus
    group_signals = compute_all_group_signals(brain_proposal_pairs, weighter)

    # Capital allocation: resolve conflicts, size position
    allocation = resolve_conflicts(group_signals)

    if allocation.should_trade:
        direction = allocation.direction
        confidence = allocation.confidence

        # Update group correlation tracker
        if correlation_tracker is not None:
            with contextlib.suppress(RuntimeError, ValueError, KeyError, TypeError, OSError):
                correlation_tracker.update(group_signals)

        # Compute dynamic volume with correlation penalty
        vol_atr = current_atr if current_atr > 0 else None
        raw_volume = compute_volume(
            base_volume=base_volume or 0.01,
            decision=allocation,
            regime=regime_info.get("regime", "normal") if regime_info else "normal",
            vol_atr=vol_atr,
        )

        # Apply correlation penalty from group history
        if correlation_tracker is not None:
            with FaultTolerantContext(
                level=FaultLevel.DEGRADE,
                component="CorrelationTracker:penalty",
            ):
                corr_penalty = correlation_tracker.get_correlation_penalty(group_signals)
                dynamic_volume = round(raw_volume * corr_penalty, 3)
        else:
            dynamic_volume = raw_volume

        # Build consensus_extra from group signals (for downstream consumers)
        all_supporting: list[str] = []
        all_opposing: list[str] = []
        total_voters = 0
        for _gname, gs in group_signals.items():
            if gs is None:
                continue
            total_voters += gs.total_count
            if gs.direction == direction:
                all_supporting.extend(gs.brain_ids)
            elif gs.direction != "neutral":
                all_opposing.extend(gs.brain_ids)

        consensus_extra = {
            "voter_count": total_voters,
            "majority_ratio": round(allocation.confidence, 4),
            "disagreement_score": round(
                1.0 - allocation.confidence if allocation.agreement_level != "full" else 0.0,
                4,
            ),
            "supporting_brains": list(set(all_supporting)),
            "opposing_brains": list(set(all_opposing)),
            "is_feasible": True,
            "allocation": {
                "agreement_level": allocation.agreement_level,
                "active_groups": allocation.active_groups,
                "dissenting_groups": allocation.dissenting_groups,
            },
            "aggregated_bias": direction,
            "consensus_score": confidence,
            "capacity_allocations": capacity_allocations,
        }
        proposals = list(raw_proposals)
    else:
        direction = "neutral"
        confidence = 0.0
        dynamic_volume = 0.0
        consensus_extra = {
            "voter_count": 0,
            "majority_ratio": 0.0,
            "disagreement_score": 1.0,
            "supporting_brains": [],
            "opposing_brains": [],
            "is_feasible": False,
            "allocation": {
                "agreement_level": "none",
                "reason": allocation.reason,
            },
            "capacity_allocations": capacity_allocations,
        }
        proposals = list(raw_proposals)

    return {
        "direction": direction,
        "confidence": confidence,
        "dynamic_volume": dynamic_volume,
        "proposals": proposals,
        "consensus_extra": consensus_extra,
    }
