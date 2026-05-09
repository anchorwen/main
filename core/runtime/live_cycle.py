"""Live trading cycle execution — one iteration of the intent loop.

Extracted from scripts/live_intent_loop.py to keep the CLI script thin
(CLI + init + main loop shell) while housing the cycle logic in core/runtime/.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from core.execution.barrier_strategy import BarrierStrategy
from core.execution.execution_queue import ExecutionQueue
from core.execution.micro_strategy import MicroStrategy
from core.execution.portfolio_risk import PortfolioRiskController
from core.execution.regime_gate import RegimeGate
from core.execution.statarb_strategy import StatArbStrategy
from core.execution.strategy_budget import StrategyBudget

# ── Strategy line imports ──
from core.execution.strategy_line import StrategyLineConfig
from core.parliament.contract_groups import ARB_GROUP, BARRIER_GROUP, MICRO_GROUP

# ── Dataclasses ──────────────────────────────────────────────────────────


@dataclass
class LiveCycleConfig:
    """Immutable per-run configuration derived from CLI args."""

    symbol: str = "XAUUSDc"
    base_dir: str = "data"
    calendar_path: str = "data/config/market_calendar.json"
    position_state_path: str = "data/state/active_position.json"
    interval_seconds: float = 30.0
    confidence_threshold: float = 0.50
    cooldown_seconds: float = 300.0
    max_positions: int = 1
    sl_atr_mult: float = 2.0
    tp_atr_mult: float = 3.5
    volume: float | None = None
    no_mt5: bool = False
    once: bool = False
    ignore_protection_flag: bool = False
    protection_flag_path: str = "data/live_dispatch_block.flag"
    mt5_terminal_path: str = ""
    brain_entry: dict[str, Any] = field(default_factory=dict)
    brain_type: str = "onnx_v9"
    multi_brain: bool = False
    feature_store_dir: str = "data/feature_store"
    disable_feature_store: bool = False
    reconciliation_interval: int = 10
    state_save_interval: int = 60

    # ── Exit management parameters ──
    exit_management_enabled: bool = True
    exit_trail_atr_mult: float = 2.0
    exit_trail_atr_mult_low: float = 1.5
    exit_trail_atr_mult_high: float = 3.0
    exit_breakeven_threshold_atr: float = 1.0
    exit_brain_reeval_interval: int = 5
    exit_flip_threshold: float = 0.5
    exit_confidence_drop: float = 0.10
    exit_max_hold_cycles: int = 60
    exit_require_min_r: float = 0.3
    exit_min_step: float = 0.005

    # ── Multi-strategy mode ──
    multi_strategy_enabled: bool = True  # False → fallback to old CapitalAllocator
    strategy_stagger_seconds: float = 20.0  # delay between strategy dispatches
    portfolio_max_gross: float = 0.10
    portfolio_max_net: float = 0.05
    portfolio_max_same_dir: int = 2

    # ── live.yaml strategy_lines overrides ──
    strategy_configs: dict[str, Any] = field(default_factory=dict)


@dataclass
class LiveCycleState:
    """Mutable state threaded through each cycle iteration."""

    last_fire: float = 0.0
    cycle_count: int = 0
    loop_iteration: int = 0
    flag_notice: bool = False
    known_open_tickets: dict[int, dict[str, Any]] = field(default_factory=dict)
    consecutive_sl_hits: int = 0
    sl_streak_blocked_until: float = 0.0
    _initial_reconciliation_done: bool = False
    position_manager: Any = None  # ActivePositionManager (set by caller)
    correlation_tracker: Any = None  # GroupCorrelationTracker (set by caller)
    shadow_verification_pending: dict[str, Any] | None = (
        None  # prev-cycle shadow decision for counterfactual settlement
    )
    regime_gate: Any = None  # RegimeGate (persisted across cycles for ADX accumulation)


# ── Helpers ──────────────────────────────────────────────────────────────


def _utc_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _log_cycle_end(iteration: int) -> None:
    print(
        json.dumps(
            {"event": "cycle_end", "time": _utc_iso(), "iteration": iteration},
            ensure_ascii=False,
        ),
        flush=True,
    )


def _check_pre_close(config: LiveCycleConfig, state: LiveCycleState) -> dict[str, Any]:
    """Check if we are approaching a market close and return action flags.

    Returns dict with keys: in_pre_close, minutes_to_close, no_new_positions,
    must_flatten, close_label.  A result of {} means no action needed.
    """
    from scripts.market_calendar import evaluate_pre_close, load_calendar

    cal = load_calendar(config.calendar_path)
    result = evaluate_pre_close(
        now_utc=datetime.now(UTC),
        symbol=config.symbol,
        config=cal,
    )
    if not result.get("in_pre_close"):
        return {}

    # If we must flatten and have an open position, close it immediately
    if (
        result.get("must_flatten")
        and state.position_manager is not None
        and state.position_manager.has_position()
    ):
        pos = state.position_manager.get_position()
        if pos is not None:
            print(
                json.dumps(
                    {
                        "event": "pre_close_flatten",
                        "time": _utc_iso(),
                        "close_label": result["close_label"],
                        "minutes_to_close": result["minutes_to_close"],
                        "ticket": getattr(pos, "ticket", 0),
                        "side": getattr(pos, "side", "?"),
                        "volume": getattr(pos, "volume", 0),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            _dispatch_managed_close(
                config,
                pos,
                reason=f"pre_close_flatten:{result['close_label']}",
                mid=None,
            )

    return result


def _bootstrap_regime_gate(mt5: Any, symbol: str, gate: Any) -> bool:
    """Bootstrap RegimeGate with recent M5 and H1 bars from MT5.

    Called once on first cycle to fill the ADX buffer. Returns True if
    enough bars were loaded for M5 ADX.
    """
    try:
        # M5 bars — last 50 (covers ~4 hours, enough for ADX(14))
        m5_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 50)
        if m5_rates is not None and len(m5_rates) >= 15:
            gate.feed_m5_bars_batch(m5_rates)

        # H1 bars — last 60 (covers ~2.5 days, enough for EMA(20) + ADX(14))
        h1_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 60)
        if h1_rates is not None and len(h1_rates) >= 20:
            gate.feed_h1_bars_batch(h1_rates)

        return gate.is_ready
    except Exception:
        return False


def _feed_regime_gate_cycle(mt5: Any, symbol: str, gate: Any) -> None:
    """Feed latest M5 and H1 bar to RegimeGate (incremental update).

    Called every cycle. Only the most recent bar is added; duplicates are
    harmless because ADX uses the full buffer.
    """
    try:
        m5_bar = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 1)
        if m5_bar is not None and len(m5_bar) == 1:
            gate.feed_m5_bar(m5_bar[0]["high"], m5_bar[0]["low"], m5_bar[0]["close"])

        h1_bar = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 1)
        if h1_bar is not None and len(h1_bar) == 1:
            gate.feed_h1_bar(h1_bar[0]["high"], h1_bar[0]["low"], h1_bar[0]["close"])
    except Exception:
        pass


def cooldown_blocks_fire(now: float, last_fire: float, cooldown_seconds: float) -> bool:
    return (now - last_fire) < cooldown_seconds


def _get_current_atr(
    mt5: Any, symbol: str, period: int = 14, count: int = 15, timeout: float = 5.0
) -> float:
    """Compute current M5 ATR(14) from MT5 rates with thread timeout."""
    import threading

    import numpy as np

    result: list[Any] = [None]
    exc_info: list[Any] = [None]

    def _target() -> None:
        try:
            result[0] = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, count)
        except Exception as e:
            exc_info[0] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive() or exc_info[0] is not None:
        return 0.0
    rates = result[0]
    if rates is None or len(rates) < period + 1:
        return 0.0
    h = np.array([r["high"] for r in rates], dtype=np.float64)
    low = np.array([r["low"] for r in rates], dtype=np.float64)
    c = np.array([r["close"] for r in rates], dtype=np.float64)
    prev_c = c[-(period + 1) : -1]
    cur_h = h[-period:]
    cur_l = low[-period:]
    tr = np.maximum(cur_h - cur_l, np.maximum(abs(cur_h - prev_c), abs(cur_l - prev_c)))
    return float(np.mean(tr))


def _position_count(mt5: Any, symbol: str, timeout: float = 5.0) -> int:
    """Count open MT5 positions with a thread timeout to prevent indefinite blocking."""
    import threading

    result: list[int | None] = [None]
    exc_info: list[Any] = [None]

    def _target() -> None:
        try:
            pos = mt5.positions_get(symbol=symbol)
            result[0] = len(pos) if pos else 0
        except Exception as e:
            exc_info[0] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        return 0
    if exc_info[0] is not None:
        raise exc_info[0]
    return result[0] if result[0] is not None else 0


def _mid_and_prices(mt5: Any, symbol: str, timeout: float = 5.0) -> tuple[float, float, float]:
    """Fetch bid/ask/mid with thread timeout to prevent MT5 hang."""
    import threading

    result: list[Any] = [None]
    exc_info: list[Any] = [None]

    def _target() -> None:
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                try:
                    mt5.initialize()
                    tick = mt5.symbol_info_tick(symbol)
                except Exception:
                    pass
            result[0] = tick
        except Exception as e:
            exc_info[0] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise TimeoutError(f"symbol_info_tick timed out after {timeout}s")
    if exc_info[0] is not None:
        raise exc_info[0]
    tick = result[0]
    if tick is None:
        raise RuntimeError("tick unavailable")
    bid = float(tick.bid)
    ask = float(tick.ask)
    return (bid + ask) / 2.0, bid, ask


def compute_sl_tp_for_side(
    side: str,
    *,
    ref_long: float,
    ref_short: float,
    sl_atr_mult: float,
    tp_atr_mult: float,
    current_atr: float,
) -> tuple[float, float, float]:
    """Returns stop_loss, take_profit, ref_for_guard."""
    sl_distance = sl_atr_mult * current_atr
    tp_distance = tp_atr_mult * current_atr
    if side == "long":
        stop_loss = ref_long - sl_distance
        take_profit = ref_long + tp_distance
        ref_for_guard = ref_long
    else:
        stop_loss = ref_short + sl_distance
        take_profit = ref_short - tp_distance
        ref_for_guard = ref_short
    return stop_loss, take_profit, ref_for_guard


def _check_recent_sl_streak(
    journal_path: str,
    lookback_seconds: float = 300.0,
    threshold: int = 3,
) -> tuple[bool, int]:
    """Scan the journal for a streak of recent SL hits — bypasses reconciliation.

    This is a defense-in-depth check that runs right before dispatch, so a
    stop-loss cascade that occurs between reconciliation cycles is caught
    before the next order goes out.
    """
    import time as _time

    try:
        p = Path(journal_path)
        if not p.exists():
            return False, 0
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return False, 0

    now = _time.time()
    sl_streak = 0
    # Walk backwards through the journal for speed; stop once we pass the window
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("action") != "close":
            continue
        recorded = rec.get("recorded_at", "")
        try:
            if recorded.endswith("Z"):
                dt = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(recorded)
            if now - dt.timestamp() > lookback_seconds:
                break
        except Exception:
            continue
        label = rec.get("label", "")
        if label in ("sl_hit_first", "loss"):
            sl_streak += 1
        elif label in ("tp_hit_first", "win"):
            break  # a win resets the streak
        # "breakeven", "manual_close", etc. — ignore, keep counting

    blocked = sl_streak >= threshold
    return blocked, sl_streak


# ── Exit Management Helpers ────────────────────────────────────────────────


def _dispatch_modify_trail(
    config: LiveCycleConfig,
    pos: Any,
    new_sl: float,
    new_tp: float,
    *,
    reason: str = "",
    brain_ids: list[str] | None = None,
) -> None:
    """Issue a modify_sltp through the existing outbox pipeline."""
    from scripts.send_live_order import dispatch_live_order

    payload: dict[str, Any] = {
        "action": "modify_sltp",
        "side": pos.side,
        "position_ticket": pos.ticket,
        "sl": new_sl,
        "tp": new_tp,
        "comment": reason,
    }
    if brain_ids:
        payload["brain_ids"] = brain_ids

    try:
        dispatch_live_order(
            base_dir=config.base_dir,
            broker=None,
            symbol=config.symbol,
            execution_payload=payload,
            skip_price_guard=True,
            ignore_protection_flag=config.ignore_protection_flag,
            protection_flag_path=config.protection_flag_path,
            adapter_name="mt5",
            extensions={"mt5_terminal_path": config.mt5_terminal_path},
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "trail_dispatch_error",
                    "time": _utc_iso(),
                    "error": str(exc),
                    "reason": reason,
                    "new_sl": new_sl,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


def _dispatch_managed_close(
    config: LiveCycleConfig,
    pos: Any,
    *,
    reason: str = "",
    mid: float | None = None,
) -> None:
    """Issue a close order for a managed position."""
    from scripts.send_live_order import dispatch_live_order

    # Estimate PnL so the journal entry has it (reconciliation corrects it later)
    pnl = None
    entry_price = getattr(pos, "entry_price", None)
    if entry_price is not None and mid is not None and pos.volume:
        if pos.side == "long":
            pnl = round((mid - entry_price) * pos.volume, 2)
        elif pos.side == "short":
            pnl = round((entry_price - mid) * pos.volume, 2)

    payload: dict[str, Any] = {
        "action": "close",
        "side": pos.side,
        "position_ticket": pos.ticket,
        "volume": pos.volume,
        "comment": reason,
    }
    if pnl is not None:
        payload["pnl"] = pnl

    try:
        dispatch_live_order(
            base_dir=config.base_dir,
            broker=None,
            symbol=config.symbol,
            execution_payload=payload,
            skip_price_guard=True,
            ignore_protection_flag=config.ignore_protection_flag,
            protection_flag_path=config.protection_flag_path,
            adapter_name="mt5",
            extensions={"mt5_terminal_path": config.mt5_terminal_path},
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "close_dispatch_error",
                    "time": _utc_iso(),
                    "error": str(exc),
                    "reason": reason,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


def _execute_management_phase(
    config: LiveCycleConfig,
    state: LiveCycleState,
    *,
    mt5: Any,
    broker: Any,
    brains: list[dict[str, Any]],
    parliament: Any,
    regime_detector: Any,
    tracker: Any,
    feature_service: Any,
    micro_feature_computer: Any,
    micro_feature_adapter: Any,
) -> bool:
    """Manage open position: trail stop, re-evaluate brains, check exits.

    Returns True if the position was closed (caller should skip post-exit
    bookkeeping that assumes the position still exists).
    """
    pm = state.position_manager
    if pm is None or not pm.has_position():
        return False

    pos = pm.get_position()
    if pos is None:
        return False

    # ── 1. Fetch current prices & ATR ──
    try:
        if broker is not None:
            mid, bid, ask = broker.fetch_prices(config.symbol)
        else:
            mid, bid, ask = _mid_and_prices(mt5, config.symbol)
    except Exception:
        return False

    current_atr = (
        broker.fetch_current_atr(config.symbol)
        if broker is not None
        else _get_current_atr(mt5, config.symbol)
    )
    if current_atr <= 0:
        current_atr = 2.31

    # ── 2. Update regime detector ──
    regime_info: dict[str, Any] = {}
    if regime_detector is not None:
        try:
            regime_info = regime_detector.update(current_atr)
        except Exception:
            pass

    # ── 3. Update position tracking ──
    pm.update_prices(mid, bid, ask, current_atr, regime_info, state.loop_iteration)

    # ── 4. Layer 1: Chandelier trailing stop ──
    new_sl = pm.compute_trail_stop(current_atr)
    if new_sl is not None and abs(new_sl - pos.current_sl) >= config.exit_min_step:
        _dispatch_modify_trail(
            config,
            pos,
            new_sl,
            pos.current_tp,
            reason="trail",
            brain_ids=pos.supporting_brain_ids,
        )
        old_sl = pos.current_sl
        pos.current_sl = new_sl
        print(
            json.dumps(
                {
                    "event": "trail_stop_moved",
                    "time": _utc_iso(),
                    "ticket": pos.ticket,
                    "side": pos.side,
                    "old_sl": round(old_sl, 3),
                    "new_sl": round(new_sl, 3),
                    "highest_high": round(pos.highest_high, 3),
                    "trail_mult": pos.trail_multiplier,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    # ── 5. Breakeven check ──
    # Only dispatch if breakeven SL improves current SL (trail may have
    # already moved SL past entry, making breakeven a downgrade).
    if not pos.breakeven_triggered and pm.should_breakeven(mid, current_atr):
        breakeven_sl = pos.entry_price
        improve = (pos.side == "long" and breakeven_sl > pos.current_sl) or (
            pos.side == "short" and breakeven_sl < pos.current_sl
        )
        if improve:
            _dispatch_modify_trail(
                config,
                pos,
                breakeven_sl,
                pos.current_tp,
                reason="breakeven",
                brain_ids=pos.supporting_brain_ids,
            )
            pos.current_sl = breakeven_sl
        pos.breakeven_triggered = True
        print(
            json.dumps(
                {
                    "event": "breakeven_triggered",
                    "time": _utc_iso(),
                    "ticket": pos.ticket,
                    "side": pos.side,
                    "entry_price": pos.entry_price,
                    "mid": round(mid, 3),
                    "dispatched": improve,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    # ── 6. R-milestone checks ──
    milestone = pm.check_r_milestones(mid)
    if milestone:
        print(
            json.dumps(
                {
                    "event": "r_milestone_hit",
                    "time": _utc_iso(),
                    "ticket": pos.ticket,
                    "milestone": milestone,
                    "r": round(pm._compute_r_multiple(mid), 2),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    # ── 7. Layer 2: Brain ensemble re-evaluation ──
    current_consensus: dict[str, Any] = {}
    current_supporting: list[str] = []
    if config.multi_brain and pm.should_reeval_brains(state.loop_iteration):
        pm.mark_brains_reevaluated(state.loop_iteration)

        # Re-run all brain inference
        raw_proposals: list[Any] = []
        for b_info in brains:
            schema_id = b_info.get("feature_schema_id", "")
            btype = b_info.get("brain_type", "")
            try:
                if btype == "ou_params_v6":
                    fv: Any = np.array([mid], dtype=np.float32)
                elif "microstructure" in str(schema_id) and micro_feature_computer is not None:
                    mf = micro_feature_computer.compute_all()
                    fv = micro_feature_adapter.build_model_input(mf).ravel()
                else:
                    fv = feature_service.build_feature_vector(
                        {"symbol": config.symbol, "venue": "MT5"}
                    )
                raw = b_info["adapter"].infer(fv)
                prop = b_info["adapter"].get_signal(raw)
                # Stamp brain_id for attribution
                bid = b_info.get("brain_id", "unknown")
                try:
                    if not getattr(prop, "brain_id", None):
                        prop.brain_id = bid
                except Exception:
                    pass
                raw_proposals.append(prop)
            except Exception:
                pass

        if raw_proposals:
            try:
                from core.execution.capital_allocator import resolve_conflicts
                from core.parliament.contract_groups import (
                    compute_all_group_signals,
                )

                # Build (brain_info, proposal) pairs
                brain_proposal_pairs: list[tuple[dict[str, Any], Any]] = []
                for i, p in enumerate(raw_proposals):
                    b_info = brains[i] if i < len(brains) else {}
                    brain_proposal_pairs.append((b_info, p))

                # Per-group consensus (contract-homogeneous voting)
                group_signals = compute_all_group_signals(brain_proposal_pairs)
                allocation = resolve_conflicts(group_signals)

                # Extract current consensus and supporting brains
                direction = allocation.direction if allocation.should_trade else "neutral"
                all_supporting: list[str] = []
                all_opposing: list[str] = []
                total_voters = 0
                for _gname, gs in group_signals.items():
                    if gs is None:
                        continue
                    total_voters += gs.total_count
                    if gs.direction == direction:
                        all_supporting.extend(gs.brain_ids)
                    elif gs.direction != "neutral":
                        all_opposing.extend(gs.brain_ids)

                current_consensus = {
                    "aggregated_bias": direction,
                    "consensus_score": allocation.confidence,
                    "voter_count": total_voters,
                    "majority_ratio": allocation.confidence,
                    "supporting_brains": list(set(all_supporting)),
                    "opposing_brains": list(set(all_opposing)),
                    "allocation": {
                        "agreement_level": allocation.agreement_level,
                        "active_groups": allocation.active_groups,
                        "dissenting_groups": allocation.dissenting_groups,
                        "reason": allocation.reason,
                    },
                }
                current_supporting = list(set(all_supporting))

                # ── OU mean-reversion exit (ARB brain) ──
                # Only applies to positions opened by the StatArb strategy
                # (positions whose supporting brains include the OU brain).
                if pos.supporting_brain_ids and any(
                    bid.startswith("OU_") or bid.lower().startswith("ou_")
                    for bid in pos.supporting_brain_ids
                ):
                    for b_info in brains:
                        if b_info.get("brain_type") == "ou_params_v6":
                            try:
                                raw_ou = b_info["adapter"].infer(np.array([mid], dtype=np.float32))
                                ou_z = float(raw_ou.get("z_score", 0.0))
                                should_ou_exit, ou_reason = pm.should_exit_ou_based(ou_z)
                                if should_ou_exit:
                                    _dispatch_managed_close(config, pos, reason=ou_reason, mid=mid)
                                    print(
                                        json.dumps(
                                            {
                                                "event": "ou_exit_triggered",
                                                "time": _utc_iso(),
                                                "ticket": pos.ticket,
                                                "z_score": round(ou_z, 3),
                                                "reason": ou_reason,
                                            },
                                            ensure_ascii=False,
                                        ),
                                        flush=True,
                                    )
                                    pm.clear_position()
                                    return True
                            except Exception:
                                pass
                            break  # only one OU brain

                should_exit, exit_reason = pm.evaluate_brain_exit(
                    current_consensus, current_supporting
                )
                if should_exit:
                    _dispatch_managed_close(config, pos, reason=exit_reason, mid=mid)
                    print(
                        json.dumps(
                            {
                                "event": "brain_exit_triggered",
                                "time": _utc_iso(),
                                "ticket": pos.ticket,
                                "reason": exit_reason,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    pm.clear_position()
                    return True
            except Exception as exc:
                print(
                    json.dumps(
                        {"event": "brain_reeval_error", "time": _utc_iso(), "error": str(exc)},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    # ── 7.5 Layer 2.5: Meta-model multi-factor exit ──
    if pm.meta_exit_engine is not None:
        try:
            # Use freshly-computed consensus if available, else fall back
            meta_consensus = current_consensus if current_consensus else pos.entry_consensus
            meta_supporting = current_supporting if current_supporting else pos.supporting_brain_ids
            # Stamp position side for trend alignment check
            _regime_with_side = dict(regime_info)
            _regime_with_side["_position_side"] = pos.side
            _regime_with_side["trend_direction"] = (
                "long"
                if "bullish" in str(_regime_with_side.get("primary_regime", ""))
                or "trending_up" in str(_regime_with_side.get("primary_regime", ""))
                else "short"
                if "bearish" in str(_regime_with_side.get("primary_regime", ""))
                or "trending_down" in str(_regime_with_side.get("primary_regime", ""))
                else ""
            )

            should_meta_exit, meta_reason = pm.evaluate_meta_exit(
                mid=mid,
                current_atr=current_atr,
                regime_info=_regime_with_side,
                current_consensus=meta_consensus,
                current_supporting=meta_supporting,
            )
            if should_meta_exit:
                _dispatch_managed_close(config, pos, reason=meta_reason, mid=mid)
                print(
                    json.dumps(
                        {
                            "event": "meta_exit_triggered",
                            "time": _utc_iso(),
                            "ticket": pos.ticket,
                            "reason": meta_reason,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                pm.clear_position()
                return True
        except Exception as exc:
            print(
                json.dumps(
                    {"event": "meta_exit_error", "time": _utc_iso(), "error": str(exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )

    # ── 8. Layer 3: Time-based exit ──
    should_time_exit, exit_reason = pm.should_exit_time_based(mid)
    if should_time_exit:
        _dispatch_managed_close(config, pos, reason=exit_reason, mid=mid)
        print(
            json.dumps(
                {
                    "event": "time_exit_triggered",
                    "time": _utc_iso(),
                    "ticket": pos.ticket,
                    "cycles_held": pos.cycles_held,
                    "r": round(pm._compute_r_multiple(mid), 2),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        pm.clear_position()
        return True

    return False


def _reconcile_closed_positions(
    mt5: Any, symbol: str, journal_path: str, known_tickets: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Detect positions closed by SL/TP and return close journal entries."""
    import threading

    closed_entries: list[dict[str, Any]] = []
    if mt5 is None:
        return closed_entries

    # ── positions_get with timeout ──
    _pos_result: list[Any] = [None]
    _pos_exc: list[Any] = [None]

    def _get_positions() -> None:
        try:
            _pos_result[0] = mt5.positions_get(symbol=symbol)
        except Exception as e:
            _pos_exc[0] = e

    _pt = threading.Thread(target=_get_positions, daemon=True)
    _pt.start()
    _pt.join(timeout=5.0)
    if _pt.is_alive() or _pos_exc[0] is not None:
        return closed_entries  # MT5 unresponsive — skip reconciliation

    current_positions = _pos_result[0]
    current_tickets = {p.ticket for p in (current_positions or [])}

    for ticket, open_entry in list(known_tickets.items()):
        if ticket in current_tickets:
            continue

        try:
            _deal_result: list[Any] = [None]
            _deal_exc: list[Any] = [None]

            def _get_deals(
                _deal_result: list = _deal_result,
                _deal_exc: list = _deal_exc,
                ticket: int = ticket,
            ) -> None:
                try:
                    _deal_result[0] = mt5.history_deals_get(position=ticket)
                except Exception as e:
                    _deal_exc[0] = e

            _dt = threading.Thread(target=_get_deals, daemon=True)
            _dt.start()
            _dt.join(timeout=5.0)
            if _dt.is_alive() or _deal_exc[0] is not None:
                deals = None
            else:
                deals = _deal_result[0]
                if not deals:
                    time.sleep(0.2)
                    _retry_result: list[Any] = [None]
                    _retry_exc: list[Any] = [None]

                    def _retry_deals(
                        _retry_result: list = _retry_result,
                        _retry_exc: list = _retry_exc,
                        ticket: int = ticket,
                    ) -> None:
                        try:
                            _retry_result[0] = mt5.history_deals_get(position=ticket)
                        except Exception as e:
                            _retry_exc[0] = e

                    _dt2 = threading.Thread(target=_retry_deals, daemon=True)
                    _dt2.start()
                    _dt2.join(timeout=5.0)
                    deals = (
                        _retry_result[0] if not _dt2.is_alive() and _retry_exc[0] is None else None
                    )
        except Exception as _exc:
            deals = None

        close_price = None
        close_time = None
        close_reason = None
        close_volume = open_entry.get("volume") or open_entry.get("effective_volume_hint", 0.0)

        if deals:
            for deal in deals:
                deal_reason = getattr(deal, "reason", -1)
                if deal_reason in (4, 5):  # DEAL_REASON_SL=4, DEAL_REASON_TP=5
                    close_price = getattr(deal, "price", None)
                    close_time = getattr(deal, "time", None)
                    close_reason = deal_reason

            if close_price is None and deals and len(deals) >= 2:
                exit_deals = [d for d in deals if getattr(d, "entry", -1) == 1]
                if exit_deals:
                    last_exit = max(exit_deals, key=lambda d: getattr(d, "time", 0))
                    close_price = getattr(last_exit, "price", None)
                    close_time = getattr(last_exit, "time", None)
                if close_price is None:
                    last_deal = max(deals, key=lambda d: getattr(d, "time", 0))
                    close_price = getattr(last_deal, "price", None)
                    close_time = getattr(last_deal, "time", None)

        side = str(open_entry.get("side", ""))
        entry_price = None
        detail = open_entry.get("detail", {})
        if isinstance(detail, dict):
            req = detail.get("request", {})
            entry_price = req.get("price")

        pnl = None
        if entry_price is not None and close_price is not None and close_volume:
            if side == "long":
                pnl = round((close_price - entry_price) * close_volume, 2)
            elif side == "short":
                pnl = round((entry_price - close_price) * close_volume, 2)

        label = None
        if close_reason in (4,):
            label = "sl_hit_first"
        elif close_reason in (5,):
            label = "tp_hit_first"
        elif pnl is not None:
            if pnl > 0:
                label = "win"
            elif pnl < 0:
                label = "loss"
            else:
                label = "breakeven"
        else:
            label = "manual_close"

        close_reason_str = {4: "sl_hit", 5: "tp_hit"}.get(
            close_reason, "manual_close" if close_reason else "unknown_close"
        )

        if close_price is None:
            print(
                json.dumps(
                    {
                        "event": "reconciliation_deals_unresolved",
                        "time": _utc_iso(),
                        "ticket": ticket,
                        "deals_count": len(deals) if deals else 0,
                        "entry_price": entry_price,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        close_time_iso = (
            datetime.fromtimestamp(close_time, tz=UTC).isoformat().replace("+00:00", "Z")
            if close_time
            else ""
        )

        close_entry = {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": close_time_iso,
            "message_id": f"close_{open_entry.get('message_id', 'unknown')}",
            "target": "exec_bridge",
            "ack_status": "closed",
            "detail": {
                "reason": close_reason_str,
                "close_price": close_price,
                "pnl": pnl,
            },
            "symbol": symbol,
            "action": "close",
            "side": side,
            "volume": close_volume,
            "pnl": pnl,
            "label": label,
            "position_ticket": ticket,
            "sl": open_entry.get("sl"),
            "tp": open_entry.get("tp"),
            "open_message_id": open_entry.get("message_id"),
            "brain_ids": open_entry.get("brain_ids"),
        }
        closed_entries.append(close_entry)
        del known_tickets[ticket]

    return closed_entries


def _build_feature_snapshot(symbol: str, feature_vector: Any) -> Any:
    """Build a minimal feature snapshot for ParliamentService.build_candidate()."""
    from apps.engine.runtime_loop import SimpleFeatureSnapshot
    from core.contracts.ids import new_snapshot_id

    return SimpleFeatureSnapshot(
        snapshot_id=new_snapshot_id(),
        event_time=datetime.now(UTC).replace(tzinfo=None),
        symbol=symbol,
        venue="live_intent_loop",
        feature_vector=feature_vector,
    )


def _build_minimal_control_snapshot() -> Any:
    """Build a minimal control snapshot for ParliamentService.build_candidate()."""
    from core.contracts.domain.system_mode_state import SystemModeState
    from core.contracts.enums import SystemMode
    from core.state.schema_versions import SCHEMA_SYSTEM_MODE_STATE

    mode_state = SystemModeState(
        schema_version=SCHEMA_SYSTEM_MODE_STATE,
        mode_state_id="intent_loop_default",
        current_mode=SystemMode.NORMAL,
        entered_at=datetime.now(UTC).replace(tzinfo=None),
        previous_mode=None,
        reason="live_intent_loop",
    )

    class _MinimalControlSnapshot:
        def __init__(self, mode_state):
            self.mode_state = mode_state
            self.active_overrides = []

    return _MinimalControlSnapshot(mode_state)


def _record_brain_outcomes(proposals, direction, execution_outcome, tracker):
    """Record each brain's performance based on consensus agreement."""
    for p in proposals:
        p_dir = p.prediction.get("direction_bias", "neutral")
        matched = p_dir == direction if direction != "neutral" else p_dir == "neutral"
        composite = (
            round(0.55 + p.prediction.get("confidence", 0.0) * 0.3, 4)
            if matched
            else round(0.25 + p.prediction.get("confidence", 0.0) * 0.2, 4)
        )
        tracker.record_outcome(
            p.brain_id,
            {"composite_score": composite, "execution_outcome": execution_outcome},
        )


def _build_risk_context(mt5: Any, symbol: str) -> dict[str, Any]:
    """Query MT5 for risk metrics: positions, exposure, drawdown (with timeout)."""
    import threading

    ctx: dict[str, Any] = {
        "open_position_count": 0,
        "current_drawdown_pct": 0.0,
        "current_notional_exposure": 0.0,
        "positions_per_symbol": {},
    }
    if mt5 is None:
        ctx["_source"] = "no_mt5"
        return ctx

    try:
        # positions_get with 5s timeout
        _pos_result: list[Any] = [None]
        _pos_exc: list[Any] = [None]

        def _get_pos() -> None:
            try:
                _pos_result[0] = mt5.positions_get(symbol=symbol)
            except Exception as e:
                _pos_exc[0] = e

        _pt = threading.Thread(target=_get_pos, daemon=True)
        _pt.start()
        _pt.join(timeout=5.0)
        if _pt.is_alive() or _pos_exc[0] is not None:
            ctx["_source"] = "mt5_timeout"
            return ctx

        positions = _pos_result[0] or []
        ctx["open_position_count"] = len(positions)

        per_sym: dict[str, int] = {}
        for pos in positions:
            sym = getattr(pos, "symbol", symbol)
            per_sym[sym] = per_sym.get(sym, 0) + 1
            vol = float(getattr(pos, "volume", 0))
            price = float(getattr(pos, "price_open", 0))
            ctx["current_notional_exposure"] += vol * price
        ctx["positions_per_symbol"] = per_sym

        # account_info with 5s timeout
        _acc_result: list[Any] = [None]
        _acc_exc: list[Any] = [None]

        def _get_acc() -> None:
            try:
                _acc_result[0] = mt5.account_info()
            except Exception as e:
                _acc_exc[0] = e

        _at = threading.Thread(target=_get_acc, daemon=True)
        _at.start()
        _at.join(timeout=5.0)
        if not _at.is_alive() and _acc_exc[0] is None and _acc_result[0] is not None:
            acc = _acc_result[0]
            equity = float(getattr(acc, "equity", 0))
            balance = float(getattr(acc, "balance", 0))
            if balance > 0:
                ctx["current_drawdown_pct"] = round(max(0.0, (balance - equity) / balance) * 100, 2)
        ctx["_source"] = "mt5_live"
    except Exception as exc:
        ctx["_source"] = "mt5_error"
        ctx["_error"] = str(exc)

    return ctx


def _build_risk_context_from_broker(broker: Any, symbol: str) -> dict[str, Any]:
    """Build risk context from a BrokerAdapter (broker-agnostic path)."""
    ctx: dict[str, Any] = {
        "open_position_count": 0,
        "current_drawdown_pct": 0.0,
        "current_notional_exposure": 0.0,
        "positions_per_symbol": {},
    }
    try:
        positions = broker.get_open_positions_detail(symbol)
        ctx["open_position_count"] = len(positions)

        per_sym: dict[str, int] = {}
        for pos in positions:
            sym = pos.get("symbol", symbol)
            per_sym[sym] = per_sym.get(sym, 0) + 1
            vol = float(pos.get("volume", 0))
            price = float(pos.get("price_open", 0))
            ctx["current_notional_exposure"] += vol * price
        ctx["positions_per_symbol"] = per_sym
        ctx["current_drawdown_pct"] = broker.get_account_drawdown_pct()
        ctx["_source"] = f"{getattr(broker, 'broker_name', 'unknown')}_live"
    except Exception as exc:
        ctx["_source"] = "broker_error"
        ctx["_error"] = str(exc)
    return ctx


def _evaluate_risk(
    risk_service: Any,
    control_snapshot: Any,
    risk_context: dict[str, Any],
    symbol: str,
    direction: str,
    confidence: float,
) -> dict[str, Any]:
    """Run risk evaluation and return a lightweight verdict dict."""
    from core.contracts.domain.decision_intent import DecisionIntent
    from core.contracts.enums import DecisionAction, RiskDecisionStatus
    from core.contracts.ids import new_intent_id

    action = DecisionAction.OPEN
    intent = DecisionIntent(
        schema_version="decision_intent.v1",
        intent_id=new_intent_id(),
        candidate_id=new_intent_id(),
        snapshot_id=new_intent_id(),
        event_time=datetime.now(UTC).replace(tzinfo=None),
        compiled_at=datetime.now(UTC).replace(tzinfo=None),
        symbol=symbol,
        venue="live",
        action=action,
        side=direction.upper(),
        conviction=confidence,
        priority="normal",
    )

    try:
        verdict = risk_service.evaluate(intent, control_snapshot, context=risk_context)
        return {
            "status": verdict.status.value
            if hasattr(verdict.status, "value")
            else str(verdict.status),
            "risk_tier": verdict.risk_tier,
            "blocking_reasons": verdict.blocking_reasons,
            "warning_reasons": verdict.warning_reasons,
            "blocked": verdict.status in (RiskDecisionStatus.DENY, RiskDecisionStatus.DEFER),
            "mode": control_snapshot.mode_state.current_mode.value
            if hasattr(control_snapshot.mode_state.current_mode, "value")
            else str(control_snapshot.mode_state.current_mode),
        }
    except Exception as exc:
        return {
            "status": "error",
            "risk_tier": "unknown",
            "blocking_reasons": [],
            "warning_reasons": [f"risk_eval_error: {exc}"],
            "blocked": False,
            "mode": "unknown",
        }


def _ensemble_proposals(
    group: dict[str, Any],
    member_proposals: list[Any],
) -> Any:
    """Merge proposals from correlated brains into a single ensemble vote."""
    if len(member_proposals) <= 1:
        return member_proposals[0] if member_proposals else None

    from core.brains.schema_versions import SCHEMA_BRAIN_DECISION_PROPOSAL
    from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal
    from core.contracts.ids import new_proposal_id

    avg_up = float(np.mean([p.prediction.get("up_probability", 0.5) for p in member_proposals]))
    avg_down = float(np.mean([p.prediction.get("down_probability", 0.5) for p in member_proposals]))
    confidence = max(avg_up, avg_down)
    if confidence < 0.01:
        direction = "neutral"
    else:
        direction = "long" if avg_up >= avg_down else "short"

    worst_fallback = any(p.health.get("fallback_used", False) for p in member_proposals)
    worst_runtime = max(p.health.get("runtime_ms", 0.0) for p in member_proposals)
    max_risk = max(p.health.get("risk_score", 0.0) for p in member_proposals)
    backends = list({p.health.get("backend", "unknown") for p in member_proposals})

    return BrainDecisionProposal(
        schema_version=SCHEMA_BRAIN_DECISION_PROPOSAL,
        proposal_id=new_proposal_id(),
        snapshot_id="",
        brain_id=group["group_id"],
        brain_role=group.get("role", "alpha_brain"),
        brain_status="shadow",
        model_version="ensemble",
        event_time=datetime.now(UTC).replace(tzinfo=None),
        generated_at=datetime.now(UTC).replace(tzinfo=None),
        prediction={
            "direction_bias": direction,
            "up_probability": avg_up,
            "down_probability": avg_down,
            "confidence": confidence,
            "uncertainty": 1.0 - confidence,
            "expected_edge_bps": None,
            "expected_hold_seconds": None,
        },
        health={
            "input_ok": True,
            "fallback_used": worst_fallback,
            "runtime_ms": worst_runtime,
            "risk_score": max_risk,
            "volatility_score": 0.5,
            "backend": "+".join(sorted(backends)),
        },
        vote_weight=group.get("vote_weight", 1.0),
    )


# ── ENSEMBLE_GROUPS (shared constant) ────────────────────────────────────

ENSEMBLE_GROUPS: list[dict[str, Any]] = [
    {
        "group_id": "SurvivalAlpha_Ensemble",
        "label": "Survival Alpha (V9 + CRT)",
        "brain_ids": ["V9_Institutional_01", "CRT.sur.chlg.g2026.1"],
        "magic": 90005,
        "role": "alpha_brain",
        "vote_weight": 1.0,
    },
    {
        "group_id": "TreeAlpha_Ensemble",
        "label": "Tree Alpha (LightGBM Champ + XGBoost V9 Challenger)",
        "brain_ids": ["LightGBM_V1_Institutional", "XGBoost_V9_Institutional"],
        "magic": 90008,
        "role": "alpha_brain",
        "vote_weight": 0.9,
    },
]


# ── Contract-group consensus helper ──────────────────────────────────────


def _compute_contract_group_consensus(
    raw_proposals: list[Any],
    brains: list[dict[str, Any]],
    tracker: Any,
    pnl_ledger: Any,
    correlation_tracker: Any,
    base_volume: float,
    current_atr: float,
    regime_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute contract-group consensus and capital allocation from raw proposals.

    Returns a dict with keys: direction, confidence, dynamic_volume, proposals,
    consensus_extra.
    """
    from core.execution.capital_allocator import compute_volume, resolve_conflicts
    from core.parliament.contract_groups import compute_all_group_signals

    # Build (brain_info, proposal) pairs for group assignment
    brain_proposal_pairs: list[tuple[dict[str, Any], Any]] = []
    for i, p in enumerate(raw_proposals):
        b_info = brains[i] if i < len(brains) else {}
        bid = b_info.get("brain_id", "unknown")
        try:
            if not getattr(p, "brain_id", None):
                p.brain_id = bid
        except Exception:
            pass
        brain_proposal_pairs.append((b_info, p))

    # Apply dynamic vote weights (same weighter, but now used per-group)
    from core.brains.services.dynamic_brain_weighter import DynamicBrainWeighter

    weighter = DynamicBrainWeighter(tracker, pnl_store=pnl_ledger)
    weighter.apply_weights(raw_proposals)

    # Per-group consensus
    group_signals = compute_all_group_signals(brain_proposal_pairs, weighter)

    # Capital allocation: resolve conflicts, size position
    allocation = resolve_conflicts(group_signals)

    if allocation.should_trade:
        direction = allocation.direction
        confidence = allocation.confidence

        # Update group correlation tracker
        if correlation_tracker is not None:
            try:
                correlation_tracker.update(group_signals)
            except Exception:
                pass

        # Compute dynamic volume with correlation penalty
        vol_atr = current_atr if current_atr > 0 else None
        raw_volume = compute_volume(
            base_volume=base_volume or 0.01,
            decision=allocation,
            regime=regime_info.get("regime", "normal") if regime_info else "normal",
            vol_atr=vol_atr,
        )

        # Apply correlation penalty from group history
        if correlation_tracker is not None:
            try:
                corr_penalty = correlation_tracker.get_correlation_penalty(group_signals)
                dynamic_volume = round(raw_volume * corr_penalty, 3)
            except Exception:
                dynamic_volume = raw_volume
        else:
            dynamic_volume = raw_volume

        # Build consensus_extra from group signals (for downstream consumers)
        all_supporting: list[str] = []
        all_opposing: list[str] = []
        total_voters = 0
        for _gname, gs in group_signals.items():
            if gs is None:
                continue
            total_voters += gs.total_count
            if gs.direction == direction:
                all_supporting.extend(gs.brain_ids)
            elif gs.direction != "neutral":
                all_opposing.extend(gs.brain_ids)

        consensus_extra = {
            "voter_count": total_voters,
            "majority_ratio": round(allocation.confidence, 4),
            "disagreement_score": round(
                1.0 - allocation.confidence if allocation.agreement_level != "full" else 0.0,
                4,
            ),
            "supporting_brains": list(set(all_supporting)),
            "opposing_brains": list(set(all_opposing)),
            "is_feasible": True,
            "allocation": {
                "agreement_level": allocation.agreement_level,
                "active_groups": allocation.active_groups,
                "dissenting_groups": allocation.dissenting_groups,
            },
            "aggregated_bias": direction,
            "consensus_score": confidence,
        }
        proposals = list(raw_proposals)
    else:
        direction = "neutral"
        confidence = 0.0
        dynamic_volume = 0.0
        consensus_extra = {
            "voter_count": 0,
            "majority_ratio": 0.0,
            "disagreement_score": 1.0,
            "supporting_brains": [],
            "opposing_brains": [],
            "is_feasible": False,
            "allocation": {
                "agreement_level": "none",
                "reason": allocation.reason,
            },
        }
        proposals = list(raw_proposals)

    return {
        "direction": direction,
        "confidence": confidence,
        "dynamic_volume": dynamic_volume,
        "proposals": proposals,
        "consensus_extra": consensus_extra,
    }


# ── Multi-strategy evaluation ────────────────────────────────────────────


def _build_strategy_lines(
    brains: list[dict[str, Any]],
    config: LiveCycleConfig,
) -> dict[str, Any]:
    """Partition brains into contract groups and create strategy line objects.

    Returns dict mapping strategy_name → StrategyLine instance.
    """
    # Partition brains by contract type
    barrier_brains: list[dict[str, Any]] = []
    micro_brains: list[dict[str, Any]] = []
    statarb_brains: list[dict[str, Any]] = []

    for b_info in brains:
        btype = b_info.get("brain_type", "")
        if btype in BARRIER_GROUP["brain_types"]:
            barrier_brains.append(b_info)
        elif btype in MICRO_GROUP["brain_types"]:
            micro_brains.append(b_info)
        elif btype in ARB_GROUP["brain_types"]:
            statarb_brains.append(b_info)
        else:
            # Unknown types go to barrier as default
            barrier_brains.append(b_info)

    def _cfg(name: str, key: str, default: Any) -> Any:
        """Read a value from live.yaml strategy_lines.<name>.<key>, falling back to default."""
        return config.strategy_configs.get(name, {}).get(key, default)

    strategies: dict[str, Any] = {}

    if barrier_brains:
        strategies["barrier_12bar"] = BarrierStrategy(
            StrategyLineConfig(
                name="barrier_12bar",
                magic=90001,
                brain_types=BARRIER_GROUP["brain_types"],
                base_volume=config.volume or _cfg("barrier_12bar", "base_volume", 0.01),
                max_volume=_cfg("barrier_12bar", "max_volume", 0.05),
                base_sl_atr_mult=_cfg("barrier_12bar", "sl", {}).get(
                    "base_atr_mult", config.sl_atr_mult
                ),
                base_tp_atr_mult=_cfg("barrier_12bar", "tp", {}).get(
                    "base_atr_mult", config.tp_atr_mult
                ),
                hard_sl_ratio=_cfg("barrier_12bar", "sl", {}).get("hard_sl_ratio", 1.5),
                confidence_threshold=config.confidence_threshold,
                long_bias_discount=_cfg("barrier_12bar", "direction_balance", {}).get(
                    "long_bias_discount", 0.05
                ),
            ),
            barrier_brains,
            budget=StrategyBudget(
                "barrier_12bar",
                daily_loss_limit_pct=_cfg("barrier_12bar", "budget", {}).get(
                    "daily_loss_limit_pct", -0.03
                ),
                max_consecutive_losses=_cfg("barrier_12bar", "budget", {}).get(
                    "max_consecutive_losses", 5
                ),
            ),
        )

    if micro_brains:
        strategies["micro_3bar"] = MicroStrategy(
            StrategyLineConfig(
                name="micro_3bar",
                magic=90002,
                brain_types=MICRO_GROUP["brain_types"],
                base_volume=config.volume or _cfg("micro_3bar", "base_volume", 0.01),
                max_volume=_cfg("micro_3bar", "max_volume", 0.03),
                base_sl_atr_mult=_cfg("micro_3bar", "sl", {}).get("base_atr_mult", 1.0),
                base_tp_atr_mult=_cfg("micro_3bar", "tp", {}).get("base_atr_mult", 1.5),
                hard_sl_ratio=_cfg("micro_3bar", "sl", {}).get("hard_sl_ratio", 1.5),
                confidence_threshold=config.confidence_threshold,
                long_bias_discount=_cfg("micro_3bar", "direction_balance", {}).get(
                    "long_bias_discount", 0.03
                ),
            ),
            micro_brains,
            budget=StrategyBudget(
                "micro_3bar",
                daily_loss_limit_pct=_cfg("micro_3bar", "budget", {}).get(
                    "daily_loss_limit_pct", -0.02
                ),
                max_consecutive_losses=_cfg("micro_3bar", "budget", {}).get(
                    "max_consecutive_losses", 6
                ),
            ),
        )

    if statarb_brains:
        strategies["statarb_dynamic"] = StatArbStrategy(
            StrategyLineConfig(
                name="statarb_dynamic",
                magic=90003,
                brain_types=ARB_GROUP["brain_types"],
                base_volume=config.volume or _cfg("statarb_dynamic", "base_volume", 0.01),
                max_volume=_cfg("statarb_dynamic", "max_volume", 0.03),
                base_sl_atr_mult=_cfg("statarb_dynamic", "sl", {}).get("base_atr_mult", 1.5),
                base_tp_atr_mult=_cfg("statarb_dynamic", "tp", {}).get("base_atr_mult", 3.0),
                hard_sl_ratio=_cfg("statarb_dynamic", "sl", {}).get("hard_sl_ratio", 1.5),
                confidence_threshold=config.confidence_threshold,
                long_bias_discount=_cfg("statarb_dynamic", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
            ),
            statarb_brains,
            budget=StrategyBudget(
                "statarb_dynamic",
                daily_loss_limit_pct=_cfg("statarb_dynamic", "budget", {}).get(
                    "daily_loss_limit_pct", -0.015
                ),
                max_consecutive_losses=_cfg("statarb_dynamic", "budget", {}).get(
                    "max_consecutive_losses", 4
                ),
            ),
        )

    return strategies


def _evaluate_strategy_lines(
    *,
    strategy_lines: dict[str, Any],
    feature_vector: Any,
    micro_feature_vector: Any,
    mid_price: float | None,
    bid: float | None,
    ask: float | None,
    current_atr: float,
    regime_info: dict[str, Any],
    regime_gate: RegimeGate | None,
    trend_direction: str = "neutral",
    trend_strength: float = 0.0,
    portfolio_risk: PortfolioRiskController,
    execution_queue: ExecutionQueue,
    tracker: Any,
    pnl_ledger: Any,
    current_positions: dict[str, dict[str, Any]],
    session_volume_mult: float = 1.0,
) -> dict[str, Any]:
    """Run independent strategy evaluations + portfolio risk + execution queue.

    Returns a summary dict for logging.
    """
    decisions: list[Any] = []
    strategy_results: list[dict[str, Any]] = []

    for sname, strategy in strategy_lines.items():
        gate_mode = "full"
        if regime_gate is not None:
            gate_mode = regime_gate.get_strategy_mode(sname)

        decision = strategy.evaluate(
            feature_vector=feature_vector,
            micro_feature_vector=micro_feature_vector,
            mid_price=mid_price,
            bid=bid,
            ask=ask,
            current_atr=current_atr,
            regime_info=regime_info,
            regime_gate_mode=gate_mode,
            trend_direction=trend_direction,
            trend_strength=trend_strength,
            tracker=tracker,
            pnl_ledger=pnl_ledger,
        )

        # Apply session volume multiplier
        if session_volume_mult != 1.0 and decision.should_trade:
            decision.volume = max(0.01, round(decision.volume * session_volume_mult, 2))

        strategy_results.append(
            {
                "strategy": sname,
                "should_trade": decision.should_trade,
                "direction": decision.direction,
                "confidence": decision.confidence,
                "volume": decision.volume,
                "regime_mode": gate_mode,
                "reason": decision.reason,
                "supporting": decision.supporting_count,
                "total": decision.total_count,
            }
        )

        if not decision.should_trade:
            continue

        # Portfolio risk check
        risk_result = portfolio_risk.check(decision, current_positions)

        if risk_result.verdict.value == "rejected":
            strategy_results[-1]["risk"] = "rejected"
            strategy_results[-1]["risk_reason"] = risk_result.reason
            continue

        # Queue for execution
        execution_queue.enqueue(sname, decision, risk_result)
        decisions.append(decision)
        strategy_results[-1]["risk"] = risk_result.verdict.value
        if risk_result.adjusted_volume != decision.volume:
            strategy_results[-1]["adjusted_volume"] = risk_result.adjusted_volume

    # Summary
    len([d for d in decisions if d.should_trade])
    decisions_map: dict[str, Any] = {}
    for sname in strategy_lines:
        for d in decisions:
            if d.strategy_name == sname:
                decisions_map[sname] = d
                break
    return {
        "strategy_results": strategy_results,
        "trade_decisions": len(decisions),
        "queued": execution_queue.queue_size,
        "active_strategies": list(strategy_lines.keys()),
        "decisions_map": decisions_map,
    }


# ── Cycle execution ──────────────────────────────────────────────────────


def execute_live_cycle(
    config: LiveCycleConfig,
    state: LiveCycleState,
    *,
    mt5: Any,
    broker: Any = None,
    feature_service: Any,
    feature_computer: Any,
    micro_feature_computer: Any = None,
    micro_feature_adapter: Any = None,
    feature_schema: Any,
    feature_store: Any,
    brains: list[dict[str, Any]],
    parliament: Any,
    risk_service: Any,
    regime_detector: Any,
    tracker: Any,
    rolling_norm: Any,
    feature_adapter: Any,
    journal_path: Path,
    pnl_ledger: Any = None,
) -> tuple[LiveCycleState, bool]:
    """Execute one iteration of the live intent cycle.

    Args:
        mt5: Raw MetaTrader5 module (for deal history queries; kept for
            backward compat — prefer *broker* for new code).
        broker: :class:`BrokerAdapter` for price/ATR/position queries.
            When None, falls back to raw *mt5* calls.  This is the swap
            point for future FIX / cloud brokers.

    Returns (updated_state, should_continue). The caller owns the ``while True``
    loop and the ``time.sleep()`` between iterations.
    """
    state.loop_iteration += 1

    # ── Cycle-start heartbeat (every iteration — catches freeze location) ──
    print(
        json.dumps(
            {"event": "cycle_start", "time": _utc_iso(), "iteration": state.loop_iteration},
            ensure_ascii=False,
        ),
        flush=True,
    )

    # ── On first cycle, filter known_open_tickets to only currently-open positions ──
    # Old tickets from previous sessions cause history_deals_get() to hang.
    if state.loop_iteration == 1 and state.known_open_tickets and not config.no_mt5:
        try:
            _positions = mt5.positions_get(symbol=config.symbol) or []
            _open_tickets = {p.ticket for p in _positions}
            state.known_open_tickets = {
                t: r for t, r in state.known_open_tickets.items() if t in _open_tickets
            }
        except Exception:
            pass

    # ── Reconcile closed positions ──
    # Run on every Nth cycle (reconciliation_interval), AND on the very first
    # cycle after a restart if known_open_tickets was bootstrapped from the
    # journal — this prevents a dispatch from slipping through before the
    # first scheduled reconciliation detects a stop-loss cascade.
    _run_reconciliation = state.loop_iteration % config.reconciliation_interval == 0
    if not state._initial_reconciliation_done and state.known_open_tickets:
        _run_reconciliation = True
        state._initial_reconciliation_done = True

    if not config.no_mt5 and state.known_open_tickets and _run_reconciliation:
        try:
            _closed = _reconcile_closed_positions(
                mt5, config.symbol, str(journal_path), state.known_open_tickets
            )
            if _closed:
                _existing = (
                    journal_path.read_text(encoding="utf-8") if journal_path.exists() else ""
                )
                with open(journal_path, "a", encoding="utf-8") as _jf:
                    for _entry in _closed:
                        _mid = _entry.get("message_id", "")
                        if _mid and _mid in _existing:
                            continue
                        _jf.write(json.dumps(_entry, ensure_ascii=False) + "\n")
                # ── Update losing-streak tracker ──
                for _entry in _closed:
                    _label = _entry.get("label", "")
                    if _label in ("sl_hit_first", "loss"):
                        state.consecutive_sl_hits += 1
                    elif _label in ("tp_hit_first", "win"):
                        state.consecutive_sl_hits = 0

                if state.consecutive_sl_hits >= 3:
                    state.sl_streak_blocked_until = time.time() + 1800  # 30 min block
                    print(
                        json.dumps(
                            {
                                "event": "sl_streak_blocked",
                                "time": _utc_iso(),
                                "consecutive_sl": state.consecutive_sl_hits,
                                "blocked_until_utc": datetime.fromtimestamp(
                                    state.sl_streak_blocked_until, tz=UTC
                                ).isoformat(),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

                print(
                    json.dumps(
                        {
                            "event": "positions_closed",
                            "time": _utc_iso(),
                            "count": len(_closed),
                            "tickets": [e["position_ticket"] for e in _closed],
                            "sl_streak": state.consecutive_sl_hits,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        except Exception:
            pass

    # ── Protection flag check ──
    from scripts.send_live_order import resolve_protection_flag_path

    flag_path = resolve_protection_flag_path(config.base_dir, config.protection_flag_path)
    if flag_path.exists() and not config.ignore_protection_flag:
        if not state.flag_notice:
            print(
                json.dumps(
                    {
                        "event": "protection_skip",
                        "time": _utc_iso(),
                        "flag": str(flag_path),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            state.flag_notice = True
        return state, True  # continue
    state.flag_notice = False

    # ── P&L ledger: settle pending signals (MUST run before cooldown /
    #    SL-streak early returns so counterfactual signals are always closed)
    mid_price: float | None = None
    if pnl_ledger is not None:
        try:
            if broker is not None:
                mid_price, _, _ = broker.fetch_prices(config.symbol)
            elif not config.no_mt5:
                mid_price, _, _ = _mid_and_prices(mt5, config.symbol)
        except Exception:
            pass
        if mid_price is not None and mid_price > 0 and pnl_ledger.pending_count > 0:
            try:
                pnl_ledger.settle_all(mid_price)
            except Exception:
                pass

        # ── Shadow verification: settle previous cycle's consensus decision ──
        if mid_price is not None and mid_price > 0 and state.shadow_verification_pending:
            try:
                pending = state.shadow_verification_pending
                entry_price = pending["entry_price"]
                direction = pending["direction"]
                if direction == "long":
                    ctf_pnl = round(mid_price - entry_price, 6)
                elif direction == "short":
                    ctf_pnl = round(entry_price - mid_price, 6)
                else:
                    ctf_pnl = 0.0
                ctf_bps = round((ctf_pnl / entry_price) * 10000, 2) if entry_price > 0 else 0.0
                print(
                    json.dumps(
                        {
                            "event": "shadow_verified",
                            "time": _utc_iso(),
                            "direction": direction,
                            "entry_price": round(entry_price, 2),
                            "exit_price": round(mid_price, 2),
                            "counterfactual_pnl": ctf_pnl,
                            "counterfactual_bps": ctf_bps,
                            "consensus_score": pending.get("consensus_score", 0),
                            "supporting_brains": pending.get("supporting_brains", []),
                            "opposing_brains": pending.get("opposing_brains", []),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception:
                pass
            finally:
                state.shadow_verification_pending = None

    # ── Cycle start heartbeat (every 10 iterations) ──
    if state.loop_iteration % 10 == 0:
        print(
            json.dumps(
                {"event": "cycle_tick", "time": _utc_iso(), "cycle": state.cycle_count},
                ensure_ascii=False,
            ),
            flush=True,
        )

    # ── Cooldown check ──
    now = time.monotonic()
    if cooldown_blocks_fire(now, state.last_fire, config.cooldown_seconds):
        return state, True  # continue

    # ── SL streak circuit breaker ──
    if state.sl_streak_blocked_until > 0 and time.time() < state.sl_streak_blocked_until:
        if state.loop_iteration % 10 == 0:
            print(
                json.dumps(
                    {
                        "event": "sl_streak_block_active",
                        "time": _utc_iso(),
                        "consecutive_sl": state.consecutive_sl_hits,
                        "blocked_until_utc": datetime.fromtimestamp(
                            state.sl_streak_blocked_until, tz=UTC
                        ).isoformat(),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        return state, True  # continue
    elif state.sl_streak_blocked_until > 0 and time.time() >= state.sl_streak_blocked_until:
        # Block expired — reset streak and lift
        state.sl_streak_blocked_until = 0.0
        state.consecutive_sl_hits = 0

    # ── Journal-based SL streak check (bypasses reconciliation cycle timing) ──
    if not state.sl_streak_blocked_until:
        _blocked, _streak_count = _check_recent_sl_streak(
            str(journal_path), lookback_seconds=300.0, threshold=3
        )
        if _blocked:
            state.sl_streak_blocked_until = time.time() + 1800
            state.consecutive_sl_hits = max(state.consecutive_sl_hits, _streak_count)
            print(
                json.dumps(
                    {
                        "event": "sl_streak_blocked_journal",
                        "time": _utc_iso(),
                        "consecutive_sl": _streak_count,
                        "blocked_until_utc": datetime.fromtimestamp(
                            state.sl_streak_blocked_until, tz=UTC
                        ).isoformat(),
                        "source": "journal_scan_pre_dispatch",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return state, True  # continue

    # ── Position limit check ──
    if not config.no_mt5:
        try:
            pos_count = (
                broker.count_positions(config.symbol)
                if broker is not None
                else _position_count(mt5, config.symbol)
            )
        except Exception as _pos_exc:
            print(
                json.dumps(
                    {"event": "position_count_error", "time": _utc_iso(), "error": str(_pos_exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            pos_count = -1

        # -1 means MT5 connection is dead — attempt reconnect once
        if pos_count < 0:
            try:
                if mt5 is not None:
                    mt5.initialize()
                    pos_count = (
                        broker.count_positions(config.symbol)
                        if broker is not None
                        else _position_count(mt5, config.symbol)
                    )
            except Exception:
                pass

        # Still unknown after reconnect — block trading for safety
        if pos_count < 0:
            if state.loop_iteration % 10 == 0:
                print(
                    json.dumps(
                        {
                            "event": "position_count_unavailable",
                            "time": _utc_iso(),
                            "detail": "MT5 connection lost, blocking dispatch for safety",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            return state, True  # continue (skip this cycle)

        # ── Dynamic exit management phase ──
        # Runs whenever positions are registered, regardless of position limit.
        if (
            config.exit_management_enabled
            and state.position_manager is not None
            and state.position_manager.has_position()
        ):
            try:
                _execute_management_phase(
                    config,
                    state,
                    mt5=mt5,
                    broker=broker,
                    brains=brains,
                    parliament=parliament,
                    regime_detector=regime_detector,
                    tracker=tracker,
                    feature_service=feature_service,
                    micro_feature_computer=micro_feature_computer,
                    micro_feature_adapter=micro_feature_adapter,
                )
            except Exception:
                pass
            # Persist position state every N cycles (trail steps, breakeven, etc.)
            if state.loop_iteration % 5 == 0 and state.position_manager is not None:
                try:
                    state.position_manager.save_state(config.position_state_path)
                except Exception:
                    pass

        if pos_count >= config.max_positions:
            if state.loop_iteration % 10 == 0:
                print(
                    json.dumps(
                        {
                            "event": "position_limit_skip",
                            "time": _utc_iso(),
                            "pos_count": pos_count,
                            "max": config.max_positions,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            return state, True  # continue (skip entry logic)

    # ── Compute features ──
    if config.no_mt5:
        feature_vector: Any = np.zeros(40, dtype=np.float64)
        micro_feature_vector: Any = np.zeros(9, dtype=np.float64)
    else:
        trigger = {"symbol": config.symbol, "venue": "MT5"}
        feature_vector = feature_service.build_feature_vector(trigger)

        # Compute microstructure 9-feature vector for Transformer/XGBoost brains
        if micro_feature_computer is not None and micro_feature_adapter is not None:
            micro_features = micro_feature_computer.compute_all()
            micro_feature_vector = micro_feature_adapter.build_model_input(micro_features).ravel()
        else:
            micro_feature_vector = np.zeros(9, dtype=np.float64)

    # ── Persist features to LocalFeatureStore ──
    if not config.disable_feature_store and not config.no_mt5:
        try:
            from core.deployment.feature_update_producer import produce_from_live_computer

            for record in produce_from_live_computer(
                feature_computer, feature_schema, config.symbol
            ):
                feature_store.write_records([record])
        except Exception as exc:
            print(
                json.dumps(
                    {"event": "feature_store_write_error", "time": _utc_iso(), "error": str(exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )

    # ── Market regime detection ──
    regime_info: dict[str, Any] = {}
    if not config.no_mt5 and regime_detector is not None:
        try:
            current_atr = (
                broker.fetch_current_atr(config.symbol)
                if broker is not None
                else _get_current_atr(mt5, config.symbol)
            )
            if current_atr > 0:
                regime_info = regime_detector.update(current_atr)
        except Exception:
            pass

    # ── Run inference ──
    raw_output: dict[str, Any] = {}
    proposal: Any = None
    proposals: list[Any] = []
    consensus_extra: dict[str, Any] = {}
    control_snapshot: Any = None

    dynamic_volume = config.volume or 0.01

    if config.multi_brain and config.multi_strategy_enabled:
        # ── NEW: Multi-strategy independent evaluation ──
        # Each contract group runs independently → portfolio risk → staggered dispatch

        # Partition brains into contract groups and build strategy lines
        strategies = _build_strategy_lines(brains, config)

        # ── Regime gate: persist across cycles, feed M5+H1 bars ──
        if state.regime_gate is None:
            state.regime_gate = RegimeGate()
            if not config.no_mt5:
                _bootstrap_regime_gate(mt5, config.symbol, state.regime_gate)

        regime_gate: RegimeGate | None = state.regime_gate
        regime_gate_result: dict[str, Any] = {}
        trend_direction: str = "neutral"
        trend_strength: float = 0.0

        if not config.no_mt5 and regime_gate is not None:
            try:
                _feed_regime_gate_cycle(mt5, config.symbol, regime_gate)
                atr_val = current_atr if current_atr > 0 else 5.0
                atr_pct = regime_info.get("atr_pct", 0.5) if regime_info else 0.5
                vol_regime = regime_info.get("regime", "normal") if regime_info else "normal"
                regime_gate_result = regime_gate.classify(atr_val, atr_pct, vol_regime=vol_regime)
                if regime_info:
                    regime_info["regime_gate"] = regime_gate_result
                trend_direction = regime_gate_result.get("primary_trend", "neutral")
                trend_strength = regime_gate_result.get("h1_trend_strength", 0.0)
            except Exception:
                regime_gate = None

        # ── Pre-close check: stop new positions / flatten before market close ──
        pre_close = _check_pre_close(config, state)
        if pre_close.get("no_new_positions"):
            print(
                json.dumps(
                    {
                        "event": "pre_close_block",
                        "time": _utc_iso(),
                        "close_label": pre_close["close_label"],
                        "minutes_to_close": pre_close["minutes_to_close"],
                        "must_flatten": pre_close.get("must_flatten", False),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return state, True  # continue (skip new position entry)

        # ── Session detection + data quality guards ──
        session_info: dict[str, Any] = {}
        if not config.no_mt5:
            try:
                from core.execution.pre_trade_guards import check_feature_vector, detect_session

                session_info = detect_session()
                if session_info.get("risk_tier") == "off":
                    return state, True  # market closed, skip cycle

                # Feature vector quality check
                fv_check = check_feature_vector(feature_vector)
                if not fv_check.get("passed"):
                    print(
                        json.dumps(
                            {
                                "event": "feature_vector_rejected",
                                "time": _utc_iso(),
                                "issues": fv_check["issues"],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    return state, True  # skip cycle on bad features
            except Exception:
                pass

        # Portfolio risk controller + execution queue
        portfolio_risk = PortfolioRiskController(
            max_gross_exposure=config.portfolio_max_gross,
            max_net_exposure=config.portfolio_max_net,
            max_same_direction=config.portfolio_max_same_dir,
        )
        exec_queue = ExecutionQueue(
            stagger_seconds=config.strategy_stagger_seconds,
        )

        # Current positions for portfolio risk (map strategy_name → position)
        current_positions: dict[str, dict[str, Any]] = {}
        if state.position_manager is not None and state.position_manager.has_position():
            pos = state.position_manager.get_position()
            if pos is not None:
                # Determine which strategy owns this position (from supporting brains)
                owner = "barrier_12bar"  # default
                if pos.supporting_brain_ids:
                    for bid in pos.supporting_brain_ids:
                        for bi in brains:
                            if bi.get("brain_id") == bid:
                                bt = bi.get("brain_type", "")
                                if bt in MICRO_GROUP["brain_types"]:
                                    owner = "micro_3bar"
                                elif bt in ARB_GROUP["brain_types"]:
                                    owner = "statarb_dynamic"
                                break
                current_positions[owner] = {
                    "strategy": owner,
                    "direction": pos.side,
                    "volume": pos.volume,
                    "ticket": pos.ticket,
                }

        # Evaluate all strategy lines
        eval_summary = _evaluate_strategy_lines(
            strategy_lines=strategies,
            feature_vector=feature_vector,
            micro_feature_vector=micro_feature_vector,
            mid_price=mid_price,
            bid=None,
            ask=None,
            current_atr=current_atr,
            regime_info=regime_info,
            regime_gate=regime_gate,
            trend_direction=trend_direction,
            trend_strength=trend_strength,
            portfolio_risk=portfolio_risk,
            execution_queue=exec_queue,
            tracker=tracker,
            pnl_ledger=pnl_ledger,
            current_positions=current_positions,
            session_volume_mult=session_info.get("volume_mult", 1.0),
        )

        # Log strategy evaluation results
        print(
            json.dumps(
                {
                    "event": "multi_strategy_eval",
                    "time": _utc_iso(),
                    "strategies": eval_summary["strategy_results"],
                    "trade_decisions": eval_summary["trade_decisions"],
                    "queued": eval_summary["queued"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        # Flush execution queue → dispatch to MT5
        if exec_queue.queue_size > 0 and not config.no_mt5:
            from scripts.send_live_order import dispatch_live_open_order

            dispatch_results = exec_queue.flush(
                dispatch_live_open_order,
                journal_path=str(journal_path),
                mt5_terminal_path=config.mt5_terminal_path,
                symbol=config.symbol,
                base_dir=config.base_dir,
                ignore_protection_flag=config.ignore_protection_flag,
                protection_flag_path=config.protection_flag_path,
            )

            # Log dispatch results
            dispatched_count = sum(1 for r in dispatch_results if r.dispatched)
            if dispatched_count > 0:
                state.last_fire = time.monotonic()
                state.cycle_count += 1

            for dr in dispatch_results:
                print(
                    json.dumps(
                        {
                            "event": "strategy_dispatched" if dr.dispatched else "strategy_skipped",
                            "time": _utc_iso(),
                            "strategy": dr.strategy_name,
                            "magic": dr.magic,
                            "dispatched": dr.dispatched,
                            "reason": dr.reason,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

            # Log brain outcomes for dispatched strategies
            for dr in dispatch_results:
                if dr.dispatched and dr.strategy_name in strategies:
                    strategy = strategies[dr.strategy_name]
                    try:
                        strategy_proposals = strategy._run_inference(
                            feature_vector,
                            micro_feature_vector,
                            mid_price,
                        )
                        _record_brain_outcomes(strategy_proposals, "long", "pending", tracker)
                    except Exception:
                        pass

            # ── Register opened positions for dynamic exit management ──
            if (
                config.exit_management_enabled
                and state.position_manager is not None
                and not config.no_mt5
            ):
                _HORIZON_BY_TYPE: dict[str, int] = {
                    "onnx_v9": 12,
                    "xgboost_v4.5": 3,
                    "ou_params_v6": 0,
                    "transformer_v4.3": 3,
                    "transformer_v5": 3,
                    "lightgbm_v1": 12,
                    "xgboost_v9": 12,
                    "deepresmlp": 12,
                    "online_sgd": 12,
                }
                decisions_map = eval_summary.get("decisions_map", {})

                for dr in dispatch_results:
                    if not dr.dispatched:
                        continue
                    decision = decisions_map.get(dr.strategy_name)
                    if decision is None:
                        continue

                    intent_id = (dr.journal_entry or {}).get("intent_id", "")
                    ticket: int | None = None
                    entry_from_journal: float | None = None

                    # Retry up to 10 times (5s total) — bridge writes journal async
                    if intent_id and journal_path:
                        import time as _time2

                        for _retry in range(10):
                            if journal_path.exists():
                                for line in journal_path.read_text(encoding="utf-8").splitlines():
                                    line = line.strip()
                                    if not line or intent_id not in line:
                                        continue
                                    try:
                                        rec = json.loads(line)
                                        if rec.get("message_id") == intent_id:
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
                                            if ticket is not None:
                                                break  # found valid entry, stop scanning lines
                                    except Exception:
                                        pass
                                if ticket is not None:
                                    break  # got the ticket, stop retrying
                            _time2.sleep(0.5)

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

                    # Build per-model horizon map
                    model_horizons: dict[str, int] = {}
                    for bid in decision.brain_ids:
                        btype = ""
                        for bi in brains:
                            if bi.get("brain_id") == bid:
                                btype = bi.get("brain_type", "")
                                break
                        model_horizons[bid] = _HORIZON_BY_TYPE.get(btype, 12)

                    try:
                        state.position_manager.register_position(
                            ticket=ticket,
                            side=decision.direction,
                            entry_price=entry_price,
                            volume=decision.volume,
                            initial_sl=decision.sl,
                            initial_tp=decision.tp,
                            entry_atr=current_atr,
                            entry_cycle=state.loop_iteration,
                            entry_consensus=entry_consensus,
                            supporting_brain_ids=decision.brain_ids,
                            model_horizons=model_horizons,
                            current_high=entry_price,
                        )
                        # Persist immediately after registration
                        try:
                            state.position_manager.save_state(config.position_state_path)
                        except Exception:
                            pass
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
                    except Exception as _reg_exc:
                        print(
                            json.dumps(
                                {
                                    "event": "position_register_error",
                                    "time": _utc_iso(),
                                    "strategy": dr.strategy_name,
                                    "error": str(_reg_exc),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )

        # Shadow verification recording
        if config.no_mt5:
            for sname, strategy in strategies.items():
                try:
                    decision = strategy.evaluate(
                        feature_vector=feature_vector,
                        micro_feature_vector=micro_feature_vector,
                        mid_price=mid_price,
                        bid=None,
                        ask=None,
                        current_atr=current_atr,
                        regime_info=regime_info,
                        regime_gate_mode="full",
                        trend_direction=trend_direction,
                        trend_strength=trend_strength,
                    )
                    if decision.should_trade and mid_price is not None:
                        state.shadow_verification_pending = {
                            "direction": decision.direction,
                            "entry_price": mid_price,
                            "consensus_score": decision.confidence,
                            "strategy": sname,
                            "supporting_brains": decision.brain_ids,
                            "opposing_brains": [],
                        }
                except Exception:
                    pass

        if config.multi_strategy_enabled:
            _log_cycle_end(state.loop_iteration)
            return state, not config.once

    elif config.multi_brain:
        # ── LEGACY: Contract-group consensus (fallback) ──
        pre_close = _check_pre_close(config, state)
        if pre_close.get("no_new_positions"):
            print(
                json.dumps(
                    {
                        "event": "pre_close_block",
                        "time": _utc_iso(),
                        "close_label": pre_close["close_label"],
                        "minutes_to_close": pre_close["minutes_to_close"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            _log_cycle_end(state.loop_iteration)
            return state, not config.once

        # Session + feature quality check for legacy path
        if not config.no_mt5:
            try:
                from core.execution.pre_trade_guards import check_feature_vector, detect_session

                _s = detect_session()
                if _s.get("risk_tier") == "off":
                    _log_cycle_end(state.loop_iteration)
                    return state, not config.once
                float(_s.get("volume_mult", 1.0))

                _fv = check_feature_vector(feature_vector)
                if not _fv.get("passed"):
                    _log_cycle_end(state.loop_iteration)
                    return state, not config.once
            except Exception:
                pass

        raw_proposals: list[Any] = []
        for b_info in brains:
            schema_id = b_info.get("feature_schema_id", "")
            btype = b_info.get("brain_type", "")
            if btype == "ou_params_v6":
                fv = (
                    np.array([mid_price], dtype=np.float32)
                    if mid_price
                    else np.zeros(1, dtype=np.float32)
                )
            elif "microstructure" in schema_id:
                fv = micro_feature_vector
            else:
                fv = feature_vector

            try:
                raw = b_info["adapter"].infer(fv)
                prop = b_info["adapter"].get_signal(raw)
                raw_proposals.append(prop)
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "event": "brain_infer_error",
                            "time": _utc_iso(),
                            "brain_id": b_info.get("brain_id", "unknown"),
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        result = _compute_contract_group_consensus(
            raw_proposals=raw_proposals,
            brains=brains,
            tracker=tracker,
            pnl_ledger=pnl_ledger,
            correlation_tracker=state.correlation_tracker,
            base_volume=config.volume or 0.01,
            current_atr=current_atr,
            regime_info=regime_info,
        )
        direction = result["direction"]
        confidence = result["confidence"]
        dynamic_volume = result["dynamic_volume"]
        proposals = result["proposals"]
        consensus_extra = result["consensus_extra"]
    else:
        raw_output = brains[0]["adapter"].infer(feature_vector) if brains else {}
        proposal = brains[0]["adapter"].get_signal(raw_output) if brains else None
        if proposal is None:
            direction = "neutral"
            confidence = 0.0
        else:
            direction = proposal.prediction.get("direction_bias", "neutral")
            confidence = proposal.prediction.get("confidence", 0.0)

    # ── Record counterfactual signals for P&L tracking ──
    if pnl_ledger is not None and mid_price is not None and mid_price > 0:
        try:
            if config.multi_brain:
                for p in raw_proposals:
                    pnl_ledger.record_signal(
                        brain_id=p.brain_id,
                        symbol=config.symbol,
                        direction=p.prediction.get("direction_bias", "neutral"),
                        entry_price=mid_price,
                        confidence=p.prediction.get("confidence", 0.5),
                    )
            elif proposal is not None:
                pnl_ledger.record_signal(
                    brain_id=config.brain_entry.get("brain_id", "unknown"),
                    symbol=config.symbol,
                    direction=proposal.prediction.get("direction_bias", "neutral"),
                    entry_price=mid_price,
                    confidence=proposal.prediction.get("confidence", 0.5),
                )
        except Exception:
            pass

    # ── Regime-aware direction bias (legacy path) ──
    # Uses H1 trend from RegimeGate when available, falls back to
    # RegimeDetector primary_regime for backward compat.
    if direction != "neutral":
        _td = "neutral"
        _ts = 0.0
        # Prefer real trend from RegimeGate (persisted in state)
        if state.regime_gate is not None and state.regime_gate.h1_is_ready:
            _td = state.regime_gate.h1_trend_direction
            _ts = state.regime_gate.h1_trend_strength
        elif regime_info:
            primary_regime = regime_info.get("primary_regime", "")
            regime_conf = float(regime_info.get("regime_confidence", 0.0))
            if primary_regime and regime_conf > 0.5:
                if "trending_up" in primary_regime or "bullish" in primary_regime:
                    _td = "long"
                    _ts = regime_conf
                elif "trending_down" in primary_regime or "bearish" in primary_regime:
                    _td = "short"
                    _ts = regime_conf

        if _td != "neutral" and _ts > 0.15:
            if direction == _td:
                confidence = min(0.99, confidence + 0.03 * _ts)
            else:
                confidence = max(0.30, confidence - 0.06 * _ts)

    # ── Low confidence skip ──
    if confidence < config.confidence_threshold or direction == "neutral":
        skip_event: dict[str, Any] = {
            "event": "low_confidence_skip",
            "time": _utc_iso(),
            "direction": direction,
            "confidence": round(confidence, 6),
            "threshold": config.confidence_threshold,
        }
        if config.multi_brain:
            skip_event["mode"] = "multi_brain"
            skip_event.update(consensus_extra)
            _record_brain_outcomes(proposals, direction, "consensus_skip", tracker)
        else:
            skip_event["out_risk"] = round(raw_output.get("out_risk", 0.0), 6)
            skip_event["out_vol"] = round(raw_output.get("out_vol", 0.0), 6)
            skip_event["runtime_ms"] = round(raw_output.get("runtime_ms", 0.0), 2)
            skip_event["backend"] = "unknown"
            brain_id = config.brain_entry.get("brain_id", "unknown")
            tracker.record_outcome(
                brain_id,
                {
                    "composite_score": round(0.3 + confidence * 0.3, 4),
                    "execution_outcome": "consensus_skip",
                },
            )
        print(json.dumps(skip_event, ensure_ascii=False), flush=True)
        _log_cycle_end(state.loop_iteration)
        return state, not config.once  # break if --once, else continue

    side = direction  # "long" or "short"

    # ── Stage shadow verification for next cycle's settlement ──
    if direction != "neutral" and mid_price is not None and mid_price > 0:
        all_supporting_v: list[str] = []
        all_opposing_v: list[str] = []
        if config.multi_brain and consensus_extra:
            all_supporting_v = consensus_extra.get("supporting_brains", [])
            all_opposing_v = consensus_extra.get("opposing_brains", [])
        state.shadow_verification_pending = {
            "direction": direction,
            "entry_price": mid_price,
            "consensus_score": confidence,
            "supporting_brains": all_supporting_v,
            "opposing_brains": all_opposing_v,
        }

    # ── Risk evaluation ──
    if control_snapshot is None:
        control_snapshot = _build_minimal_control_snapshot()
    risk_context = (
        _build_risk_context_from_broker(broker, config.symbol)
        if broker is not None
        else _build_risk_context(mt5, config.symbol)
    )
    risk_verdict = _evaluate_risk(
        risk_service,
        control_snapshot,
        risk_context,
        config.symbol,
        direction,
        confidence,
    )
    risk_event: dict[str, Any] = {
        "event": "risk_verdict",
        "time": _utc_iso(),
        "verdict": risk_verdict,
    }
    print(json.dumps(risk_event, ensure_ascii=False, default=str), flush=True)

    if risk_verdict.get("blocked") and not config.no_mt5:
        block_event: dict[str, Any] = {
            "event": "risk_blocked",
            "time": _utc_iso(),
            "side": side,
            "confidence": round(confidence, 6),
            "blocking_reasons": risk_verdict.get("blocking_reasons", []),
        }
        if config.multi_brain:
            block_event["mode"] = "multi_brain"
            block_event.update(consensus_extra)
            _record_brain_outcomes(proposals, direction, "risk_blocked", tracker)
        print(json.dumps(block_event, ensure_ascii=False, default=str), flush=True)
        _log_cycle_end(state.loop_iteration)
        return state, not config.once

    # ── Dispatch or shadow-verify ──
    if config.no_mt5:
        # ── Verification-only mode ──
        verify_event: dict[str, Any] = {
            "event": "inference_verified",
            "time": _utc_iso(),
            "side": side,
            "confidence": round(confidence, 6),
            "mode": "no_mt5_dry_run",
        }
        if config.multi_brain:
            verify_event.update(consensus_extra)
        print(json.dumps(verify_event, ensure_ascii=False, default=str), flush=True)

        # Persist shadow decision
        if config.multi_brain and proposals:
            try:
                from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
                from scripts.shadow_decision_recorder import record_shadow_from_proposals

                store = JsonlLedgerStore(config.base_dir)
                consensus_for_record = {
                    "aggregated_bias": direction,
                    "consensus_score": confidence,
                    "voter_count": consensus_extra.get("voter_count", 0),
                    "majority_ratio": consensus_extra.get("majority_ratio", 0.0),
                    "disagreement_score": consensus_extra.get("disagreement_score", 0.0),
                }
                record_shadow_from_proposals(
                    proposals=proposals,
                    consensus=consensus_for_record,
                    symbol=config.symbol,
                    store=store,
                    dispatch_status="shadow_verify",
                    feature_vector=feature_vector,
                    regime_info=regime_info,
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {"event": "record_error", "time": _utc_iso(), "error": str(exc)},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

            _record_brain_outcomes(proposals, direction, "shadow_verified", tracker)
        elif not config.multi_brain:
            brain_id = config.brain_entry.get("brain_id", "unknown")
            tracker.record_outcome(
                brain_id,
                {
                    "composite_score": round(0.5 + confidence * 0.35, 4),
                    "execution_outcome": "shadow_verified",
                },
            )
    else:
        # ── Live dispatch path ──
        from scripts.send_live_order import _validate_sl_tp, dispatch_live_open_order

        # Attempt price fetch with MT5 reconnection fallback
        try:
            if broker is not None:
                mid, bid, ask = broker.fetch_prices(config.symbol)
            else:
                mid, bid, ask = _mid_and_prices(mt5, config.symbol)
        except Exception as _price_exc:
            # MT5 connection may have gone stale during cooldown — attempt reconnect
            try:
                if not config.no_mt5:
                    mt5.initialize()
                if broker is not None:
                    mid, bid, ask = broker.fetch_prices(config.symbol)
                else:
                    mid, bid, ask = _mid_and_prices(mt5, config.symbol)
            except Exception:
                print(
                    json.dumps(
                        {
                            "event": "dispatch_price_error",
                            "time": _utc_iso(),
                            "error": str(_price_exc),
                            "recovered": False,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return state, True  # skip this cycle, retry next
        ref_long = ask
        ref_short = bid

        # Compute SL/TP (ATR-based, regime-adjusted)
        current_atr = (
            broker.fetch_current_atr(config.symbol)
            if broker is not None
            else _get_current_atr(mt5, config.symbol)
        )
        if current_atr <= 0:
            current_atr = 2.31  # training-set M5_ATR mean as fallback

        sl_mult = config.sl_atr_mult
        tp_mult = config.tp_atr_mult
        if regime_detector is not None and regime_info:
            sl_mult, tp_mult = regime_detector.get_adjusted_multipliers(
                regime_info, base_sl=config.sl_atr_mult, base_tp=config.tp_atr_mult
            )

        stop_loss, take_profit, ref_for_guard = compute_sl_tp_for_side(
            side,
            ref_long=ref_long,
            ref_short=ref_short,
            sl_atr_mult=sl_mult,
            tp_atr_mult=tp_mult,
            current_atr=current_atr,
        )

        _validate_sl_tp(
            side=side,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reference_price=ref_for_guard,
        )

        # ── Persist shadow decision (multi-brain) ──
        if config.multi_brain and proposals:
            try:
                from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
                from scripts.shadow_decision_recorder import record_shadow_from_proposals

                store = JsonlLedgerStore(config.base_dir)
                consensus_for_record = {
                    "aggregated_bias": direction,
                    "consensus_score": confidence,
                    "voter_count": consensus_extra.get("voter_count", 0),
                    "majority_ratio": consensus_extra.get("majority_ratio", 0.0),
                    "disagreement_score": consensus_extra.get("disagreement_score", 0.0),
                }
                record_shadow_from_proposals(
                    proposals=proposals,
                    consensus=consensus_for_record,
                    symbol=config.symbol,
                    store=store,
                    dispatch_status="live_dispatched",
                    feature_vector=feature_vector,
                    regime_info=regime_info,
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {"event": "record_error", "time": _utc_iso(), "error": str(exc)},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        # ── Determine per-brain magic for MT5 attribution ──
        dispatch_magic: int | None = None
        if config.multi_brain:
            supporting = consensus_extra.get("supporting_brains", [])
            for bid in supporting:
                for bi in brains:
                    if bi["brain_id"] == bid:
                        dispatch_magic = bi.get("magic")
                        break
                if dispatch_magic is not None:
                    break
        elif config.brain_entry:
            dispatch_magic = config.brain_entry.get("magic")

        # ── Persist shadow decision (single-brain) ──
        if not config.multi_brain and proposal is not None:
            try:
                from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
                from scripts.shadow_decision_recorder import record_shadow_from_proposals

                store = JsonlLedgerStore(config.base_dir)
                consensus_for_record = {
                    "aggregated_bias": direction,
                    "consensus_score": confidence,
                    "voter_count": 1,
                    "majority_ratio": 1.0,
                    "disagreement_score": 0.0,
                }
                record_shadow_from_proposals(
                    proposals=[proposal],
                    consensus=consensus_for_record,
                    symbol=config.symbol,
                    store=store,
                    dispatch_status="live_dispatched",
                    feature_vector=feature_vector,
                    regime_info=regime_info,
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {"event": "record_error", "time": _utc_iso(), "error": str(exc)},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        # ── Collect brain_ids for journal attribution ──
        dispatch_brain_ids: list[str] | None = None
        if config.multi_brain:
            supporting = consensus_extra.get("supporting_brains", [])
            opposing = consensus_extra.get("opposing_brains", [])
            dispatch_brain_ids = list(supporting) + list(opposing)
        elif config.brain_entry:
            dispatch_brain_ids = [config.brain_entry.get("brain_id", "unknown")]

        # ── Dispatch order ──
        out = dispatch_live_open_order(
            base_dir=config.base_dir,
            mt5_terminal_path=config.mt5_terminal_path,
            symbol=config.symbol,
            side=side,
            stop_loss=stop_loss,
            take_profit=take_profit,
            skip_price_guard=True,
            ignore_protection_flag=config.ignore_protection_flag,
            protection_flag_path=config.protection_flag_path,
            volume=dynamic_volume,
            magic=dispatch_magic,
            brain_ids=dispatch_brain_ids,
        )
        state.last_fire = now

        # ── Register position for dynamic exit management ──
        dispatch_ok = out.get("status", "") not in ("error", "rejected", "timeout")
        if dispatch_ok and config.exit_management_enabled and state.position_manager is not None:
            try:
                # Extract ticket from journal (written by dispatch_live_open_order)
                intent_id = out.get("intent_id", "")
                ticket: int | None = None
                if intent_id and journal_path and journal_path.exists():
                    for line in journal_path.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line or intent_id not in line:
                            continue
                        try:
                            rec = json.loads(line)
                            if rec.get("message_id") == intent_id and rec.get("action") == "open":
                                t = rec.get("position_ticket")
                                if t is not None and isinstance(t, int) and t > 0:
                                    ticket = t
                        except Exception:
                            pass
                        break
                if ticket is not None:
                    # Build entry consensus snapshot
                    entry_consensus: dict[str, Any] = {}
                    supporting: list[str] = []
                    if config.multi_brain:
                        entry_consensus = {
                            "aggregated_bias": consensus_extra.get("aggregated_bias", side),
                            "consensus_score": consensus_extra.get("consensus_score", confidence),
                            "voter_count": consensus_extra.get("voter_count", 0),
                            "majority_ratio": consensus_extra.get("majority_ratio", 0.0),
                            "disagreement_score": consensus_extra.get("disagreement_score", 0.0),
                        }
                        supporting = list(consensus_extra.get("supporting_brains", []))
                    else:
                        entry_consensus = {
                            "aggregated_bias": side,
                            "consensus_score": confidence,
                            "voter_count": 1,
                            "majority_ratio": 1.0,
                        }
                        supporting = dispatch_brain_ids or []

                    # ── Build per-model horizon map ──
                    # Maps brain_id → training horizon in M5 cycles.
                    # 12-bar models: 12 cycles (trained on survival-barrier contract)
                    # MTX models: 3 cycles (trained on 5 tick-bar forward return)
                    # OU model: 0 cycles (dynamic half-life, no fixed horizon)
                    _HORIZON_BY_TYPE: dict[str, int] = {
                        "onnx_v9": 12,
                        "xgboost_v4.5": 3,
                        "ou_params_v6": 0,
                        "transformer_v4.3": 3,
                        "lightgbm_v1": 12,
                        "xgboost_v9": 12,
                        "deepresmlp": 12,
                        "online_sgd": 12,
                    }
                    model_horizons: dict[str, int] = {}
                    if config.multi_brain:
                        for bi in brains:
                            bid = bi.get("brain_id", "")
                            btype = bi.get("brain_type", "")
                            model_horizons[bid] = _HORIZON_BY_TYPE.get(btype, 12)
                    elif dispatch_brain_ids:
                        btype = config.brain_type
                        for bid in dispatch_brain_ids:
                            model_horizons[bid] = _HORIZON_BY_TYPE.get(btype, 12)

                    state.position_manager.register_position(
                        ticket=ticket,
                        side=side,
                        entry_price=ref_for_guard,
                        volume=config.volume or 0.01,
                        initial_sl=stop_loss,
                        initial_tp=take_profit,
                        entry_atr=current_atr,
                        entry_cycle=state.loop_iteration,
                        entry_consensus=entry_consensus,
                        supporting_brain_ids=supporting,
                        model_horizons=model_horizons,
                        current_high=ref_for_guard,
                    )
                    print(
                        json.dumps(
                            {
                                "event": "position_registered_for_mgmt",
                                "time": _utc_iso(),
                                "ticket": ticket,
                                "side": side,
                                "entry_price": ref_for_guard,
                                "initial_sl": stop_loss,
                                "initial_tp": take_profit,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            except Exception as _reg_exc:
                print(
                    json.dumps(
                        {
                            "event": "position_register_error",
                            "time": _utc_iso(),
                            "error": str(_reg_exc),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        # ── Track new position for reconciliation ──
        if out.get("status", "") not in ("error", "rejected", "timeout"):
            try:
                intent_id = out.get("intent_id", "")
                if intent_id and journal_path.exists():
                    for line in journal_path.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line or intent_id not in line:
                            continue
                        try:
                            rec = json.loads(line)
                            if rec.get("message_id") == intent_id and rec.get("action") == "open":
                                ticket = rec.get("position_ticket")
                                if ticket is not None and isinstance(ticket, int) and ticket > 0:
                                    state.known_open_tickets[ticket] = rec
                        except Exception:
                            pass
                        break
            except Exception:
                pass

        dispatch_event = {
            "event": "intent_dispatched",
            "time": _utc_iso(),
            "mid": mid,
            "side": side,
            "confidence": round(confidence, 6),
            "reference_used": ref_for_guard,
            "sl": stop_loss,
            "tp": take_profit,
            "atr": round(current_atr, 6),
            "sl_atr_mult": sl_mult,
            "tp_atr_mult": tp_mult,
            "regime": regime_info,
            "magic": dispatch_magic,
            "dispatch": out,
        }
        if config.multi_brain:
            dispatch_event["mode"] = "multi_brain"
            dispatch_event.update(consensus_extra)
        else:
            dispatch_event["out_risk"] = round(raw_output.get("out_risk", 0.0), 6)
            dispatch_event["out_vol"] = round(raw_output.get("out_vol", 0.0), 6)
            dispatch_event["runtime_ms"] = round(raw_output.get("runtime_ms", 0.0), 2)
            dispatch_event["backend"] = "unknown"

        print(json.dumps(dispatch_event, ensure_ascii=False, default=str), flush=True)

        # Record dispatch outcome as "pending"
        if config.multi_brain:
            dispatch_ok = out.get("status", "") not in ("error", "rejected", "timeout")
            outcome = "pending" if dispatch_ok else "pending_rejected"
            _record_brain_outcomes(proposals, direction, outcome, tracker)
        else:
            dispatch_ok = out.get("status", "") not in ("error", "rejected", "timeout")
            outcome = "pending" if dispatch_ok else "pending_rejected"
            brain_id = config.brain_entry.get("brain_id", "unknown")
            tracker.record_outcome(
                brain_id,
                {
                    "composite_score": round(0.5 + confidence * 0.35, 4),
                    "execution_outcome": outcome,
                },
            )

    _log_cycle_end(state.loop_iteration)
    return state, not config.once
