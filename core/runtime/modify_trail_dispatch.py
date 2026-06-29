"""Modify SL/TP dispatch — Strangler Fig extraction from live_cycle.py.

Strangler Fig #25 (FIX-20260619-063): Extracted _dispatch_modify_trail()
as a standalone function.  Builds the modify_sltp payload and dispatches
through the live_order_sender outbox pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


def dispatch_modify_trail(
    *,
    base_dir: str,
    symbol: str,
    adapter_name: str,
    mt5_terminal_path: str,
    ignore_protection_flag: bool,
    protection_flag_path: str,
    pos_side: str,
    pos_ticket: int,
    new_sl: float,
    new_tp: float,
    open_message_id: str = "",
    reason: str = "",
    brain_ids: list[str] | None = None,
    strategy_name: str = "",
) -> dict | None:  # DQAF-064 §2: return dispatch result for rejection tracking
    """Issue a modify_sltp through the existing outbox pipeline.

    Args:
        base_dir: Live config base directory.
        symbol: Trading symbol.
        adapter_name: MT5 adapter name.
        mt5_terminal_path: Path to MT5 terminal executable.
        ignore_protection_flag: Whether to skip protection flag check.
        protection_flag_path: Path to protection flag file.
        pos_side: Position side (long/short).
        pos_ticket: MT5 position ticket.
        new_sl: New stop-loss price.
        new_tp: New take-profit price.
        open_message_id: Original open message ID for journal linking.
        reason: Human-readable reason for the modification.
        brain_ids: Brain IDs for journal attribution.
        strategy_name: Strategy name for magic resolution.
    """
    from core.execution.live_order_sender import dispatch_live_order

    payload: dict[str, Any] = {
        "action": "modify_sltp",
        "side": pos_side,
        "position_ticket": pos_ticket,
        "sl": new_sl,
        "tp": new_tp,
        "comment": reason,
    }
    if brain_ids:
        payload["brain_ids"] = brain_ids
    # Resolve magic from strategy name for correct journal attribution
    if strategy_name:
        try:
            from core.contracts.strategy_magic import STRATEGY_TO_MAGIC

            _strat_magic = STRATEGY_TO_MAGIC.get(strategy_name, 0)
            if _strat_magic:
                payload["magic"] = _strat_magic
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            pass
    if open_message_id:
        payload["open_message_id"] = open_message_id

    # DQAF-064 §2: Return dispatch result instead of silently suppressing failures.
    # Previously contextlib.suppress() ate all exceptions, making trail rejection
    # invisible to the management layer.  Now we log and return the status so the
    # caller (trail_dispatch.py) can track rejection streaks.
    try:
        result = dispatch_live_order(
            base_dir=base_dir,
            broker=None,
            symbol=symbol,
            execution_payload=payload,
            skip_price_guard=True,
            ignore_protection_flag=ignore_protection_flag,
            protection_flag_path=protection_flag_path,
            adapter_name=adapter_name,
            extensions={"mt5_terminal_path": mt5_terminal_path},
        )
        return result
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as _exc:
        _logger.warning(
            "Trail dispatch failed for ticket=%s strategy=%s: %s",
            pos_ticket,
            strategy_name,
            _exc,
        )
        return {"status": "failed", "error": str(_exc), "ticket": pos_ticket}
