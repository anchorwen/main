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

from core.execution.trail_stop_engine import compute_rr_floor_price


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
    pre_close_ctx: Any = None,  # PreCloseContext — institutional pre-close risk overrides
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
        pre_close_ctx: PreCloseContext — when in pre-close window, contains
            pre-computed multiplier overrides for trail/breakeven/TP.
            None or in_pre_close=False → zero overhead (all multipliers = 1.0).

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

    # ── Pre-close context extraction (zero-cost when None or in_pre_close=False) ──
    _pcc = pre_close_ctx
    _trail_mult_override = (
        getattr(_pcc, "trail_atr_mult", 1.0) if _pcc is not None and _pcc.in_pre_close else None
    )
    _be_mult_override = (
        getattr(_pcc, "breakeven_mult", 1.0) if _pcc is not None and _pcc.in_pre_close else None
    )
    _disable_tp = bool(getattr(_pcc, "disable_dynamic_tp", False)) if _pcc is not None else False

    # ── Layer 1: Chandelier trailing stop ──
    if not getattr(pos, "cold_explore", False):
        _trail_sl = pm.compute_trail_stop(
            current_atr, ticket=pos.ticket, pre_close_atr_mult_override=_trail_mult_override
        )
        if _trail_sl is not None and abs(_trail_sl - pos.current_sl) >= config.exit_min_step:
            if pos.cycles_held >= pm.min_hold_cycles:
                _reasons.append("trail")
                _final_sl = _trail_sl
                # FIX-20260610-006: count trail tightenings for telemetry
                pos.trail_advances += 1
    else:
        _trail_sl = None

    # ── Spread & market-distance floor (shared by breakeven, SL, and trailing TP gates) ──
    _spread = (
        abs(ask - bid) if (bid is not None and ask is not None and bid > 0 and ask > 0) else 0.0
    )
    _min_market_distance = max(
        config.exit_min_step,  # Floor: minimum step
        round(_spread * 2.0, 8) if _spread > 0 else 0.0,  # Dynamic: 2× spread
    )

    # ── Breakeven check — only fires once per position ──
    if not pos.breakeven_triggered and pm.should_breakeven(
        mid,
        current_atr,
        ticket=pos.ticket,
        breakeven_threshold_mult_override=_be_mult_override,
    ):
        _be_triggered = True
        # FIX-20260726-011: Spread-aware breakeven — offset SL by current spread
        # so the exit fill lands at true breakeven instead of a guaranteed
        # micro-loss.  SHORT enters at Bid, exits at Ask → SL must be placed
        # *below* entry by spread.  LONG enters at Ask, exits at Bid → SL
        # must be placed *above* entry by spread.  Degrades to entry_price
        # when bid/ask unavailable (preserves existing behavior).
        if pos.side == "short":
            _be_sl = pos.entry_price - _spread if _spread > 0 else pos.entry_price
        else:
            _be_sl = pos.entry_price + _spread if _spread > 0 else pos.entry_price
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
    _trail_tp = pm.compute_trail_tp(
        current_atr, ticket=pos.ticket, mid=mid, disable_dynamic_tp=_disable_tp
    )
    _tp_tightened = False
    if _trail_tp is not None and abs(_trail_tp - pos.current_tp) >= config.exit_min_step:
        _reasons.append("tp")
        _final_tp = _trail_tp
        # TECH_DEBT-019 §2 trigger: TP actually moved INWARD this cycle (LONG
        # lower / SHORT higher).  Elastic expansion (outward) never arms the SL
        # volatility trail — only genuine ATR-contraction tightening does.
        _tp_tightened = (pos.side == "long" and _trail_tp < pos.current_tp) or (
            pos.side == "short" and _trail_tp > pos.current_tp
        )

    # ── TECH_DEBT-019 §2: Symmetric Volatility Tightening (SL_Volatility_Trail) ──
    # When ATR contraction tightened the TP, tighten the risk leg by the SAME
    # ratio so the reduced absolute bracket space keeps the open-time RR
    # expectation.  Bounded: fires only when TP actually tightened AND
    # atr_ratio <= 0.80; the engine skips post-breakeven and floors the result
    # at the ratchet lock / max_lock.  min_step suppresses retcode 10025 resends.
    _sl_vol_trail_fired = False
    if _tp_tightened and pos.entry_atr > 0:
        _atr_ratio_trail = current_atr / pos.entry_atr
        if _atr_ratio_trail <= 0.80:
            _sl_vol = pm.compute_volatility_trail_sl(pos.ticket, _atr_ratio_trail)
            if _sl_vol is not None and abs(_sl_vol - _final_sl) >= config.exit_min_step:
                _final_sl = (
                    max(_final_sl, _sl_vol) if pos.side == "long" else min(_final_sl, _sl_vol)
                )
                _reasons.append("sl_vol_trail")
                _sl_vol_trail_fired = True

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
        else:
            # ── TECH_DEBT-019 §1: RR hard floor — dispatch-time final assertion ──
            # Registry-mandated "下发前注入最终 RR 耦合断言".  Uses the cycle's FINAL
            # SL (post-breakeven / post-SL_Volatility_Trail) so the dispatched pair
            # always satisfies RR >= min_rr.  Self-heals an already-collapsed TP in
            # one clamp (stable thereafter — min_step suppresses 10025 resends).
            # min_rr == 0 → zero-change (structural/legacy).
            _tp_eff = pm.get_effective_trail_policy(pos.ticket)
            _rr_min = getattr(_tp_eff, "tp_min_rr_ratio", 0.0)
            if _rr_min > 0:
                _rr_floor = compute_rr_floor_price(pos.side, pos.entry_price, _final_sl, _rr_min)
                if _rr_floor is not None:
                    if pos.side == "long":
                        _clamped = (
                            min(max(_final_tp, _rr_floor), pos.initial_tp)
                            if pos.initial_tp > 0
                            else max(_final_tp, _rr_floor)
                        )
                    else:
                        _clamped = (
                            max(min(_final_tp, _rr_floor), pos.initial_tp)
                            if pos.initial_tp > 0
                            else min(_final_tp, _rr_floor)
                        )
                    if abs(_clamped - _final_tp) >= config.exit_min_step:
                        _final_tp = _clamped
                        _reasons.append("tp_rr_floor")

    # ── DQAF-20260710-004: Trailing TP Market-Price Feasibility Gate ──
    # FIX-20260707-009 guards bracket inversion (SL >= TP) but does NOT
    # validate the TP distance from the current market price.  MT5 enforces
    # a minimum stop distance (STOPLEVEL): for LONG, TP must be > bid +
    # stop_level; for SHORT, TP must be < ask - stop_level.  When price has
    # advanced toward the original TP, compute_trail_tp() — which derives the
    # TP candidate from entry_price + ATR-based distance — produces a
    # candidate that falls inside the STOPLEVEL exclusion zone.  MT5 rejects
    # the entire modify_sltp with "Invalid stops" (retcode 10016), and the
    # trailing SL (which WAS valid) is also discarded.  This gate mirrors the
    # breakeven feasibility check above and reverts the TP to its pre-cycle
    # value (pos.current_tp) so MT5 keeps the existing TP unchanged.
    # NOTE: reverting to pos.current_tp (not 0.0) prevents state corruption
    # where pos.current_tp would be optimistically set to 0 on line 335.
    if _final_tp > 0:
        if pos.side == "long":
            _tp_feasible = (
                _final_tp > bid + _min_market_distance
                if bid is not None and bid > 0
                else _final_tp > mid + _min_market_distance  # fallback to mid
            )
        else:
            _tp_feasible = (
                _final_tp < ask - _min_market_distance
                if ask is not None and ask > 0
                else _final_tp < mid - _min_market_distance  # fallback to mid
            )
        if not _tp_feasible:
            _reasons.append("tp_released_stoplevel_violation")
            _final_tp = pos.current_tp  # revert: keep existing TP unchanged

    # ── DQAF-20260710-004b: Trailing SL Market-Price Feasibility Gate ──
    # The counterpart to the TP gate above.  When price retraces after a
    # Chandelier trail tightening, the computed trailing SL can end up on the
    # wrong side of the current market price (e.g. LONG SL > bid after a
    # pullback).  MT5 enforces: LONG SL must be < bid − StopLevel, SHORT SL
    # must be > ask + StopLevel.  Without this gate, a valid TP modification
    # is also discarded because MT5 rejects the entire modify_sltp payload
    # when any single stop is invalid (retcode 10016).  Observed on XAU
    # #4118779584 (m30_swing) and #4118881082 (m15_swing) post-restart:
    # SL=4108.01 > bid=4106.47 → 10016 loop from 11:00Z onward.
    # Reverting to pos.current_sl keeps the existing MT5-side SL unchanged.
    if _final_sl > 0:
        if pos.side == "long":
            _sl_feasible = (
                _final_sl < bid - _min_market_distance
                if bid is not None and bid > 0
                else _final_sl < mid - _min_market_distance  # fallback to mid
            )
        else:
            _sl_feasible = (
                _final_sl > ask + _min_market_distance
                if ask is not None and ask > 0
                else _final_sl > mid + _min_market_distance  # fallback to mid
            )
        if not _sl_feasible:
            _reasons.append("sl_released_stoplevel_violation")
            _final_sl = pos.current_sl  # revert: keep existing SL unchanged

    # ── TECH_DEBT-019 telemetry: dispatched-pair RR (entry reference) ──
    _rr_current: float | None = None
    if _final_tp > 0 and _final_sl > 0 and pos.entry_price > 0:
        _rr_tp_d = (
            _final_tp - pos.entry_price if pos.side == "long" else pos.entry_price - _final_tp
        )
        _rr_sl_d = (
            pos.entry_price - _final_sl if pos.side == "long" else _final_sl - pos.entry_price
        )
        if _rr_sl_d > 0:
            _rr_current = _rr_tp_d / _rr_sl_d
    _rr_floor_tp_diag = round(getattr(pos, "rr_floor_tp", 0.0), 3) or None
    _tp_rr_floor_fired = "tp_rr_floor" in _reasons

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
                # FIX-20260708-004: profit-ratchet floor demanded this cycle (R
                # in entry_atr units).  Non-zero once the peak armed the ratchet;
                # lets the give-back audit confirm a positive floor was enforced.
                "ratchet_floor_r": round(getattr(pos, "ratchet_floor_r", 0.0), 4),
                "breakeven_fired": _be_triggered,
                "breakeven_improves": _be_dispatched,
                "breakeven_skipped_price": _be_skipped_price,
                "cycles_held": pos.cycles_held,
                "breakeven_triggered_flag": pos.breakeven_triggered,
                "final_sl": round(_final_sl, 3),
                "final_tp": round(_final_tp, 3),
                # TECH_DEBT-019: RR coupling telemetry — lets the daily audit
                # assert rr_current >= tp_min_rr_ratio snapshot-by-snapshot.
                "atr_ratio": round(current_atr / pos.entry_atr, 4) if pos.entry_atr > 0 else None,
                "rr_current": round(_rr_current, 4) if _rr_current is not None else None,
                "rr_floor_price": _rr_floor_tp_diag,
                "tp_rr_floor_fired": _tp_rr_floor_fired,
                "sl_vol_trail_fired": _sl_vol_trail_fired,
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
                    # TECH_DEBT-019: RR contract evidence for offline audit —
                    # assert rr_current >= tp_min_rr_ratio (when min_rr > 0 and
                    # pre-breakeven), flag any surviving collapse.
                    "rr_current": round(_rr_current, 6) if _rr_current is not None else None,
                    "tp_min_rr_ratio": getattr(
                        pm.get_effective_trail_policy(pos.ticket), "tp_min_rr_ratio", 0.0
                    ),
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
    # Only dispatch if at least one stop effectively changed after all gates.
    # When both SL and TP were reverted by feasibility gates (sl_released_* /
    # tp_released_*), _reasons is non-empty but _final_sl == pos.current_sl
    # and _final_tp == pos.current_tp — dispatching would be a no-op round-trip
    # to MT5.
    if _reasons and (_sl_changed or _tp_changed):
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
