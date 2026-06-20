"""Real-time trade notification — Strangler Fig #35 from live_cycle.py.

Extracted from live_cycle.py (~61 lines).  Sends DingTalk notifications
for dispatched trades with dedup per position_ticket to prevent retry
storms from flooding the alert channel.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from core.runtime.time_utils import _utc_iso


def _emit(event: str, /, **fields: Any) -> None:
    payload: dict[str, Any] = {"event": event, "time": _utc_iso()}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def notify_dispatched_trades(
    dispatch_results: list[Any],
    state: Any,
    symbol: str,
    *,
    emit_close_notification_fn: Any = None,
) -> None:
    """Send real-time trade notifications for dispatched strategies.

    Deduplicates close notifications per position_ticket to prevent
    retry storms (DQAF-006).  Open notifications are fire-and-forget.

    Args:
        dispatch_results: List of DispatchResult from exec_queue.flush().
        state: LiveCycleState, reads ``alert_hub``.
        symbol: Trading symbol.
    """
    _notified_tickets: set[int] = set()
    for dr in dispatch_results:
        if not dr.dispatched:
            continue
        _ah = getattr(state, "alert_hub", None)
        if _ah is None:
            continue

        _action = "open" if dr.reason != "net_out_close" else "close"
        _tkt = (
            dr.net_out_ticket_update.get("old_ticket", 0)
            if getattr(dr, "net_out_ticket_update", None)
            else 0
        )
        if _action == "close" and _tkt:
            if _tkt in _notified_tickets:
                continue
            _notified_tickets.add(_tkt)
        if _action == "close":
            _emit_close_notification(
                _ah=_ah,
                _sym=symbol,
                _side=dr.direction,
                _vol=dr.volume,
                _price=dr.price if hasattr(dr, "price") else None,
                _pnl=dr.pnl,
            )
        else:
            with contextlib.suppress(Exception):
                _ah.notify_trade(
                    action="open",
                    symbol=symbol,
                    side=dr.direction,
                    volume=dr.volume,
                    price=dr.price if hasattr(dr, "price") else None,
                    pnl=dr.pnl,
                )

    # Log dispatched/skipped strategies
    for dr in dispatch_results:
        _emit(
            "strategy_dispatched" if dr.dispatched else "strategy_skipped",
            strategy=dr.strategy_name,
            magic=dr.magic,
            dispatched=dr.dispatched,
            reason=dr.reason,
        )
