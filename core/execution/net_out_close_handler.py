"""Net-out close dispatch handler — Strangler Fig extraction from live_cycle.py.

FIX-20260611-018 Phase 1: Extracted the ``_net_out_close_dispatch_fn``
inline closure (L4967-5076, ~80 lines) from
``execute_live_cycle()`` → ``handle_net_out_close()``.

Pure function contract (Strangler Fig Iron Law):
  - Receives explicitly-needed fields via parameters
  - Returns (result_dict, updated_streak, updated_cooldown)
  - Side effects: watchdog execution, stdout JSON events
  - Caller writes cooldown state mutations back to LiveCycleState

Prior art:
  - FIX-20260517-022: Phase 3 ExitWatchdog bypass wiring
  - FIX-20260606-138-Phase2: Cross-cycle exit retry cooldown (DQAF-005)
  - FIX-20260606-138-Phase3: PnL passthrough to notify_trade (DQAF-006)
  - ReB: ``metric_pollution_via_rejected_retries``
"""

from __future__ import annotations

import json
import time as _time
from collections.abc import Callable
from typing import Any

from core.execution.live_order_sender import dispatch_live_order as _net_dispatch


def handle_net_out_close(
    *,
    ctx: Any,  # DispatchContext — immutable routing bundle (DQAF-20260615-010/Phase1)
    payload: dict[str, Any],
    exit_reject_streak: dict[int, int],
    exit_reject_cooldown: dict[int, float],
    known_open_tickets: dict[int, dict[str, Any]],
    mid_price: float | None,
    exit_watchdog: Any,
    utc_iso_fn: Callable[[], str],
) -> tuple[dict[str, Any], dict[int, int], dict[int, float]]:
    """Execute a net-out close through the exit watchdog with cooldown gating.

    Extracted from ``live_cycle.py`` inline closure ``_net_out_close_dispatch_fn``.

    Args:
        ctx: :class:`DispatchContext` — all routing params (adapter_name, base_dir,
            symbol, mt5_terminal_path, zmq endpoints, protection flag).
        payload: Close dispatch payload with keys:
            position_ticket, volume, side, comment, magic, brain_ids, pnl.
        exit_reject_streak: Per-ticket consecutive reject counter.
        exit_reject_cooldown: Per-ticket cooldown deadline (Unix timestamp).
        known_open_tickets: Map of ticket → journal entry for PnL estimation.
        mid_price: Current mid price for PnL estimation.
        exit_watchdog: ExitWatchdog instance for safe close execution.
        ignore_protection_flag: Whether to skip protection flag check.
        protection_flag_path: Path to protection flag file.
        mt5_terminal_path: Path to MT5 terminal executable.
        utc_iso_fn: Callable returning current UTC ISO timestamp string.

    Returns:
        Tuple of (result_dict, updated_streak, updated_cooldown):
        - result_dict: ``{"dispatched": bool, "intent_id": str, "pnl": float|None}``
        - updated_streak: Mutated copy of exit_reject_streak.
        - updated_cooldown: Mutated copy of exit_reject_cooldown.
    """
    _utc_iso = utc_iso_fn
    _ticket = int(payload.get("position_ticket", 0))
    _vol = float(payload.get("volume", 0.01))
    _side = str(payload.get("side", "long"))
    _reason = str(payload.get("comment", "net_out"))
    _magic = int(payload.get("magic", 0))
    _brain_ids = payload.get("brain_ids")

    # ── Phase 2: cross-cycle exit retry cooldown ──
    # If this position has been rejected ≥3 consecutive cycles, skip the
    # exit attempt for 10 cycles to prevent retry storms (DQAF-20260606-005).
    _now_ts = _time.time()
    _cd_until = exit_reject_cooldown.get(_ticket, 0.0)
    if _now_ts < _cd_until:
        _remaining = int(_cd_until - _now_ts)
        print(
            json.dumps(
                {
                    "event": "exit_cooldown_skipped",
                    "time": _utc_iso(),
                    "ticket": _ticket,
                    "reason": _reason,
                    "cooldown_remaining_s": _remaining,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return (
            {"dispatched": False, "intent_id": "", "reason": "exit_cooldown_active"},
            exit_reject_streak,
            exit_reject_cooldown,
        )

    # ── Calculate estimated PnL for journal recording ──
    _net_pnl = payload.get("pnl")
    if _net_pnl is None and mid_price is not None and _ticket:
        _net_entry = known_open_tickets.get(_ticket, {})
        _net_ep = _net_entry.get("entry_price")
        if not _net_ep:
            _net_ep = _net_entry.get("detail", {}).get("request", {}).get("price")
        if _net_ep and _vol:
            if _side == "long":
                _net_pnl = round((mid_price - float(_net_ep)) * _vol, 2)
            elif _side == "short":
                _net_pnl = round((float(_net_ep) - mid_price) * _vol, 2)

    # ── Execute through watchdog ──
    _wd = exit_watchdog.execute_exit(
        position_ticket=_ticket,
        volume=_vol,
        side=_side,
        reason=_reason,
        magic=_magic,
        dispatch_fn=lambda p: _net_dispatch(
            base_dir=ctx.base_dir,
            broker=None,
            symbol=ctx.symbol,
            execution_payload=p,
            skip_price_guard=True,
            ignore_protection_flag=ctx.ignore_protection_flag,
            protection_flag_path=ctx.protection_flag_path,
            adapter_name=ctx.adapter_name,
            # TECH_DEBT-010 Blueprint C: close 路径同样显式注入 per-symbol endpoint。
            extensions={
                "mt5_terminal_path": ctx.mt5_terminal_path,
                "zmq_order_endpoint": ctx.zmq_order_endpoint,
            },
        ),
        brain_ids=_brain_ids,
        pnl=_net_pnl,
    )

    # ── Phase 2: update reject streak / cooldown ──
    _streak = exit_reject_streak
    _cooldown = exit_reject_cooldown
    _tkt_key = _ticket
    if _wd.success:
        _streak.pop(_tkt_key, None)
        _cooldown.pop(_tkt_key, None)
    else:
        _consecutive = _streak.get(_tkt_key, 0) + 1
        _streak[_tkt_key] = _consecutive
        if _consecutive >= 3:
            _cooldown_s = 300  # 10 cycles × 30s
            _cd_deadline = _now_ts + _cooldown_s
            _cooldown[_tkt_key] = _cd_deadline
            print(
                json.dumps(
                    {
                        "event": "exit_cooldown_activated",
                        "time": _utc_iso(),
                        "ticket": _ticket,
                        "consecutive_rejects": _consecutive,
                        "cooldown_seconds": _cooldown_s,
                        "message": (
                            f"Position exit has been rejected "
                            f"{_consecutive} times consecutively. "
                            "Cooling down for 10 cycles to "
                            "prevent retry storm."
                        ),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    return (
        {
            "dispatched": _wd.success,
            "intent_id": "",
            "pnl": _net_pnl,  # FIX-138-Phase3: pass PnL through to notify_trade
        },
        _streak,
        _cooldown,
    )
