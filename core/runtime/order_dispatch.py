"""Order dispatch helpers — SL/TP computation, risk evaluation, brain outcomes.

Extracted from live_cycle.py. These functions are independent of
LiveCycleConfig/LiveCycleState and can be used by any dispatch path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.runtime.fault_handler import fail_open_guard


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


def _strategy_from_brain_ids(brain_ids: list[str]) -> str:
    """Map a list of brain_ids to the owning strategy line via BrainRegistry.

    Uses the ``contract_group`` field from brain JSON configs as the single
    source of truth — no substring heuristics.
    """
    if not brain_ids:
        return "barrier_12bar"
    from core.brains.brain_registry import BrainRegistry

    return BrainRegistry.instance().resolve_ids_to_group(brain_ids)


def _check_recent_sl_streak(
    journal_path: str,
    lookback_seconds: float = 300.0,
    threshold: int = 3,
    strategy_name: str | None = None,
) -> tuple[bool, int]:
    """Scan the journal for a streak of recent SL hits — bypasses reconciliation.

    When ``strategy_name`` is provided, only counts SL hits for that strategy.
    Otherwise counts across all strategies.

    This is a defense-in-depth check that runs right before dispatch.
    """
    import time as _time

    try:
        p = Path(journal_path)
        if not p.exists():
            return False, 0
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:  # BLE001:FOG
        with fail_open_guard("order_dispatch:_check_recent_sl_streak"):
            return False, 0
    now = _time.time()
    sl_streak = 0
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:  # BLE001:FOG
            with fail_open_guard("order_dispatch:_check_recent_sl_streak"):
                continue
        if rec.get("action") != "close":
            continue
        # Filter by strategy if requested
        if strategy_name:
            entry_brain_ids = rec.get("brain_ids", [])
            if _strategy_from_brain_ids(entry_brain_ids) != strategy_name:
                continue
        recorded = rec.get("recorded_at", "")
        try:
            if recorded.endswith("Z"):
                dt = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(recorded)
            if now - dt.timestamp() > lookback_seconds:
                break
        except Exception:  # BLE001:FOG
            with fail_open_guard("order_dispatch:_check_recent_sl_streak"):
                continue
        label = rec.get("label", "")
        if label in ("sl_hit_first", "loss"):
            sl_streak += 1
        elif label in ("tp_hit_first", "win"):
            break  # a win resets the streak

    blocked = sl_streak >= threshold
    return blocked, sl_streak


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


def _record_brain_outcomes(proposals, direction, execution_outcome, tracker, *, symbol: str = ""):
    """Record each brain's performance based on consensus agreement.

    Compatible with both BrainSignal (new — has .direction/.confidence)
    and BrainDecisionProposal (old — has .prediction dict).
    FIX-20260609-002: BrainSignal contract repair.
    FIX-20260617-002: Added per-brain dimensions (confidence, vote_match, direction)
    to differentiate records when multiple brains share the same trade outcome.
    """
    for p in proposals:
        # ── FIX-20260609-002: BrainSignal compatibility ──
        # BrainSignal has .direction + .confidence; BrainDecisionProposal
        # has .prediction dict.  Check hasattr to support both.
        if hasattr(p, "direction"):
            p_dir = p.direction
        else:
            p_dir = p.prediction.get("direction_bias", "neutral")
        if hasattr(p, "confidence"):
            p_conf = p.confidence
        else:
            p_conf = p.prediction.get("confidence", 0.0)

        matched = p_dir == direction if direction != "neutral" else p_dir == "neutral"
        composite = round(0.55 + p_conf * 0.3, 4) if matched else round(0.25 + p_conf * 0.2, 4)
        tracker.record_outcome(
            p.brain_id,
            {
                "composite_score": composite,
                "execution_outcome": execution_outcome,
                "dimensions": {
                    "brain_confidence": round(p_conf, 4),
                    "brain_direction": p_dir,
                    "consensus_direction": direction,
                    "vote_matched": matched,
                    "symbol": symbol,
                },
            },
        )


def _build_risk_context(mt5_worker: Any, symbol: str) -> dict[str, Any]:
    """Query MT5 for risk metrics via MT5Worker (solves T1-C1 daemon-thread anti-pattern).

    All MT5 calls are executed on the worker's dedicated thread with built-in
    timeout handling — no ad-hoc daemon threads.
    """
    ctx: dict[str, Any] = {
        "open_position_count": 0,
        "current_drawdown_pct": 0.0,
        "current_notional_exposure": 0.0,
        "positions_per_symbol": {},
    }
    if mt5_worker is None:
        ctx["_source"] = "no_mt5"
        return ctx

    try:
        positions = mt5_worker.positions_get(symbol=symbol, timeout=5.0)
        ctx["open_position_count"] = len(positions)

        per_sym: dict[str, int] = {}
        for pos in positions:
            sym = getattr(pos, "symbol", symbol)
            per_sym[sym] = per_sym.get(sym, 0) + 1
            vol = float(getattr(pos, "volume", 0))
            price = float(getattr(pos, "price_open", 0))
            ctx["current_notional_exposure"] += vol * price
        ctx["positions_per_symbol"] = per_sym

        acc = mt5_worker.account_info(timeout=5.0)
        if acc is not None:
            equity = float(getattr(acc, "equity", 0))
            balance = float(getattr(acc, "balance", 0))
            if balance > 0:
                ctx["current_drawdown_pct"] = round(max(0.0, (balance - equity) / balance) * 100, 2)
        ctx["_source"] = "mt5_live"
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("order_dispatch:_build_risk_context"):
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
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("order_dispatch:_build_risk_context_from_broker"):
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
    from core.contracts.enums import DecisionAction, DecisionSide, RiskDecisionStatus
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
        side=DecisionSide(direction.lower()),
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
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("order_dispatch:_evaluate_risk"):
            return {
                "status": "error",
                "risk_tier": "unknown",
                "blocking_reasons": [],
                "warning_reasons": [f"risk_eval_error: {exc}"],
                "blocked": False,
                "mode": "unknown",
            }
