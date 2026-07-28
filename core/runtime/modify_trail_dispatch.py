"""Modify SL/TP dispatch — Strangler Fig extraction from live_cycle.py.

Strangler Fig #25 (FIX-20260619-063): Extracted _dispatch_modify_trail()
as a standalone function.  Builds the modify_sltp payload and dispatches
through the live_order_sender outbox pipeline.

DQAF-20260728-003 (Component A): Global anti-starvation rate limiter.
Prevents MT5 broker 10024 "Too many trade requests" by enforcing a
minimum interval between consecutive modify_sltp calls across all
positions.  Uses micro-sleep (blocking) rather than rejection — every
position gets through within the same M5 cycle, just serialised.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

_logger = logging.getLogger(__name__)

# ── DQAF-20260728-003: Global modify_sltp rate limiter ──────────────
# Per-account rate limit: max 1 modify_sltp per _MIN_INTERVAL seconds.
# Micro-sleep (blocking) ensures anti-starvation — no position is
# deferred to the next 5-minute cycle.  Total sleep across N positions
# in one cycle ≈ (N-1) × _MIN_INTERVAL; with N≤4 and interval=1.5s,
# worst-case total is 4.5s < 5s M5 cycle budget.
# ⛔ CLOSE ORDERS MUST NEVER USE THIS LIMITER (IC Hard Constraint).
#    Closes go through dispatch_managed_close(), not this function.
_MODIFY_SLTP_MIN_INTERVAL: float = 1.5  # seconds between dispatches
_last_modify_sltp_time: float = 0.0
_modify_sltp_lock: threading.Lock = threading.Lock()


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
    # ── DQAF-20260728-003: Global anti-starvation rate limiter ──────
    # Micro-sleep to enforce minimum interval between consecutive
    # modify_sltp dispatches across ALL positions on the account.
    # Blocking (not rejection) ensures no position is starved —
    # every position gets its SL update within the same M5 cycle,
    # just serialised.  Worst-case ~4.5s total for 4 positions.
    # ⛔ CLOSE ORDERS NEVER ROUTED THROUGH HERE (IC Hard Constraint).
    global _last_modify_sltp_time
    with _modify_sltp_lock:
        _now = time.monotonic()
        _gap = _now - _last_modify_sltp_time
        if _gap < _MODIFY_SLTP_MIN_INTERVAL:
            _sleep_s = round(_MODIFY_SLTP_MIN_INTERVAL - _gap, 3)
            if _sleep_s > 0:
                time.sleep(_sleep_s)
            _now = time.monotonic()
        _last_modify_sltp_time = _now

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
    # ── DQAF-20260715-022: strategy attribution for modify_sltp journal entries ──
    # Explicitly pass strategy name in the payload so the bridge can use it
    # for journal attribution even when magic resolution is unavailable.
    # The bridge's _build_journal_entry path currently only resolves strategy
    # from magic (MAGIC_TO_STRATEGY); passing the strategy field enables a
    # future bridge-side fallback without waiting for bridge redeployment.
    if strategy_name:
        payload["strategy"] = strategy_name
    # Resolve magic from strategy name for correct journal attribution.
    # When strategy_name is empty, this is skipped → bridge uses its default
    # magic (90401) → journal shows __UNATTRIBUTED_BRIDGE_DEFAULT__.
    # The caller (management_phase.py) now has a magic-based fallback
    # (DQAF-20260715-022) that should prevent empty strategy_name from
    # reaching this point.
    if strategy_name:
        try:
            from core.contracts.strategy_magic import STRATEGY_TO_MAGIC

            _strat_magic = STRATEGY_TO_MAGIC.get(strategy_name, 0)
            if _strat_magic:
                payload["magic"] = _strat_magic
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            pass
    else:
        _logger.warning(
            "Trail dispatch with empty strategy_name — ticket=%s magic will "
            "default to bridge sentinel (90401).  Journal attribution lost.",
            pos_ticket,
        )
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
