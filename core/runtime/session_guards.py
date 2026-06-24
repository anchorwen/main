"""Session detection + intraday drawdown guards — Strangler Fig #27 from live_cycle.py.

Extracted from live_cycle.py:execute_live_cycle() (~219 lines).
Handles market-session detection, intraday equity drawdown kill switch,
and force-close on severe drawdown events.
"""

from __future__ import annotations

import json
from typing import Any

from core.runtime.fault_handler import FaultLevel, FaultTolerantContext
from core.runtime.time_utils import _utc_iso


def _emit(event: str, /, **fields: Any) -> None:
    """Emit a structured JSON event to stdout."""
    payload: dict[str, Any] = {"event": event, "time": _utc_iso()}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _log_cycle_end(iteration: int) -> None:
    _emit("cycle_end", iteration=iteration)


def run_session_guards(
    config: Any,
    state: Any,
    mt5_worker: Any,
) -> tuple[bool, dict[str, Any]]:
    """Run session detection + data quality guards at cycle start.

    Returns (skip_cycle: bool, session_info: dict).
    skip_cycle=True means the cycle should be skipped (market closed,
    drawdown kill activated, or circuit breaker open).

    Modifies state inline: sets block_new_entries, creates IntradayDrawdownKill,
    and may dispatch force-close orders.
    """
    if config.no_mt5:
        return False, {}  # no MT5 connection → skip session checks

    try:
        from core.execution.pre_trade_guards import detect_session

        session_info = detect_session(market_type=getattr(config, "market_type", "forex_24_5"))
        if session_info.get("risk_tier") == "off":
            _log_cycle_end(state.loop_iteration)
            return True, session_info  # market closed, skip cycle

        # ── Intraday drawdown kill switch ──
        if config.intraday_drawdown_kill_enabled:
            with FaultTolerantContext(
                level=FaultLevel.DEGRADE,
                component="MT5_IPC:account_info:drawdown_kill",
            ):
                _acc = mt5_worker.account_info()
            try:
                from core.execution.pre_trade_guards import IntradayDrawdownKill

                if state.intraday_dd_kill is None:
                    state.intraday_dd_kill = IntradayDrawdownKill(
                        kill_pct=config.intraday_drawdown_kill_pct,
                        force_close_enabled=config.intraday_dd_force_close,
                        force_close_pct=config.intraday_dd_force_close_pct,
                    )
                if _acc is not None:
                    _eq = float(getattr(_acc, "equity", 0))
                    dd_result = state.intraday_dd_kill.update(_eq)
                    if dd_result.get("blocked"):
                        state.block_new_entries = True
                        _emit(
                            "intraday_drawdown_kill",
                            drawdown_pct=dd_result["drawdown_pct"],
                            high_watermark=dd_result["high_watermark"],
                            current_equity=dd_result["current_equity"],
                            force_close=dd_result.get("force_close", False),
                            circuit_breaker="OPEN — new entries blocked",
                        )
                    elif state.block_new_entries:
                        # DD recovered — clear the block
                        state.block_new_entries = False
                        _emit(
                            "intraday_drawdown_recovered",
                            circuit_breaker="CLOSED — new entries allowed",
                        )
                        # Force-close existing positions when drawdown is severe
                        if dd_result.get("force_close") and state.position_manager is not None:
                            _pos = state.position_manager.get_position()
                            if _pos is not None:
                                try:
                                    from core.execution.live_order_sender import (
                                        dispatch_live_order,
                                    )

                                    _dd_brain_ids = getattr(_pos, "supporting_brain_ids", None)
                                    _dd_payload: dict[str, Any] = {
                                        "action": "close",
                                        "side": _pos.side,
                                        "position_ticket": _pos.ticket,
                                        "volume": _pos.volume,
                                        "comment": "intraday_drawdown_force_close",
                                    }
                                    if _dd_brain_ids:
                                        _dd_payload["brain_ids"] = _dd_brain_ids

                                    _dd_result = dispatch_live_order(
                                        base_dir=config.base_dir,
                                        broker=None,
                                        symbol=config.symbol,
                                        execution_payload=_dd_payload,
                                        skip_price_guard=True,
                                        ignore_protection_flag=config.ignore_protection_flag,
                                        protection_flag_path=config.protection_flag_path,
                                        adapter_name=config.adapter_name,
                                        extensions={"mt5_terminal_path": config.mt5_terminal_path},
                                    )
                                    _emit(
                                        "force_close_executed",
                                        ticket=_pos.ticket,
                                        reason="intraday_drawdown",
                                        dispatched=_dd_result.get("dispatched", False),
                                    )
                                except (
                                    RuntimeError,
                                    ValueError,
                                    KeyError,
                                    TypeError,
                                    OSError,
                                ) as _dd_exc:
                                    _emit(
                                        "force_close_error",
                                        error=str(_dd_exc),
                                    )
                if dd_result.get("blocked"):
                    return True, session_info  # skip cycle — drawdown kill active
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as _dd_setup_exc:
                # DEGRADE — intraday DD check setup failed, continue without it
                _emit("intraday_drawdown_kill_error", error=str(_dd_setup_exc))
        # ── Feature freshness check ──
        # DQAF-20260623-067: Use getattr with safe default so a missing
        # dataclass field does not cause a silent fail-open AttributeError.
        # Fail-closed: if the attribute is absent for any reason, treat
        # the buffers as cold and skip the cycle.
        if not getattr(state, "_feature_buffers_warm", False):
            _log_cycle_end(state.loop_iteration)
            return True, session_info  # skip cycle — insufficient warm-up

        # ── Circuit breaker check ──
        if getattr(state, "circuit_breaker", None) is not None:
            if state.circuit_breaker.is_open():
                _emit(
                    "circuit_breaker_entries_blocked",
                    state=state.circuit_breaker.state.value,
                    opened_at=state.circuit_breaker.opened_at,
                )
                _log_cycle_end(state.loop_iteration)
                return True, {}  # skip cycle — circuit breaker open (no session_info)

    except (AttributeError, TypeError) as _state_exc:
        # ── DQAF-20260623-067: State integrity errors → FAIL-CLOSED ──
        # Missing dataclass fields, type mismatches — the system state is
        # structurally corrupted.  Letting the cycle continue would mean
        # trading on unverified state.  Skip the cycle and alert.
        _emit(
            "session_guard_state_integrity_error",
            error=str(_state_exc),
            error_type=type(_state_exc).__name__,
            action="fail_closed_skip_cycle",
        )
        _log_cycle_end(state.loop_iteration)
        return True, session_info  # skip cycle — state integrity unverified
    except (RuntimeError, ValueError, KeyError, OSError) as _session_exc:
        # ── Transient / data-quality errors → FAIL-OPEN ──
        # MT5 timeouts, network blips, calendar I/O — the guard itself is
        # non-critical; failing open lets the cycle proceed with the other
        # downstream safety nets (price guards, circuit breaker, etc.).
        _emit("session_guard_error", error=str(_session_exc))
        # Fail-open on session detection failure — let the cycle continue
    return False, session_info
