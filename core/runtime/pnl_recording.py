"""PnL ledger signal recording — Strangler Fig #36 from live_cycle.py.

Extracted from live_cycle.py (~54 lines).  Records counterfactual brain
signals to the PnL ledger for performance tracking.  Gated by
multi_strategy_enabled=False to prevent phantom records flooding the
ledger when the main eval path is authoritative (FIX-20260611-003).
"""

from __future__ import annotations

import logging
from typing import Any

from core.brains.brain_registry import BrainRegistry

logger = logging.getLogger(__name__)


def record_counterfactual_signals(
    config: Any,
    pnl_ledger: Any,
    raw_proposals: list[Any],
    proposal: Any | None,
    mid_price: float | None,
    bid: float | None,
    ask: float | None,
) -> None:
    """Record counterfactual brain signals to PnL ledger.

    Per-proposal try/except prevents one misbehaving brain from silently
    dropping P&L records for all other brains.  Gated by
    multi_strategy_enabled=False per FIX-20260611-003.

    Args:
        config: LiveCycleConfig.
        pnl_ledger: BrainPnLStore instance.
        raw_proposals: List of BrainSignal proposals.
        proposal: Single proposal (legacy path fallback).
        mid_price: Current mid price.
        bid: Current bid price.
        ask: Current ask price.
    """
    if (
        pnl_ledger is None
        or mid_price is None
        or mid_price <= 0
        or config.multi_strategy_enabled
    ):
        return

    _live_spread = float(ask - bid) if (bid and ask and ask > bid) else 0.0

    if config.multi_brain:
        _registry = BrainRegistry.instance()
        for p in raw_proposals:
            try:
                _brain_id_str: str = str(getattr(p, "brain_id", "unknown"))
                _horizon = _registry.get_training_horizon(_brain_id_str)
                pnl_ledger.record_signal(
                    brain_id=_brain_id_str,
                    symbol=config.symbol,
                    direction=getattr(p, "direction", "neutral"),
                    entry_price=mid_price,
                    confidence=getattr(p, "confidence", 0.5),
                    expected_horizon=_horizon,
                    entry_spread=_live_spread,
                    entry_slippage=0.10,
                )
            except Exception:  # BLE001:REVIEWED
                logger.warning("PnL ledger signal recording failed (multi-strategy)")
    elif proposal is not None:
        try:
            _single_brain_id2: str = str(
                getattr(proposal, "brain_id", config.brain_entry.get("brain_id", "unknown"))
            )
            _horizon = BrainRegistry.instance().get_training_horizon(_single_brain_id2)
            pnl_ledger.record_signal(
                brain_id=_single_brain_id2,
                symbol=config.symbol,
                direction=getattr(proposal, "direction", "neutral"),
                entry_price=mid_price,
                confidence=getattr(proposal, "confidence", 0.5),
                expected_horizon=_horizon,
                entry_spread=_live_spread,
                entry_slippage=0.10,
            )
        except Exception:  # BLE001:REVIEWED
            logger.warning("PnL ledger signal recording failed (legacy)")
