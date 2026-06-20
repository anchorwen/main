"""OFI Toxicity Gate — Order Flow Imbalance risk signal for intraday strategies.

FIX-20260620-018: Extracted from strategy_line.evaluate() lines 813-851.

Hard physical gate: when Order Flow Imbalance is extremely one-sided,
physically block counter-trend mean-reversion signals. Mean-reversion
against toxic order flow must surrender — the liquidity vacuum crushes
any reversal attempt.

OFI is NOT an ML feature — it's a standalone risk signal computed in
``MicrostructureFeatureComputer._compute_tick_features()``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def apply_ofi_toxicity_gate(
    *,
    strategy_name: str,
    micro_feature_dict: dict[str, float] | None,
    direction: str,
    confidence: float,
    brain_ids: list[str],
    support_count: int,
    total_count: int,
    regime_gate_mode: str,
    make_decision: Callable[..., Any],
) -> Any | None:
    """Block trades when OFI is toxic for the signal direction.

    Only applies to statarb strategies (``statarb_dynamic`` M5,
    ``statarb_m15`` M15).  Other strategies pass through.

    Args:
        strategy_name: Strategy line name.
        micro_feature_dict: Optional micro features dict (contains ``OFI`` key).
        direction: Consensus direction (``"long"`` / ``"short"``).
        confidence: Consensus confidence.
        brain_ids: Brain IDs from consensus.
        support_count: Supporting voter count.
        total_count: Total voter count.
        regime_gate_mode: Current regime mode.
        make_decision: Callable to create ``StrategyDecision``.

    Returns:
        ``StrategyDecision`` to return immediately if blocked, or ``None``.
    """
    if strategy_name not in ("statarb_dynamic", "statarb_m15"):
        return None
    if not micro_feature_dict:
        return None

    ofi_z = micro_feature_dict.get("OFI", 0.0)

    if direction == "short" and ofi_z > 2.0:
        return make_decision(
            should_trade=False,
            direction=direction,
            confidence=confidence,
            volume=0.0,
            sl=0.0,
            tp=0.0,
            hard_sl=0.0,
            brain_ids=brain_ids,
            supporting_count=support_count,
            total_count=total_count,
            regime_mode=regime_gate_mode,
            reason=f"ofi_toxicity_blocked_short:OFI_Z={ofi_z:.2f}_gt_2.0",
        )

    if direction == "long" and ofi_z < -2.0:
        return make_decision(
            should_trade=False,
            direction=direction,
            confidence=confidence,
            volume=0.0,
            sl=0.0,
            tp=0.0,
            hard_sl=0.0,
            brain_ids=brain_ids,
            supporting_count=support_count,
            total_count=total_count,
            regime_mode=regime_gate_mode,
            reason=f"ofi_toxicity_blocked_long:OFI_Z={ofi_z:.2f}_lt_-2.0",
        )

    return None
