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


def compute_and_dispatch_trail(
    *,
    config: Any,
    pos: Any,
    pm: Any,
    state: Any,
    mid: float | None,
    bid: float | None = None,
    ask: float | None = None,
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
    _be_skipped_price = False

    # ── Layer 1: Chandelier trailing stop ──
    if not getattr(pos, "cold_explore", False):
        _trail_sl = pm.compute_trail_stop(current_atr, ticket=pos.ticket)
        if _trail_sl is not None and abs(_trail_sl - pos.current_sl) >= config.exit_min_step:
            if pos.cycles_held >= pm.min_hold_cycles:
                _reasons.append("trail")
                _final_sl = _trail_sl
                # FIX-20260610-006: count trail tightenings for telemetry
                pos.trail_advances += 1
    else:
        _trail_sl = None

    # ── Breakeven check — only fires once per position ──
    if not pos.breakeven_triggered and pm.should_breakeven(mid, current_atr, ticket=pos.ticket):
        _be_triggered = True
        _be_sl = pos.entry_price
        _be_improves = (pos.side == "long" and _be_sl > _final_sl) or (
            pos.side == "short" and _be_sl < _final_sl
        )
        # ── FIX-20260629-196: Pre-Trade Feasibility Gateway ──
        # Validate breakeven SL against MT5 order-book microstructural constraints.
        # MT5 requires: SHORT SL ≥ Ask + StopLevel, LONG SL ≤ Bid − StopLevel.
        # Using dynamic spread-proportional buffer (×2.0) as a StopLevel proxy,
        # with exit_min_step as absolute floor.  Degrades gracefully to mid-based
        # check when bid/ask unavailable (price-degraded cycles).
        if _be_improves:
            _spread = (
                abs(ask - bid)
                if (bid is not None and ask is not None and bid > 0 and ask > 0)
                else 0.0
            )
            _min_market_distance = max(
                config.exit_min_step,  # Floor: minimum step
                round(_spread * 2.0, 8) if _spread > 0 else 0.0,  # Dynamic: 2× spread
            )
            if pos.side == "short":
                _be_feasible = (
                    _be_sl > ask + _min_market_distance
                    if ask is not None and ask > 0
                    else _be_sl > mid + _min_market_distance  # fallback to mid
                )
            else:
                _be_feasible = (
                    _be_sl < bid - _min_market_distance
                    if bid is not None and bid > 0
                    else _be_sl < mid - _min_market_distance  # fallback to mid
                )
            if not _be_feasible:
                _be_improves = False
                _be_skipped_price = True
        if _be_improves:
            _reasons.append("breakeven")
            _final_sl = _be_sl
            _be_dispatched = True
        pos.breakeven_triggered = True
    else:
        _be_skipped_price = False

    # ── Dynamic trailing TP ──
    _trail_tp = pm.compute_trail_tp(current_atr, ticket=pos.ticket)
    if _trail_tp is not None and abs(_trail_tp - pos.current_tp) >= config.exit_min_step:
        _reasons.append("tp")
        _final_tp = _trail_tp

    # ── FIX-20260707-009: Bracket Integrity Guard ──
    # Last line of defence before dispatch.  If the trailing SL has
    # overtaken the TP (or compute_trail_tp released TP as 0.0), the
    # bracket is invalid and MT5 will reject with "Invalid stops"
    # (retcode 10016).  Drop the TP modification and let trailing SL
    # fully manage the position.
    # Only fires when both SL and TP are non-trivial (>0) — a TP of 0.0
    # from the Dynamic Anchor release above is already correct and passes
    # through without further modification.
    if _final_tp > 0 and _final_sl > 0:
        if pos.side == "long" and _final_sl >= _final_tp:
            _final_tp = 0.0  # TP yields to trailing SL
            _reasons.append("tp_released_bracket_inversion")
        elif pos.side == "short" and _final_sl <= _final_tp:
            _final_tp = 0.0
            _reasons.append("tp_released_bracket_inversion")

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
                "breakeven_skipped_price": _be_skipped_price,
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
    try:
        _snap_path = Path(config.base_dir) / "position_snapshots.jsonl"

        # ── FIX-20260611-003: Data flywheel — enriched snapshot fields ──
        _entry_price = getattr(pos, "entry_price", 0) or 0
        _current_sl = getattr(pos, "current_sl", 0) or 0
        _current_tp = getattr(pos, "current_tp", 0) or 0
        _side = getattr(pos, "side", "?")
        _strategy = getattr(pos, "strategy_name", "?")

        # ── DQAF-20260621-034: state-machine-aware snapshot guard ──
        # _entry_price <= 0  → hard skip (irrecoverable — no anchor price)
        # _current_sl  <= 0  → force_init_snapshot (V3 cold-start SL sync
        #                       may have failed; write first sighting to pull
        #                       the position into trail lifecycle anyway)
        if _entry_price <= 0:
            import logging as _snap_log

            _snap_log.getLogger(__name__).warning(
                "[DATA_ASSERT] Position snapshot SKIPPED: ticket=%s "
                "entry_price=%s — entry price missing or zero",
                pos.ticket,
                _entry_price,
            )
        else:
            _sl_uninitialized = _current_sl <= 0
            _snap = json.dumps(
                {
                    "ticket": pos.ticket,
                    "time": _utc_iso(),
                    "side": _side,
                    "strategy": _strategy,
                    "entry_price": _entry_price,
                    "current_sl": _current_sl,
                    "current_tp": _current_tp,
                    "bars_held": pos.cycles_held,
                    "unrealized_pnl_r": round(_pnl_r, 6),
                    "current_volatility": _vol_change,
                    "trailing_sl_distance": _trail_dist,
                    "current_atr": round(current_atr, 4),
                    "entry_atr": round(pos.entry_atr, 4),
                    **({} if not _sl_uninitialized else {"sl_uninitialized": True}),
                },
                ensure_ascii=False,
            )
            _snap_path.parent.mkdir(parents=True, exist_ok=True)
            with open(_snap_path, "a", encoding="utf-8") as _sf:
                _sf.write(_snap + "\n")
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        pass

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
        # ── DQAF-064 §2: Defer optimistic SL/TP update if rejection streak active ──
        _rejection_active = getattr(pos, "trail_rejection_streak", 0) > 0
        if _rejection_active:
            print(
                json.dumps(
                    {
                        "event": "trail_update_suppressed",
                        "time": _utc_iso(),
                        "ticket": pos.ticket,
                        "rejection_streak": pos.trail_rejection_streak,
                        "would_be_sl": round(_final_sl, 3) if _sl_changed else None,
                        "would_be_tp": round(_final_tp, 3) if _tp_changed else None,
                        "reason": "pending_rejection_resolution",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        else:
            # Update local state and log AFTER dispatch (only when no rejection streak)
            if _sl_changed:
                pos.current_sl = _final_sl
            if _tp_changed:
                pos.current_tp = _final_tp

        # Log trail movement regardless of suppression
        if _sl_changed and not _rejection_active:
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
        if _tp_changed and not _rejection_active:
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
