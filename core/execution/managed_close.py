"""Managed position close dispatcher — issue close orders + record exits.

Extracted from live_cycle.py per the Strangler Fig pattern (Directive 4, P3.2).
Handles watchdog-protected exit dispatch, re-entry guard recording, ghost-volume
audit, and PnL/budget tracking for managed position closes.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any


def _utc_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def dispatch_managed_close(
    config: Any,  # LiveCycleConfig
    pos: Any,
    *,
    reason: str = "",
    mid: float | None = None,
    state: Any = None,
    strategy_name: str = "",
    exit_confidence: float = 0.0,
    exit_watchdog: Any = None,
    mt5_worker: Any = None,
    exit_urgency: float = 0.5,
    factor_breakdown: dict[str, float] | None = None,
) -> bool:
    """Issue a close order for a managed position and record exit for re-entry guard.

    Returns True if the close was dispatched successfully, False otherwise.
    Callers MUST only clear the position from the position manager when True.

    When *exit_watchdog* is provided, wraps the dispatch with heartbeat-protected
    retry and escalation (Pitfall 3 safeguard).
    """
    from core.execution.live_order_sender import dispatch_live_order
    from core.execution.reentry_guard import ExitRecord, ensure_reentry_state
    from core.runtime.fault_handler import FaultLevel, FaultTolerantContext, log_and_continue

    # Estimate PnL so the journal entry has it (reconciliation corrects it later)
    pnl = None
    entry_price = getattr(pos, "entry_price", None)
    if entry_price is not None and mid is not None and pos.volume:
        if pos.side == "long":
            pnl = round((mid - entry_price) * pos.volume, 2)
        elif pos.side == "short":
            pnl = round((entry_price - mid) * pos.volume, 2)

    # ── Record exit for re-entry guard ──
    if state is not None and strategy_name and pos.side in ("long", "short"):
        try:
            exit_price = mid if mid is not None else 0.0
            record = ExitRecord(
                timestamp=time.time(),
                strategy_name=strategy_name,
                direction=pos.side,
                reason=reason,
                confidence=exit_confidence,
                price=exit_price,
                ticket=pos.ticket,
            )
            reentry_state = ensure_reentry_state(state._reentry_states, strategy_name)
            reentry_state.record_exit(record)
            print(
                json.dumps(
                    {
                        "event": "exit_recorded",
                        "time": _utc_iso(),
                        "strategy": strategy_name,
                        "direction": pos.side,
                        "raw_reason": reason[:80],
                        "classified_category": record.category,
                        "exit_confidence": round(exit_confidence, 4),
                        "ticket": pos.ticket,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            # ── Cut 1: Record exit to CooldownRegistry ──
            if hasattr(state, "_cooldown_registry") and state._cooldown_registry is not None:
                try:
                    _cd_entry = state._cooldown_registry.record_exit(
                        strategy=strategy_name,
                        direction=pos.side,
                        reason=reason,
                        timestamp=time.time(),
                    )
                    print(
                        json.dumps(
                            {
                                "event": "cooldown_registered",
                                "time": _utc_iso(),
                                "strategy": strategy_name,
                                "direction": pos.side,
                                "cooldown_sec": _cd_entry["cooldown_sec"],
                                "cooldown_type": _cd_entry["type"],
                                "exit_reason": reason[:80],
                                "deadline_iso": datetime.fromtimestamp(
                                    _cd_entry["deadline"], tz=UTC
                                )
                                .isoformat()
                                .replace("+00:00", "Z"),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                except Exception as _cd_exc:
                    print(
                        json.dumps(
                            {
                                "event": "cooldown_record_failed",
                                "time": _utc_iso(),
                                "strategy": strategy_name,
                                "error": f"{type(_cd_exc).__name__}: {str(_cd_exc)[:200]}",
                                "level": "LOG",
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
        except Exception as _ew_exc:
            print(
                json.dumps(
                    {
                        "event": "exit_recording_failed",
                        "time": _utc_iso(),
                        "strategy": strategy_name,
                        "reason": reason[:80],
                        "error": f"{type(_ew_exc).__name__}: {str(_ew_exc)[:200]}",
                        "level": "LOG",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    # ── Pillar 4: Ghost-volume audit ──
    _close_volume = pos.volume
    _expected = getattr(pos, "expected_remaining_volume", pos.volume)
    if (
        _expected > 0
        and _close_volume < _expected
        and "partial" not in reason
        and "net_out" not in reason
    ):
        _true_vol = _close_volume
        if mt5_worker is not None:
            with FaultTolerantContext(
                level=FaultLevel.CRASH,
                component="MT5_IPC:positions_get:ghost_volume_audit",
            ):
                _mt5_positions = mt5_worker.positions_get(ticket=pos.ticket)
                if _mt5_positions and len(_mt5_positions) > 0:
                    _true_vol = float(_mt5_positions[0].volume)
        print(
            json.dumps(
                {
                    "event": "ghost_volume_audit",
                    "time": _utc_iso(),
                    "ticket": pos.ticket,
                    "system_volume": _close_volume,
                    "expected_remaining": _expected,
                    "mt5_true_volume": _true_vol,
                    "action": "using_mt5_ground_truth"
                    if _true_vol != _close_volume
                    else "no_discrepancy",
                    "reason": reason[:60],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        _close_volume = _true_vol

    payload: dict[str, Any] = {
        "action": "close",
        "side": pos.side,
        "position_ticket": pos.ticket,
        "volume": _close_volume,
        "comment": reason,
    }
    if pnl is not None:
        payload["pnl"] = pnl
    _close_brain_ids = getattr(pos, "supporting_brain_ids", None)
    if _close_brain_ids:
        payload["brain_ids"] = _close_brain_ids
    if strategy_name:
        with log_and_continue(component="MagicAttribution:close"):
            from core.contracts.strategy_magic import STRATEGY_TO_MAGIC

            _strat_magic = STRATEGY_TO_MAGIC.get(strategy_name, 0)
            if _strat_magic:
                payload["magic"] = _strat_magic
    if state is not None:
        _open_entry = state.known_open_tickets.get(pos.ticket, {})
        _open_msg_id = _open_entry.get("message_id", "")
        if _open_msg_id:
            payload["open_message_id"] = _open_msg_id

    # FIX-20260610-006: structured trail telemetry —
    # records initial_sl, final_sl, and trail_advances so downstream
    # journal consumers can distinguish "original SL hit" from "trailed SL hit"
    payload["trail_contribution"] = {
        "initial_sl": getattr(pos, "initial_sl", 0.0),
        "final_sl": getattr(pos, "current_sl", 0.0),
        "trail_advances": getattr(pos, "trail_advances", 0),
    }

    # ── Dispatch with optional watchdog protection ──
    _close_dispatched = False
    if exit_watchdog is not None:
        try:
            wd_result = exit_watchdog.execute_exit(
                position_ticket=pos.ticket,
                volume=pos.volume,
                side=pos.side,
                reason=reason,
                magic=payload.get("magic", 0),
                dispatch_fn=lambda p: dispatch_live_order(
                    base_dir=config.base_dir,
                    broker=None,
                    symbol=config.symbol,
                    execution_payload=p,
                    skip_price_guard=True,
                    ignore_protection_flag=config.ignore_protection_flag,
                    protection_flag_path=config.protection_flag_path,
                    adapter_name=config.adapter_name,
                    extensions={"mt5_terminal_path": config.mt5_terminal_path},
                ),
                brain_ids=_close_brain_ids,
                pnl=pnl,
                exit_urgency=exit_urgency,
                factor_breakdown=factor_breakdown,
            )
            if not wd_result.success:
                print(
                    json.dumps(
                        {
                            "event": "exit_watchdog_failed",
                            "time": _utc_iso(),
                            "ticket": pos.ticket,
                            "final_status": wd_result.final_status,
                            "total_attempts": wd_result.total_attempts,
                            "alerts": wd_result.alerts,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            elif state is not None and pnl is not None:
                _close_dispatched = True
                with log_and_continue(component="ExitWatchdog:PnL_store"):
                    _oe = state.known_open_tickets.get(pos.ticket, {})
                    if _oe:
                        _oe["_engine_close_pnl"] = pnl
        except Exception as _wd_exc:
            print(
                json.dumps(
                    {
                        "event": "exit_watchdog_exception",
                        "time": _utc_iso(),
                        "error": f"{type(_wd_exc).__name__}: {str(_wd_exc)[:200]}",
                        "reason": reason,
                        "level": "CRASH",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    else:
        try:
            dispatch_live_order(
                base_dir=config.base_dir,
                broker=None,
                symbol=config.symbol,
                execution_payload=payload,
                skip_price_guard=True,
                ignore_protection_flag=config.ignore_protection_flag,
                protection_flag_path=config.protection_flag_path,
                adapter_name=config.adapter_name,
                extensions={"mt5_terminal_path": config.mt5_terminal_path},
            )
            _close_dispatched = True
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "close_dispatch_error",
                        "time": _utc_iso(),
                        "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                        "level": "CRASH",
                        "reason": reason,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    # ── After successful close: remove tracking + record PnL ──
    if _close_dispatched and state is not None and pos.ticket:
        state.known_open_tickets.pop(pos.ticket, None)
        if pnl is not None and strategy_name:
            _pnl_pct = float(pnl) / 1000.0
            state._pending_budget_records.append(
                {
                    "strategy": strategy_name,
                    "pnl": _pnl_pct,
                    "is_win": pnl > 0,
                }
            )
            if pnl < 0:
                state._pending_sl_records.append(
                    {
                        "strategy": strategy_name,
                        "timestamp": time.time(),
                    }
                )

    # ── FIX-20260608-005: close notification (managed close path) ──
    # Intent-driven: operator needs real-time push that the engine decided
    # to close.  Fire-and-forget — never blocks or throws from managed close.
    if _close_dispatched and state is not None:
        import contextlib as _ctxlib

        with _ctxlib.suppress(Exception):
            _ah = getattr(state, "alert_hub", None)
            if _ah is not None:
                _ah.notify_trade(
                    action="close",
                    symbol=config.symbol,
                    side=pos.side,
                    volume=pos.volume,
                    price=mid,
                    pnl=pnl,
                )

    return _close_dispatched
