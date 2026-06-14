"""Position Registration — Strangler Fig extraction from live_cycle.py.

FIX-20260609-006: Extracted from ``execute_live_cycle()`` L5342-5518.
Triggered by FIX-005 which modified the TrailPolicy construction in this
exact section.

Pure function contract (Strangler Fig Iron Law):
  - Receives explicitly-needed fields via parameters (no whole-state pass)
  - Returns results dict; caller writes back to state
  - No implicit I/O beyond print/journal reads which are inherent to
    the registration pipeline
"""

from __future__ import annotations

import json
import time as _time_module
from pathlib import Path
from typing import Any

from core.execution.trail_stop_engine import TrailPolicy
from core.runtime.fault_handler import FaultLevel, FaultTolerantContext


def register_dispatched_positions(
    *,
    config: Any,
    position_manager: Any,
    known_open_tickets: dict[int, Any],
    loop_iteration: int,
    limit_monitor: Any = None,
    dispatch_results: list[Any],
    eval_summary: dict[str, Any],
    brains: list[dict[str, Any]],
    journal_path: Path | None,
    current_atr: float,
    mid_price: float | None,
    bid: float | None = None,
    ask: float | None = None,
    mt5_worker: Any = None,
    _utc_iso_fn: Any = None,
    _DEFAULT_HORIZON: int = 12,
) -> dict[str, Any]:
    """Register dispatched positions for dynamic exit management.

    Strangler Fig #10: extracted from live_cycle.py L5342-5518.
    Reads journal to resolve MT5 ticket numbers, then calls
    position_manager.register_position() with strategy-specific
    TrailPolicy and partial TP parameters.

    Returns a dict with keys:
      - registered_count: int
      - position_state_path: str | None (for caller to save state)
    """
    if not (
        getattr(config, "exit_management_enabled", False)
        and position_manager is not None
        and not getattr(config, "no_mt5", True)
    ):
        return {"registered_count": 0, "position_state_path": None}

    decisions_map = eval_summary.get("decisions_map", {})
    registered_count = 0
    _utc_iso = _utc_iso_fn if _utc_iso_fn is not None else (lambda: "")

    for dr in dispatch_results:
        if not dr.dispatched:
            continue
        decision = decisions_map.get(dr.strategy_name)
        if decision is None:
            continue

        intent_id = (dr.journal_entry or {}).get("intent_id", "")
        ticket: int | None = None
        entry_from_journal: float | None = None
        brain_votes_from_journal: list[dict[str, Any]] | None = None

        # ── Retry up to 10 times (5s total) — bridge writes journal async ──
        # DQAF-20260614-007: The MT5 bridge writes follow-up entries with
        # open_message_id referencing the original intent.  Previously we
        # only checked message_id, missing the bridge's ticket-bearing
        # entries — causing position_register_skip and breaking the
        # SignalSettled chain (no position_ticket → no brain→trade link).
        if intent_id and journal_path is not None:
            for _retry in range(10):
                if journal_path.exists():
                    for line in journal_path.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line or intent_id not in line:
                            continue
                        try:
                            rec = json.loads(line)
                            # Match either message_id (original) or open_message_id (bridge follow-up)
                            _mid = rec.get("message_id", "")
                            _omid = rec.get("open_message_id", "")
                            if _mid == intent_id or _omid == intent_id:
                                t = rec.get("position_ticket")
                                if t is not None and isinstance(t, int) and t > 0:
                                    ticket = t
                                ep = rec.get("entry_price")
                                if (
                                    ep is not None
                                    and isinstance(ep, int | float)
                                    and ep > 0
                                ):
                                    entry_from_journal = float(ep)
                                bv = rec.get("brain_votes")
                                if isinstance(bv, list):
                                    brain_votes_from_journal = bv
                                if ticket is not None:
                                    break
                        except Exception:  # noqa: BLE001
                            pass
                    if ticket is not None:
                        break
                _time_module.sleep(0.5)

        # ── DQAF-20260614-008: MT5 direct query (primary fallback) ──
        # Query MT5 positions directly by magic number.  This is the most
        # reliable fallback — no Bridge, no filesystem, no async dependency.
        if ticket is None and mt5_worker is not None and decision.magic > 0:
            try:
                import MetaTrader5 as _mt5_module

                _magic = decision.magic
                _symbol = config.symbol if hasattr(config, "symbol") else "BTCUSDc"
                _positions = _mt5_module.positions_get(symbol=_symbol)
                if _positions:
                    for _pos in _positions:
                        if _pos.magic == _magic:
                            ticket = _pos.ticket
                            if _pos.price_open > 0:
                                entry_from_journal = float(_pos.price_open)
                            print(
                                json.dumps(
                                    {
                                        "event": "position_ticket_mt5_fallback",
                                        "time": _utc_iso(),
                                        "strategy": dr.strategy_name,
                                        "ticket": ticket,
                                        "source": "mt5_direct",
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                            break
            except Exception:  # noqa: BLE001
                pass

        if ticket is None:
            print(
                json.dumps(
                    {
                        "event": "position_register_skip",
                        "time": _utc_iso(),
                        "strategy": dr.strategy_name,
                        "reason": "no_ticket_in_journal",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            continue

        entry_price = entry_from_journal or mid_price or 0.0
        entry_consensus = {
            "aggregated_bias": decision.direction,
            "consensus_score": decision.confidence,
            "voter_count": decision.total_count,
            "majority_ratio": (
                decision.supporting_count / decision.total_count
                if decision.total_count > 0
                else 0.0
            ),
        }

        # ── Build per-model horizon map ──
        model_horizons: dict[str, int] = {}
        for bid in decision.brain_ids:
            horizon = _DEFAULT_HORIZON
            for bi in brains:
                if bi.get("brain_id") == bid:
                    horizon = bi.get("training_horizon", _DEFAULT_HORIZON)
                    break
            model_horizons[bid] = horizon

        try:
            _s_cfg = config.strategy_configs.get(dr.strategy_name, {})
            _tp_cfg = _s_cfg.get("tp", {})
            _exit_cfg = _s_cfg.get("exit", {})
            _ptp_r = (
                _tp_cfg.get("partial_tp_r", 0.0)
                if _tp_cfg.get("partial_tp_enabled")
                else 0.0
            )
            _ptp_ratio = _tp_cfg.get("partial_tp_ratio", 0.5)
            position_manager.register_position(
                ticket=ticket,
                side=decision.direction,
                entry_price=entry_price,
                volume=decision.volume,
                initial_sl=decision.sl,
                initial_tp=decision.tp,
                entry_atr=current_atr,
                entry_cycle=loop_iteration,
                entry_z_score=getattr(decision, "entry_z_score", 0.0),
                entry_half_life=getattr(decision, "entry_half_life", 0.0),
                entry_consensus=entry_consensus,
                supporting_brain_ids=decision.brain_ids,
                model_horizons=model_horizons,
                current_high=entry_price,
                partial_tp_r=_ptp_r,
                partial_tp_ratio=_ptp_ratio,
                ofi_partial_tp_threshold=_tp_cfg.get("ofi_partial_tp_threshold", 0.0),
                ofi_partial_tp_r_mult=_tp_cfg.get("ofi_partial_tp_r_mult", 0.5),
                strategy_name=dr.strategy_name,
                trail_policy=TrailPolicy(
                    trail_atr_mult=_exit_cfg.get("trail_atr_mult", 2.0),
                    trail_atr_mult_low=_exit_cfg.get("trail_atr_mult_low", 1.5),
                    trail_atr_mult_high=_exit_cfg.get("trail_atr_mult_high", 3.0),
                    breakeven_threshold_atr=_exit_cfg.get(
                        "breakeven_threshold_atr", 1.0
                    ),
                    trail_activation_atr=_exit_cfg.get(
                        "trail_activation_atr",
                        getattr(config, "exit_trail_activation_atr", 1.0),
                    ),
                ),
                cold_explore=getattr(decision, "cold_explore", False),
            )
            # ── Sync known_open_tickets ──
            known_open_tickets[ticket] = {
                "position_ticket": ticket,
                "action": "open",
                "side": decision.direction,
                "volume": decision.volume,
                "entry_price": entry_price,
                "strategy": dr.strategy_name,
                "magic": decision.magic,
                "message_id": intent_id,
                "brain_ids": decision.brain_ids,
                "brain_votes": brain_votes_from_journal or [],
                "entry_consensus": {
                    "consensus_score": decision.confidence,
                    "direction": decision.direction,
                },
            }
            # ── Persist immediately after registration ──
            with FaultTolerantContext(
                level=FaultLevel.CRASH,
                component="PositionState:save_after_register",
            ):
                position_manager.save_state(config.position_state_path)
            print(
                json.dumps(
                    {
                        "event": "position_registered_for_mgmt",
                        "time": _utc_iso(),
                        "ticket": ticket,
                        "strategy": dr.strategy_name,
                        "side": decision.direction,
                        "entry_price": entry_price,
                        "initial_sl": decision.sl,
                        "initial_tp": decision.tp,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            # ── Shadow record for limit-order execution quality ──
            if limit_monitor is not None:
                try:
                    _lom_spread_pts = 0.0
                    _lom_b = bid if bid is not None else 0.0
                    _lom_a = ask if ask is not None else 0.0
                    if _lom_b > 0 and _lom_a > _lom_b:
                        _tick_size = getattr(config, "tick_size", 0.01) or 0.01
                        _lom_spread_pts = (_lom_a - _lom_b) / _tick_size
                    limit_monitor.record_market_order(
                        ticket=ticket,
                        strategy=dr.strategy_name,
                        side=decision.direction,
                        volume=decision.volume,
                        entry_price=entry_price,
                        spread_points=_lom_spread_pts,
                        atr=current_atr,
                    )
                except Exception:  # noqa: BLE001
                    pass

            registered_count += 1
        except Exception as _reg_exc:  # noqa: BLE001
            print(
                json.dumps(
                    {
                        "event": "position_register_failed",
                        "time": _utc_iso(),
                        "strategy": dr.strategy_name,
                        "ticket": ticket,
                        "error": str(_reg_exc)[:200],
                        "level": "DEGRADE",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    return {
        "registered_count": registered_count,
        "position_state_path": getattr(config, "position_state_path", None),
    }
