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

# ── Dataclasses ──────────────────────────────────────────────────────────


@dataclass
class LiveCycleConfig:
    """Immutable per-run configuration derived from CLI args."""

    symbol: str = "XAUUSDc"
    base_dir: str = "data"
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


@dataclass
class LiveCycleState:
    """Mutable state threaded through each cycle iteration."""

    last_fire: float = 0.0
    cycle_count: int = 0
    loop_iteration: int = 0
    flag_notice: bool = False
    known_open_tickets: dict[int, dict[str, Any]] = field(default_factory=dict)


# ── Helpers ──────────────────────────────────────────────────────────────


def _utc_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def cooldown_blocks_fire(now: float, last_fire: float, cooldown_seconds: float) -> bool:
    return (now - last_fire) < cooldown_seconds


def _get_current_atr(mt5: Any, symbol: str, period: int = 14, count: int = 15) -> float:
    """Compute current M5 ATR(14) from MT5 rates."""
    try:
        import numpy as np

        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, count)
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
    except Exception:
        return 0.0


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


def _mid_and_prices(mt5: Any, symbol: str) -> tuple[float, float, float]:
    tick = mt5.symbol_info_tick(symbol)
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


def _reconcile_closed_positions(
    mt5: Any, symbol: str, journal_path: str, known_tickets: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Detect positions closed by SL/TP and return close journal entries."""
    closed_entries: list[dict[str, Any]] = []
    if mt5 is None:
        return closed_entries

    current_positions = mt5.positions_get(symbol=symbol)
    current_tickets = {p.ticket for p in (current_positions or [])}

    for ticket, open_entry in list(known_tickets.items()):
        if ticket in current_tickets:
            continue

        try:
            deals = mt5.history_deals_get(position=ticket)
        except Exception:
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

        close_reason_str = {4: "sl_hit", 5: "tp_hit"}.get(close_reason, "manual_close")

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
    """Query MT5 for risk metrics: positions, exposure, drawdown."""
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
        positions = mt5.positions_get(symbol=symbol) or []
        ctx["open_position_count"] = len(positions)

        per_sym: dict[str, int] = {}
        for pos in positions:
            sym = getattr(pos, "symbol", symbol)
            per_sym[sym] = per_sym.get(sym, 0) + 1
            vol = float(getattr(pos, "volume", 0))
            price = float(getattr(pos, "price_open", 0))
            ctx["current_notional_exposure"] += vol * price
        ctx["positions_per_symbol"] = per_sym

        acc = mt5.account_info()
        if acc is not None:
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

    import numpy as np

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
]


# ── Cycle execution ──────────────────────────────────────────────────────


def execute_live_cycle(
    config: LiveCycleConfig,
    state: LiveCycleState,
    *,
    mt5: Any,
    broker: Any = None,
    feature_service: Any,
    feature_computer: Any,
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

    # ── Reconcile closed positions ──
    if (
        not config.no_mt5
        and state.known_open_tickets
        and state.loop_iteration % config.reconciliation_interval == 0
    ):
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
                print(
                    json.dumps(
                        {
                            "event": "positions_closed",
                            "time": _utc_iso(),
                            "count": len(_closed),
                            "tickets": [e["position_ticket"] for e in _closed],
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
            pos_count = 0
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
            return state, True  # continue

    # ── Compute features ──
    if config.no_mt5:
        import numpy as np

        feature_vector: Any = np.zeros(40, dtype=np.float64)
    else:
        trigger = {"symbol": config.symbol, "venue": "MT5"}
        feature_vector = feature_service.build_feature_vector(trigger)

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

    # ── P&L ledger: settle pending signals, fetch mid price ──
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

    # ── Run inference ──
    raw_output: dict[str, Any] = {}
    proposal: Any = None
    proposals: list[Any] = []
    consensus_extra: dict[str, Any] = {}
    control_snapshot: Any = None

    if config.multi_brain:
        # ── Run all brain adapters ──
        raw_proposals: list[Any] = []
        for b_info in brains:
            try:
                raw = b_info["adapter"].infer(feature_vector)
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

        # ── Ensemble correlated brains → one vote per alpha ──
        grouped_ids: set[str] = set()
        for group in ENSEMBLE_GROUPS:
            members = [p for p in raw_proposals if p.brain_id in group["brain_ids"]]
            if members:
                grouped_ids.update(p.brain_id for p in members)
                ensemble_prop = _ensemble_proposals(group, members)
                if ensemble_prop is not None:
                    proposals.append(ensemble_prop)

        # Solo brains vote individually
        for p in raw_proposals:
            if p.brain_id not in grouped_ids:
                proposals.append(p)

        # Apply dynamic vote weights from tracked performance
        from core.brains.services.dynamic_brain_weighter import DynamicBrainWeighter

        weighter = DynamicBrainWeighter(tracker, pnl_store=pnl_ledger)
        weighter.apply_weights(proposals)

        # Build candidate via ParliamentService
        feature_snapshot = _build_feature_snapshot(config.symbol, feature_vector)
        control_snapshot = _build_minimal_control_snapshot()
        candidate = parliament.build_candidate(feature_snapshot, proposals, control_snapshot)
        direction = candidate.consensus.get("aggregated_bias", "neutral")
        confidence = candidate.consensus.get("consensus_score", 0.0)
        consensus_extra = {
            "voter_count": candidate.consensus.get("voter_count", 0),
            "majority_ratio": candidate.consensus.get("majority_ratio", 0.0),
            "disagreement_score": candidate.consensus.get("disagreement_score", 0.0),
            "supporting_brains": candidate.supporting_brains,
            "opposing_brains": candidate.opposing_brains,
            "is_feasible": candidate.execution_feasibility.get("is_feasible", True),
        }
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

    # ── Regime-aware direction bias ──
    # Penalise counter-trend signals; reward trend-aligned ones.
    if direction != "neutral" and regime_info:
        primary_regime = regime_info.get("primary_regime", "")
        regime_conf = float(regime_info.get("regime_confidence", 0.0))
        if primary_regime and regime_conf > 0.5:
            trend_direction = None
            if "trending_up" in primary_regime or "bullish" in primary_regime:
                trend_direction = "long"
            elif "trending_down" in primary_regime or "bearish" in primary_regime:
                trend_direction = "short"
            if trend_direction:
                if direction == trend_direction:
                    # Trend-aligned: modest boost proportional to regime confidence
                    confidence = min(0.99, confidence + 0.03 * regime_conf)
                else:
                    # Counter-trend: penalise proportional to regime confidence
                    confidence = max(0.30, confidence - 0.06 * regime_conf)

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
        return state, not config.once  # break if --once, else continue

    side = direction  # "long" or "short"

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

        if broker is not None:
            mid, bid, ask = broker.fetch_prices(config.symbol)
        else:
            mid, bid, ask = _mid_and_prices(mt5, config.symbol)
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
            volume=config.volume,
            magic=dispatch_magic,
        )
        state.last_fire = now

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

    return state, not config.once
