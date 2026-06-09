"""Trail SL/TP Dispatch — Strangler Fig extraction from live_cycle.py.

FIX-20260609-007: Extracted from ``_execute_management_phase()`` L957-1123.
Computes Chandelier trail SL, breakeven, and trail TP; dispatches as a
single modify_sltp; records diagnostics and position snapshots.

Pure function contract (Strangler Fig Iron Law):
  - Receives explicitly-needed fields via parameters
  - Returns computed results; caller writes back to state
  - Side effects: dispatch, print diagnostics, write snapshot file
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.runtime.fault_handler import log_and_continue


def compute_and_dispatch_trail(
    *,
    config: Any,
    pos: Any,
    pm: Any,
    state: Any,
    mid: float | None,
    current_atr: float,
    strategy_name: str = "",
    utc_iso_fn: Any = None,
    dispatch_modify_trail_fn: Any = None,  # _dispatch_modify_trail from live_cycle
) -> dict[str, Any]:
    """Compute trail SL, breakeven, trail TP and dispatch.

    Args:
        config: LiveCycleConfig with exit_min_step, base_dir, exit_min_sl_step etc.
        pos: ActivePosition to manage.
        pm: ActivePositionManager.
        state: LiveCycleState (for broker/limit_monitor access via dispatch).
        mid: Current mid price.
        current_atr: Current ATR(14) value.
        strategy_name: Strategy name for dispatch attribution.
        utc_iso_fn: Callable returning UTC ISO timestamp string.

    Returns:
        dict with keys: final_sl, final_tp, reasons, sl_changed, tp_changed,
                        be_triggered, be_dispatched, trail_sl, trail_tp.
        Caller writes final_sl/tp back to pos, handles be_triggered downstream.
    """
    _utc_iso = utc_iso_fn if utc_iso_fn is not None else (lambda: "")

    _final_sl = pos.current_sl
    _final_tp = pos.current_tp
    _reasons: list[str] = []
    _old_sl = pos.current_sl
    _old_tp = pos.current_tp
    _trail_sl: float | None = None
    _be_triggered = False
    _be_dispatched = False

    # ── Layer 1: Chandelier trailing stop ──
    if not getattr(pos, "cold_explore", False):
        _trail_sl = pm.compute_trail_stop(current_atr, ticket=pos.ticket)
        if _trail_sl is not None and abs(_trail_sl - pos.current_sl) >= config.exit_min_step:
            if pos.cycles_held >= pm.min_hold_cycles:
                _reasons.append("trail")
                _final_sl = _trail_sl
    else:
        _trail_sl = None

    # ── Breakeven check — only fires once per position ──
    if not pos.breakeven_triggered and pm.should_breakeven(mid, current_atr, ticket=pos.ticket):
        _be_triggered = True
        _be_sl = pos.entry_price
        _be_improves = (pos.side == "long" and _be_sl > _final_sl) or (
            pos.side == "short" and _be_sl < _final_sl
        )
        if _be_improves:
            _reasons.append("breakeven")
            _final_sl = _be_sl
            _be_dispatched = True
        pos.breakeven_triggered = True

    # ── Dynamic trailing TP ──
    _trail_tp = pm.compute_trail_tp(current_atr, ticket=pos.ticket)
    if _trail_tp is not None and abs(_trail_tp - pos.current_tp) >= config.exit_min_step:
        _reasons.append("tp")
        _final_tp = _trail_tp

    # ── Diagnostic log ──
    print(
        json.dumps(
            {
                "event": "management_phase_diag",
                "time": _utc_iso(),
                "ticket": pos.ticket,
                "side": pos.side,
                "entry": round(pos.entry_price, 3),
                "lowest_low": round(pos.lowest_low, 3),
                "highest_high": round(pos.highest_high, 3),
                "current_sl": round(pos.current_sl, 3),
                "current_tp": round(pos.current_tp, 3),
                "trail_mult": round(pos.trail_multiplier, 4),
                "current_atr": round(current_atr, 4),
                "entry_atr": round(pos.entry_atr, 4),
                "trail_sl_candidate": round(_trail_sl, 3) if _trail_sl is not None else None,
                "trail_fired": _trail_sl is not None
                and abs(_trail_sl - pos.current_sl) >= config.exit_min_step,
                "breakeven_fired": _be_triggered,
                "breakeven_improves": _be_dispatched,
                "cycles_held": pos.cycles_held,
                "breakeven_triggered_flag": pos.breakeven_triggered,
                "final_sl": round(_final_sl, 3),
                "final_tp": round(_final_tp, 3),
                "reasons": _reasons,
                "exit_min_step": config.exit_min_step,
                "pm_min_step": getattr(pm, "min_step", "N/A"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    # ── Position snapshot for meta-classifier training ──
    _pnl_r = 0.0
    if mid is not None and pos.entry_price > 0 and pos.entry_atr > 0:
        _pnl_r = (
            (mid - pos.entry_price) / pos.entry_atr
            if pos.side == "long"
            else (pos.entry_price - mid) / pos.entry_atr
        )
    _vol_change = round(current_atr / pos.entry_atr, 4) if pos.entry_atr > 0 else 1.0
    _trail_dist = round(abs(pos.current_sl - pos.entry_price), 3) if pos.current_sl > 0 else 0.0
    with log_and_continue(component="PositionSnapshot:record"):
        _snap_path = Path(config.base_dir) / "position_snapshots.jsonl"
        _snap = json.dumps(
            {
                "ticket": pos.ticket,
                "time": _utc_iso(),
                "bars_held": pos.cycles_held,
                "unrealized_pnl_r": round(_pnl_r, 6),
                "current_volatility": _vol_change,
                "trailing_sl_distance": _trail_dist,
                "current_atr": round(current_atr, 4),
                "entry_atr": round(pos.entry_atr, 4),
            },
            ensure_ascii=False,
        )
        _snap_path.parent.mkdir(parents=True, exist_ok=True)
        with open(_snap_path, "a", encoding="utf-8") as _sf:
            _sf.write(_snap + "\n")

    # ── Single dispatch ──
    _sl_changed = abs(_final_sl - pos.current_sl) >= config.exit_min_step
    _tp_changed = abs(_final_tp - pos.current_tp) >= config.exit_min_step
    if _reasons:
        _dispatch_modify_trail = dispatch_modify_trail_fn
        _dispatch_modify_trail(
            config,
            pos,
            _final_sl,
            _final_tp,
            reason="+".join(_reasons),
            brain_ids=pos.supporting_brain_ids,
            strategy_name=strategy_name,
            state=state,
        )
        # Update local state and log AFTER dispatch
        if _sl_changed:
            pos.current_sl = _final_sl
            print(
                json.dumps(
                    {
                        "event": "trail_stop_moved",
                        "time": _utc_iso(),
                        "ticket": pos.ticket,
                        "side": pos.side,
                        "old_sl": round(_old_sl, 3),
                        "new_sl": round(_final_sl, 3),
                        "highest_high": round(pos.highest_high, 3),
                        "trail_mult": pos.trail_multiplier,
                        "merged_reasons": "+".join(_reasons),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        if _tp_changed:
            pos.current_tp = _final_tp
            print(
                json.dumps(
                    {
                        "event": "trail_tp_moved",
                        "time": _utc_iso(),
                        "ticket": pos.ticket,
                        "side": pos.side,
                        "old_tp": round(_old_tp, 3),
                        "new_tp": round(_final_tp, 3),
                        "atr_ratio": round(current_atr / max(pos.entry_atr, 0.01), 2),
                        "merged_reasons": "+".join(_reasons),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    return {
        "final_sl": _final_sl,
        "final_tp": _final_tp,
        "reasons": _reasons,
        "sl_changed": _sl_changed,
        "tp_changed": _tp_changed,
        "be_triggered": _be_triggered,
        "be_dispatched": _be_dispatched,
        "trail_sl": _trail_sl,
        "trail_tp": _trail_tp if "_trail_tp" in dir() else None,
    }
