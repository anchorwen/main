"""Live trading cycle execution — one iteration of the intent loop.

Extracted from scripts/live_intent_loop.py to keep the CLI script thin
(CLI + init + main loop shell) while housing the cycle logic in core/runtime/.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from core.brains.brain_registry import BrainRegistry
from core.execution.barrier_strategy import BarrierStrategy
from core.execution.execution_queue import ExecutionQueue
from core.execution.micro_strategy import MicroStrategy
from core.execution.portfolio_risk import PortfolioRiskController
from core.execution.regime_gate import RegimeGate
from core.execution.statarb_strategy import StatArbStrategy
from core.execution.strategy_budget import StrategyBudget
from core.execution.strategy_line import StrategyLineConfig
from core.execution.swing_strategy import SwingStrategy

# ── Strategy line imports ──
from core.execution.trail_stop_engine import TrailPolicy

# ── Extracted sub-modules (P2 refactor) ──
from core.market.mtf_price_service import MTFPriceService
from core.parliament.contract_groups import (
    ALL_GROUPS,
    ARB_GROUP,
    BARRIER_12BAR_META_GROUP,
    BARRIER_GROUP,
    MICRO_GROUP,
    MICRO_H1_GROUP,
    MICRO_H4_GROUP,
    MICRO_M15_GROUP,
    STATARB_M15_GROUP,
)
from core.runtime.market_ingress import (  # noqa: F401 — re-export
    _bootstrap_regime_gate,
    _feed_regime_gate_cycle,
    _get_current_atr,
    _mid_and_prices,
    _position_count,
)
from core.runtime.order_dispatch import (  # noqa: F401 — re-export
    _build_feature_snapshot,
    _build_minimal_control_snapshot,
    _build_risk_context,
    _build_risk_context_from_broker,
    _check_recent_sl_streak,
    _evaluate_risk,
    _record_brain_outcomes,
    _strategy_from_brain_ids,
    compute_sl_tp_for_side,
)
from core.runtime.signal_pipeline import (  # noqa: F401 — re-export
    ENSEMBLE_GROUPS,
    _ensemble_proposals,
)

# ── Dataclasses ──────────────────────────────────────────────────────────


# ── Training horizon (M5 cycles) ──
# Read from brain JSON ``training_horizon`` field — no hardcoded mapping.
# Fallback: 12 cycles for barrier, 3 for micro, 0 for statarb.
_DEFAULT_HORIZON = 12
META_FILTER_GATE_THRESHOLD = (
    0.40  # lowered from 0.60 — model trained on 1,217 samples, specificity 38%
)


@dataclass
class LiveCycleConfig:
    """Immutable per-run configuration derived from CLI args."""

    symbol: str = "XAUUSDc"
    base_dir: str = "data"
    calendar_path: str = "data/config/market_calendar.json"
    position_state_path: str = "state/active_position.json"
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
    exit_breakeven_threshold_atr: float = 1.5
    exit_brain_reeval_interval: int = 5
    exit_flip_threshold: float = 0.5
    exit_confidence_drop: float = 0.10
    exit_max_hold_cycles: int = 60
    exit_require_min_r: float = 0.3
    exit_min_step: float = 0.15

    # ── Multi-strategy mode ──
    multi_strategy_enabled: bool = True  # False → fallback to old CapitalAllocator
    strategy_stagger_seconds: float = 20.0  # delay between strategy dispatches
    portfolio_max_gross: float = 0.10
    portfolio_max_net: float = 0.05
    portfolio_max_same_dir: int = 2
    portfolio_netting_mode: str = "net_out"  # "net_out" | "allow_coexist"

    # ── live.yaml strategy_lines overrides ──
    strategy_configs: dict[str, Any] = field(default_factory=dict)

    # ── live.yaml regime_gate.regime_map ──
    regime_map: dict[str, dict[str, str]] | None = None

    # ── Vol-targeted position sizing ──
    risk_budget_usd: float = 10.0  # fixed USD risk per trade; 0 → use fixed volume
    equity_risk_pct: float = 0.0  # if >0, risk_budget = equity × equity_risk_pct (overrides fixed)
    min_lot: float = 0.01
    max_lot: float = 0.10
    lot_step: float = 0.01

    # ── Intraday drawdown kill ──
    intraday_drawdown_kill_enabled: bool = True
    intraday_drawdown_kill_pct: float = 0.02
    intraday_dd_force_close: bool = False
    intraday_dd_force_close_pct: float = 0.03  # force-close at 3% dd (vs 2% block)


@dataclass
class LiveCycleState:
    """Mutable state threaded through each cycle iteration."""

    last_fire: float = 0.0
    cycle_count: int = 0
    loop_iteration: int = 0
    flag_notice: bool = False
    known_open_tickets: dict[int, dict[str, Any]] = field(default_factory=dict)
    consecutive_sl_hits: dict[str, int] = field(default_factory=dict)
    sl_streak_blocked_until: dict[str, float] = field(default_factory=dict)
    sl_streak_blocked_all_until: float = 0.0  # blocks ALL strategies only
    _initial_reconciliation_done: bool = False
    # Rolling buffers for adaptive circuit breaker & confidence quantile
    _recent_atr_values: list[float] = field(default_factory=list)  # rolling 50 ATR samples
    _recent_mid_prices: list[float] = field(default_factory=list)  # rolling 50 mid prices (ER calc)
    _recent_consensus_scores: list[float] = field(default_factory=list)  # rolling 500 scores (P80)
    _reentry_states: dict[str, Any] = field(default_factory=dict)  # {strategy_name: ReentryState}
    position_manager: Any = None  # ActivePositionManager (set by caller)
    correlation_tracker: Any = None  # GroupCorrelationTracker (set by caller)
    shadow_verification_pending: dict[str, Any] | None = (
        None  # prev-cycle shadow decision for counterfactual settlement
    )
    regime_gate: Any = None  # RegimeGate (persisted across cycles for ADX accumulation)
    intraday_dd_kill: Any = None  # IntradayDrawdownKill (persisted across cycles)
    portfolio_risk_controller: Any = None  # PortfolioRiskController (persisted for VaR/correlation)
    signal_health_monitor: Any = None  # SignalHealthMonitor (persisted for drift detection)
    _last_health_report: dict[str, Any] | None = None  # latest health check report + actions
    _last_health_volume_mult: float | None = None  # volume multiplier from health actions (if any)
    _pending_budget_records: list[dict[str, Any]] = field(default_factory=list)
    _pending_sl_records: list[dict[str, Any]] = field(default_factory=list)  # {strategy, timestamp}
    _last_daily_ops_utc: float = 0.0  # Unix ts of last successful daily_ops run
    _tracker_reload_pending: bool = False  # set after daily_ops enriches tracker on disk
    exit_watchdog: Any = None  # ExitWatchdog instance (Pitfall 3 safeguard)
    limit_monitor: Any = None  # LimitOrderMonitor instance (Pitfall 1 safeguard)
    _cooldown_registry: Any = None  # CooldownRegistry (Cut 1: Absolute Refractory Period)
    _family_entry_tracker: Any = None  # FamilyEntryTracker (Cut 2: Cross-Strategy Spacing)
    _meta_filter_gate: Any = None  # MetaFilterGate (LightGBM 47-dim OU signal quality filter)
    _conformal_ou_gate: Any = None  # ConformalOUGate (physics-based OU signal quality gate)
    _mtf_price_service: Any = None  # MTFPriceService — M15 bar reconstruction from M5 tick history
    _last_ou_params: dict[str, float] | None = None  # {z_score, half_life, theta} for meta labeler
    # MIA close entries collected by _execute_management_phase, consumed by caller
    _pending_mia_closes: list[dict[str, Any]] = field(default_factory=list)

    # Circuit breaker: 3 consecutive degraded cycles → management-only mode
    _consecutive_degraded_cycles: int = 0
    _circuit_breaker_tripped: bool = False

    # Regime gate fail-closed: stale counter for fail-open → fail-closed migration
    _regime_gate_stale_counter: int = 0


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


# ── Daily ops auto-scheduler ────────────────────────────────────────────

DAILY_OPS_STATE_PATH = "data/state/daily_ops_state.json"


def _load_daily_ops_state(base_dir: str) -> float:
    """Restore last daily_ops timestamp from disk. Returns 0.0 if not found."""
    try:
        state_path = os.path.join(base_dir, "state", "daily_ops_state.json")
        if os.path.exists(state_path):
            with open(state_path) as f:
                data = json.load(f)
            return float(data.get("last_daily_ops_utc", 0.0))
    except Exception:
        pass
    return 0.0


def _save_daily_ops_state(base_dir: str, ts: float) -> None:
    """Persist last daily_ops timestamp to disk."""
    try:
        state_dir = os.path.join(base_dir, "state")
        os.makedirs(state_dir, exist_ok=True)
        state_path = os.path.join(state_dir, "daily_ops_state.json")
        with open(state_path, "w") as f:
            json.dump({"last_daily_ops_utc": ts}, f)
    except Exception:
        pass


def _run_scheduled_daily_ops(config: LiveCycleConfig, state: LiveCycleState) -> None:
    """Execute daily_ops pipeline synchronously within the current cycle."""
    print(
        json.dumps({"event": "daily_ops_scheduled", "time": _utc_iso()}, ensure_ascii=False),
        flush=True,
    )

    # ── Persist "decided to execute" BEFORE running to prevent edge reentry ──
    # If the process crashes mid-execution, the persisted timestamp ensures
    # the post-restart date-based check skips re-trigger for the same day.
    state._last_daily_ops_utc = datetime.now(UTC).timestamp()
    _save_daily_ops_state(config.base_dir, state._last_daily_ops_utc)

    try:
        from scripts.daily_ops import run_daily_ops

        result = run_daily_ops(
            base_dir=config.base_dir,
            skip_shadow=True,
            skip_recap=True,
            mt5_terminal_path=config.mt5_terminal_path,
        )
        state._tracker_reload_pending = True  # daily_ops wrote enriched tracker to disk

        # Persist the full report to disk (CLI uses --output, API path doesn't)
        _report_path = os.path.join(config.base_dir, "reports", "ops_logs", "p1_daily_run.log")
        try:
            os.makedirs(os.path.dirname(_report_path), exist_ok=True)
            with open(_report_path, "a", encoding="utf-8") as _f:
                _f.write(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n")
        except OSError as _exc:
            print(
                json.dumps(
                    {
                        "event": "daily_ops_report_write_failed",
                        "path": _report_path,
                        "error": str(_exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        print(
            json.dumps(
                {
                    "event": "daily_ops_complete",
                    "time": _utc_iso(),
                    "steps": len(result.get("steps", [])),
                    "errors": result.get("errors", 0),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        # ── Resource cleanup: force GC + compact feature store ──
        _cleanup_started = time.perf_counter()
        try:
            import gc

            gc.collect()
            # Compact local feature store to prevent unbounded JSONL growth
            try:
                from core.features.local_feature_store import LocalFeatureStore

                _store = LocalFeatureStore(base_dir=config.base_dir)
                _store.compact(retention_days=7)
            except Exception:
                pass
            _cleanup_ms = (time.perf_counter() - _cleanup_started) * 1000.0
            print(
                json.dumps(
                    {"event": "resource_cleanup_complete", "cleanup_ms": round(_cleanup_ms, 1)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as _cleanup_exc:
            print(
                json.dumps(
                    {"event": "resource_cleanup_failed", "error": str(_cleanup_exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )

        # ── Re-run governance after daily_ops refreshes PnL data ──
        try:
            from core.feedback.brain_pnl_ledger import BrainPnLStore
            from core.governance.governance_service import GovernanceService
            from scripts.training.governance_scheduler import run_governance_cycle

            _pnl_path = os.path.join(config.base_dir, "brain_pnl_ledger.json")
            _gov_path = os.path.join(config.base_dir, "governance_state.json")

            _pnl_store = BrainPnLStore.load(_pnl_path) if os.path.exists(_pnl_path) else None
            _governance = (
                GovernanceService.load(_gov_path)
                if os.path.exists(_gov_path)
                else GovernanceService()
            )

            if _pnl_store is not None:
                from core.feedback.brain_performance_tracker import BrainPerformanceTracker

                _tracker = BrainPerformanceTracker(window_size=100)
                _gov_report = run_governance_cycle(
                    _tracker, _governance, dry_run=False, pnl_store=_pnl_store
                )
                _governance.save(_gov_path)

                _applied = len(_gov_report.get("actions_applied", []))
                _flagged = len(_gov_report.get("actions_flagged", []))
                if _applied or _flagged:
                    print(
                        json.dumps(
                            {
                                "event": "daily_governance_cycle",
                                "time": _utc_iso(),
                                "actions_applied": _applied,
                                "actions_flagged": _flagged,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            else:
                print(
                    json.dumps(
                        {
                            "event": "daily_governance_skip",
                            "reason": "no_pnl_ledger",
                            "time": _utc_iso(),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        except Exception as _gov_exc:
            print(
                json.dumps(
                    {"event": "daily_governance_error", "time": _utc_iso(), "error": str(_gov_exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    except Exception as exc:
        print(
            json.dumps(
                {"event": "daily_ops_error", "time": _utc_iso(), "error": str(exc)},
                ensure_ascii=False,
            ),
            flush=True,
        )


def _check_pre_close(config: LiveCycleConfig, state: LiveCycleState) -> dict[str, Any]:
    """Check if we are approaching a market close and return action flags.

    Returns dict with keys: in_pre_close, minutes_to_close, no_new_positions,
    must_flatten, close_label.  A result of {} means no action needed.
    """
    from core.market.calendar import evaluate_pre_close, load_calendar

    cal = load_calendar(config.calendar_path)
    result = evaluate_pre_close(
        now_utc=datetime.now(UTC),
        symbol=config.symbol,
        config=cal,
    )
    if not result.get("in_pre_close"):
        return {}

    # If we must flatten and have open positions, close them all
    if (
        result.get("must_flatten")
        and state.position_manager is not None
        and state.position_manager.has_position()
    ):
        for pos in state.position_manager.get_all_positions():
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
                state=state,
                exit_watchdog=state.exit_watchdog,
            )

    return result


def cooldown_blocks_fire(now: float, last_fire: float, cooldown_seconds: float) -> bool:
    return (now - last_fire) < cooldown_seconds


# ── Exit Management Helpers ────────────────────────────────────────────────


def _dispatch_modify_trail(
    config: LiveCycleConfig,
    pos: Any,
    new_sl: float,
    new_tp: float,
    *,
    reason: str = "",
    brain_ids: list[str] | None = None,
    strategy_name: str = "",
    state: Any = None,
) -> None:
    """Issue a modify_sltp through the existing outbox pipeline."""
    from core.execution.live_order_sender import dispatch_live_order

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
    # Resolve magic from strategy name for correct journal attribution
    if strategy_name:
        try:
            from core.contracts.strategy_magic import STRATEGY_TO_MAGIC

            _strat_magic = STRATEGY_TO_MAGIC.get(strategy_name, 0)
            if _strat_magic:
                payload["magic"] = _strat_magic
        except Exception:
            pass
    if state is not None:
        _open_entry = state.known_open_tickets.get(pos.ticket, {})
        _open_msg_id = _open_entry.get("message_id", "")
        if _open_msg_id:
            payload["open_message_id"] = _open_msg_id

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
    state: Any = None,
    strategy_name: str = "",
    exit_confidence: float = 0.0,
    exit_watchdog: Any = None,
    mt5_worker: Any = None,
) -> bool:
    """Issue a close order for a managed position and record exit for re-entry guard.

    Returns True if the close was dispatched successfully, False otherwise.
    Callers MUST only clear the position from the position manager when True.

    When *exit_watchdog* is provided, wraps the dispatch with heartbeat-protected
    retry and escalation (Pitfall 3 safeguard).
    """
    from core.execution.live_order_sender import dispatch_live_order
    from core.execution.reentry_guard import ExitRecord, ensure_reentry_state

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
            # ── Diagnostic: log exit classification ──
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
            # ── Cut 1: Record exit to CooldownRegistry (Absolute Refractory Period) ──
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
                except Exception:
                    pass
        except Exception:
            pass

    # ── Pillar 4: Ghost-volume audit ──
    # If pos.volume < expected_remaining_volume and this isn't a legitimate
    # partial close, query MT5 for ground truth instead of blindly trusting
    # the system's volume (which may be stale after net_out / partial_tp).
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
            try:
                _mt5_positions = mt5_worker.positions_get(ticket=pos.ticket)
                if _mt5_positions and len(_mt5_positions) > 0:
                    _true_vol = float(_mt5_positions[0].volume)
            except Exception:
                pass
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
    # Carry forward brain attribution from the original open
    _close_brain_ids = getattr(pos, "supporting_brain_ids", None)
    if _close_brain_ids:
        payload["brain_ids"] = _close_brain_ids
    # Resolve magic from strategy name and carry open_message_id for journal linkage
    if strategy_name:
        try:
            from core.contracts.strategy_magic import STRATEGY_TO_MAGIC

            _strat_magic = STRATEGY_TO_MAGIC.get(strategy_name, 0)
            if _strat_magic:
                payload["magic"] = _strat_magic
        except Exception:
            pass
    if state is not None:
        _open_entry = state.known_open_tickets.get(pos.ticket, {})
        _open_msg_id = _open_entry.get("message_id", "")
        if _open_msg_id:
            payload["open_message_id"] = _open_msg_id

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
                    adapter_name="mt5",
                    extensions={"mt5_terminal_path": config.mt5_terminal_path},
                ),
                brain_ids=_close_brain_ids,
                pnl=pnl,
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
                # Store engine-calculated PnL for reconciliation fallback
                try:
                    _oe = state.known_open_tickets.get(pos.ticket, {})
                    if _oe:
                        _oe["_engine_close_pnl"] = pnl
                except Exception:
                    pass
        except Exception as _wd_exc:
            print(
                json.dumps(
                    {
                        "event": "exit_watchdog_exception",
                        "time": _utc_iso(),
                        "error": str(_wd_exc),
                        "reason": reason,
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
                adapter_name="mt5",
                extensions={"mt5_terminal_path": config.mt5_terminal_path},
            )
            _close_dispatched = True
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

    # ── After successful close dispatch: remove from known_open_tickets ──
    # Prevents reconciliation from finding the position gone and creating
    # a ghost close entry (unknown_close) with no PnL data.
    # Only remove tracking when the close was actually confirmed; otherwise
    # the position is still open in MT5 and reconciliation must find it.
    if _close_dispatched and state is not None and pos.ticket:
        state.known_open_tickets.pop(pos.ticket, None)
        # Record PnL for strategy budget tracking (managed exits bypass
        # reconciliation, so we must record here or budget stays stale).
        if pnl is not None and strategy_name:
            _pnl_pct = float(pnl) / 1000.0  # conservative: assume $1k account
            state._pending_budget_records.append(
                {
                    "strategy": strategy_name,
                    "pnl": _pnl_pct,
                    "is_win": pnl > 0,
                }
            )
            # Record SL-equivalent for graduated cooldown on losses
            if pnl < 0:
                state._pending_sl_records.append(
                    {
                        "strategy": strategy_name,
                        "timestamp": time.time(),
                    }
                )

    return _close_dispatched


def _execute_management_phase(
    config: LiveCycleConfig,
    state: LiveCycleState,
    *,
    mt5_worker: Any,
    broker: Any,
    brains: list[dict[str, Any]],
    parliament: Any,
    regime_detector: Any,
    tracker: Any,
    feature_service: Any,
    micro_feature_computer: Any,
    micro_feature_adapter: Any,
    daily_feature_provider: Any = None,
    ticket: int | None = None,
) -> Any:
    """Manage open position: trail stop, re-evaluate brains, check exits.

    If *ticket* is given, manages that specific position; otherwise manages
    the primary (backward compat).  Returns True if the position was closed.
    """
    pm = state.position_manager
    if pm is None or not pm.has_position(ticket=ticket):
        return False

    pos = pm.get_position(ticket=ticket)
    if pos is None:
        return False

    # Guard: if MT5 already closed this position (detected by reconciliation),
    # clear the stale position and skip management phase entirely.
    if state.known_open_tickets and pos.ticket not in state.known_open_tickets:
        pm.clear_position(ticket=pos.ticket)
        print(
            json.dumps(
                {
                    "event": "position_manager_stale_cleared",
                    "time": _utc_iso(),
                    "ticket": pos.ticket,
                    "reason": "not_in_known_open_tickets",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return False

    # Guard 2: verify position still exists in MT5 (catches closes between
    # reconciliation cycles — up to 10 min window). Single-ticket query is
    # lightweight and prevents position_not_found dispatches.
    if mt5_worker is not None and not config.no_mt5:
        try:
            _mt5_pos = mt5_worker.positions_get(ticket=pos.ticket)
            if not _mt5_pos:
                # ── Position closed in MT5 between reconciliation cycles ──
                # FIX-20260525-024: previously just cleared and returned, which:
                #   (a) left no close journal entry (ticket already gone from
                #       known_open_tickets → reconciliation never sees it)
                #   (b) left stale position_state file
                #   (c) left reentry guard with unknown_exit → permanent block
                # Now: collect close info, defer journal/state/reentry to caller.
                _mia_entry = _build_mia_close_entry(
                    pos,
                    state.known_open_tickets.get(pos.ticket, {}),
                )
                # Try to enrich with MT5 deal history (close_price, reason)
                try:
                    _deals = mt5_worker.history_deals_get(position=pos.ticket)
                    if _deals:
                        _enrich_mia_from_deals(_mia_entry, _deals)
                except Exception:
                    pass
                state._pending_mia_closes.append(_mia_entry)
                pm.clear_position(ticket=pos.ticket)
                state.known_open_tickets.pop(pos.ticket, None)
                # Save position state immediately — don't wait for periodic save
                try:
                    pm.save_state(config.position_state_path)
                except Exception:
                    pass
                print(
                    json.dumps(
                        {
                            "event": "position_manager_mt5_not_found",
                            "time": _utc_iso(),
                            "ticket": pos.ticket,
                            "reason": "position_closed_in_mt5",
                            "close_price": _mia_entry.get("detail", {}).get("close_price"),
                            "pnl": _mia_entry.get("pnl"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return False
        except Exception:
            # MT5 IPC failure — cannot confirm position existence.
            # Dispatching modify_sltp for a stale position creates rejection
            # noise (49/50 May 12 rejections were modify_sltp for closed
            # tickets).  Bail out; the next cycle will retry.
            print(
                json.dumps(
                    {
                        "event": "position_manager_mt5_unreachable",
                        "time": _utc_iso(),
                        "ticket": pos.ticket,
                        "reason": "mt5_ipc_exception_cannot_verify_position",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return False

    # Resolve strategy name early — needed for dispatch magic attribution
    _sname = state.known_open_tickets.get(pos.ticket, {}).get("strategy", "")
    if not _sname and pos.supporting_brain_ids:
        # Build brain_id → contract_group lookup from the brain registry
        _bid_to_cg: dict[str, str] = {}
        for _bi in brains:
            _bid = _bi.get("brain_id", "")
            _cg = _bi.get("contract_group", "")
            if _bid and _cg:
                _bid_to_cg[_bid] = _cg
        for _bid in pos.supporting_brain_ids:
            _cg = _bid_to_cg.get(_bid, "")
            if _cg:
                _sname = _cg  # contract_group name ≡ strategy name
                break
        if not _sname:
            # Legacy fallback — only for brain IDs without contract_group
            for _bid in pos.supporting_brain_ids:
                if _bid.lower().startswith("ou_"):
                    _sname = "statarb_dynamic"
                    break
            if not _sname:
                _sname = "barrier_12bar"

    # ── 1. Fetch current prices & ATR ──
    # FIX-20260522-014: a single price-fetch failure must not skip trail/
    # breakeven/exit management for this position.  Use the position's own
    # entry price as a fallback so the management phase can still evaluate
    # emergency exit conditions.  The warning event ensures the operator
    # sees the degradation.
    _price_degraded = False
    try:
        if broker is not None:
            mid, bid, ask = broker.fetch_prices(config.symbol)
        else:
            mid, bid, ask = _mid_and_prices(mt5_worker, config.symbol)
    except Exception:
        _price_degraded = True
        mid = float(getattr(pos, "entry_price", 0.0) or 0.0)
        bid = mid
        ask = mid
        print(
            json.dumps(
                {
                    "event": "management_price_fetch_failed",
                    "time": _utc_iso(),
                    "ticket": pos.ticket,
                    "fallback_entry_price": mid,
                    "action": "continuing_management_with_fallback",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if mid <= 0:
            return False  # truly hopeless — no price reference at all

    current_atr = (
        broker.fetch_current_atr(config.symbol)
        if broker is not None
        else _get_current_atr(mt5_worker, config.symbol)
    )
    if current_atr <= 0:
        current_atr = 2.31

    # ── 1b. Signal health monitor — feed data every cycle; run checks on interval or when position open ──
    if state.signal_health_monitor is None:
        from core.runtime.signal_health import SignalHealthMonitor

        state.signal_health_monitor = SignalHealthMonitor()
    _hmon = state.signal_health_monitor
    _hmon.feed_atr(current_atr)
    _hmon.mark_feature_received()
    if bid is not None and ask is not None and mid > 0:
        _spread_pct = (ask - bid) / mid
        _hmon.feed_spread(_spread_pct)
    # Run full checks: every cycle when position active, else every 20th
    _has_position = pm.has_position() if pos is not None else False
    if _has_position or state.loop_iteration % 20 == 0:
        from core.runtime.signal_health import run_signal_health_checks

        state._last_health_report = run_signal_health_checks(
            _hmon,
            current_atr=current_atr,
            current_spread_pct=(
                (ask - bid) / mid if (bid is not None and ask is not None and mid > 0) else None
            ),
            symbol=config.symbol,
        )
        # ── Apply autonomous health actions ──
        _health_actions = state._last_health_report.get("actions", [])
        for _action in _health_actions:
            _act_type = _action.get("action", "")
            if _act_type == "skip_new_positions":
                print(
                    json.dumps(
                        {
                            "event": "health_action_skip",
                            "time": _utc_iso(),
                            "reason": _action.get("reason", ""),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                _log_cycle_end(state.loop_iteration)
                return state, True  # skip cycle
            elif _act_type == "reduce_new_position_sizes":
                _mult = _action.get("multiplier", 1.0)
                if _mult < (state._last_health_volume_mult or 1.0):
                    state._last_health_volume_mult = _mult
                print(
                    json.dumps(
                        {
                            "event": "health_action_reduce_size",
                            "time": _utc_iso(),
                            "multiplier": _mult,
                            "reason": _action.get("reason", ""),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    # ── 2. Update regime detector ──
    regime_info: dict[str, Any] = {}
    if regime_detector is not None:
        try:
            regime_info = regime_detector.update(current_atr)
        except Exception:
            pass

    # ── 3. Update position tracking ──
    # Pillar 1: Fetch current M5 bar for OHLC-calibrated extreme tracking.
    # M5 covers the full inter-cycle window; graceful degradation on failure.
    _m5_high, _m5_low, _m5_spread = None, None, 0
    if mt5_worker is not None:
        try:
            _m5_rates = mt5_worker.copy_rates_from_pos(config.symbol, 5, 0, 1)  # TIMEFRAME_M5
            if _m5_rates is not None and len(_m5_rates) > 0:
                _m5_bar = _m5_rates[0]
                _m5_high = float(_m5_bar["high"])
                _m5_low = float(_m5_bar["low"])
                _m5_spread = int(_m5_bar.get("spread", 0))
        except Exception:
            pass
    pm.update_prices(
        mid,
        bid,
        ask,
        current_atr,
        regime_info,
        state.loop_iteration,
        m5_high=_m5_high,
        m5_low=_m5_low,
        m5_spread_points=_m5_spread,
    )

    # ── 4-5.2: Trail SL, breakeven, trail TP — computed separately,
    # dispatched as a SINGLE modify_sltp to prevent MT5 rejecting
    # back-to-back requests for the same ticket (retcode 10006). ──
    _final_sl = pos.current_sl
    _final_tp = pos.current_tp
    _reasons: list[str] = []
    _old_sl = pos.current_sl
    _old_tp = pos.current_tp
    _trail_sl: float | None = None
    _be_triggered = False
    _be_dispatched = False

    # Layer 1: Chandelier trailing stop
    # FIX-20260524-002: respect min_hold_cycles so trailing stop does not
    # tighten the hard SL before the position has had reasonable time to
    # develop.  Previously Layer 1 ran from cycle 1 with no protection,
    # causing exits at 0.5-1.0R instead of the designed 2.0R SL for
    # strategies whose breakeven threshold was not reached in time.
    #
    # FIX-20260527-004: cold_explore bypass — forced exploration trades must
    # run to hard SL or hard TP to collect uncensored labels for ConformalOU
    # online calibration.  Trailing stops would produce truncated data.
    if not getattr(pos, "cold_explore", False):
        _trail_sl = pm.compute_trail_stop(current_atr, ticket=pos.ticket)
        if _trail_sl is not None and abs(_trail_sl - pos.current_sl) >= config.exit_min_step:
            if pos.cycles_held >= pm.min_hold_cycles:
                _reasons.append("trail")
                _final_sl = _trail_sl
    else:
        _trail_sl = None

    # Breakeven check — only fires once per position
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

    # Dynamic trailing TP — tightens when ATR contracts
    _trail_tp = pm.compute_trail_tp(current_atr, ticket=pos.ticket)
    if _trail_tp is not None and abs(_trail_tp - pos.current_tp) >= config.exit_min_step:
        _reasons.append("tp")
        _final_tp = _trail_tp

    # ── Diagnostic: log trail/breakeven decision details every cycle ──
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
    # ── Single dispatch (prevents MT5 retcode 10006 rejections) ──
    _sl_changed = abs(_final_sl - pos.current_sl) >= config.exit_min_step
    _tp_changed = abs(_final_tp - pos.current_tp) >= config.exit_min_step
    if _reasons:
        _dispatch_modify_trail(
            config,
            pos,
            _final_sl,
            _final_tp,
            reason="+".join(_reasons),
            brain_ids=pos.supporting_brain_ids,
            strategy_name=_sname,
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
    if _be_triggered:
        print(
            json.dumps(
                {
                    "event": "breakeven_triggered",
                    "time": _utc_iso(),
                    "ticket": pos.ticket,
                    "side": pos.side,
                    "entry_price": pos.entry_price,
                    "mid": round(mid, 3),
                    "dispatched": _be_dispatched,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    # ── 5.5 Partial take-profit ──
    if not pos.partial_tp_triggered and pos.partial_tp_r > 0:
        should_ptp, ptp_close_vol, ptp_remain_vol = pm.should_partial_tp(mid, ticket=pos.ticket)
        if should_ptp:
            _close_payload: dict[str, Any] = {
                "action": "close",
                "side": pos.side,
                "position_ticket": pos.ticket,
                "volume": ptp_close_vol,
                "comment": f"partial_tp_{pos.partial_tp_r}R",
            }
            _ptp_brain_ids = getattr(pos, "supporting_brain_ids", None)
            if _ptp_brain_ids:
                _close_payload["brain_ids"] = _ptp_brain_ids
            # Resolve magic from strategy name and carry open_message_id
            if _sname:
                try:
                    from core.contracts.strategy_magic import STRATEGY_TO_MAGIC

                    _strat_magic = STRATEGY_TO_MAGIC.get(_sname, 0)
                    if _strat_magic:
                        _close_payload["magic"] = _strat_magic
                except Exception:
                    pass
            _open_entry = state.known_open_tickets.get(pos.ticket, {})
            _open_msg_id = _open_entry.get("message_id", "")
            if _open_msg_id:
                _close_payload["open_message_id"] = _open_msg_id
            _ptp_dispatched = False
            _ptp_watchdog = getattr(state, "exit_watchdog", None)
            try:
                if _ptp_watchdog is not None:
                    from core.execution.live_order_sender import dispatch_live_order

                    def _ptp_dispatch_fn(p: dict) -> dict:
                        return dispatch_live_order(
                            base_dir=config.base_dir,
                            broker=None,
                            symbol=config.symbol,
                            execution_payload=p,
                            skip_price_guard=True,
                            ignore_protection_flag=config.ignore_protection_flag,
                            protection_flag_path=config.protection_flag_path,
                            adapter_name="mt5",
                            extensions={"mt5_terminal_path": config.mt5_terminal_path},
                        )

                    _ptp_pnl = None
                    if mid is not None and hasattr(pos, "entry_price") and pos.entry_price:
                        _ptp_pnl = (
                            round((mid - pos.entry_price) * ptp_close_vol, 2)
                            if pos.side == "long"
                            else round((pos.entry_price - mid) * ptp_close_vol, 2)
                        )
                    _ptp_result = _ptp_watchdog.execute_exit(
                        position_ticket=pos.ticket,
                        volume=ptp_close_vol,
                        side=pos.side,
                        reason=f"partial_tp_{pos.partial_tp_r}R",
                        magic=_close_payload.get("magic", 0),
                        dispatch_fn=_ptp_dispatch_fn,
                        brain_ids=_ptp_brain_ids,
                        pnl=_ptp_pnl,
                    )
                    _ptp_dispatched = _ptp_result.success
                    if not _ptp_result.success:
                        print(
                            json.dumps(
                                {
                                    "event": "partial_tp_watchdog_failed",
                                    "time": _utc_iso(),
                                    "ticket": pos.ticket,
                                    "final_status": _ptp_result.final_status,
                                    "alerts": _ptp_result.alerts,
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                else:
                    from core.execution.live_order_sender import dispatch_live_order

                    dispatch_live_order(
                        base_dir=config.base_dir,
                        broker=None,
                        symbol=config.symbol,
                        execution_payload=_close_payload,
                        skip_price_guard=True,
                        ignore_protection_flag=config.ignore_protection_flag,
                        protection_flag_path=config.protection_flag_path,
                        adapter_name="mt5",
                        extensions={"mt5_terminal_path": config.mt5_terminal_path},
                    )
                    _ptp_dispatched = True
            except Exception as _ptp_exc:
                print(
                    json.dumps(
                        {
                            "event": "partial_tp_dispatch_error",
                            "time": _utc_iso(),
                            "ticket": pos.ticket,
                            "error": str(_ptp_exc),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

            if not _ptp_dispatched:
                # Leave position manager state unchanged — retry next cycle
                pass
            else:
                pos.partial_tp_triggered = True
                # Move remaining SL to breakeven
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
                        reason="partial_tp_be",
                        brain_ids=pos.supporting_brain_ids,
                        strategy_name=_sname,
                        state=state,
                    )
                    pos.current_sl = breakeven_sl

                pos.volume = ptp_remain_vol
                pos.expected_remaining_volume = ptp_remain_vol
                print(
                    json.dumps(
                        {
                            "event": "partial_tp_executed",
                            "time": _utc_iso(),
                            "ticket": pos.ticket,
                            "r": round(pm._compute_r_multiple(mid, ticket=pos.ticket), 2),
                            "closed_volume": ptp_close_vol,
                            "remaining_volume": ptp_remain_vol,
                            "sl_moved_to_be": improve,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    # ── 6. R-milestone checks ──
    milestone = pm.check_r_milestones(mid, ticket=pos.ticket)
    if milestone:
        print(
            json.dumps(
                {
                    "event": "r_milestone_hit",
                    "time": _utc_iso(),
                    "ticket": pos.ticket,
                    "milestone": milestone,
                    "r": round(pm._compute_r_multiple(mid, ticket=pos.ticket), 2),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    # ── 6.5. Per-strategy exit config lookup ──
    _scfg = config.strategy_configs.get(_sname, {})
    _exit_cfg = _scfg.get("exit", {})
    _flip_enabled = _exit_cfg.get("flip_exit_enabled", True)
    _zscore_enabled = _exit_cfg.get("zscore_exit_enabled", False)
    _exit_time_cycles = _exit_cfg.get("time_exit_cycles", None)
    _confidence_decay_enabled = _exit_cfg.get("confidence_decay_exit", True)
    _hesitation_cycles = int(_exit_cfg.get("hesitation_cycles", 0) or 0)
    _exit_min_r = _exit_cfg.get("min_r_for_hold", config.exit_require_min_r)
    _exit_confidence = float(
        (pos.entry_consensus or {}).get(
            "consensus_score", (pos.entry_consensus or {}).get("majority_ratio", 0.5)
        )
    )

    # Apply per-strategy exit toggles to position manager
    pm.confidence_decay_enabled = _confidence_decay_enabled
    pm.hesitation_cycles = _hesitation_cycles
    _flip_threshold = _exit_cfg.get("flip_threshold")
    if _flip_threshold is not None:
        pm.flip_exit_threshold = float(_flip_threshold)

    # ── 6.6 Recovery grace period ──
    # After a restart, the position is recovered from MT5 but feature buffers
    # (Transformer window, RollingNormalizer, ADX) are cold-started.  Skip
    # brain-flip, Meta Exit, and time-decay layers for N cycles to avoid
    # premature exits caused by transient feature instability.
    # Layer 1 (trailing stop + hard SL) still runs normally.
    if pm.is_in_grace_period():
        _gr_r = round(pm._compute_r_multiple(mid, ticket=pos.ticket), 2) if mid is not None else 0.0
        # Emergency exit: if position is deeply underwater, close immediately
        # even during grace period.  Feature buffers may be cold, but a severe
        # adverse move is a high-confidence signal that transcends noise.
        _emergency_r = float(
            _exit_cfg.get("grace_period_emergency_r", -1.0)
        )  # ↓ -1.5→-1.0: faster exit
        if _emergency_r < 0 and _gr_r < _emergency_r:
            _dispatched = _dispatch_managed_close(
                config,
                pos,
                reason="grace_period_emergency",
                mid=mid,
                state=state,
                strategy_name=_sname,
                exit_confidence=_exit_confidence,
                exit_watchdog=state.exit_watchdog,
                mt5_worker=mt5_worker,
            )
            print(
                json.dumps(
                    {
                        "event": "grace_period_emergency_exit",
                        "time": _utc_iso(),
                        "ticket": pos.ticket,
                        "r": _gr_r,
                        "emergency_threshold": _emergency_r,
                        "dispatched": _dispatched,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if _dispatched:
                pm.clear_position(ticket=pos.ticket)
            return True

        print(
            json.dumps(
                {
                    "event": "grace_period_skip",
                    "time": _utc_iso(),
                    "ticket": pos.ticket,
                    "recovery_cycle": pm._recovery_cycle,
                    "cycles_held": pos.cycles_held,
                    "r": _gr_r,
                    "skipped_layers": ["brain_flip", "meta_exit", "time_decay"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return False

    # ── 7. Layer 2: Brain ensemble re-evaluation ──
    current_consensus: dict[str, Any] = {}
    current_supporting: list[str] = []
    meta_consensus: dict[str, Any] = {}  # global consensus for Meta Exit (Layer 2.5)
    meta_supporting: list[str] = []
    if config.multi_brain and pm.should_reeval_brains(state.loop_iteration):
        pm.mark_brains_reevaluated(state.loop_iteration)

        # Re-run all brain inference
        # Compute fresh multi-TF sequences for position re-evaluation
        mgmt_sequences: dict[str, np.ndarray] = {}
        if micro_feature_computer is not None:
            try:
                mgmt_sequences = micro_feature_computer.compute_all_sequences(32)
            except Exception as _seq_exc:
                print(
                    json.dumps(
                        {
                            "event": "sequence_compute_error",
                            "time": _utc_iso(),
                            "error": str(_seq_exc),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        raw_proposals: list[Any] = []
        for b_info in brains:
            schema_id = b_info.get("feature_schema_id", "")
            btype = b_info.get("brain_type", "")
            try:
                if btype == "ou_params_v6":
                    fv: Any = np.array([mid], dtype=np.float32)
                    raw = b_info["adapter"].infer(fv)
                    prop = b_info["adapter"].get_signal(raw)
                elif "microstructure" in str(schema_id):
                    hmre_layer = b_info.get("hmre_layer", "M5")
                    seq = mgmt_sequences.get(hmre_layer)
                    if seq is not None and seq.ndim == 2 and seq.shape[0] >= 32:
                        prop = b_info["adapter"].run(None, seq)
                    elif micro_feature_computer is not None:
                        mf = micro_feature_computer.compute_all()
                        fv = micro_feature_adapter.build_model_input(mf).ravel()
                        raw = b_info["adapter"].infer(fv)
                        prop = b_info["adapter"].get_signal(raw)
                    else:
                        prop = None
                elif schema_id in ("daily_swing_24", "swing_24"):
                    # Swing brains use D1 daily features
                    if daily_feature_provider is not None:
                        try:
                            fv = daily_feature_provider.get_latest()
                            raw = b_info["adapter"].infer(fv)
                            prop = b_info["adapter"].get_signal(raw)
                        except Exception:
                            prop = None
                    else:
                        prop = None
                else:
                    fv = feature_service.build_feature_vector(
                        {"symbol": config.symbol, "venue": "MT5"}
                    )
                    raw = b_info["adapter"].infer(fv)
                    prop = b_info["adapter"].get_signal(raw)

                if prop is not None:
                    # BrainSignal always carries brain_id from the adapter.
                    # No stamping needed — frozen objects reject mutation.
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

                # ── Layer 2 consensus: strategy-line-filtered ──
                # Brain flip exit must only use the entry's strategy-line
                # brains, NOT the global 17-brain consensus.  Global drift
                # is fed to Meta Exit (Layer 2.5) as a separate factor.
                _entry_group_signal = group_signals.get(_sname) if _sname else None
                _l2_direction: str
                if _entry_group_signal is not None and _entry_group_signal.direction != "neutral":
                    _l2_direction = _entry_group_signal.direction
                    _l2_confidence = _entry_group_signal.confidence
                    _l2_supporting = _entry_group_signal.brain_ids
                    _l2_total = _entry_group_signal.total_count
                elif _entry_group_signal is not None:
                    # Direction is neutral — use actual confidence from the
                    # group signal (not hardcoded 0.0) so confidence-drop
                    # exits are proportional.  Same pattern as union-mode
                    # all-neutral fix in contract_groups.py.
                    _l2_direction = "neutral"
                    _l2_confidence = _entry_group_signal.confidence
                    _l2_supporting = []
                    _l2_total = _entry_group_signal.total_count
                else:
                    _l2_direction = "neutral"
                    _l2_confidence = 0.0
                    _l2_supporting = []
                    _l2_total = 0

                current_consensus = {
                    "aggregated_bias": _l2_direction,
                    "consensus_score": _l2_confidence,
                    "voter_count": _l2_total,
                    "majority_ratio": _l2_confidence,
                    "supporting_brains": _l2_supporting,
                    "opposing_brains": [],
                }
                current_supporting = _l2_supporting

                # ── Global consensus (for Meta Exit cross-strategy drift) ──
                # Wave C: Hierarchical consensus — positions at larger
                # timeframes only listen to signals from >= same-TF groups.
                # An H1 freighter doesn't tack on an M5 lake-breeze shift.
                _pos_tf_mult = int(
                    (config.strategy_configs.get(_sname, {}) or {}).get("_tf_mult", 1) or 1
                )
                _global_direction = allocation.direction if allocation.should_trade else "neutral"
                _global_supporting: list[str] = []
                _global_total = 0
                for _gname, gs in group_signals.items():
                    if gs is None:
                        continue
                    _g_tf_mult = int(
                        (config.strategy_configs.get(_gname, {}) or {}).get("_tf_mult", 1) or 1
                    )
                    if _g_tf_mult < _pos_tf_mult:
                        continue  # skip smaller-TF groups
                    _global_total += gs.total_count
                    if gs.direction == _global_direction:
                        _global_supporting.extend(gs.brain_ids)

                meta_consensus = {
                    "aggregated_bias": _global_direction,
                    "consensus_score": allocation.confidence,
                    "voter_count": _global_total,
                    "majority_ratio": allocation.confidence,
                    "supporting_brains": list(set(_global_supporting)),
                    "allocation": {
                        "agreement_level": allocation.agreement_level,
                        "active_groups": allocation.active_groups,
                        "dissenting_groups": allocation.dissenting_groups,
                        "reason": allocation.reason,
                    },
                }
                meta_supporting = list(set(_global_supporting))

                # ── Opt3: Bleed stop (v3.2, hardened v3.3) ──
                # Exit if N consecutive bars have negative PnL, where N scales
                # with the strategy's horizon.  barrier_12bar (60 min) → 4 bars,
                # micro_3bar (15 min) → 3 bars.  min_hold_cycles prevents the
                # bleed_stop from firing before the position has had reasonable
                # time to develop (FIX-20260522-027).
                #
                # FIX-20260525-020: Mean-reversion (statarb/OU) strategies are
                # EXEMPT from bleed_stop.  They enter at trend extremes — price
                # continuing 3-5 bars in the same direction is normal "rubber band
                # stretching," not thesis failure.  Killing during the stretch is
                # a category error (trend exit applied to mean-reversion position).
                _sname_lower = (_sname or "").lower()
                if "statarb" not in _sname_lower and mid is not None and mid > 0:
                    _strat_cfg = (config.strategy_configs or {}).get(_sname, {}) or {}
                    _horizon = int(
                        _strat_cfg.get("horizon_cycles", 0) or _strat_cfg.get("horizon", 0) or 0
                    )
                    _bleed_bars = max(3, _horizon // 3) if _horizon > 0 else 3
                    _min_hold = max(2, _bleed_bars)
                    if getattr(pos, "cycles_held", 0) < _min_hold:
                        _should_bleed, _bleed_reason = False, ""
                    else:
                        _r_now = pm._compute_r_multiple(mid, ticket=pos.ticket)
                        _should_bleed, _bleed_reason = pm.should_exit_bleed(
                            pos, _r_now, bleed_bars=_bleed_bars
                        )
                    if _should_bleed:
                        _dispatched = _dispatch_managed_close(
                            config,
                            pos,
                            reason=_bleed_reason,
                            mid=mid,
                            state=state,
                            strategy_name=_sname,
                            exit_confidence=_exit_confidence,
                            exit_watchdog=state.exit_watchdog,
                            mt5_worker=mt5_worker,
                        )
                        print(
                            json.dumps(
                                {
                                    "event": "bleed_stop_triggered",
                                    "time": _utc_iso(),
                                    "ticket": pos.ticket,
                                    "r_now": round(_r_now, 3),
                                    "reason": _bleed_reason,
                                    "bleed_bars": _bleed_bars,
                                    "cycles_held": getattr(pos, "cycles_held", 0),
                                    "min_hold_cycles": _min_hold,
                                    "horizon_cycles": _horizon,
                                    "dispatched": _dispatched,
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        if _dispatched:
                            pm.clear_position(ticket=pos.ticket)
                        return True

                # ── OU mean-reversion exit (ARB brain) ──
                # Only applies to positions opened by the StatArb strategy
                # (positions whose supporting brains include the OU brain).
                # Gated by per-strategy exit.zscore_exit_enabled config.
                if (
                    _zscore_enabled
                    and pos.supporting_brain_ids
                    and any(
                        bid.startswith("OU_") or bid.lower().startswith("ou_")
                        for bid in pos.supporting_brain_ids
                    )
                ):
                    for b_info in brains:
                        if b_info.get("brain_type") == "ou_params_v6":
                            try:
                                raw_ou = b_info["adapter"].infer(np.array([mid], dtype=np.float32))
                                ou_z = float(raw_ou.get("z_score", 0.0))
                                should_ou_exit, ou_reason = pm.should_exit_ou_based(
                                    ou_z, ticket=pos.ticket
                                )
                                if should_ou_exit:
                                    _dispatched = _dispatch_managed_close(
                                        config,
                                        pos,
                                        reason=ou_reason,
                                        mid=mid,
                                        state=state,
                                        strategy_name=_sname,
                                        exit_confidence=_exit_confidence,
                                        exit_watchdog=state.exit_watchdog,
                                        mt5_worker=mt5_worker,
                                    )
                                    print(
                                        json.dumps(
                                            {
                                                "event": "ou_exit_triggered",
                                                "time": _utc_iso(),
                                                "ticket": pos.ticket,
                                                "z_score": round(ou_z, 3),
                                                "reason": ou_reason,
                                                "dispatched": _dispatched,
                                            },
                                            ensure_ascii=False,
                                        ),
                                        flush=True,
                                    )
                                    if _dispatched:
                                        pm.clear_position(ticket=pos.ticket)
                                    return True
                            except Exception:
                                pass
                            break  # only one OU brain

                should_exit = False
                exit_reason = ""
                if _flip_enabled:
                    should_exit, exit_reason = pm.evaluate_brain_exit(
                        current_consensus, current_supporting, mid=mid, ticket=pos.ticket
                    )
                if should_exit:
                    _bf_confidence = float(
                        current_consensus.get("consensus_score", _exit_confidence)
                    )
                    _dispatched = _dispatch_managed_close(
                        config,
                        pos,
                        reason=exit_reason,
                        mid=mid,
                        state=state,
                        strategy_name=_sname,
                        exit_confidence=_bf_confidence,
                        exit_watchdog=state.exit_watchdog,
                        mt5_worker=mt5_worker,
                    )
                    print(
                        json.dumps(
                            {
                                "event": "brain_exit_triggered",
                                "time": _utc_iso(),
                                "ticket": pos.ticket,
                                "reason": exit_reason,
                                "dispatched": _dispatched,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    if _dispatched:
                        pm.clear_position(ticket=pos.ticket)
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
    _skip_meta = pm._is_protected_period(ticket=pos.ticket) and not pm._toxicity_veto(
        mid if mid is not None else 0.0, ticket=pos.ticket
    )
    if not _skip_meta and pm.meta_exit_engine is not None:
        try:
            # Meta Exit uses GLOBAL cross-strategy consensus (not filtered to
            # entry's strategy line) to detect macro-level drift and divergence.
            _meta_cons = meta_consensus if meta_consensus else pos.entry_consensus
            _meta_sup = meta_supporting if meta_supporting else pos.supporting_brain_ids
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
                current_consensus=_meta_cons,
                current_supporting=_meta_sup,
                ticket=pos.ticket,
            )
            if should_meta_exit:
                _dispatched = _dispatch_managed_close(
                    config,
                    pos,
                    reason=meta_reason,
                    mid=mid,
                    state=state,
                    strategy_name=_sname,
                    exit_confidence=_exit_confidence,
                    exit_watchdog=state.exit_watchdog,
                    mt5_worker=mt5_worker,
                )
                print(
                    json.dumps(
                        {
                            "event": "meta_exit_triggered",
                            "time": _utc_iso(),
                            "ticket": pos.ticket,
                            "reason": meta_reason,
                            "dispatched": _dispatched,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if _dispatched:
                    pm.clear_position(ticket=pos.ticket)
                return True
        except Exception as exc:
            print(
                json.dumps(
                    {"event": "meta_exit_error", "time": _utc_iso(), "error": str(exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )

    # ── 7.8 Layer 2.8: Hesitation exit (no breakeven within N cycles) ──
    _skip_hesitation = pm._is_protected_period(ticket=pos.ticket) and not pm._toxicity_veto(
        mid if mid is not None else 0.0, ticket=pos.ticket
    )
    if not _skip_hesitation:
        should_hesitate, hesitate_reason = pm.should_exit_hesitation(
            mid if mid is not None else 0.0, ticket=pos.ticket
        )
        if should_hesitate:
            _dispatched = _dispatch_managed_close(
                config,
                pos,
                reason=hesitate_reason,
                mid=mid,
                state=state,
                strategy_name=_sname,
                exit_confidence=_exit_confidence,
                exit_watchdog=state.exit_watchdog,
                mt5_worker=mt5_worker,
            )
            print(
                json.dumps(
                    {
                        "event": "hesitation_exit_triggered",
                        "time": _utc_iso(),
                        "ticket": pos.ticket,
                        "cycles_held": pos.cycles_held,
                        "hesitation_cycles": pm.hesitation_cycles,
                        "dispatched": _dispatched,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if _dispatched:
                pm.clear_position(ticket=pos.ticket)
            return True

    # ── 8. Layer 3: Time-based exit ──
    _skip_time = pm._is_protected_period(ticket=pos.ticket) and not pm._toxicity_veto(
        mid if mid is not None else 0.0, ticket=pos.ticket
    )
    if not _skip_time:
        _tz_override = int(_exit_time_cycles) if _exit_time_cycles is not None else None
        should_time_exit, exit_reason = pm.should_exit_time_based(
            mid,
            override_horizon=_tz_override,
            override_min_r=_exit_min_r,
            ticket=pos.ticket,
        )
        if should_time_exit:
            _dispatched = _dispatch_managed_close(
                config,
                pos,
                reason=exit_reason,
                mid=mid,
                state=state,
                strategy_name=_sname,
                exit_confidence=_exit_confidence,
                exit_watchdog=state.exit_watchdog,
                mt5_worker=mt5_worker,
            )
            print(
                json.dumps(
                    {
                        "event": "time_exit_triggered",
                        "time": _utc_iso(),
                        "ticket": pos.ticket,
                        "cycles_held": pos.cycles_held,
                        "r": round(pm._compute_r_multiple(mid, ticket=pos.ticket), 2),
                        "dispatched": _dispatched,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if _dispatched:
                pm.clear_position(ticket=pos.ticket)
            return True

    return False


# ── Exit config keys expected across all strategy definitions ──
_EXPECTED_EXIT_KEYS = {
    "flip_exit_enabled",
    "flip_threshold",
    "zscore_exit_enabled",
    "time_exit_cycles",
    "min_r_for_hold",
    "confidence_decay_exit",
    "hesitation_cycles",
    "trail_enabled",
    "trail_atr_mult",
    "trail_atr_mult_low",
    "trail_atr_mult_high",
    "breakeven_threshold_atr",
}


# ── Timeframe auto-scaling ────────────────────────────────────────────────
# Maps human-readable timeframe labels to M5-bar multipliers.
# For √t-based ATR scaling, we use sqrt(multiplier) because variance grows
# linearly with time (random walk), so stddev grows with √time.
TIMEFRAME_TO_M5 = {
    "M5": 1,
    "M15": 3,
    "M30": 6,
    "H1": 12,
    "H4": 48,
    "D1": 288,
}


def apply_timeframe_scaling(strategy_configs: dict) -> dict:
    """Auto-scale human-readable exit parameters to M5-bar cycles.

    Transforms the strategy_configs dict in-place so that every consumer
    downstream (strategy evaluation, position management) receives values
    already expressed in M5-bar units.  YAML authors write the physically
    intuitive number (e.g. ``hesitation_cycles: 3`` on an H1 strategy means
    "3 × H1 bars"), and this function multiplies by the timeframe ratio.

    Returns the same dict (mutated) for call-site convenience.
    """
    for _name, scfg in strategy_configs.items():
        if not isinstance(scfg, dict):
            continue
        tf = str(scfg.get("timeframe", "M5"))
        mult = TIMEFRAME_TO_M5.get(tf, 1)

        exit_cfg = scfg.get("exit")
        if isinstance(exit_cfg, dict):
            # Scale hesitation_cycles
            raw_hesitation = exit_cfg.get("hesitation_cycles")
            if raw_hesitation is not None:
                exit_cfg["hesitation_cycles"] = int(raw_hesitation) * mult
            # Scale time_exit_cycles
            raw_time = exit_cfg.get("time_exit_cycles")
            if raw_time is not None:
                exit_cfg["time_exit_cycles"] = int(raw_time) * mult
            # Scale max_hold_cycles if present
            raw_max_hold = exit_cfg.get("max_hold_cycles")
            if raw_max_hold is not None:
                exit_cfg["max_hold_cycles"] = int(raw_max_hold) * mult

        # Stash the multiplier so downstream (SL/TP, Meta Exit) can use it
        scfg["_tf_mult"] = mult

    return strategy_configs


def validate_strategy_exit_configs(strategy_configs: dict) -> list[str]:
    """Check all strategy ``exit:`` blocks for unknown keys.

    Returns a list of warning strings (empty if clean).  Unknown keys are
    silently ignored at runtime, so this catches configuration drift before
    it causes surprising behaviour.
    """
    warnings: list[str] = []
    for name, scfg in strategy_configs.items():
        exit_cfg = scfg.get("exit", {}) if isinstance(scfg, dict) else {}
        unknown = set(exit_cfg) - _EXPECTED_EXIT_KEYS
        if unknown:
            warnings.append(f"strategy_lines.{name}.exit: unknown keys {sorted(unknown)}")
    return warnings


def _bootstrap_restart_state(state: Any, journal_path: str, config: Any) -> None:
    """Replay recent journal close entries to restore runtime guard state.

    Called once on the first cycle after restart.  Scans the last 30 min
    of journal closes and populates:
      - _reentry_states  (most recent exit per strategy)
      - _pending_sl_records  (all SL/loss events for graduated cooldown)
      - consecutive_sl_hits  (per-strategy SL streak counter)
    """
    import json as _json
    from pathlib import Path as _Path

    _jp = _Path(journal_path)
    if not _jp.exists():
        return

    now = time.time()
    cutoff = now - 1800.0  # 30 min lookback
    _close_entries: list[dict[str, Any]] = []

    # Build set of message_ids for currently-open positions so we skip
    # their closes (those will be handled by normal reconciliation).
    _active_open_mids: set[str] = {
        _v.get("message_id", "")
        for _v in state.known_open_tickets.values()
        if _v.get("message_id", "")
    }

    try:
        for _line in _jp.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line:
                continue
            try:
                _entry = _json.loads(_line)
            except _json.JSONDecodeError:
                continue

            _action = _entry.get("action", "")
            if _action not in ("close",):
                continue

            # Skip closes whose open is still tracked (will be reconciled normally)
            _open_mid = _entry.get("open_message_id", "")
            if _open_mid and _open_mid in _active_open_mids:
                continue

            # Check timestamp
            _ts_str = _entry.get("recorded_at", "")
            try:
                if _ts_str:
                    _ts = datetime.fromisoformat(_ts_str.replace("Z", "+00:00")).timestamp()
                else:
                    continue
            except Exception:
                continue

            if _ts < cutoff:
                continue

            _close_entries.append(_entry)
    except Exception:
        return

    if not _close_entries:
        return

    # Sort by timestamp ascending
    _close_entries.sort(key=lambda e: e.get("recorded_at", ""))

    from core.execution.reentry_guard import ExitRecord, ensure_reentry_state

    for _entry in _close_entries:
        _strategy = _entry.get("strategy", "")
        if not _strategy:
            # Fallback: resolve from magic
            _magic = _entry.get("magic", 0)
            if _magic:
                from core.contracts.strategy_magic import MAGIC_TO_STRATEGY as _M

                _strategy = _M.get(_magic, "")
        if not _strategy:
            continue

        _side = _entry.get("side", "")
        _label = _entry.get("label", "")
        _close_price = _entry.get("detail", {}).get("close_price") or 0.0
        _ticket = _entry.get("position_ticket", 0)
        _reason = _entry.get("detail", {}).get("reason", "unknown_close")

        # ── Record exit for re-entry guard ──
        if _side in ("long", "short"):
            try:
                _rec = ExitRecord(
                    timestamp=now,  # use now — we only care about "has recent exit"
                    strategy_name=_strategy,
                    direction=_side,
                    reason=_reason,
                    confidence=0.5,  # unknown, conservative
                    price=float(_close_price) if _close_price else 0.0,
                    ticket=int(_ticket) if _ticket else 0,
                )
                _rs = ensure_reentry_state(state._reentry_states, _strategy)
                _rs.record_exit(_rec)
            except Exception:
                pass

        # ── Count SL/loss for streak tracker ──
        if _label in ("sl_hit_first", "loss"):
            _curr = state.consecutive_sl_hits.get(_strategy, 0) + 1
            state.consecutive_sl_hits[_strategy] = _curr
            # Also feed graduated SL cooldown
            state._pending_sl_records.append(
                {
                    "strategy": _strategy,
                    "timestamp": now,
                }
            )
            # If streak >= 3, apply per-strategy block
            if _curr >= 3:
                state.sl_streak_blocked_until[_strategy] = now + 1800
        elif _label in ("tp_hit_first", "win"):
            state.consecutive_sl_hits[_strategy] = 0

    if state._pending_sl_records:
        print(
            _json.dumps(
                {
                    "event": "restart_state_bootstrapped",
                    "time": datetime.now(UTC).isoformat(),
                    "close_entries_replayed": len(_close_entries),
                    "sl_records": len(state._pending_sl_records),
                    "reentry_strategies": list(state._reentry_states.keys()),
                    "sl_streaks": dict(state.consecutive_sl_hits),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


def _reconcile_closed_positions(
    mt5_worker: Any,
    symbol: str,
    journal_path: str,
    known_tickets: dict[int, dict[str, Any]],
    state: Any = None,
) -> list[dict[str, Any]]:
    """Detect positions closed by SL/TP and return close journal entries.

    Uses ThreadPoolExecutor (not daemon threads) so that MT5 timeouts can be
    logged with structured reason-codes and affected tickets.
    """
    closed_entries: list[dict[str, Any]] = []
    if mt5_worker is None:
        return closed_entries

    # ── positions_get ──
    try:
        current_positions = mt5_worker.positions_get(symbol=symbol)
    except Exception:
        print(
            json.dumps(
                {
                    "event": "reconciliation_timeout",
                    "phase": "positions_get",
                    "symbol": symbol,
                    "reason_code": "EXEC_RECON_TIMEOUT",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return closed_entries

    current_tickets = {p.ticket for p in (current_positions or [])}

    for ticket, open_entry in list(known_tickets.items()):
        if ticket in current_tickets:
            continue

        deals = None
        try:
            deals = mt5_worker.history_deals_get(position=ticket)
        except Exception:
            print(
                json.dumps(
                    {
                        "event": "reconciliation_timeout",
                        "phase": "history_deals_get",
                        "ticket": ticket,
                        "reason_code": "EXEC_RECON_TIMEOUT",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        if not deals:
            time.sleep(0.2)
            try:
                deals = mt5_worker.history_deals_get(position=ticket)
            except Exception:
                print(
                    json.dumps(
                        {
                            "event": "reconciliation_timeout",
                            "phase": "history_deals_get_retry",
                            "ticket": ticket,
                            "reason_code": "EXEC_RECON_TIMEOUT",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        close_price = None
        close_time = None
        close_reason: int | None = None
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
        # ── Resolve entry price from MT5 deal history (actual fill) ──
        entry_price: float | None = None
        if deals:
            entry_deals = [d for d in deals if getattr(d, "entry", -1) == 0]
            if entry_deals:
                _entry_fill = getattr(entry_deals[0], "price", None)
                if _entry_fill is not None and _entry_fill > 0:
                    entry_price = float(_entry_fill)
        # Fallback L1: open journal entry's order request price
        if entry_price is None:
            detail = open_entry.get("detail", {})
            if isinstance(detail, dict):
                req = detail.get("request", {})
                _req_price = req.get("price")
                if _req_price is not None and _req_price > 0:
                    entry_price = float(_req_price)
        # Fallback L2: engine-registered entry_price (top-level field from live open dispatch)
        if entry_price is None:
            _reg_ep = open_entry.get("entry_price")
            if _reg_ep is not None and float(_reg_ep) > 0:
                entry_price = float(_reg_ep)

        pnl = None
        if entry_price is not None and close_price is not None and close_volume:
            if side == "long":
                pnl = round((close_price - entry_price) * close_volume, 2)
            elif side == "short":
                pnl = round((entry_price - close_price) * close_volume, 2)

        # ── PnL fallback: when MT5 deal history fails, use the engine's
        #    own PnL calculated at dispatch time (stored by _dispatch_managed_close)
        #    or the most recent mid-price as a close-price estimate.
        if pnl is None and entry_price is not None and close_volume:
            _engine_pnl = open_entry.get("_engine_close_pnl")
            if _engine_pnl is not None:
                pnl = float(_engine_pnl)
            elif state is not None and getattr(state, "_recent_mid_prices", None):
                try:
                    _fallback_close = state._recent_mid_prices[-1]
                    if _fallback_close > 0:
                        if side == "long":
                            pnl = round((_fallback_close - entry_price) * close_volume, 2)
                        elif side == "short":
                            pnl = round((entry_price - _fallback_close) * close_volume, 2)
                        close_price = close_price or _fallback_close
                except (IndexError, ValueError):
                    pass

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
            close_reason or 0, "manual_close" if close_reason else "unknown_close"
        )

        # ── Resolve strategy name and magic with fallback ──
        _resolved_strategy = open_entry.get("strategy", "")
        _resolved_magic = open_entry.get("magic")
        if _resolved_magic is None:
            _resolved_magic = open_entry.get("detail", {}).get("request", {}).get("magic", 0)
        if not _resolved_strategy and _resolved_magic:
            try:
                from core.contracts.strategy_magic import MAGIC_TO_STRATEGY

                _resolved_strategy = MAGIC_TO_STRATEGY.get(int(_resolved_magic), "")
            except Exception:
                pass

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
            "magic": _resolved_magic,
            "strategy": _resolved_strategy,
            "sl": open_entry.get("sl"),
            "tp": open_entry.get("tp"),
            "open_message_id": open_entry.get("message_id"),
            "brain_ids": open_entry.get("brain_ids"),
        }
        closed_entries.append(close_entry)

        # ── Record exit for re-entry guard (native MT5 SL/TP) ──
        if state is not None:
            _exit_strategy = _resolved_strategy
            _exit_side = side
            _exit_price = float(close_price) if close_price else 0.0
            _exit_ts = float(close_time) if close_time else time.time()
            _exit_confidence = open_entry.get("entry_consensus", {}).get("consensus_score", 0.5)
            if _exit_strategy and _exit_side in ("long", "short"):
                try:
                    from core.execution.reentry_guard import ExitRecord, ensure_reentry_state

                    _rec = ExitRecord(
                        timestamp=_exit_ts,
                        strategy_name=_exit_strategy,
                        direction=_exit_side,
                        reason=close_reason_str,
                        confidence=float(_exit_confidence),
                        price=_exit_price,
                        ticket=ticket,
                    )
                    _rs = ensure_reentry_state(state._reentry_states, _exit_strategy)
                    _rs.record_exit(_rec)
                except Exception:
                    pass

        del known_tickets[ticket]

    return closed_entries


def _build_mia_close_entry(pos: Any, known_entry: dict[str, Any]) -> dict[str, Any]:
    """Build a close journal entry for a position detected MIA in MT5.

    Called by _execute_management_phase when positions_get returns empty
    for a tracked ticket.  Uses all available engine-side info since MT5
    deal history may not be available yet (position just closed).
    """
    side = str(getattr(pos, "side", known_entry.get("side", "")))
    entry_price = float(
        getattr(pos, "entry_price", None) or known_entry.get("entry_price", 0.0) or 0.0
    )
    close_volume = float(
        getattr(pos, "volume", None)
        or known_entry.get("volume", 0.0)
        or known_entry.get("effective_volume_hint", 0.0)
    )
    initial_sl = float(getattr(pos, "initial_sl", None) or known_entry.get("sl", 0.0) or 0.0)
    initial_tp = float(getattr(pos, "initial_tp", None) or known_entry.get("tp", 0.0) or 0.0)
    current_sl = float(getattr(pos, "current_sl", initial_sl) or initial_sl)
    close_time_iso = _utc_iso()

    # Estimate close_price: assume SL hit (most conservative).
    # _enrich_mia_from_deals() will override with actual deal data.
    close_price = current_sl

    pnl = None
    if entry_price > 0 and close_price > 0 and close_volume > 0:
        if side == "long":
            pnl = round((close_price - entry_price) * close_volume, 2)
        elif side == "short":
            pnl = round((entry_price - close_price) * close_volume, 2)

    _resolved_strategy = known_entry.get("strategy", "")
    _resolved_magic = known_entry.get("magic") or known_entry.get("detail", {}).get(
        "request", {}
    ).get("magic", 0)
    if not _resolved_strategy and _resolved_magic:
        try:
            from core.contracts.strategy_magic import MAGIC_TO_STRATEGY

            _resolved_strategy = MAGIC_TO_STRATEGY.get(int(_resolved_magic), "")
        except Exception:
            pass

    return {
        "schema_version": "live_trade_journal.v2",
        "recorded_at": close_time_iso,
        "message_id": f"mia_close_{known_entry.get('message_id', 'unknown')}",
        "target": "exec_bridge",
        "ack_status": "closed",
        "detail": {
            "reason": "mia_close",
            "close_price": close_price,
            "pnl": pnl,
            "mia_detected_at": close_time_iso,
        },
        "symbol": "XAUUSDc",
        "action": "close",
        "side": side,
        "volume": close_volume,
        "pnl": pnl,
        "label": "loss"
        if (pnl is not None and pnl < 0)
        else ("win" if (pnl is not None and pnl > 0) else "breakeven"),
        "position_ticket": pos.ticket,
        "magic": _resolved_magic,
        "strategy": _resolved_strategy,
        "sl": initial_sl,
        "tp": initial_tp,
        "open_message_id": known_entry.get("message_id"),
        "brain_ids": known_entry.get("brain_ids"),
    }


def _enrich_mia_from_deals(
    mia_entry: dict[str, Any],
    deals: list[Any],
) -> None:
    """Enrich an MIA close entry with actual MT5 deal history data.

    Overrides the conservative SL-hit estimate in _build_mia_close_entry
    with actual close_price and close_reason from deal history.
    """
    close_price = None
    close_time = None
    close_reason: int | None = None

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

    if close_price is not None:
        mia_entry["detail"]["close_price"] = close_price
        close_reason_str = {4: "sl_hit", 5: "tp_hit"}.get(close_reason or 0, "unknown_close")
        mia_entry["detail"]["reason"] = close_reason_str

        # Recompute PnL with actual close price
        side = mia_entry.get("side", "")
        entry_price = mia_entry.get("detail", {}).get("entry_price") or mia_entry.get(
            "entry_price", 0
        )
        close_volume = mia_entry.get("volume", 0)
        if entry_price and close_price and close_volume:
            if isinstance(entry_price, int | float) and entry_price > 0:
                if side == "long":
                    mia_entry["pnl"] = round((close_price - entry_price) * close_volume, 2)
                elif side == "short":
                    mia_entry["pnl"] = round((entry_price - close_price) * close_volume, 2)
            if mia_entry.get("pnl", 0) is not None:
                pnl = mia_entry["pnl"]
                if pnl < 0:
                    mia_entry["label"] = "loss"
                elif pnl > 0:
                    mia_entry["label"] = "win"
                else:
                    mia_entry["label"] = "breakeven"

        if close_reason == 4:
            mia_entry["label"] = "sl_hit_first"
        elif close_reason == 5:
            mia_entry["label"] = "tp_hit_first"

    if close_time is not None:
        mia_entry["recorded_at"] = (
            datetime.fromtimestamp(close_time, tz=UTC).isoformat().replace("+00:00", "Z")
        )


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
    total_budget: float = 0.0,
    lot_value: float | None = None,
) -> dict[str, Any]:
    """Compute contract-group consensus and capital allocation from raw proposals.

    Returns a dict with keys: direction, confidence, dynamic_volume, proposals,
    consensus_extra.
    """
    from core.execution.capital_allocator import CapitalAllocator, compute_volume, resolve_conflicts
    from core.parliament.contract_groups import compute_all_group_signals

    # Build (brain_info, proposal) pairs for group assignment
    brain_proposal_pairs: list[tuple[dict[str, Any], Any]] = []
    for i, p in enumerate(raw_proposals):
        b_info = brains[i] if i < len(brains) else {}
        # BrainSignal always carries brain_id from the adapter.
        brain_proposal_pairs.append((b_info, p))

    # Apply dynamic vote weights (same weighter, but now used per-group)
    from core.brains.services.dynamic_brain_weighter import DynamicBrainWeighter

    weighter = DynamicBrainWeighter(tracker, pnl_store=pnl_ledger)
    # Wire brain metadata for redundancy detection
    for b_info in brains:
        bid = b_info.get("brain_id", "")
        if bid:
            weighter.set_brain_metadata(
                bid,
                {
                    "contract_group": b_info.get("contract_group", ""),
                    "feature_schema": b_info.get("feature_schema", ""),
                },
            )
    weighter.apply_weights(raw_proposals)

    # ── Capacity-aware position sizing (P&L Phase 4) ──
    capacity_allocations: dict[str, float] = {}
    if total_budget > 0:
        try:
            allocator = CapitalAllocator()
            brain_weights = weighter.get_weights()
            capacity_allocations = allocator.allocate_capacity(
                total_budget=total_budget,
                brain_weights=brain_weights,
                lot_value=lot_value,
            )
        except Exception:
            pass

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
            "capacity_allocations": capacity_allocations,
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
            "capacity_allocations": capacity_allocations,
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


def _warn_contract_mismatch(
    brain_info: dict[str, Any],
    strategy_name: str,
    required_contracts: dict[str, str],
) -> None:
    """Hard-mute a brain whose training contract doesn't match its strategy line.

    A regression-contract brain placed in a barrier strategy would silently
    predict the wrong target.  Previously this was a soft warning; now the
    brain's ``vote_weight`` is forced to 0.0 — it cannot influence any
    parliament decision until its contract is reconciled.

    The brain still runs inference (so we can monitor its output quality)
    but its vote is discarded before consensus aggregation.
    """
    training_contract = str(brain_info.get("training_contract", ""))
    required = required_contracts.get(strategy_name, "")
    # Accept if training_contract contains `required` prefix (legacy check)
    # OR if training_contract starts with the strategy_name (e.g.
    # barrier_12bar_regression_huber starts with barrier_12bar).
    contract_ok = (required and required in training_contract) or training_contract.startswith(
        strategy_name
    )
    if required and not contract_ok:
        # ── Hard mute: zero the vote weight ──
        _prev_weight = brain_info.get("vote_weight", 1.0)
        brain_info["vote_weight"] = 0.0
        # Suppress duplicate log events: only print on first mute per brain
        if brain_info.get("_contract_muted"):
            return
        brain_info["_contract_muted"] = True
        print(
            json.dumps(
                {
                    "event": "brain_hard_muted_contract",
                    "brain_id": brain_info.get("brain_id", "unknown"),
                    "brain_type": brain_info.get("brain_type", "unknown"),
                    "brain_contract": training_contract,
                    "strategy_name": strategy_name,
                    "strategy_requires": required,
                    "previous_vote_weight": _prev_weight,
                    "new_vote_weight": 0.0,
                    "reason": "training_contract_mismatch",
                    "action_required": "retrain_brain_with_correct_contract_or_reassign_group",
                }
            ),
            flush=True,
        )


def _build_meta_feature_vector(
    *,
    brains: list[dict[str, Any]],
    feature_store: Any,
    mid_price: float | None,
    symbol: str,
) -> tuple[Any, dict[str, float] | None]:
    """Build 43-dim raw feature vector for meta-labeling binary classifier.

    The meta labeler (Meta_Stage1_MetaLabel_Binary_V1) was trained on
    40 raw V9 institutional features + 3 OU physics features WITHOUT
    z-score normalization.  This function builds the same 43-dim raw
    vector at inference time.

    Returns (feature_vector, ou_params) where:
      - feature_vector is a 1×43 np.ndarray (float32)
      - ou_params is {z_score, half_life, theta} for diagnostic logging
      - returns (None, None) if OU params cannot be computed
    """
    import numpy as np

    from core.brains.adapters.params_brain_adapter import ParamsBrainAdapter

    # ── Step 1: Compute OU params from statarb brain adapter ──
    ou_params: dict[str, float] | None = None
    _price = mid_price if mid_price is not None and mid_price > 0 else 0.0
    for b_info in brains:
        adapter = b_info.get("adapter")
        if isinstance(adapter, ParamsBrainAdapter):
            try:
                raw = adapter.infer(np.array([_price], dtype=np.float32))
                ou_params = {
                    "z_score": float(raw.get("z_score", 0.0)),
                    "half_life": float(raw.get("half_life", float("inf"))),
                    "theta": float(raw.get("theta", 0.0)),
                }
                break
            except Exception:
                pass

    if ou_params is None:
        return None, None

    # ── Step 2: Clip z_score to training boundary [1.3, 2.5] ──
    z_clipped = max(1.3, min(2.5, ou_params["z_score"]))
    hl = ou_params["half_life"] if ou_params["half_life"] != float("inf") else 999.0

    # ── Step 3: Read raw V9 features from feature store ──
    raw_features: dict[str, float] = {}
    try:
        record = feature_store.latest(symbol, "M5", schema_name="v9_institutional_40")
        if record is not None and record.values:
            raw_features = record.values
    except Exception:
        pass

    # ── Step 4: Build 43-dim raw vector in TRAINING feature order ──
    # FIX-20260525-026: The V9_INSTITUTIONAL_40_FEATURES schema order
    # (M5→H1, OU_Theta/Hurst blocked at end) does NOT match the training
    # order (H1→M5, OU_Theta/Hurst inline per-TF).  LightGBM uses
    # position-based indexing — every single feature position was
    # scrambled, making the model receive random noise.
    # Fix: read the authoritative feature_names from the MetaLabel brain
    # config or model metadata, then assemble in that exact order.
    _feature_names: list[str] | None = None

    # Source 1: brain config features field (authoritative — training order)
    for b_info in brains:
        _bid = str(b_info.get("brain_id", ""))
        if (
            "metalabel" in _bid.lower()
            or "barrier_12bar_meta" in str(b_info.get("contract_group", "")).lower()
        ):
            _features = b_info.get("features")
            if _features and isinstance(_features, list) and len(_features) == 43:
                _feature_names = [str(f) for f in _features]
                break

    # Source 2: model metadata file (fallback)
    if _feature_names is None:
        _meta_path = None
        for b_info in brains:
            _bid = str(b_info.get("brain_id", ""))
            if "metalabel" in _bid.lower():
                _meta_path = b_info.get("normalization_config_path")
                break
        if _meta_path:
            try:
                _meta = json.loads(Path(_meta_path).read_text(encoding="utf-8"))
                _names = _meta.get("feature_names")
                if _names and isinstance(_names, list) and len(_names) == 43:
                    _feature_names = [str(f) for f in _names]
            except Exception:
                pass

    # Build full feature dict: raw V9 values + OU augmentation
    _full_dict: dict[str, float] = dict(raw_features)
    _full_dict["ou_z_score"] = z_clipped
    _full_dict["ou_half_life"] = hl
    _full_dict["ou_theta"] = ou_params["theta"]

    if _feature_names is not None:
        values = [float(_full_dict.get(name, 0.0)) for name in _feature_names]
    else:
        # Legacy fallback with a loud diagnostic — this path should
        # never be reached in production, but preserves back-compat
        # for environments where the brain config is unavailable.
        import logging

        _logger = logging.getLogger(__name__)
        _logger.error(
            "MetaLabel brain feature_names unavailable — "
            "falling back to V9 schema order (TRAIN-SERVE SKEW LIKELY)"
        )
        from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES

        values = [float(raw_features.get(name, 0.0)) for name in V9_INSTITUTIONAL_40_FEATURES]
        values.append(z_clipped)
        values.append(hl)
        values.append(ou_params["theta"])

    feature_vec = np.array(values, dtype=np.float32).reshape(1, -1)
    return feature_vec, ou_params


def _build_strategy_lines(
    brains: list[dict[str, Any]],
    config: LiveCycleConfig,
) -> dict[str, Any]:
    """Partition brains into contract groups and create strategy line objects.

    Returns dict mapping strategy_name → StrategyLine instance.
    """
    # Contract type → strategy line mapping (for validation)
    _STRATEGY_CONTRACT_TYPES = {
        "barrier_12bar": "survival_barrier",
        "barrier_12bar_meta": "barrier_12bar_meta_binary_cls",
        "micro_3bar": "label-micro-barrier",
        "micro_m15": "label-micro-barrier",
        "micro_h1": "label-micro-barrier",
        "micro_h4": "label-micro-barrier",
        "statarb_dynamic": "ou_mean_reversion",
        "statarb_m15": "ou_mean_reversion",
        "daily_swing": "d1_swing",
        "m15_swing": "m15_swing",
        "m30_swing": "m30_swing",
        "h1_swing": "h1_swing",
        "h4_swing": "h4_swing",
    }

    # Phase 4: strategy family auto-inference — explicit YAML config wins over this map
    _STRATEGY_FAMILY_MAP: dict[str, str] = {
        "statarb_dynamic": "mean_reversion",
        "statarb_m15": "mean_reversion",
        # everything else defaults to trend_following
    }

    # Partition brains by contract_group (declared in brain JSON, not brain_type)

    _known_groups: dict[str, list[Any]] = {g["name"]: [] for g in ALL_GROUPS}
    _unknown_brains: list[dict[str, Any]] = []

    for b_info in brains:
        # ── Defense-in-depth: frozen/retired brains must not vote ──
        brain_status = b_info.get("status", "")
        if brain_status in ("frozen", "retired"):
            print(
                json.dumps(
                    {
                        "event": "brain_excluded_from_voting",
                        "brain_id": b_info.get("brain_id", "unknown"),
                        "status": brain_status,
                        "reason": "frozen_or_retired",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            continue

        cg = b_info.get("contract_group", "")
        if cg in _known_groups:
            _known_groups[cg].append(b_info)
            _warn_contract_mismatch(b_info, cg, _STRATEGY_CONTRACT_TYPES)
        else:
            print(
                json.dumps(
                    {
                        "event": "unknown_contract_group_at_build",
                        "contract_group": cg,
                        "brain_id": b_info.get("brain_id", "unknown"),
                        "brain_type": b_info.get("brain_type", ""),
                        "skipped": True,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            _unknown_brains.append(b_info)

    barrier_brains = _known_groups["barrier_12bar"]
    barrier_12bar_meta_brains = _known_groups["barrier_12bar_meta"]
    micro_brains = _known_groups["micro_3bar"]
    micro_m15_brains = _known_groups["micro_m15"]
    micro_h1_brains = _known_groups["micro_h1"]
    _micro_h4_brains = _known_groups["micro_h4"]
    statarb_brains = _known_groups["statarb_dynamic"]
    statarb_m15_brains = _known_groups["statarb_m15"]
    daily_swing_brains = _known_groups["daily_swing"]
    m15_swing_brains = _known_groups["m15_swing"]
    m30_swing_brains = _known_groups["m30_swing"]
    h1_swing_brains = _known_groups["h1_swing"]
    h4_swing_brains = _known_groups["h4_swing"]

    def _cfg(name: str, key: str, default: Any) -> Any:
        """Read a value from live.yaml strategy_lines.<name>.<key>, falling back to default."""
        return config.strategy_configs.get(name, {}).get(key, default)

    def _vol_cfg(name: str) -> float:
        """Read base_volume from live.yaml, respecting explicit 0.0 (shadow mode).

        Python's ``or`` chain would treat 0.0 as falsy and silently fall through
        to config.volume, making it impossible to set base_volume=0 for
        capital-isolated shadow strategies.
        """
        sc = config.strategy_configs.get(name, {})
        if "base_volume" in sc:
            return float(sc["base_volume"])
        return float(config.volume or 0.01)

    def _exit_cfg(name: str, key: str, default: Any) -> Any:
        """Read a value from live.yaml strategy_lines.<name>.exit.<key>."""
        return config.strategy_configs.get(name, {}).get("exit", {}).get(key, default)

    # ── Enforce strategy-level enabled flag ──
    # Clears brain lists for disabled strategies so the existing if-blocks
    # naturally skip them.  Defaults to enabled=True when the strategy
    # has no config entry at all (backward compat).
    for _gname in list(_known_groups.keys()):
        if not _cfg(_gname, "enabled", True):
            _known_groups[
                _gname
            ].clear()  # in-place clear so local variable references see empty list
            print(
                json.dumps(
                    {
                        "event": "strategy_disabled_by_config",
                        "time": _utc_iso(),
                        "strategy": _gname,
                        "reason": "enabled: false in live.yaml",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    strategies: dict[str, Any] = {}

    if barrier_brains:
        # Dictator Protocol (2026-05-22): filter by brain_type from BARRIER_GROUP.
        # contract_group alone is too coarse — brain_type is the per-model gate.
        barrier_brains = [
            b for b in barrier_brains if b.get("brain_type", "") in BARRIER_GROUP["brain_types"]
        ]
    if barrier_brains:
        # ── Auto-discover meta-probe specs from brain JSON roles ──
        from core.execution.meta_pipeline import discover_probe_specs

        _meta_probe_specs = discover_probe_specs(barrier_brains)
        # live.yaml override (if configured):
        _yaml_probes = _cfg("barrier_12bar", "meta_probes", None)
        if _yaml_probes is not None:
            from core.execution.meta_pipeline import MetaProbeSpec

            _meta_probe_specs = [
                MetaProbeSpec(
                    brain_id=str(p.get("brain_id", "")),
                    threshold=float(p.get("threshold", 0.30)),
                    filter_stage=str(p.get("filter_stage", "stage2")),
                )
                for p in _yaml_probes
            ]

        strategies["barrier_12bar"] = BarrierStrategy(
            StrategyLineConfig(
                name="barrier_12bar",
                strategy_family=_cfg("barrier_12bar", "strategy_family", None)
                or _STRATEGY_FAMILY_MAP.get("barrier_12bar", "trend_following"),
                magic=90001,
                brain_types=BARRIER_GROUP["brain_types"],
                base_volume=_vol_cfg("barrier_12bar"),
                max_volume=_cfg("barrier_12bar", "max_volume", 0.05),
                base_sl_atr_mult=_cfg("barrier_12bar", "sl", {}).get(
                    "base_atr_mult", config.sl_atr_mult
                ),
                base_tp_atr_mult=_cfg("barrier_12bar", "tp", {}).get(
                    "base_atr_mult", config.tp_atr_mult
                ),
                hard_sl_ratio=_cfg("barrier_12bar", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("barrier_12bar", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("barrier_12bar", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg(
                    "barrier_12bar", "confidence_threshold", config.confidence_threshold
                ),
                long_bias_discount=_cfg("barrier_12bar", "direction_balance", {}).get(
                    "long_bias_discount", 0.05
                ),
                exit_flip_enabled=_exit_cfg("barrier_12bar", "flip_exit_enabled", True),
                exit_time_cycles=_exit_cfg("barrier_12bar", "time_exit_cycles", None),
                exit_zscore_enabled=_exit_cfg("barrier_12bar", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("barrier_12bar", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("barrier_12bar", "min_valid_brains", 3),
                timeframe=_cfg("barrier_12bar", "timeframe", "M5"),
                exit_hesitation_cycles=_exit_cfg("barrier_12bar", "hesitation_cycles", 0),
                meta_probe_specs=_meta_probe_specs,
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
                strategy_family=_STRATEGY_FAMILY_MAP.get("micro_3bar", "trend_following"),
                magic=90002,
                brain_types=MICRO_GROUP["brain_types"],
                base_volume=_vol_cfg("micro_3bar"),
                max_volume=_cfg("micro_3bar", "max_volume", 0.03),
                base_sl_atr_mult=_cfg("micro_3bar", "sl", {}).get("base_atr_mult", 2.0),
                base_tp_atr_mult=_cfg("micro_3bar", "tp", {}).get("base_atr_mult", 2.5),
                hard_sl_ratio=_cfg("micro_3bar", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("micro_3bar", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("micro_3bar", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg(
                    "micro_3bar", "confidence_threshold", config.confidence_threshold
                ),
                long_bias_discount=_cfg("micro_3bar", "direction_balance", {}).get(
                    "long_bias_discount", 0.03
                ),
                exit_flip_enabled=_exit_cfg("micro_3bar", "flip_exit_enabled", True),
                exit_time_cycles=_exit_cfg("micro_3bar", "time_exit_cycles", None),
                exit_zscore_enabled=_exit_cfg("micro_3bar", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("micro_3bar", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("micro_3bar", "min_valid_brains", 2),
                timeframe=_cfg("micro_3bar", "timeframe", "M5"),
                exit_hesitation_cycles=_exit_cfg("micro_3bar", "hesitation_cycles", 0),
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

    if micro_m15_brains:
        strategies["micro_m15"] = MicroStrategy(
            StrategyLineConfig(
                name="micro_m15",
                strategy_family=_STRATEGY_FAMILY_MAP.get("micro_m15", "trend_following"),
                magic=90101,
                brain_types=MICRO_M15_GROUP["brain_types"],
                base_volume=_vol_cfg("micro_m15"),
                max_volume=_cfg("micro_m15", "max_volume", 0.03),
                base_sl_atr_mult=_cfg("micro_m15", "sl", {}).get("base_atr_mult", 1.5),
                base_tp_atr_mult=_cfg("micro_m15", "tp", {}).get("base_atr_mult", 2.5),
                hard_sl_ratio=_cfg("micro_m15", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("micro_m15", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("micro_m15", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg(
                    "micro_m15", "confidence_threshold", config.confidence_threshold
                ),
                long_bias_discount=_cfg("micro_m15", "direction_balance", {}).get(
                    "long_bias_discount", 0.03
                ),
                exit_flip_enabled=_exit_cfg("micro_m15", "flip_exit_enabled", True),
                exit_time_cycles=_exit_cfg("micro_m15", "time_exit_cycles", None),
                exit_zscore_enabled=_exit_cfg("micro_m15", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("micro_m15", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("micro_m15", "min_valid_brains", 2),
                timeframe=_cfg("micro_m15", "timeframe", "M15"),
                exit_hesitation_cycles=_exit_cfg("micro_m15", "hesitation_cycles", 0),
            ),
            micro_m15_brains,
            budget=StrategyBudget(
                "micro_m15",
                daily_loss_limit_pct=_cfg("micro_m15", "budget", {}).get(
                    "daily_loss_limit_pct", -0.02
                ),
                max_consecutive_losses=_cfg("micro_m15", "budget", {}).get(
                    "max_consecutive_losses", 6
                ),
            ),
        )

    if micro_h1_brains:
        strategies["micro_h1"] = MicroStrategy(
            StrategyLineConfig(
                name="micro_h1",
                strategy_family=_STRATEGY_FAMILY_MAP.get("micro_h1", "trend_following"),
                magic=90201,
                brain_types=MICRO_H1_GROUP["brain_types"],
                base_volume=_vol_cfg("micro_h1"),
                max_volume=_cfg("micro_h1", "max_volume", 0.02),
                base_sl_atr_mult=_cfg("micro_h1", "sl", {}).get("base_atr_mult", 1.8),
                base_tp_atr_mult=_cfg("micro_h1", "tp", {}).get("base_atr_mult", 2.8),
                hard_sl_ratio=_cfg("micro_h1", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("micro_h1", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("micro_h1", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg(
                    "micro_h1", "confidence_threshold", config.confidence_threshold
                ),
                long_bias_discount=_cfg("micro_h1", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("micro_h1", "flip_exit_enabled", True),
                exit_time_cycles=_exit_cfg("micro_h1", "time_exit_cycles", None),
                exit_zscore_enabled=_exit_cfg("micro_h1", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("micro_h1", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("micro_h1", "min_valid_brains", 2),
                timeframe=_cfg("micro_h1", "timeframe", "H1"),
                exit_hesitation_cycles=_exit_cfg("micro_h1", "hesitation_cycles", 0),
            ),
            micro_h1_brains,
            budget=StrategyBudget(
                "micro_h1",
                daily_loss_limit_pct=_cfg("micro_h1", "budget", {}).get(
                    "daily_loss_limit_pct", -0.015
                ),
                max_consecutive_losses=_cfg("micro_h1", "budget", {}).get(
                    "max_consecutive_losses", 4
                ),
            ),
        )

    # H4 brains are trend gate only — no independent strategy line

    if statarb_brains:
        strategies["statarb_dynamic"] = StatArbStrategy(
            StrategyLineConfig(
                name="statarb_dynamic",
                strategy_family=_cfg("statarb_dynamic", "strategy_family", None)
                or _STRATEGY_FAMILY_MAP.get("statarb_dynamic", "trend_following"),
                magic=90003,
                brain_types=ARB_GROUP["brain_types"],
                base_volume=_vol_cfg("statarb_dynamic"),
                max_volume=_cfg("statarb_dynamic", "max_volume", 0.03),
                base_sl_atr_mult=_cfg("statarb_dynamic", "sl", {}).get("base_atr_mult", 1.5),
                base_tp_atr_mult=_cfg("statarb_dynamic", "tp", {}).get("base_atr_mult", 3.0),
                hard_sl_ratio=_cfg("statarb_dynamic", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("statarb_dynamic", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("statarb_dynamic", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg(
                    "statarb_dynamic", "confidence_threshold", config.confidence_threshold
                ),
                long_bias_discount=_cfg("statarb_dynamic", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("statarb_dynamic", "flip_exit_enabled", False),
                exit_time_cycles=_exit_cfg("statarb_dynamic", "time_exit_cycles", 40),
                exit_zscore_enabled=_exit_cfg("statarb_dynamic", "zscore_exit_enabled", True),
                exit_min_r=_exit_cfg("statarb_dynamic", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("statarb_dynamic", "min_valid_brains", 1),
                timeframe=_cfg("statarb_dynamic", "timeframe", "M5"),
                exit_hesitation_cycles=_exit_cfg("statarb_dynamic", "hesitation_cycles", 0),
                min_p_win=_cfg("statarb_dynamic", "min_p_win", 0.50),
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

    if statarb_m15_brains:
        strategies["statarb_m15"] = StatArbStrategy(
            StrategyLineConfig(
                name="statarb_m15",
                strategy_family=_cfg("statarb_m15", "strategy_family", None)
                or _STRATEGY_FAMILY_MAP.get("statarb_m15", "trend_following"),
                magic=90103,
                brain_types=STATARB_M15_GROUP["brain_types"],
                base_volume=_vol_cfg("statarb_m15"),
                max_volume=_cfg("statarb_m15", "max_volume", 0.02),
                base_sl_atr_mult=_cfg("statarb_m15", "sl", {}).get("base_atr_mult", 2.0),
                base_tp_atr_mult=_cfg("statarb_m15", "tp", {}).get("base_atr_mult", 4.0),
                hard_sl_ratio=_cfg("statarb_m15", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("statarb_m15", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("statarb_m15", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg(
                    "statarb_m15", "confidence_threshold", config.confidence_threshold
                ),
                long_bias_discount=_cfg("statarb_m15", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("statarb_m15", "flip_exit_enabled", False),
                exit_time_cycles=_exit_cfg("statarb_m15", "time_exit_cycles", 120),
                exit_zscore_enabled=_exit_cfg("statarb_m15", "zscore_exit_enabled", True),
                exit_min_r=_exit_cfg("statarb_m15", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("statarb_m15", "min_valid_brains", 1),
                timeframe=_cfg("statarb_m15", "timeframe", "M15"),
                exit_hesitation_cycles=_exit_cfg("statarb_m15", "hesitation_cycles", 0),
                min_p_win=_cfg("statarb_m15", "min_p_win", 0.50),
            ),
            statarb_m15_brains,
            budget=StrategyBudget(
                "statarb_m15",
                daily_loss_limit_pct=_cfg("statarb_m15", "budget", {}).get(
                    "daily_loss_limit_pct", -0.01
                ),
                max_consecutive_losses=_cfg("statarb_m15", "budget", {}).get(
                    "max_consecutive_losses", 3
                ),
            ),
        )

    if barrier_12bar_meta_brains:
        strategies["barrier_12bar_meta"] = BarrierStrategy(
            StrategyLineConfig(
                name="barrier_12bar_meta",
                strategy_family=_cfg("barrier_12bar_meta", "strategy_family", None)
                or _STRATEGY_FAMILY_MAP.get("barrier_12bar_meta", "trend_following"),
                magic=90014,
                brain_types=BARRIER_12BAR_META_GROUP["brain_types"],
                base_volume=_vol_cfg("barrier_12bar_meta"),
                max_volume=_cfg("barrier_12bar_meta", "max_volume", 0.0),
                base_sl_atr_mult=_cfg("barrier_12bar_meta", "sl", {}).get("base_atr_mult", 3.0),
                base_tp_atr_mult=_cfg("barrier_12bar_meta", "tp", {}).get("base_atr_mult", 1.5),
                hard_sl_ratio=_cfg("barrier_12bar_meta", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("barrier_12bar_meta", "sl", {}).get("min_sl_distance", 8.0),
                min_rr_ratio=_cfg("barrier_12bar_meta", "sl", {}).get("min_rr_ratio", 0.5),
                confidence_threshold=_cfg("barrier_12bar_meta", "confidence_threshold", 0.40),
                long_bias_discount=_cfg("barrier_12bar_meta", "direction_balance", {}).get(
                    "long_bias_discount", 0.05
                ),
                exit_flip_enabled=_exit_cfg("barrier_12bar_meta", "flip_exit_enabled", True),
                exit_time_cycles=_exit_cfg("barrier_12bar_meta", "time_exit_cycles", 60),
                exit_zscore_enabled=_exit_cfg("barrier_12bar_meta", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("barrier_12bar_meta", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("barrier_12bar_meta", "min_valid_brains", 1),
                timeframe=_cfg("barrier_12bar_meta", "timeframe", "M5"),
                exit_hesitation_cycles=_exit_cfg("barrier_12bar_meta", "hesitation_cycles", 12),
            ),
            barrier_12bar_meta_brains,
            budget=StrategyBudget(
                "barrier_12bar_meta",
                daily_loss_limit_pct=_cfg("barrier_12bar_meta", "budget", {}).get(
                    "daily_loss_limit_pct", -0.03
                ),
                max_consecutive_losses=_cfg("barrier_12bar_meta", "budget", {}).get(
                    "max_consecutive_losses", 5
                ),
            ),
        )

    # ── Swing strategies (D1 features, TF-specific barrier contracts) ──
    from core.parliament.contract_groups import (
        DAILY_SWING_GROUP,
        H1_SWING_GROUP,
        H4_SWING_GROUP,
        M15_SWING_GROUP,
        M30_SWING_GROUP,
    )

    if daily_swing_brains:
        strategies["daily_swing"] = SwingStrategy(
            StrategyLineConfig(
                name="daily_swing",
                strategy_family=_STRATEGY_FAMILY_MAP.get("daily_swing", "trend_following"),
                magic=90301,
                brain_types=DAILY_SWING_GROUP["brain_types"],
                base_volume=_vol_cfg("daily_swing"),
                max_volume=_cfg("daily_swing", "max_volume", 0.03),
                base_sl_atr_mult=_cfg("daily_swing", "sl", {}).get("base_atr_mult", 2.0),
                base_tp_atr_mult=_cfg("daily_swing", "tp", {}).get("base_atr_mult", 3.5),
                hard_sl_ratio=_cfg("daily_swing", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("daily_swing", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("daily_swing", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg("daily_swing", "confidence_threshold", 0.45),
                long_bias_discount=_cfg("daily_swing", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("daily_swing", "flip_exit_enabled", False),
                exit_time_cycles=_exit_cfg("daily_swing", "time_exit_cycles", 1440),
                exit_zscore_enabled=_exit_cfg("daily_swing", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("daily_swing", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("daily_swing", "min_valid_brains", 2),
                timeframe=_cfg("daily_swing", "timeframe", "D1"),
                exit_hesitation_cycles=_exit_cfg("daily_swing", "hesitation_cycles", 0),
            ),
            daily_swing_brains,
            budget=StrategyBudget(
                "daily_swing",
                daily_loss_limit_pct=_cfg("daily_swing", "budget", {}).get(
                    "daily_loss_limit_pct", -0.02
                ),
                max_consecutive_losses=_cfg("daily_swing", "budget", {}).get(
                    "max_consecutive_losses", 3
                ),
            ),
        )

    if m15_swing_brains:
        strategies["m15_swing"] = SwingStrategy(
            StrategyLineConfig(
                name="m15_swing",
                strategy_family=_STRATEGY_FAMILY_MAP.get("m15_swing", "trend_following"),
                magic=90310,
                brain_types=M15_SWING_GROUP["brain_types"],
                base_volume=_vol_cfg("m15_swing"),
                max_volume=_cfg("m15_swing", "max_volume", 0.03),
                base_sl_atr_mult=_cfg("m15_swing", "sl", {}).get("base_atr_mult", 1.5),
                base_tp_atr_mult=_cfg("m15_swing", "tp", {}).get("base_atr_mult", 3.0),
                hard_sl_ratio=_cfg("m15_swing", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("m15_swing", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("m15_swing", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg("m15_swing", "confidence_threshold", 0.45),
                long_bias_discount=_cfg("m15_swing", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("m15_swing", "flip_exit_enabled", False),
                exit_time_cycles=_exit_cfg("m15_swing", "time_exit_cycles", 72),
                exit_zscore_enabled=_exit_cfg("m15_swing", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("m15_swing", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("m15_swing", "min_valid_brains", 1),
                timeframe=_cfg("m15_swing", "timeframe", "M15"),
                exit_hesitation_cycles=_exit_cfg("m15_swing", "hesitation_cycles", 0),
            ),
            m15_swing_brains,
            budget=StrategyBudget(
                "m15_swing",
                daily_loss_limit_pct=_cfg("m15_swing", "budget", {}).get(
                    "daily_loss_limit_pct", -0.015
                ),
                max_consecutive_losses=_cfg("m15_swing", "budget", {}).get(
                    "max_consecutive_losses", 4
                ),
            ),
        )

    if m30_swing_brains:
        strategies["m30_swing"] = SwingStrategy(
            StrategyLineConfig(
                name="m30_swing",
                strategy_family=_STRATEGY_FAMILY_MAP.get("m30_swing", "trend_following"),
                magic=90320,
                brain_types=M30_SWING_GROUP["brain_types"],
                base_volume=_vol_cfg("m30_swing"),
                max_volume=_cfg("m30_swing", "max_volume", 0.03),
                base_sl_atr_mult=_cfg("m30_swing", "sl", {}).get("base_atr_mult", 1.5),
                base_tp_atr_mult=_cfg("m30_swing", "tp", {}).get("base_atr_mult", 3.0),
                hard_sl_ratio=_cfg("m30_swing", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("m30_swing", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("m30_swing", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg("m30_swing", "confidence_threshold", 0.45),
                long_bias_discount=_cfg("m30_swing", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("m30_swing", "flip_exit_enabled", False),
                exit_time_cycles=_exit_cfg("m30_swing", "time_exit_cycles", 36),
                exit_zscore_enabled=_exit_cfg("m30_swing", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("m30_swing", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("m30_swing", "min_valid_brains", 1),
                timeframe=_cfg("m30_swing", "timeframe", "M30"),
                exit_hesitation_cycles=_exit_cfg("m30_swing", "hesitation_cycles", 0),
            ),
            m30_swing_brains,
            budget=StrategyBudget(
                "m30_swing",
                daily_loss_limit_pct=_cfg("m30_swing", "budget", {}).get(
                    "daily_loss_limit_pct", -0.015
                ),
                max_consecutive_losses=_cfg("m30_swing", "budget", {}).get(
                    "max_consecutive_losses", 4
                ),
            ),
        )

    if h1_swing_brains:
        strategies["h1_swing"] = SwingStrategy(
            StrategyLineConfig(
                name="h1_swing",
                strategy_family=_STRATEGY_FAMILY_MAP.get("h1_swing", "trend_following"),
                magic=90330,
                brain_types=H1_SWING_GROUP["brain_types"],
                base_volume=_vol_cfg("h1_swing"),
                max_volume=_cfg("h1_swing", "max_volume", 0.02),
                base_sl_atr_mult=_cfg("h1_swing", "sl", {}).get("base_atr_mult", 2.0),
                base_tp_atr_mult=_cfg("h1_swing", "tp", {}).get("base_atr_mult", 3.5),
                hard_sl_ratio=_cfg("h1_swing", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("h1_swing", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("h1_swing", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg("h1_swing", "confidence_threshold", 0.45),
                long_bias_discount=_cfg("h1_swing", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("h1_swing", "flip_exit_enabled", False),
                exit_time_cycles=_exit_cfg("h1_swing", "time_exit_cycles", 288),
                exit_zscore_enabled=_exit_cfg("h1_swing", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("h1_swing", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("h1_swing", "min_valid_brains", 1),
                timeframe=_cfg("h1_swing", "timeframe", "H1"),
                exit_hesitation_cycles=_exit_cfg("h1_swing", "hesitation_cycles", 0),
            ),
            h1_swing_brains,
            budget=StrategyBudget(
                "h1_swing",
                daily_loss_limit_pct=_cfg("h1_swing", "budget", {}).get(
                    "daily_loss_limit_pct", -0.015
                ),
                max_consecutive_losses=_cfg("h1_swing", "budget", {}).get(
                    "max_consecutive_losses", 3
                ),
            ),
        )

    if h4_swing_brains:
        strategies["h4_swing"] = SwingStrategy(
            StrategyLineConfig(
                name="h4_swing",
                strategy_family=_STRATEGY_FAMILY_MAP.get("h4_swing", "trend_following"),
                magic=90340,
                brain_types=H4_SWING_GROUP["brain_types"],
                base_volume=_vol_cfg("h4_swing"),
                max_volume=_cfg("h4_swing", "max_volume", 0.02),
                base_sl_atr_mult=_cfg("h4_swing", "sl", {}).get("base_atr_mult", 2.0),
                base_tp_atr_mult=_cfg("h4_swing", "tp", {}).get("base_atr_mult", 4.0),
                hard_sl_ratio=_cfg("h4_swing", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("h4_swing", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("h4_swing", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg("h4_swing", "confidence_threshold", 0.45),
                long_bias_discount=_cfg("h4_swing", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("h4_swing", "flip_exit_enabled", False),
                exit_time_cycles=_exit_cfg("h4_swing", "time_exit_cycles", 864),
                exit_zscore_enabled=_exit_cfg("h4_swing", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("h4_swing", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("h4_swing", "min_valid_brains", 1),
                timeframe=_cfg("h4_swing", "timeframe", "H4"),
                exit_hesitation_cycles=_exit_cfg("h4_swing", "hesitation_cycles", 0),
            ),
            h4_swing_brains,
            budget=StrategyBudget(
                "h4_swing",
                daily_loss_limit_pct=_cfg("h4_swing", "budget", {}).get(
                    "daily_loss_limit_pct", -0.015
                ),
                max_consecutive_losses=_cfg("h4_swing", "budget", {}).get(
                    "max_consecutive_losses", 2
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
    regime_modulation: Any = None,
    trend_direction: str = "neutral",
    trend_strength: float = 0.0,
    h4_trend_strength: float = 0.0,
    macro_regime: str = "mixed",
    risk_budget_usd: float = 0.0,
    sl_streak_blocked_until: dict[str, float] | None = None,
    portfolio_risk: PortfolioRiskController,
    execution_queue: ExecutionQueue,
    tracker: Any,
    pnl_ledger: Any,
    current_positions: dict[str, dict[str, Any]],
    session_volume_mult: float = 1.0,
    health_volume_mult: float = 1.0,
    micro_sequences: dict[str, Any] | None = None,
    daily_feature_vector: Any = None,
    account_equity: float | None = None,
    cycle_count: int = 0,
    meta_signal_filter: Any = None,
    meta_filter_gate: Any = None,
    conformal_ou_gate: Any = None,
    micro_feature_dict: dict[str, float] | None = None,
    cooldown_registry: Any = None,
    family_entry_tracker: Any = None,
    mtf_price_service: Any = None,
    meta_feature_vector: Any = None,
) -> dict[str, Any]:
    """Run independent strategy evaluations + portfolio risk + execution queue.

    Returns a summary dict for logging.
    """
    decisions: list[Any] = []
    _blocked = sl_streak_blocked_until or {}
    strategy_results: list[dict[str, Any]] = []

    for sname, strategy in strategy_lines.items():
        gate_mode = "full"
        if regime_gate is not None:
            base_mode = regime_gate.get_strategy_mode(sname)
            if regime_modulation is not None and hasattr(regime_modulation, "strategy_activation"):
                from core.execution.regime_gate import get_stricter_mode

                gate_mode = get_stricter_mode(base_mode, regime_modulation.strategy_activation)
            else:
                gate_mode = base_mode

        # ── Per-strategy SL streak block ──
        if sname in _blocked and time.time() < _blocked[sname]:
            strategy_results.append(
                {"strategy": sname, "action": "blocked_sl_streak", "blocked_until": _blocked[sname]}
            )
            continue

        # ── M15 bar-boundary gating: only evaluate M15 strategies at 00/15/30/45 ──
        # The boundary check prevents future function leakage (incomplete M15 bars).
        # _effective_mid MUST be the current spot mid_price, NOT latest_m15_close.
        # Using a stale M15 bar close as the SL/TP entry reference creates a
        # price-reference mismatch: SL computed from historical close (~4572) but
        # fill executed at current spot (~4575) → effective SL cut by 2.5+ points.
        # See FIX-20260525-023 for full root cause analysis.
        _tf = getattr(getattr(strategy, "config", None), "timeframe", "M5")
        if _tf == "M15" and mtf_price_service is not None:
            _utc_minute = datetime.now(UTC).minute
            if not mtf_price_service.is_m15_boundary(_utc_minute):
                continue  # skip — M15 bar not yet complete, no future function leakage
        _effective_mid = mid_price

        # ── Cut 1: Absolute Refractory Period (cooldown check) ──
        if cooldown_registry is not None:
            _cd_allowed, _cd_reason = cooldown_registry.check_cooldown(
                sname,
                "long",  # direction unknown until evaluate runs; check both
            )
            if not _cd_allowed:
                # Cooldown may be direction-specific; still run evaluate but
                # reject the decision afterwards if direction matches
                pass

        # ── Cut 2: Family entry spacing check (pre-evaluate) ──
        if family_entry_tracker is not None:
            from core.execution.pre_trade_guards import strategy_to_family

            _fam = strategy_to_family(sname)
            if _fam != sname:  # only check family members
                # direction unknown pre-evaluate; check post-evaluate below
                pass

        # ── OU-augmented feature vector for meta-labeler strategy ──
        _fv = feature_vector
        if sname == "barrier_12bar_meta" and meta_feature_vector is not None:
            _fv = meta_feature_vector

        decision = strategy.evaluate(
            feature_vector=_fv,
            micro_feature_vector=micro_feature_vector,
            mid_price=_effective_mid,
            bid=bid,
            ask=ask,
            current_atr=current_atr,
            regime_info=regime_info,
            regime_gate_mode=gate_mode,
            trend_direction=trend_direction,
            trend_strength=trend_strength,
            h4_trend_strength=h4_trend_strength,
            macro_regime=macro_regime,
            risk_budget_usd=risk_budget_usd,
            tracker=tracker,
            pnl_ledger=pnl_ledger,
            pnl_store=pnl_ledger,
            micro_sequences=micro_sequences,
            daily_feature_vector=daily_feature_vector,
            meta_filter=meta_signal_filter,
            meta_filter_gate=meta_filter_gate,
            conformal_ou_gate=conformal_ou_gate,
            micro_feature_dict=micro_feature_dict,
        )

        # ── Cut 1: Post-evaluate cooldown check (direction known) ──
        if decision.should_trade and cooldown_registry is not None:
            _cd_allowed, _cd_reason = cooldown_registry.check_cooldown(sname, decision.direction)
            if not _cd_allowed:
                decision.should_trade = False
                decision.reason = _cd_reason
                print(
                    json.dumps(
                        {
                            "event": "cooldown_blocked",
                            "time": _utc_iso(),
                            "strategy": sname,
                            "direction": decision.direction,
                            "reason": _cd_reason,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        # ── Cut 2: Post-evaluate family spacing check (direction known) ──
        if decision.should_trade and family_entry_tracker is not None:
            from core.execution.pre_trade_guards import strategy_to_family

            _fam = strategy_to_family(sname)
            if _fam != sname:  # family member
                _fs_allowed, _fs_reason = family_entry_tracker.check_spacing(
                    _fam, decision.direction, sname
                )
                if not _fs_allowed:
                    decision.should_trade = False
                    decision.reason = _fs_reason
                    print(
                        json.dumps(
                            {
                                "event": "family_spacing_blocked",
                                "time": _utc_iso(),
                                "strategy": sname,
                                "family": _fam,
                                "direction": decision.direction,
                                "reason": _fs_reason,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

        # Apply session + health volume multipliers
        if decision.should_trade:
            combined_mult = session_volume_mult * health_volume_mult
            if combined_mult != 1.0:
                decision.volume = max(0.01, round(decision.volume * combined_mult, 2))

        strategy_results.append(
            {
                "strategy": sname,
                "should_trade": decision.should_trade,
                "direction": decision.direction,
                "confidence": decision.confidence,
                "volume": decision.volume,
                "p_win": getattr(decision, "p_win", 0.5),
                "kelly_mult": getattr(decision, "kelly_mult", 1.0),
                "regime_mode": gate_mode,
                "venue": getattr(decision, "venue", "live"),
                "reason": decision.reason,
                "supporting": decision.supporting_count,
                "total": decision.total_count,
            }
        )

        if not decision.should_trade:
            try:
                from core.runtime.gate_audit_recorder import record_gate_block

                record_gate_block(
                    strategy_name=sname,
                    direction=decision.direction,
                    reason=decision.reason,
                    gate_diag=getattr(decision, "gate_diag", None) or None,
                )
            except Exception:
                pass
            continue

        # Portfolio risk check
        risk_result = portfolio_risk.check(
            decision,
            current_positions,
            current_price=mid_price,
            account_equity=account_equity,
            current_cycle=cycle_count,
        )

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

        # Update current_positions snapshot so subsequent strategies see this
        # pending decision for same-direction concentration checks (H3 fix)
        current_positions[sname] = {
            "strategy": sname,
            "direction": decision.direction,
            "volume": risk_result.adjusted_volume
            if risk_result.adjusted_volume > 0
            else decision.volume,
            "ticket": 0,  # 0 = pending, not yet in MT5
            "entry_cycle": cycle_count,  # entry in current cycle
            "brain_ids": getattr(decision, "brain_ids", []),
        }

    # ── Tier 3: √N correlation discount ─────────────────────────────
    # When N strategies signal same direction on same symbol, total
    # position is discounted by 1/√N to prevent linear risk concentration.
    from core.execution.correlation_sizer import apply_sqrt_n_discount
    from core.execution.portfolio_risk import RiskVerdict

    _, sqrt_n_clusters = apply_sqrt_n_discount(decisions)

    # Update queued items for dropped strategies so flush() skips them
    dropped_names = {
        d.strategy_name
        for d in decisions
        if not d.should_trade and "sqrt_n_dropped" in getattr(d, "reason", "")
    }
    if dropped_names:
        for qd in execution_queue._queue:
            if qd.strategy_name in dropped_names:
                qd.risk_result.verdict = RiskVerdict.REJECTED
                qd.risk_result.reason = getattr(qd.decision, "reason", "sqrt_n_dropped")
        # Remove dropped strategies from current_positions snapshot
        for sname in list(current_positions.keys()):
            if sname in dropped_names:
                del current_positions[sname]
        # Update strategy_results entries for dropped strategies
        for sr in strategy_results:
            if sr.get("strategy", "") in dropped_names:
                for d in decisions:
                    if d.strategy_name == sr["strategy"]:
                        sr["should_trade"] = False
                        sr["reason"] = d.reason
                        sr["volume"] = 0.0
                        break

    # Log √N discount clusters for audit
    for cluster in sqrt_n_clusters:
        if cluster.dropped_strategies:
            print(
                json.dumps(
                    {
                        "event": "sqrt_n_discount",
                        "time": _utc_iso(),
                        "direction": cluster.direction,
                        "n_same_direction": cluster.n_same_direction,
                        "raw_total": cluster.raw_total_volume,
                        "discounted_total": cluster.discounted_volume,
                        "dropped": cluster.dropped_strategies,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

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
    mt5_worker: Any = None,
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
    daily_feature_provider: Any = None,
    journal_path: Path,
    pnl_ledger: Any = None,
    exit_watchdog: Any = None,
    limit_monitor: Any = None,
    meta_signal_filter: Any = None,
    degraded_wakeup: bool = False,
) -> tuple[LiveCycleState, bool]:
    """Execute one iteration of the live intent cycle.

    Args:
        mt5_worker: :class:`MT5Worker` singleton for all MT5 C++ calls.
            When None, attempts to resolve via ``get_mt5_worker()``.
            All MT5 operations execute on a single dedicated thread.
        broker: :class:`BrokerAdapter` for price/ATR/position queries.
            When None, falls back to *mt5_worker* calls.  This is the swap
            point for future FIX / cloud brokers.

    Returns (updated_state, should_continue). The caller owns the ``while True``
    loop and the ``time.sleep()`` between iterations.
    """
    state.loop_iteration += 1

    # ── Resolve MT5 worker (param takes priority, then global singleton) ──
    if mt5_worker is None and not config.no_mt5:
        from core.execution.mt5_worker import get_mt5_worker

        mt5_worker = get_mt5_worker()

    # ── Stash safeguard modules on state for access by internal helpers ──
    if exit_watchdog is not None:
        state.exit_watchdog = exit_watchdog
    if limit_monitor is not None:
        state.limit_monitor = limit_monitor

    # ── Cycle-start heartbeat (every iteration — catches freeze location) ──
    print(
        json.dumps(
            {"event": "cycle_start", "time": _utc_iso(), "iteration": state.loop_iteration},
            ensure_ascii=False,
        ),
        flush=True,
    )

    # ── Circuit breaker: 3 consecutive degraded cycles → management-only ──
    if state._circuit_breaker_tripped:
        print(
            json.dumps(
                {
                    "event": "circuit_breaker_active",
                    "time": _utc_iso(),
                    "consecutive_degraded": state._consecutive_degraded_cycles,
                    "mode": "management_only",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    # Reset on first non-degraded cycle
    if not degraded_wakeup and state._consecutive_degraded_cycles > 0:
        print(
            json.dumps(
                {
                    "event": "circuit_breaker_reset",
                    "time": _utc_iso(),
                    "previous_consecutive_degraded": state._consecutive_degraded_cycles,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        state._consecutive_degraded_cycles = 0
        state._circuit_breaker_tripped = False

    # ── Restart state bootstrap: replay recent journal closes ──
    # On restart, runtime state (_reentry_states, _pending_sl_records,
    # consecutive_sl_hits) is lost.  Replay the last 30 min of journal
    # close entries so the re-entry guard and graduated SL cooldown
    # survive a restart.
    if state.loop_iteration == 1 and not config.no_mt5:
        _bootstrap_restart_state(state, str(journal_path), config)

    # ── Daily ops auto-scheduler (The Highlander Rule) ──
    # Fixed UTC 22:00–23:00 window (= 06:00–07:00 CST, post-market-close).
    # Restore last-run state on first cycle; skip if already ran today.
    if state.loop_iteration == 1 and state._last_daily_ops_utc == 0:
        state._last_daily_ops_utc = _load_daily_ops_state(config.base_dir)
    try:
        _now_utc = datetime.now(UTC)
        _today_22z = _now_utc.replace(hour=22, minute=0, second=0, microsecond=0)
        _window_end = _today_22z + timedelta(hours=1)
        _last_date = (
            datetime.fromtimestamp(state._last_daily_ops_utc, UTC).date()
            if state._last_daily_ops_utc > 0
            else None
        )
        _already_ran_today = _last_date == _now_utc.date()
        if _today_22z <= _now_utc < _window_end and not _already_ran_today:
            _run_scheduled_daily_ops(config, state)
    except Exception:
        pass  # never let scheduling error disrupt the cycle

    # ── On first cycle, reconcile positions closed during downtime ──
    # Positions in known_open_tickets that are no longer open in MT5 were closed
    # (by SL/TP/external) while the process was down.  Run reconciliation BEFORE
    # filtering so close journal entries are created — otherwise they are silently
    # discarded and the trade journal has a permanent gap (no close entry).
    if state.loop_iteration == 1 and state.known_open_tickets and not config.no_mt5:
        try:
            _positions = mt5_worker.positions_get(symbol=config.symbol) or []
            _open_tickets = {p.ticket for p in _positions}
            _gone_tickets = set(state.known_open_tickets.keys()) - _open_tickets
            if _gone_tickets:
                _gone_dict = {
                    t: state.known_open_tickets[t]
                    for t in _gone_tickets
                    if t in state.known_open_tickets
                }
                try:
                    _closed_entries = _reconcile_closed_positions(
                        mt5_worker,
                        config.symbol,
                        str(journal_path),
                        _gone_dict,
                        state=state,
                    )
                    if _closed_entries:
                        from core.infrastructure.distributed_lock import (
                            FileLock,
                        )

                        _jlock = FileLock(
                            "live_trade_journal",
                            lock_dir=str(journal_path.parent / ".locks"),
                            ttl_seconds=10,
                        )
                        _jacquired = _jlock.acquire(blocking=True, timeout_seconds=5)
                        if _jacquired.acquired:
                            try:
                                _existing = (
                                    journal_path.read_text(encoding="utf-8")
                                    if journal_path.exists()
                                    else ""
                                )
                                with open(journal_path, "a", encoding="utf-8") as _jf:
                                    for _entry in _closed_entries:
                                        _mid = _entry.get("message_id", "")
                                        if _mid and _mid in _existing:
                                            continue
                                        _jf.write(json.dumps(_entry, ensure_ascii=False) + "\n")
                            finally:
                                _jlock.release()
                        # Update per-strategy SL streak from reconciled closes
                        for _entry in _closed_entries:
                            _label = _entry.get("label", "")
                            _strategy = _entry.get("strategy", "")
                            if _strategy:
                                _curr = state.consecutive_sl_hits.get(_strategy, 0)
                                if _label in ("sl_hit_first", "loss"):
                                    _curr += 1
                                elif _label in ("tp_hit_first", "win"):
                                    _curr = 0
                                state.consecutive_sl_hits[_strategy] = _curr
                except Exception:
                    pass  # best-effort — don't block startup
            # Filter to only currently-open positions AFTER reconciliation
            state.known_open_tickets = {
                t: r for t, r in state.known_open_tickets.items() if t in _open_tickets
            }
        except Exception:
            pass

        # ── Startup orphan detection: MT5 vs active_position.json ──
        try:
            _ap_path = os.path.join(config.base_dir, config.position_state_path)
            _ap_tickets: set[int] = set()
            if os.path.exists(_ap_path):
                with open(_ap_path) as _f:
                    _ap = json.load(_f)
                _ap_tickets = {
                    int(t) for t in (_ap.get("tickets", []) if isinstance(_ap, dict) else [])
                }
            _mt5_positions = mt5_worker.positions_get(symbol=config.symbol) or []
            _mt5_tickets = {p.ticket for p in _mt5_positions}
            _orphans = _mt5_tickets - _ap_tickets - set(state.known_open_tickets.keys())
            if _orphans:
                print(
                    json.dumps(
                        {
                            "event": "orphan_position_mismatch",
                            "time": _utc_iso(),
                            "severity": "HARD_BLOCK",
                            "orphan_tickets": sorted(_orphans),
                            "mt5_tickets": sorted(_mt5_tickets),
                            "active_position_tickets": sorted(_ap_tickets),
                            "known_open_tickets": sorted(state.known_open_tickets.keys()),
                            "action": "refusing_to_start",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return state, False  # refuse to start
        except json.JSONDecodeError:
            print(
                json.dumps(
                    {
                        "event": "orphan_detection_json_error",
                        "time": _utc_iso(),
                        "severity": "ERROR",
                        "file": _ap_path,
                        "message": "active_position.json is corrupt or empty; "
                        "treating as no tracked positions",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as _exc:
            print(
                json.dumps(
                    {
                        "event": "orphan_detection_failed",
                        "time": _utc_iso(),
                        "severity": "ERROR",
                        "error": f"{type(_exc).__name__}: {_exc}",
                        "message": "orphan detection skipped — manual review recommended",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

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
                mt5_worker, config.symbol, str(journal_path), state.known_open_tickets, state=state
            )
            if _closed:
                from core.infrastructure.distributed_lock import FileLock

                _jlock = FileLock(
                    "live_trade_journal",
                    lock_dir=str(journal_path.parent / ".locks"),
                    ttl_seconds=10,
                )
                _jacquired = _jlock.acquire(blocking=True, timeout_seconds=5)
                if _jacquired.acquired:
                    try:
                        _existing = (
                            journal_path.read_text(encoding="utf-8")
                            if journal_path.exists()
                            else ""
                        )
                        with open(journal_path, "a", encoding="utf-8") as _jf:
                            for _entry in _closed:
                                _mid = _entry.get("message_id", "")
                                if _mid and _mid in _existing:
                                    continue
                                _jf.write(json.dumps(_entry, ensure_ascii=False) + "\n")
                    finally:
                        _jlock.release()
                # ── Update per-strategy losing-streak tracker ──
                for _entry in _closed:
                    _label = _entry.get("label", "")
                    _entry_brain_ids = _entry.get("brain_ids", [])
                    # Prefer the strategy field from known_open_tickets; fall back
                    # to brain-id inference for pre-existing journal entries.
                    _strategy = _entry.get("strategy", "") or _strategy_from_brain_ids(
                        _entry_brain_ids
                    )
                    _curr = state.consecutive_sl_hits.get(_strategy, 0)
                    if _label in ("sl_hit_first", "loss"):
                        _curr += 1
                        # ── Collect SL event for per-strategy graduated cooldown ──
                        state._pending_sl_records.append(
                            {
                                "strategy": _strategy,
                                "timestamp": time.time(),
                            }
                        )
                    elif _label in ("tp_hit_first", "win"):
                        _curr = 0
                    state.consecutive_sl_hits[_strategy] = _curr

                    # Update portfolio risk VaR buffer with realised P&L
                    if state.portfolio_risk_controller is not None:
                        _pnl = _entry.get("pnl")
                        if _pnl is not None:
                            try:
                                state.portfolio_risk_controller.update_returns(
                                    _strategy, float(_pnl)
                                )
                            except Exception:
                                pass

                    # ── Collect for per-strategy budget recording (processed after
                    #     strategies are built, since budgets live on StrategyLine) ──
                    _pnl_val = _entry.get("pnl")
                    if _pnl_val is not None:
                        # Convert dollar PnL to percentage of account equity
                        _pnl_pct = 0.0
                        try:
                            _acc = mt5_worker.account_info() if mt5_worker is not None else None
                            _eq = float(getattr(_acc, "equity", 0)) if _acc is not None else 0.0
                            if _eq > 0:
                                _pnl_pct = float(_pnl_val) / _eq
                        except Exception:
                            _pnl_pct = float(_pnl_val) / 1000.0  # fallback: assume $1k account
                        state._pending_budget_records.append(
                            {
                                "strategy": _strategy,
                                "pnl": _pnl_pct,
                                "is_win": _label in ("tp_hit_first", "win"),
                            }
                        )

                    # Per-strategy block: 3 consecutive SL → 30 min pause for THIS strategy
                    if _curr >= 3:
                        state.sl_streak_blocked_until[_strategy] = time.time() + 1800
                        print(
                            json.dumps(
                                {
                                    "event": "sl_streak_blocked",
                                    "time": _utc_iso(),
                                    "strategy": _strategy,
                                    "consecutive_sl": _curr,
                                    "blocked_until_utc": datetime.fromtimestamp(
                                        state.sl_streak_blocked_until[_strategy], tz=UTC
                                    ).isoformat(),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )

                    # Global block: 3 strategies each with ≥2 SL → pause ALL (market regime shift)
                    # Max 3600s timeout — recovery checked per-cycle via market state
                    _blocked_count = sum(1 for v in state.consecutive_sl_hits.values() if v >= 2)
                    if _blocked_count >= 3:
                        state.sl_streak_blocked_all_until = time.time() + 3600

                # Remove closed tickets from tracking
                for _entry in _closed:
                    _ticket = _entry.get("position_ticket")
                    if _ticket is not None:
                        state.known_open_tickets.pop(_ticket, None)

                # Sync position_manager: clear positions that were closed by MT5
                if state.position_manager is not None and state.position_manager.has_position():
                    for _pm_pos in list(state.position_manager.get_all_positions()):
                        if _pm_pos.ticket not in state.known_open_tickets:
                            state.position_manager.clear_position(ticket=_pm_pos.ticket)
                            print(
                                json.dumps(
                                    {
                                        "event": "position_manager_synced_clear",
                                        "time": _utc_iso(),
                                        "ticket": _pm_pos.ticket,
                                        "reason": "mt5_already_closed",
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
                            "sl_streak_by_strategy": dict(state.consecutive_sl_hits),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        except Exception:
            pass

    # ── Protection flag check ──
    from core.execution.live_order_sender import resolve_protection_flag_path

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
        _log_cycle_end(state.loop_iteration)
        return state, True  # continue
    state.flag_notice = False

    # ── Fetch mid-price (MUST run before cooldown / SL-streak early returns
    #    so that strategy evaluation always has a valid entry price, even when
    #    pnl_ledger is not configured)
    mid_price: float | None = None
    _bid: float | None = None
    _ask: float | None = None
    try:
        if broker is not None:
            mid_price, _bid, _ask = broker.fetch_prices(config.symbol)
        elif not config.no_mt5:
            mid_price, _bid, _ask = _mid_and_prices(mt5_worker, config.symbol)
    except Exception:
        pass

    # ── Rolling mid-price buffer (circuit breaker & ER calc) ──
    if mid_price is not None and mid_price > 0:
        state._recent_mid_prices.append(mid_price)
        if len(state._recent_mid_prices) > 50:
            state._recent_mid_prices.pop(0)

    # ── MTF Price Service: M15 bar reconstruction from M5 tick history ──
    if not hasattr(state, "_mtf_price_service") or state._mtf_price_service is None:
        state._mtf_price_service = MTFPriceService()
        # Bootstrap from historical M5 closes so M15 bars are available immediately
        if not config.no_mt5 and mt5_worker is not None:
            try:
                _hist_rates = mt5_worker.copy_rates_from_pos(
                    config.symbol, 5, 0, 200
                )  # TIMEFRAME_M5
                if _hist_rates is not None and len(_hist_rates) >= 6:
                    _closes = [float(r[4]) for r in _hist_rates]
                    state._mtf_price_service.bootstrap(_closes)
            except Exception:
                pass
    if mid_price is not None and mid_price > 0 and state._mtf_price_service is not None:
        try:
            _now_s = int(datetime.now(UTC).timestamp())
            state._mtf_price_service.feed_tick(_now_s, mid_price)
        except Exception:
            pass

    # ── Tick sanity check ──
    if _bid is not None and _ask is not None and _bid > 0:
        try:
            from core.execution.pre_trade_guards import check_tick_sanity

            tick_ok = check_tick_sanity(_bid, _ask, config.symbol)
            if not tick_ok["passed"]:
                print(
                    json.dumps(
                        {
                            "event": "tick_sanity_failed",
                            "time": _utc_iso(),
                            "bid": tick_ok["bid"],
                            "ask": tick_ok["ask"],
                            "spread_bps": tick_ok["spread_bps"],
                            "issues": tick_ok["issues"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        except Exception:
            pass

    # ── Limit order monitor: check pending orders for spread-aware fills ──
    if state.limit_monitor is not None and state.limit_monitor.has_pending():
        try:
            _lom_bid = _bid if _bid else 0.0
            _lom_ask = _ask if _ask else 0.0
            _lom_low = None  # bar low not tracked cycle-by-cycle; fill uses bid/ask
            _lom_high = None
            _lom_spread = round((_lom_ask - _lom_bid) * 10000, 1) if _lom_bid > 0 else 0.0
            fill = state.limit_monitor.check_fill(
                current_bar=state.loop_iteration,
                bid=_lom_bid,
                ask=_lom_ask,
                spread_points=_lom_spread,
                low=_lom_low,
                high=_lom_high,
            )
            if fill.filled:
                print(
                    json.dumps(
                        {
                            "event": "limit_order_filled",
                            "time": _utc_iso(),
                            "intent_id": fill.intent_id,
                            "fill_price": fill.fill_price,
                            "fill_bar": fill.fill_bar,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            elif fill.should_cancel:
                print(
                    json.dumps(
                        {
                            "event": "limit_order_expired",
                            "time": _utc_iso(),
                            "intent_id": fill.intent_id,
                            "cancel_reason": fill.cancel_reason,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
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
        _log_cycle_end(state.loop_iteration)
        return state, True  # continue

    # ── SL streak circuit breaker (global — all strategies blocked) ──
    _cb_now = time.time()
    if state.sl_streak_blocked_all_until > 0 and _cb_now < state.sl_streak_blocked_all_until:
        # Check market-state recovery after 5 min cooling-off
        _blocked_for = _cb_now - (state.sl_streak_blocked_all_until - 3600)
        _recovered = False
        _recovery_reason = ""
        if _blocked_for > 300 and state._recent_atr_values and state._recent_mid_prices:
            try:
                import numpy as np

                from core.execution.market_efficiency import (
                    check_market_normalized,
                    compute_kaufman_er,
                )

                _atr_vals = np.array(state._recent_atr_values[-20:], dtype=np.float64)
                _atr_mean = float(np.mean(_atr_vals))
                _atr_std = float(np.std(_atr_vals))
                _current_atr = float(_atr_vals[-1])
                _er = compute_kaufman_er(state._recent_mid_prices, period=10)
                _recovered, _recovery_reason = check_market_normalized(
                    current_atr=_current_atr,
                    rolling_atr_mean=_atr_mean,
                    rolling_atr_std=_atr_std,
                    kaufman_er=_er,
                )
            except Exception:
                pass

        if _recovered:
            print(
                json.dumps(
                    {
                        "event": "sl_streak_recovered_state",
                        "time": _utc_iso(),
                        "recovery_reason": _recovery_reason,
                        "blocked_seconds": round(_blocked_for, 1),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            state.sl_streak_blocked_all_until = 0.0
            state.consecutive_sl_hits.clear()
        else:
            if state.loop_iteration % 10 == 0:
                print(
                    json.dumps(
                        {
                            "event": "sl_streak_block_active_global",
                            "time": _utc_iso(),
                            "sl_streak_by_strategy": dict(state.consecutive_sl_hits),
                            "blocked_seconds": round(_blocked_for, 1),
                            "recovery_check": _recovery_reason or "cooling_off",
                            "blocked_until_utc": datetime.fromtimestamp(
                                state.sl_streak_blocked_all_until, tz=UTC
                            ).isoformat(),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            _log_cycle_end(state.loop_iteration)
            return state, True  # continue
    elif state.sl_streak_blocked_all_until > 0 and _cb_now >= state.sl_streak_blocked_all_until:
        # Max timeout expired — unconditional unblock
        print(
            json.dumps(
                {
                    "event": "sl_streak_unblocked_timeout",
                    "time": _utc_iso(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        state.sl_streak_blocked_all_until = 0.0
        state.consecutive_sl_hits.clear()

    # ── Per-strategy block expiry ──
    _now_ts = time.time()
    for _sname in list(state.sl_streak_blocked_until.keys()):
        if _now_ts >= state.sl_streak_blocked_until[_sname]:
            del state.sl_streak_blocked_until[_sname]
            if _sname in state.consecutive_sl_hits:
                state.consecutive_sl_hits[_sname] = 0

    # ── Journal-based SL streak check (per-strategy, bypasses reconciliation timing) ──
    from core.contracts.strategy_magic import STRATEGY_TO_MAGIC as _ALL_STRATEGIES

    for _sname in sorted(_ALL_STRATEGIES.keys()):
        if _sname not in state.sl_streak_blocked_until:
            _blocked, _streak_count = _check_recent_sl_streak(
                str(journal_path), lookback_seconds=300.0, threshold=3, strategy_name=_sname
            )
            if _blocked:
                state.sl_streak_blocked_until[_sname] = time.time() + 1800
                state.consecutive_sl_hits[_sname] = max(
                    state.consecutive_sl_hits.get(_sname, 0), _streak_count
                )
                print(
                    json.dumps(
                        {
                            "event": "sl_streak_blocked_journal",
                            "time": _utc_iso(),
                            "strategy": _sname,
                            "consecutive_sl": _streak_count,
                            "blocked_until_utc": datetime.fromtimestamp(
                                state.sl_streak_blocked_until[_sname], tz=UTC
                            ).isoformat(),
                            "source": "journal_scan_pre_dispatch",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                _log_cycle_end(state.loop_iteration)
                return state, True  # continue (skip cycle — strategy is blocked)

    # ── Position limit check ──
    if not config.no_mt5:
        try:
            pos_count = (
                broker.count_positions(config.symbol)
                if broker is not None
                else _position_count(mt5_worker, config.symbol)
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
                if mt5_worker is not None:
                    mt5_worker.reconnect()
                    pos_count = (
                        broker.count_positions(config.symbol)
                        if broker is not None
                        else _position_count(mt5_worker, config.symbol)
                    )
            except Exception:
                pass

        # Still unknown after reconnect — fall back to position manager cache
        # instead of skipping the cycle entirely.  MT5 connection is flaky on
        # Windows multi-process setups; the position manager has authoritative
        # local state and is always correct for positions we opened.
        if pos_count < 0:
            _pm_count = 0
            if state.position_manager is not None and state.position_manager.has_position():
                _pm_count = len(state.position_manager.get_all_positions())
            pos_count = _pm_count
            if state.loop_iteration % 5 == 0:
                print(
                    json.dumps(
                        {
                            "event": "position_count_fallback",
                            "time": _utc_iso(),
                            "detail": "MT5 connection lost, using position manager cache",
                            "fallback_count": _pm_count,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        # ── Market-closed guard (before exit management and entry logic) ──
        if not config.no_mt5:
            try:
                from core.execution.pre_trade_guards import detect_session

                _pre_session = detect_session()
                if _pre_session.get("risk_tier") == "off":
                    _log_cycle_end(state.loop_iteration)
                    return state, True  # market closed — skip entire cycle
            except Exception:
                pass

        # ── Global P&L settlement anchor (护栏二: 唯一结算点) ──
        # All safety guards have passed.
        # Step A: Update MFE/MAE for all pending signals + decrement TTL (Track 2).
        # Step B: Settle only signals whose horizon-matched TTL has expired (Track 1).
        if (
            pnl_ledger is not None
            and mid_price is not None
            and mid_price > 0
            and pnl_ledger.pending_count > 0
        ):
            try:
                _live_spread = float(_ask - _bid) if (_bid and _ask and _ask > _bid) else 0.0
                pnl_ledger.update_pending(mid_price)
                pnl_ledger.settle_all(mid_price, spread=_live_spread, slippage=0.10)
            except Exception:
                pass

        # ── Dynamic exit management phase ──
        # Runs whenever positions are registered, regardless of position limit.
        if (
            config.exit_management_enabled
            and state.position_manager is not None
            and state.position_manager.has_position()
        ):
            try:
                for _pm_pos in list(state.position_manager.get_all_positions()):
                    _execute_management_phase(
                        config,
                        state,
                        mt5_worker=mt5_worker,
                        broker=broker,
                        brains=brains,
                        parliament=parliament,
                        regime_detector=regime_detector,
                        tracker=tracker,
                        feature_service=feature_service,
                        micro_feature_computer=micro_feature_computer,
                        micro_feature_adapter=micro_feature_adapter,
                        daily_feature_provider=daily_feature_provider,
                        ticket=_pm_pos.ticket,
                    )
            except Exception:
                pass
            # Persist position state every N cycles (trail steps, breakeven, etc.)
            if state.loop_iteration % 5 == 0 and state.position_manager is not None:
                try:
                    state.position_manager.save_state(config.position_state_path)
                except Exception:
                    pass

        # ── Process MIA close entries collected by _execute_management_phase ──
        # FIX-20260525-024: When a position disappears from MT5 between
        # reconciliation cycles, the management phase detects it and stores
        # a close entry in _pending_mia_closes.  We must write these to the
        # journal and record them for reentry guard, otherwise:
        #   - Journal has no close entry → PnL hole
        #   - Reentry guard gets unknown_exit → permanent block
        #   - Position state file stays stale
        if state._pending_mia_closes:
            _mia_closed = state._pending_mia_closes
            state._pending_mia_closes = []
            # ── Write to journal (same FileLock pattern as reconciliation) ──
            try:
                from core.infrastructure.distributed_lock import FileLock

                _jlock = FileLock(
                    "live_trade_journal",
                    lock_dir=str(journal_path.parent / ".locks"),
                    ttl_seconds=10,
                )
                _jacquired = _jlock.acquire(blocking=True, timeout_seconds=5)
                if _jacquired.acquired:
                    try:
                        _existing = (
                            journal_path.read_text(encoding="utf-8")
                            if journal_path.exists()
                            else ""
                        )
                        with open(journal_path, "a", encoding="utf-8") as _jf:
                            for _entry in _mia_closed:
                                _mid = _entry.get("message_id", "")
                                if _mid and _mid in _existing:
                                    continue
                                _jf.write(json.dumps(_entry, ensure_ascii=False) + "\n")
                    finally:
                        _jlock.release()
            except Exception:
                pass
            # ── Record exit for reentry guard ──
            for _entry in _mia_closed:
                _exit_strategy = _entry.get("strategy", "")
                _exit_side = _entry.get("side", "")
                _exit_price = float(_entry.get("detail", {}).get("close_price", 0) or 0)
                _exit_ts_str = _entry.get("recorded_at", "")
                _exit_ts = time.time()
                if _exit_ts_str:
                    try:
                        _parsed = datetime.fromisoformat(_exit_ts_str.replace("Z", "+00:00"))
                        _exit_ts = _parsed.timestamp()
                    except Exception:
                        pass
                _exit_confidence = (
                    _entry.get("entry_consensus", {}).get("consensus_score", 0.5)
                    if isinstance(_entry.get("entry_consensus"), dict)
                    else 0.5
                )
                _exit_reason = _entry.get("detail", {}).get("reason", "mia_close")
                if _exit_strategy and _exit_side in ("long", "short"):
                    try:
                        from core.execution.reentry_guard import (
                            ExitRecord,
                            ensure_reentry_state,
                        )

                        _mia_rec = ExitRecord(
                            timestamp=_exit_ts,
                            strategy_name=_exit_strategy,
                            direction=_exit_side,
                            reason=_exit_reason,
                            confidence=float(_exit_confidence),
                            price=_exit_price,
                            ticket=_entry.get("position_ticket", 0),
                        )
                        _rs = ensure_reentry_state(state._reentry_states, _exit_strategy)
                        _rs.record_exit(_mia_rec)
                        print(
                            json.dumps(
                                {
                                    "event": "mia_close_reentry_recorded",
                                    "time": _utc_iso(),
                                    "ticket": _entry.get("position_ticket"),
                                    "strategy": _exit_strategy,
                                    "direction": _exit_side,
                                    "reason": _exit_reason,
                                    "close_price": _exit_price,
                                    "pnl": _entry.get("pnl"),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                    except Exception:
                        pass
            # ── Save position state immediately ──
            if state.position_manager is not None:
                try:
                    state.position_manager.save_state(config.position_state_path)
                except Exception:
                    pass

    # ── Degraded wakeup guard: skip Alpha computation, management only ──
    if degraded_wakeup:
        print(
            json.dumps(
                {
                    "event": "bar_sync_degraded_alpha_skip",
                    "time": _utc_iso(),
                    "iteration": state.loop_iteration,
                    "action": "skipping_feature_computation_and_strategy_eval",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        _log_cycle_end(state.loop_iteration)
        return state, True

    # ── Compute features ──
    # Moved BEFORE position-limit check so the feature store stays fresh even
    # when every strategy is at max positions.  A stale store silently degrades
    # signal quality on the next cycle that DOES trade.
    micro_sequences: dict[str, np.ndarray] = {}
    micro_feature_dict: dict[str, float] | None = None
    if config.no_mt5:
        feature_vector: Any = np.zeros(40, dtype=np.float64)
        micro_feature_vector: Any = np.zeros(9, dtype=np.float64)
    else:
        trigger = {"symbol": config.symbol, "venue": "MT5"}
        feature_vector = feature_service.build_feature_vector(trigger)

        # Compute microstructure 9-feature vector for Transformer/XGBoost brains
        if micro_feature_computer is not None and micro_feature_adapter is not None:
            try:
                micro_sequences = micro_feature_computer.compute_all_sequences(32)
            except Exception:
                pass
            try:
                micro_features = micro_feature_computer.compute_all()
                micro_feature_dict = micro_features
                micro_feature_vector = micro_feature_adapter.build_model_input(
                    micro_features
                ).ravel()
            except Exception:
                micro_feature_dict = {}
                micro_feature_vector = np.zeros(9, dtype=np.float64)
        else:
            micro_feature_vector = np.zeros(9, dtype=np.float64)

    # ── Meta-filter gate + Conformal OU Gate (lazy init on first live cycle) ──
    # Both gates share one ConformalCalibrator so the empirical P(win)
    # distribution benefits from all closed trades regardless of which
    # gate approved the signal.
    if not config.no_mt5 and getattr(state, "_meta_filter_gate", None) is None:
        # ── Shared calibrator (Track 3d: Q10 FIFO adaptive threshold) ──
        _cal = None
        try:
            from core.execution.conformal_calibrator import ConformalCalibrator

            _cal = ConformalCalibrator(
                state_path="data/conformal_calibrator_state.json",
            )
            _cal.cold_start_from_journal("data/live_trade_journal.jsonl")
        except Exception as _cal_exc:
            print(
                json.dumps(
                    {
                        "event": "conformal_calibrator_init_error",
                        "time": _utc_iso(),
                        "error": str(_cal_exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        # ── MetaFilterGate (47-dim LGB, for non-OU strategies if any) ──
        try:
            from core.execution.meta_filter_gate import MetaFilterGate

            _mg = MetaFilterGate(
                model_dir="data/models/meta_filter_v3",
                threshold=META_FILTER_GATE_THRESHOLD,
                calibrator=_cal,
            )
            _mg.load()
            if _mg.is_loaded:
                state._meta_filter_gate = _mg
                cal_diag = _cal.describe() if _cal is not None else {}
                print(
                    json.dumps(
                        {
                            "event": "meta_filter_gate_init",
                            "time": _utc_iso(),
                            "threshold": META_FILTER_GATE_THRESHOLD,
                            "model": "meta_filter_v3",
                            "conformal_samples": cal_diag.get("sample_count", 0),
                            "conformal_warm": cal_diag.get("is_warm", False),
                            "conformal_threshold": cal_diag.get("current_threshold"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        except Exception as _mg_exc:
            print(
                json.dumps(
                    {
                        "event": "meta_filter_gate_init_error",
                        "time": _utc_iso(),
                        "error": str(_mg_exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        # ── Conformal OU Gate (physics-based, for OU strategies) ──
        try:
            from core.execution.conformal_ou_gate import ConformalOUGate

            _ou_gate = ConformalOUGate(calibrator=_cal)
            _ou_gate.load_ou_configs()
            if _ou_gate.is_loaded:
                state._conformal_ou_gate = _ou_gate
                _ou_diag = _ou_gate.describe()
                print(
                    json.dumps(
                        {
                            "event": "conformal_ou_gate_init",
                            "time": _utc_iso(),
                            "strategies": _ou_diag.get("strategies", []),
                            "ou_configs": _ou_diag.get("ou_configs", {}),
                            "base_threshold": _ou_diag.get("base_threshold"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            else:
                print(
                    json.dumps(
                        {
                            "event": "conformal_ou_gate_init_warning",
                            "time": _utc_iso(),
                            "reason": "no OU brain configs found — gate disabled",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        except Exception as _oug_exc:
            print(
                json.dumps(
                    {
                        "event": "conformal_ou_gate_init_error",
                        "time": _utc_iso(),
                        "error": str(_oug_exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    # ── Daily D1 features for swing brains ──
    daily_feature_vector: Any = None
    if daily_feature_provider is not None:
        try:
            daily_feature_vector = daily_feature_provider.get_latest()
        except Exception:
            daily_feature_vector = None

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

    # ── Feature freshness check (cycle-level visible alert) ──
    if not config.no_mt5 and feature_store is not None:
        try:
            from core.execution.pre_trade_guards import check_feature_freshness

            latest_record = feature_store.latest(config.symbol, "M5")
            if latest_record is not None:
                ts = getattr(latest_record, "event_time", None)
                if ts is not None:
                    if hasattr(ts, "timestamp"):
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=UTC)
                        ts_unix = ts.timestamp()
                    else:
                        ts_unix = float(ts)
                    freshness = check_feature_freshness(ts_unix, max_age_seconds=300.0)
                    if not freshness["fresh"]:
                        print(
                            json.dumps(
                                {
                                    "event": "feature_stale_warning",
                                    "time": _utc_iso(),
                                    "age_seconds": freshness.get("age_seconds"),
                                    "max_age_seconds": freshness["max_age_seconds"],
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
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
        _log_cycle_end(state.loop_iteration)
        return state, True  # continue (skip entry logic)

    # ── Market regime detection ──
    regime_info: dict[str, Any] = {}
    if not config.no_mt5 and regime_detector is not None:
        try:
            current_atr = (
                broker.fetch_current_atr(config.symbol)
                if broker is not None
                else _get_current_atr(mt5_worker, config.symbol)
            )
            if current_atr > 0:
                regime_info = regime_detector.update(current_atr)
                # Rolling ATR buffer for adaptive circuit breaker
                state._recent_atr_values.append(current_atr)
                if len(state._recent_atr_values) > 50:
                    state._recent_atr_values.pop(0)
        except Exception:
            pass

    # ── Feature gate: block garbage-in before it becomes garbage-out ──
    if not config.no_mt5:
        try:
            from core.runtime.signal_health import FeatureGate

            _gate = FeatureGate.check(
                feature_vector=feature_vector,
                micro_vector=micro_feature_vector,
                atr=current_atr,
                mid_price=mid_price or 0.0,
            )
            if not _gate.passed:
                print(
                    json.dumps(
                        {
                            "event": "feature_gate_blocked",
                            "time": _utc_iso(),
                            "reason_code": _gate.reason_code,
                            "detail": _gate.detail,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                _log_cycle_end(state.loop_iteration)
                return state, True
        except Exception:
            pass  # gate itself should never crash the cycle

    # ── Run inference ──
    raw_output: dict[str, Any] = {}
    proposal: Any = None
    proposals: list[Any] = []
    raw_proposals: list[Any] = []
    consensus_extra: dict[str, Any] = {}
    control_snapshot: Any = None

    dynamic_volume = config.volume or 0.01
    _vol_targeted = False

    # ── Fetch account equity (used for risk budget + portfolio VaR threshold) ──
    _account_equity: float | None = None
    try:
        if broker is not None:
            _account_equity = broker.get_account_equity()
        elif mt5_worker is not None:
            _acc = mt5_worker.account_info()
            _account_equity = float(getattr(_acc, "equity", 0)) if _acc is not None else 0.0
    except Exception:
        pass

    # ── Equity-based risk budget: overrides fixed risk_budget_usd when equity_risk_pct > 0 ──
    _effective_risk_budget = config.risk_budget_usd
    if config.equity_risk_pct > 0 and _account_equity is not None and _account_equity > 0:
        _effective_risk_budget = round(_account_equity * config.equity_risk_pct, 2)

    # Vol-targeted position sizing — override fixed volume when risk_budget_usd > 0
    if _effective_risk_budget > 0 and current_atr > 0:
        try:
            from core.execution.pre_trade_guards import compute_position_size

            dynamic_volume = compute_position_size(
                risk_budget_usd=_effective_risk_budget,
                atr=current_atr,
                sl_atr_mult=config.sl_atr_mult,
                min_lot=config.min_lot,
                max_lot=config.max_lot,
                lot_step=config.lot_step,
            )
            _vol_targeted = True
        except Exception:
            pass  # fallback to fixed volume

    if config.multi_brain and config.multi_strategy_enabled:
        # ── NEW: Multi-strategy independent evaluation ──
        # Each contract group runs independently → portfolio risk → staggered dispatch

        # Partition brains into contract groups and build strategy lines
        strategies = _build_strategy_lines(brains, config)

        # ── Feed pending budget records from reconciliation ──
        if state._pending_budget_records:
            for _rec in state._pending_budget_records:
                _sname = _rec["strategy"]
                _strat = strategies.get(_sname)
                if _strat is not None and _strat.budget is not None:
                    try:
                        _strat.budget.record_trade(_rec["pnl"], _rec["is_win"])
                    except Exception:
                        pass
            state._pending_budget_records.clear()

        # ── Feed pending SL records for graduated per-SL cooldown ──
        if state._pending_sl_records:
            for _rec in state._pending_sl_records:
                _sname = _rec["strategy"]
                _strat = strategies.get(_sname)
                if _strat is not None and _strat.budget is not None:
                    try:
                        _result = _strat.budget.record_sl(_rec.get("timestamp"))
                        if _result.get("event") != "sl_recorded":
                            print(
                                json.dumps(_result, ensure_ascii=False),
                                flush=True,
                            )
                    except Exception:
                        pass
            state._pending_sl_records.clear()
        elif len(state._pending_sl_records) > 100:
            # Safety valve: prevent unbounded growth in single-brain / no-strategy mode
            state._pending_sl_records = state._pending_sl_records[-50:]

        # ── Regime gate: persist across cycles, feed M5+H1 bars ──
        if state.regime_gate is None:
            state.regime_gate = RegimeGate(regime_map=config.regime_map)
            _rm_loaded = config.regime_map is not None
            _rm_strategies = sorted({s for m in (config.regime_map or {}).values() for s in m})
            print(
                json.dumps(
                    {
                        "event": "regime_gate_init",
                        "time": _utc_iso(),
                        "regime_map_loaded": _rm_loaded,
                        "strategies_in_map": _rm_strategies,
                        "num_regimes": len(config.regime_map) if config.regime_map else 0,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if not config.no_mt5:
                _bootstrap_regime_gate(mt5_worker, config.symbol, state.regime_gate)

        regime_gate: RegimeGate | None = state.regime_gate
        regime_gate_result: dict[str, Any] = {}
        regime_modulation: Any = None
        trend_direction: str = "neutral"
        trend_strength: float = 0.0
        h4_trend_strength: float = 0.0
        macro_regime: str = "mixed"

        if not config.no_mt5 and regime_gate is not None:
            try:
                _feed_regime_gate_cycle(mt5_worker, config.symbol, regime_gate)
                atr_val = current_atr if current_atr > 0 else 5.0
                atr_pct = regime_info.get("atr_pct", 0.5) if regime_info else 0.5
                vol_pct = regime_info.get("vol_pct", 0.5) if regime_info else 0.5
                vol_regime = regime_info.get("regime", "normal") if regime_info else "normal"
                regime_gate_result = regime_gate.classify(
                    atr_val, atr_pct, vol_pct=vol_pct, vol_regime=vol_regime
                )
                state._regime_gate_stale_counter = 0  # successful classify resets stale counter
                if regime_info:
                    regime_info["regime_gate"] = regime_gate_result
                trend_direction = regime_gate_result.get("primary_trend", "neutral")
                trend_strength = regime_gate_result.get("h1_trend_strength", 0.0)
                h4_trend_strength = regime_gate_result.get("h4_trend_strength", 0.0)
                macro_regime = regime_gate_result.get("macro_regime", "mixed")
                regime_modulation = regime_gate_result.get("modulation")
                # ── Per-cycle regime gate diagnostic (FIX-20260527-004) ──
                _global_act = (
                    regime_modulation.strategy_activation
                    if regime_modulation is not None
                    and hasattr(regime_modulation, "strategy_activation")
                    else None
                )
                print(
                    json.dumps(
                        {
                            "event": "regime_gate_cycle",
                            "time": _utc_iso(),
                            "detected_regime": regime_gate_result.get("regime", "?"),
                            "strategy_gates": regime_gate_result.get("strategy_gates", {}),
                            "global_activation": _global_act,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as _rg_exc:
                state._regime_gate_stale_counter += 1
                _stale_n = state._regime_gate_stale_counter
                if _stale_n > 12:  # 1 hour at M5 — fail-closed
                    regime_gate = RegimeGate.default_fail_closed()
                    _action = "fail_closed_all_shadow"
                elif state.regime_gate is not None:
                    regime_gate = state.regime_gate
                    _action = f"using_last_valid_stale_{_stale_n}"
                else:
                    regime_gate = RegimeGate.default_fail_closed()
                    _action = "fail_closed_no_prior"
                print(
                    json.dumps(
                        {
                            "event": "regime_gate_failed",
                            "time": _utc_iso(),
                            "error": str(_rg_exc),
                            "error_type": type(_rg_exc).__name__,
                            "action": _action,
                            "stale_counter": _stale_n,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

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
            _log_cycle_end(state.loop_iteration)
            return state, True  # continue (skip new position entry)

        # ── Session detection + data quality guards ──
        session_info: dict[str, Any] = {}
        if not config.no_mt5:
            try:
                from core.execution.pre_trade_guards import check_feature_vector, detect_session

                session_info = detect_session()
                if session_info.get("risk_tier") == "off":
                    _log_cycle_end(state.loop_iteration)
                    return state, True  # market closed, skip cycle

                # Intraday drawdown kill switch — tracks equity peak-to-trough
                if config.intraday_drawdown_kill_enabled:
                    try:
                        from core.execution.pre_trade_guards import IntradayDrawdownKill

                        if state.intraday_dd_kill is None:
                            state.intraday_dd_kill = IntradayDrawdownKill(
                                kill_pct=config.intraday_drawdown_kill_pct,
                                force_close_enabled=config.intraday_dd_force_close,
                                force_close_pct=config.intraday_dd_force_close_pct,
                            )
                        # Fetch current equity from MT5 account
                        _acc = mt5_worker.account_info()
                        if _acc is not None:
                            _eq = float(getattr(_acc, "equity", 0))
                            dd_result = state.intraday_dd_kill.update(_eq)
                            if dd_result.get("blocked"):
                                print(
                                    json.dumps(
                                        {
                                            "event": "intraday_drawdown_kill",
                                            "time": _utc_iso(),
                                            "drawdown_pct": dd_result["drawdown_pct"],
                                            "high_watermark": dd_result["high_watermark"],
                                            "current_equity": dd_result["current_equity"],
                                            "force_close": dd_result.get("force_close", False),
                                        },
                                        ensure_ascii=False,
                                    ),
                                    flush=True,
                                )
                                # Force-close existing positions when drawdown is severe
                                if (
                                    dd_result.get("force_close")
                                    and state.position_manager is not None
                                ):
                                    _pos = state.position_manager.get_position()
                                    if _pos is not None:
                                        try:
                                            from core.execution.live_order_sender import (
                                                dispatch_live_order,
                                            )

                                            _dd_brain_ids = getattr(
                                                _pos, "supporting_brain_ids", None
                                            )
                                            _dd_payload: dict[str, Any] = {
                                                "action": "close",
                                                "position_ticket": _pos.ticket,
                                                "volume": _pos.volume,
                                                "comment": "force_close_drawdown_kill",
                                            }
                                            if _dd_brain_ids:
                                                _dd_payload["brain_ids"] = _dd_brain_ids
                                            _dd_dispatched = False
                                            if exit_watchdog is not None:
                                                try:

                                                    def _dd_dispatch_fn(p: dict) -> dict:
                                                        return dispatch_live_order(
                                                            base_dir=config.base_dir,
                                                            broker=None,
                                                            symbol=config.symbol,
                                                            execution_payload=p,
                                                            skip_price_guard=True,
                                                            ignore_protection_flag=config.ignore_protection_flag,
                                                            protection_flag_path=config.protection_flag_path,
                                                            adapter_name="mt5",
                                                            extensions={
                                                                "mt5_terminal_path": config.mt5_terminal_path
                                                            },
                                                        )

                                                    _dd_pnl = None
                                                    if (
                                                        mid_price is not None
                                                        and hasattr(_pos, "entry_price")
                                                        and _pos.entry_price
                                                    ):
                                                        _dd_pnl = (
                                                            round(
                                                                (mid_price - _pos.entry_price)
                                                                * _pos.volume,
                                                                2,
                                                            )
                                                            if _pos.side == "long"
                                                            else round(
                                                                (_pos.entry_price - mid_price)
                                                                * _pos.volume,
                                                                2,
                                                            )
                                                        )
                                                    _dd_wd = exit_watchdog.execute_exit(
                                                        position_ticket=_pos.ticket,
                                                        volume=_pos.volume,
                                                        side=_pos.side,
                                                        reason="force_close_drawdown_kill",
                                                        dispatch_fn=_dd_dispatch_fn,
                                                        brain_ids=_dd_brain_ids,
                                                        pnl=_dd_pnl,
                                                    )
                                                    _dd_dispatched = _dd_wd.success
                                                except Exception:
                                                    _dd_dispatched = False
                                            if not _dd_dispatched:
                                                dispatch_live_order(
                                                    base_dir=config.base_dir,
                                                    broker=None,
                                                    symbol=config.symbol,
                                                    execution_payload=_dd_payload,
                                                    skip_price_guard=True,
                                                    ignore_protection_flag=config.ignore_protection_flag,
                                                    protection_flag_path=config.protection_flag_path,
                                                    adapter_name="mt5",
                                                    extensions={
                                                        "mt5_terminal_path": config.mt5_terminal_path
                                                    },
                                                )
                                            state.position_manager.clear_position(
                                                ticket=_pos.ticket
                                            )
                                            print(
                                                json.dumps(
                                                    {
                                                        "event": "force_close_executed",
                                                        "time": _utc_iso(),
                                                        "ticket": _pos.ticket,
                                                        "drawdown_pct": dd_result["drawdown_pct"],
                                                    },
                                                    ensure_ascii=False,
                                                ),
                                                flush=True,
                                            )
                                        except Exception as _fc_exc:
                                            print(
                                                json.dumps(
                                                    {
                                                        "event": "force_close_error",
                                                        "time": _utc_iso(),
                                                        "error": str(_fc_exc),
                                                    },
                                                    ensure_ascii=False,
                                                ),
                                                flush=True,
                                            )
                                _log_cycle_end(state.loop_iteration)
                                return state, True
                    except Exception:
                        pass  # fail-open: don't block if MT5 unavailable

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
                    _log_cycle_end(state.loop_iteration)
                    return state, True  # skip cycle on bad features
            except Exception:
                pass

        # ── Cut 1 + 2: Initialize cooldown registry & family entry tracker ──
        if state._cooldown_registry is None:
            from core.execution.pre_trade_guards import CooldownRegistry

            state._cooldown_registry = CooldownRegistry()
        if state._family_entry_tracker is None:
            from core.execution.pre_trade_guards import FamilyEntryTracker

            state._family_entry_tracker = FamilyEntryTracker()

        # Portfolio risk controller (persist for VaR/correlation tracking) + execution queue
        if state.portfolio_risk_controller is None:
            state.portfolio_risk_controller = PortfolioRiskController(
                max_gross_exposure=config.portfolio_max_gross,
                max_net_exposure=config.portfolio_max_net,
                max_same_direction=config.portfolio_max_same_dir,
                netting_mode=config.portfolio_netting_mode,
            )
        portfolio_risk = state.portfolio_risk_controller
        exec_queue = ExecutionQueue(
            stagger_seconds=config.strategy_stagger_seconds,
        )

        # Current positions for portfolio risk (map strategy_name → position)
        current_positions: dict[str, dict[str, Any]] = {}
        _mt5_ok: bool = False  # True when MT5 query succeeded (even if empty)

        # ── Query MT5 for ALL open positions (by magic → strategy mapping) ──
        if not config.no_mt5 and mt5_worker is not None:
            try:
                from core.contracts.strategy_magic import MAGIC_TO_STRATEGY as _MAGIC_TO_STRATEGY

                _mt5_positions = mt5_worker.positions_get(symbol=config.symbol)
                _mt5_ok = True  # query succeeded
                if _mt5_positions:
                    for _mp in _mt5_positions:
                        _magic = int(getattr(_mp, "magic", 0))
                        _mt5_sname = _MAGIC_TO_STRATEGY.get(_magic)
                        if _mt5_sname:
                            _side = "long" if getattr(_mp, "type", 0) == 0 else "short"
                            _ticket = int(getattr(_mp, "ticket", 0))
                            _entry_cycle_from_pm = 0
                            if (
                                state.position_manager is not None
                                and state.position_manager.has_position()
                            ):
                                _pm_pos = state.position_manager.get_position()
                                if _pm_pos is not None and _pm_pos.ticket == _ticket:
                                    _entry_cycle_from_pm = _pm_pos.entry_cycle
                            _brain_ids_from_pm: list[str] = []
                            if (
                                state.position_manager is not None
                                and state.position_manager.has_position()
                            ):
                                _pm_pos = state.position_manager.get_position()
                                if _pm_pos is not None and _pm_pos.ticket == _ticket:
                                    _brain_ids_from_pm = getattr(
                                        _pm_pos, "supporting_brain_ids", []
                                    )
                            current_positions[_mt5_sname] = {
                                "strategy": _mt5_sname,
                                "direction": _side,
                                "volume": float(getattr(_mp, "volume", 0.01)),
                                "ticket": _ticket,
                                "entry_cycle": _entry_cycle_from_pm,
                                "brain_ids": _brain_ids_from_pm,
                            }
            except Exception:
                pass

        # ── Quarantine auto-clear: MT5 confirms zero positions → safe to lift ──
        if (
            _mt5_ok
            and not current_positions
            and portfolio_risk is not None
            and portfolio_risk.is_symbol_quarantined(config.symbol)
        ):
            portfolio_risk._symbol_quarantine_until.pop(config.symbol, None)

        # ── Fallback: only when MT5 query FAILED (not when it returned empty) ──
        if (
            not _mt5_ok
            and not current_positions
            and state.position_manager is not None
            and state.position_manager.has_position()
        ):
            for pos in list(state.position_manager.get_all_positions()):
                # Determine which strategy owns this position (from supporting brains)
                owner = "barrier_12bar"  # default
                if pos.supporting_brain_ids:
                    for bid in pos.supporting_brain_ids:
                        for bi in brains:
                            if bi.get("brain_id") == bid:
                                bt = bi.get("brain_type", "")
                                if bt in MICRO_M15_GROUP["brain_types"]:
                                    owner = "micro_m15"
                                elif bt in MICRO_H1_GROUP["brain_types"]:
                                    owner = "micro_h1"
                                elif bt in MICRO_H4_GROUP["brain_types"]:
                                    owner = "micro_h4"
                                elif bt in MICRO_GROUP["brain_types"]:
                                    owner = "micro_3bar"
                                elif bt in ARB_GROUP["brain_types"]:
                                    owner = "statarb_dynamic"
                                break
                # Only add to current_positions if not already populated by MT5 query
                if owner not in current_positions:
                    current_positions[owner] = {
                        "strategy": owner,
                        "direction": pos.side,
                        "volume": pos.volume,
                        "ticket": pos.ticket,
                        "entry_cycle": pos.entry_cycle,
                        "brain_ids": pos.supporting_brain_ids,
                    }

        # ── OU-augmented feature vector for meta-labeler strategy ──
        _meta_feature_vector: Any = None
        if "barrier_12bar_meta" in strategies and not config.no_mt5:
            _meta_feature_vector, _ou_parms = _build_meta_feature_vector(
                brains=brains,
                feature_store=feature_store,
                mid_price=mid_price,
                symbol=config.symbol,
            )
            if _ou_parms is not None:
                state._last_ou_params = _ou_parms

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
            regime_modulation=regime_modulation,
            trend_direction=trend_direction,
            trend_strength=trend_strength,
            h4_trend_strength=h4_trend_strength,
            macro_regime=macro_regime,
            risk_budget_usd=_effective_risk_budget,
            sl_streak_blocked_until=state.sl_streak_blocked_until,
            portfolio_risk=portfolio_risk,
            execution_queue=exec_queue,
            tracker=tracker,
            pnl_ledger=pnl_ledger,
            current_positions=current_positions,
            session_volume_mult=session_info.get("volume_mult", 1.0),
            health_volume_mult=state._last_health_volume_mult or 1.0,
            micro_sequences=micro_sequences,
            daily_feature_vector=daily_feature_vector,
            account_equity=_account_equity,
            cycle_count=state.cycle_count,
            meta_signal_filter=meta_signal_filter,
            meta_filter_gate=state._meta_filter_gate
            if hasattr(state, "_meta_filter_gate")
            else None,
            conformal_ou_gate=getattr(state, "_conformal_ou_gate", None),
            micro_feature_dict=micro_feature_dict,
            cooldown_registry=state._cooldown_registry,
            family_entry_tracker=state._family_entry_tracker,
            mtf_price_service=getattr(state, "_mtf_price_service", None),
            meta_feature_vector=_meta_feature_vector,
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

        # Feed brain predictions to signal health monitor for drift detection
        if state.signal_health_monitor is not None:
            for sname in strategies:
                decision = eval_summary.get("decisions_map", {}).get(sname)
                if decision is None:
                    continue
                entry_ctx = getattr(decision, "entry_context", None) or {}
                brain_preds = entry_ctx.get("brain_predictions", [])
                for bp in brain_preds:
                    try:
                        state.signal_health_monitor.feed_prediction(
                            up_prob=float(bp.get("up_prob", 0.5)),
                            down_prob=float(bp.get("down_prob", 0.5)),
                            confidence=float(bp.get("confidence", 0.5)),
                        )
                    except (TypeError, ValueError):
                        pass

        # ── Re-entry quality guard: filter decisions that would churn ──
        if exec_queue.queue_size > 0 and state._reentry_states:
            from core.execution.reentry_guard import ensure_reentry_state

            _filtered_queue: list[Any] = []
            _reentry_skipped: list[dict[str, Any]] = []
            for _qd in exec_queue._queue:
                _rs = ensure_reentry_state(state._reentry_states, _qd.strategy_name)
                _d = _qd.decision
                _entry_price = mid_price if mid_price is not None and mid_price > 0 else 0.0
                _allowed, _rr_reason, _cons_count_f = _rs.check_and_record_entry(
                    direction=_d.direction,
                    confidence=_d.confidence,
                    mid=_entry_price,
                )
                # ── Diagnostic: log every re-entry check ──
                _last_exit = _rs.last_exit
                print(
                    json.dumps(
                        {
                            "event": "reentry_check",
                            "time": _utc_iso(),
                            "strategy": _qd.strategy_name,
                            "direction": _d.direction,
                            "confidence": round(_d.confidence, 4),
                            "allowed": _allowed,
                            "reason": _rr_reason,
                            "consecutive_same_dir": int(_cons_count_f),
                            "elapsed_since_exit_s": (
                                round(time.time() - _last_exit.timestamp, 1) if _last_exit else -1
                            ),
                            "last_exit_category": _last_exit.category if _last_exit else "none",
                            "last_exit_reason": (_last_exit.reason[:60]) if _last_exit else "",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if not _allowed:
                    _reentry_skipped.append(
                        {
                            "strategy": _qd.strategy_name,
                            "direction": _d.direction,
                            "confidence": _d.confidence,
                            "reason": _rr_reason,
                        }
                    )
                    continue
                # Apply volume decay for consecutive same-direction entries
                _cons_count = int(_cons_count_f)
                if _cons_count > 0:
                    from core.execution.reentry_guard import apply_reentry_volume_scale

                    _scaled_vol, _should_block = apply_reentry_volume_scale(_d.volume, _cons_count)
                    if _should_block:
                        _reentry_skipped.append(
                            {
                                "strategy": _qd.strategy_name,
                                "direction": _d.direction,
                                "confidence": _d.confidence,
                                "reason": f"volume_decay_blocked_consecutive_{_cons_count}",
                            }
                        )
                        continue
                    _d.volume = _scaled_vol
                _filtered_queue.append(_qd)
            if _reentry_skipped:
                exec_queue._queue = _filtered_queue
                print(
                    json.dumps(
                        {
                            "event": "reentry_guard_skip",
                            "time": _utc_iso(),
                            "skipped": _reentry_skipped,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        # Flush execution queue → dispatch to MT5
        if exec_queue.queue_size > 0 and not config.no_mt5:
            from core.execution.live_order_sender import dispatch_live_open_order

            # 陷阱三: net-out close orders intercepted at upper layer for Watchdog wrapping
            _net_out_close_dispatch_fn = None
            if exit_watchdog is not None:
                from core.execution.live_order_sender import dispatch_live_order as _net_dispatch

                def _net_out_close_dispatch_fn(payload: dict) -> dict:
                    _ticket = payload.get("position_ticket", 0)
                    _vol = payload.get("volume", 0.01)
                    _side = payload.get("side", "long")
                    _reason = payload.get("comment", "net_out")
                    _magic = payload.get("magic", 0)
                    _brain_ids = payload.get("brain_ids")
                    # Calculate estimated PnL for journal recording
                    _net_pnl = payload.get("pnl")
                    if _net_pnl is None and mid_price is not None and _ticket:
                        _net_entry = state.known_open_tickets.get(_ticket, {})
                        _net_ep = _net_entry.get("entry_price")
                        if not _net_ep:
                            _net_ep = _net_entry.get("detail", {}).get("request", {}).get("price")
                        if _net_ep and _vol:
                            if _side == "long":
                                _net_pnl = round((mid_price - float(_net_ep)) * _vol, 2)
                            elif _side == "short":
                                _net_pnl = round((float(_net_ep) - mid_price) * _vol, 2)
                    _wd = exit_watchdog.execute_exit(
                        position_ticket=_ticket,
                        volume=_vol,
                        side=_side,
                        reason=_reason,
                        magic=_magic,
                        dispatch_fn=lambda p: _net_dispatch(
                            base_dir=config.base_dir,
                            broker=None,
                            symbol=config.symbol,
                            execution_payload=p,
                            skip_price_guard=True,
                            ignore_protection_flag=config.ignore_protection_flag,
                            protection_flag_path=config.protection_flag_path,
                            adapter_name="mt5",
                            extensions={"mt5_terminal_path": config.mt5_terminal_path},
                        ),
                        brain_ids=_brain_ids,
                        pnl=_net_pnl,
                    )
                    return {"dispatched": _wd.success, "intent_id": ""}

                _net_out_close_dispatch_fn = _net_out_close_dispatch_fn

            dispatch_results = exec_queue.flush(
                dispatch_live_open_order,
                journal_path=str(journal_path),
                mt5_terminal_path=config.mt5_terminal_path,
                symbol=config.symbol,
                base_dir=config.base_dir,
                ignore_protection_flag=config.ignore_protection_flag,
                protection_flag_path=config.protection_flag_path,
                broker=broker,
                close_dispatch_fn=_net_out_close_dispatch_fn,
            )

            # ── Quarantine check: block entries on symbols with unconfirmed net-out ──
            _unconfirmed_close = any(
                not dr.dispatched and dr.reason == "net_out_close_not_confirmed"
                for dr in dispatch_results
            )
            if _unconfirmed_close and portfolio_risk is not None:
                portfolio_risk.quarantine_symbol(config.symbol)
                print(
                    json.dumps(
                        {
                            "event": "symbol_quarantined",
                            "time": _utc_iso(),
                            "symbol": config.symbol,
                            "reason": "net_out_close_not_confirmed",
                            "duration_seconds": 60.0,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

            # ── NET_OUT ticket reassignment: partial close creates new MT5 ticket ──
            for dr in dispatch_results:
                _tkt_update = dr.net_out_ticket_update
                if _tkt_update and _tkt_update.get("new_ticket"):
                    _old_tkt = _tkt_update["old_ticket"]
                    _new_tkt = _tkt_update["new_ticket"]
                    _close_vol = _tkt_update.get("close_volume", 0.0)
                    _old_entry = state.known_open_tickets.pop(int(_old_tkt), None)
                    if _old_entry:
                        _old_vol = float(_old_entry.get("volume", 0.0))
                        _remaining = round(max(0.0, _old_vol - _close_vol), 2)
                        _new_entry = dict(_old_entry)
                        _new_entry["position_ticket"] = int(_new_tkt)
                        _new_entry["volume"] = _remaining
                        state.known_open_tickets[int(_new_tkt)] = _new_entry
                        # Sync position_manager: update volume on old ticket position
                        # if still registered, so ghost-volume audit sees correct
                        # expected_remaining_volume during the reconciliation window.
                        if state.position_manager is not None:
                            _pm_pos = state.position_manager.get_position(ticket=int(_old_tkt))
                            if _pm_pos is not None:
                                _pm_pos.volume = _remaining
                                _pm_pos.expected_remaining_volume = _remaining
                        print(
                            json.dumps(
                                {
                                    "event": "net_out_ticket_reassigned",
                                    "time": _utc_iso(),
                                    "old_ticket": int(_old_tkt),
                                    "new_ticket": int(_new_tkt),
                                    "close_volume": _close_vol,
                                    "remaining_volume": _remaining,
                                    "strategy": _old_entry.get("strategy", ""),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )

            # Log dispatch results
            dispatched_count = sum(1 for r in dispatch_results if r.dispatched)
            if dispatched_count > 0:
                state.last_fire = time.monotonic()
                # state.cycle_count 由外层 live_intent_loop.py 统一递增，
                # 避免重复递增导致状态保存间隔/对账触发/冷却计时偏移。

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

            # ── Cut 2: Record family entries for dispatched positions ──
            if state._family_entry_tracker is not None:
                from core.execution.pre_trade_guards import strategy_to_family

                for dr in dispatch_results:
                    if dr.dispatched and dr.direction in ("long", "short"):
                        _fam = strategy_to_family(dr.strategy_name)
                        if _fam != dr.strategy_name:  # family member
                            state._family_entry_tracker.record_entry(
                                family=_fam,
                                direction=dr.direction,
                                timestamp=time.time(),
                            )
                            print(
                                json.dumps(
                                    {
                                        "event": "family_entry_recorded",
                                        "time": _utc_iso(),
                                        "strategy": dr.strategy_name,
                                        "family": _fam,
                                        "direction": dr.direction,
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
                        _record_brain_outcomes(strategy_proposals, dr.direction, "pending", tracker)
                    except Exception:
                        pass

            # ── Register opened positions for dynamic exit management ──
            if (
                config.exit_management_enabled
                and state.position_manager is not None
                and not config.no_mt5
            ):
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
                    brain_votes_from_journal: list[dict[str, Any]] | None = None

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
                                            bv = rec.get("brain_votes")
                                            if isinstance(bv, list):
                                                brain_votes_from_journal = bv
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

                    # Build per-model horizon map (reads training_horizon from brain JSON)
                    model_horizons: dict[str, int] = {}
                    for bid in decision.brain_ids:
                        horizon = 12
                        for bi in brains:
                            if bi.get("brain_id") == bid:
                                horizon = bi.get("training_horizon", _DEFAULT_HORIZON)
                                break
                        model_horizons[bid] = horizon

                    try:
                        # Determine partial TP + exit parameters from strategy config
                        _s_cfg = config.strategy_configs.get(dr.strategy_name, {})
                        _tp_cfg = _s_cfg.get("tp", {})
                        _exit_cfg = _s_cfg.get("exit", {})
                        _ptp_r = (
                            _tp_cfg.get("partial_tp_r", 0.0)
                            if _tp_cfg.get("partial_tp_enabled")
                            else 0.0
                        )
                        _ptp_ratio = _tp_cfg.get("partial_tp_ratio", 0.5)
                        state.position_manager.register_position(
                            ticket=ticket,
                            side=decision.direction,
                            entry_price=entry_price,
                            volume=decision.volume,
                            initial_sl=decision.sl,
                            initial_tp=decision.tp,
                            entry_atr=current_atr,
                            entry_cycle=state.loop_iteration,
                            entry_z_score=getattr(decision, "entry_z_score", 0.0),
                            entry_half_life=getattr(decision, "entry_half_life", 0.0),
                            entry_consensus=entry_consensus,
                            supporting_brain_ids=decision.brain_ids,
                            model_horizons=model_horizons,
                            current_high=entry_price,
                            partial_tp_r=_ptp_r,
                            partial_tp_ratio=_ptp_ratio,
                            strategy_name=dr.strategy_name,
                            # Phase B: TrailPolicy from live.yaml exit.* — single source of truth for Risk Exit
                            trail_policy=TrailPolicy(
                                trail_atr_mult=_exit_cfg.get("trail_atr_mult", 2.0),
                                trail_atr_mult_low=_exit_cfg.get("trail_atr_mult_low", 1.5),
                                trail_atr_mult_high=_exit_cfg.get("trail_atr_mult_high", 3.0),
                                breakeven_threshold_atr=_exit_cfg.get(
                                    "breakeven_threshold_atr", 1.0
                                ),
                            ),
                            cold_explore=getattr(decision, "cold_explore", False),
                        )
                        # Sync known_open_tickets so reconciliation can detect closes
                        state.known_open_tickets[ticket] = {
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
                        # ── Shadow record: limit-equivalent order for execution quality analysis ──
                        if state.limit_monitor is not None:
                            try:
                                _lom_spread_pts = 0.0
                                _lom_b = _bid if _bid is not None else 0.0
                                _lom_a = _ask if _ask is not None else 0.0
                                if _lom_a > _lom_b > 0:
                                    _lom_spread_pts = round((_lom_a - _lom_b) * 10000, 1)
                                state.limit_monitor.place(
                                    signal_bar=state.loop_iteration,
                                    direction=decision.direction,
                                    signal_close=entry_price,
                                    entry_atr=current_atr,
                                    spread_points=_lom_spread_pts,
                                    current_bar=state.loop_iteration,
                                )
                            except Exception:
                                pass
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
                        h4_trend_strength=h4_trend_strength,
                        macro_regime=macro_regime,
                        risk_budget_usd=_effective_risk_budget,
                        pnl_store=pnl_ledger,
                        micro_sequences=micro_sequences,
                        meta_filter=meta_signal_filter,
                        meta_filter_gate=getattr(state, "_meta_filter_gate", None),
                        conformal_ou_gate=getattr(state, "_conformal_ou_gate", None),
                        micro_feature_dict=micro_feature_dict,
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
        # DEPRECATED: unreachable with multi_strategy_enabled=True (default).
        # Retained as rollback reference only — do not add new logic here.
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

                # Intraday drawdown kill for legacy path
                if config.intraday_drawdown_kill_enabled:
                    try:
                        from core.execution.pre_trade_guards import IntradayDrawdownKill

                        if state.intraday_dd_kill is None:
                            state.intraday_dd_kill = IntradayDrawdownKill(
                                kill_pct=config.intraday_drawdown_kill_pct,
                                force_close_enabled=config.intraday_dd_force_close,
                                force_close_pct=config.intraday_dd_force_close_pct,
                            )
                        _acc = mt5_worker.account_info()
                        if _acc is not None:
                            _eq = float(getattr(_acc, "equity", 0))
                            _dd = state.intraday_dd_kill.update(_eq)
                            if _dd.get("blocked"):
                                print(
                                    json.dumps(
                                        {
                                            "event": "intraday_drawdown_kill_legacy",
                                            "time": _utc_iso(),
                                            "drawdown_pct": _dd["drawdown_pct"],
                                            "force_close": _dd.get("force_close", False),
                                        },
                                        ensure_ascii=False,
                                    ),
                                    flush=True,
                                )
                                # DEPRECATED force-close: dead code — path B unreachable with
                                # multi_strategy_enabled=True. Retained as rollback reference.
                                if _dd.get("force_close") and state.position_manager is not None:
                                    _pos = state.position_manager.get_position()
                                    if _pos is not None:
                                        try:
                                            from core.execution.live_order_sender import (
                                                dispatch_live_order,
                                            )

                                            _dd2_brain_ids = getattr(
                                                _pos, "supporting_brain_ids", None
                                            )
                                            _dd2_payload: dict[str, Any] = {
                                                "action": "close",
                                                "position_ticket": _pos.ticket,
                                                "volume": _pos.volume,
                                                "comment": "force_close_dd_legacy",
                                            }
                                            if _dd2_brain_ids:
                                                _dd2_payload["brain_ids"] = _dd2_brain_ids
                                            dispatch_live_order(
                                                base_dir=config.base_dir,
                                                broker=None,
                                                symbol=config.symbol,
                                                execution_payload=_dd2_payload,
                                                skip_price_guard=True,
                                                ignore_protection_flag=config.ignore_protection_flag,
                                                protection_flag_path=config.protection_flag_path,
                                                adapter_name="mt5",
                                                extensions={
                                                    "mt5_terminal_path": config.mt5_terminal_path
                                                },
                                            )
                                            state.position_manager.clear_position(
                                                ticket=_pos.ticket
                                            )
                                            print(
                                                json.dumps(
                                                    {
                                                        "event": "force_close_executed_legacy",
                                                        "time": _utc_iso(),
                                                        "ticket": _pos.ticket,
                                                        "drawdown_pct": _dd["drawdown_pct"],
                                                    },
                                                    ensure_ascii=False,
                                                ),
                                                flush=True,
                                            )
                                        except Exception as _fc_exc:
                                            print(
                                                json.dumps(
                                                    {
                                                        "event": "force_close_error_legacy",
                                                        "time": _utc_iso(),
                                                        "error": str(_fc_exc),
                                                    },
                                                    ensure_ascii=False,
                                                ),
                                                flush=True,
                                            )
                                _log_cycle_end(state.loop_iteration)
                                return state, not config.once
                    except Exception:
                        pass

                _fv = check_feature_vector(feature_vector)
                if not _fv.get("passed"):
                    _log_cycle_end(state.loop_iteration)
                    return state, not config.once
            except Exception:
                pass

        raw_proposals = []
        for b_info in brains:
            schema_id = b_info.get("feature_schema_id", "")
            btype = b_info.get("brain_type", "")
            brain_id = b_info.get("brain_id", "")
            if btype == "ou_params_v6":
                fv = (
                    np.array([mid_price], dtype=np.float32)
                    if mid_price
                    else np.zeros(1, dtype=np.float32)
                )
                raw = b_info["adapter"].infer(fv)
                prop = b_info["adapter"].get_signal(raw)
            elif "microstructure" in schema_id:
                # Route by hmre_layer: M5/M15/H1/H4 → correct timeframe sequence
                hmre_layer = b_info.get("hmre_layer", "M5")
                seq = micro_sequences.get(hmre_layer)
                if seq is not None and seq.ndim == 2 and seq.shape[0] >= 32:
                    try:
                        prop = b_info["adapter"].run(None, seq)
                    except Exception:
                        # Fallback: bypass run() pipeline.
                        # Transformer adapters: use infer_sequence() to avoid rolling-buffer corruption.
                        # XGBoost adapters: use infer() with flat ravel (model expects 288-dim).
                        try:
                            if hasattr(b_info["adapter"], "infer_sequence"):
                                seq_batch = seq.astype(np.float32).reshape(1, seq.shape[0], 9)
                                raw = b_info["adapter"].infer_sequence(seq_batch)
                            else:
                                raw = b_info["adapter"].infer(seq.ravel().astype(np.float64))
                            prop = b_info["adapter"].get_signal(raw)
                        except Exception:
                            prop = None
                else:
                    prop = None
            elif schema_id in ("daily_swing_24", "swing_24"):
                # Swing brains use D1 daily features
                if daily_feature_vector is not None:
                    try:
                        raw = b_info["adapter"].infer(daily_feature_vector)
                        prop = b_info["adapter"].get_signal(raw)
                    except Exception:
                        prop = None
                else:
                    prop = None
            else:
                raw = b_info["adapter"].infer(feature_vector)
                prop = b_info["adapter"].get_signal(raw)

            if prop is not None:
                raw_proposals.append(prop)

        result = _compute_contract_group_consensus(
            raw_proposals=raw_proposals,
            brains=brains,
            tracker=tracker,
            pnl_ledger=pnl_ledger,
            correlation_tracker=state.correlation_tracker,
            base_volume=config.volume or 0.01,
            current_atr=current_atr,
            regime_info=regime_info,
            total_budget=getattr(config, "risk_budget_usd", 0.0) or 0.0,
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
            direction = getattr(proposal, "direction", "neutral")
            confidence = getattr(proposal, "confidence", 0.0)

    # ── Record counterfactual signals for P&L tracking ──
    # Per-proposal try/except prevents one misbehaving brain from
    # silently dropping P&L records for all other brains.
    if pnl_ledger is not None and mid_price is not None and mid_price > 0:
        _live_spread = float(_ask - _bid) if (_bid and _ask and _ask > _bid) else 0.0
        if config.multi_brain:
            _registry = BrainRegistry.instance()
            for p in raw_proposals:
                try:
                    _brain_id_str: str = str(getattr(p, "brain_id", "unknown"))
                    _horizon = _registry.get_training_horizon(_brain_id_str)
                    pnl_ledger.record_signal(
                        brain_id=_brain_id_str,
                        symbol=config.symbol,
                        direction=getattr(p, "direction", "neutral"),
                        entry_price=mid_price,
                        confidence=getattr(p, "confidence", 0.5),
                        expected_horizon=_horizon,
                        entry_spread=_live_spread,
                        entry_slippage=0.10,
                    )
                except Exception:
                    pass
        elif proposal is not None:
            try:
                _single_brain_id2: str = str(
                    getattr(proposal, "brain_id", config.brain_entry.get("brain_id", "unknown"))
                )
                _horizon = BrainRegistry.instance().get_training_horizon(_single_brain_id2)
                pnl_ledger.record_signal(
                    brain_id=_single_brain_id2,
                    symbol=config.symbol,
                    direction=getattr(proposal, "direction", "neutral"),
                    entry_price=mid_price,
                    confidence=getattr(proposal, "confidence", 0.5),
                    expected_horizon=_horizon,
                    entry_spread=_live_spread,
                    entry_slippage=0.10,
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
    # Push to rolling buffer (all non-neutral signals)
    if direction != "neutral" and confidence > 0:
        state._recent_consensus_scores.append(confidence)
        if len(state._recent_consensus_scores) > 500:
            state._recent_consensus_scores.pop(0)
    # Compute rolling P80 threshold when pipeline is warm
    _rolling_p80: float = 0.0
    _pipeline_ready = len(state._recent_consensus_scores) >= 100
    if _pipeline_ready:
        try:
            import numpy as np

            _rolling_p80 = float(np.percentile(state._recent_consensus_scores, 80))
        except Exception:
            _rolling_p80 = 0.0
    _effective_threshold = (
        max(config.confidence_threshold, _rolling_p80)
        if _pipeline_ready
        else config.confidence_threshold
    )
    if confidence < _effective_threshold or direction == "neutral":
        skip_event: dict[str, Any] = {
            "event": "low_confidence_skip",
            "time": _utc_iso(),
            "direction": direction,
            "confidence": round(confidence, 6),
            "threshold": _effective_threshold,
        }
        if _pipeline_ready:
            skip_event["rolling_p80"] = round(_rolling_p80, 6)
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
        else _build_risk_context(mt5_worker, config.symbol)
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
                from core.runtime.shadow_recorder import record_shadow_from_proposals

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
        from core.execution.live_order_sender import _validate_sl_tp, dispatch_live_open_order

        # Attempt price fetch with MT5 reconnection fallback
        try:
            if broker is not None:
                mid, bid, ask = broker.fetch_prices(config.symbol)
            else:
                mid, bid, ask = _mid_and_prices(mt5_worker, config.symbol)
        except Exception as _price_exc:
            # MT5 connection may have gone stale during cooldown — attempt reconnect
            try:
                if not config.no_mt5:
                    mt5_worker.reconnect()
                if broker is not None:
                    mid, bid, ask = broker.fetch_prices(config.symbol)
                else:
                    mid, bid, ask = _mid_and_prices(mt5_worker, config.symbol)
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
                _log_cycle_end(state.loop_iteration)
                return state, True  # skip this cycle, retry next
        ref_long = ask
        ref_short = bid

        # Compute SL/TP (ATR-based, regime-adjusted)
        current_atr = (
            broker.fetch_current_atr(config.symbol)
            if broker is not None
            else _get_current_atr(mt5_worker, config.symbol)
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

        # ── Risk verification log: confirm vol-targeted sizing is dollar-neutral ──
        _dollar_risk = abs(ref_for_guard - stop_loss) * dynamic_volume * 100.0
        print(
            json.dumps(
                {
                    "event": "risk_check",
                    "time": _utc_iso(),
                    "side": side,
                    "entry_ref": round(ref_for_guard, 5),
                    "stop_loss": round(stop_loss, 5),
                    "sl_distance": round(abs(ref_for_guard - stop_loss), 5),
                    "volume": dynamic_volume,
                    "dollar_risk": round(_dollar_risk, 2),
                    "risk_budget_usd": _effective_risk_budget,
                    "sl_atr_mult": sl_mult,
                    "tp_atr_mult": tp_mult,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        # ── Persist shadow decision (multi-brain) ──
        if config.multi_brain and proposals:
            try:
                from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
                from core.runtime.shadow_recorder import record_shadow_from_proposals

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
                from core.runtime.shadow_recorder import record_shadow_from_proposals

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

        # ── Collect brain_ids and brain_votes for journal attribution (Track 3) ──
        dispatch_brain_ids: list[str] | None = None
        dispatch_brain_votes: list[dict[str, Any]] | None = None
        if config.multi_brain:
            supporting = consensus_extra.get("supporting_brains", [])
            opposing = consensus_extra.get("opposing_brains", [])
            dispatch_brain_ids = list(supporting) + list(opposing)
            # Build per-brain vote details from raw_proposals for Track 3 attribution
            _votes: list[dict[str, Any]] = []
            for p in raw_proposals:
                _votes.append(
                    {
                        "brain_id": getattr(p, "brain_id", "unknown"),
                        "direction_bias": getattr(p, "direction", "neutral"),
                        "confidence": getattr(p, "confidence", 0.0),
                    }
                )
            dispatch_brain_votes = _votes
        elif config.brain_entry:
            _single_bid: str = str(config.brain_entry.get("brain_id", "unknown"))
            dispatch_brain_ids = [_single_bid]
            dispatch_brain_votes = [
                {"brain_id": _single_bid, "direction_bias": direction, "confidence": confidence}
            ]

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
            brain_votes=dispatch_brain_votes,
            confidence=confidence,
        )
        state.last_fire = now

        # ── Publish dispatch event to message broker (best-effort) ──
        try:
            from core.observability.message_broker import get_broker

            _broker = get_broker("auto")
            _broker.publish(
                "trade.intent",
                {
                    "symbol": config.symbol,
                    "direction": side,
                    "volume": dynamic_volume,
                    "magic": dispatch_magic,
                    "brain_ids": dispatch_brain_ids,
                    "status": out.get("status", "unknown"),
                    "intent_id": out.get("intent_id", ""),
                },
            )
        except Exception:
            pass

        # ── Register position for dynamic exit management ──
        dispatch_ok = out.get("status", "") not in ("error", "rejected", "timeout")
        if dispatch_ok and config.exit_management_enabled and state.position_manager is not None:
            try:
                # Extract ticket from journal (written by dispatch_live_open_order)
                intent_id = out.get("intent_id", "")
                pm_ticket: int | None = None
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
                                    pm_ticket = t
                        except Exception:
                            pass
                        break
                if pm_ticket is not None:
                    # Build entry consensus snapshot
                    pm_entry_consensus: dict[str, Any] = {}
                    pm_supporting: list[str] = []
                    if config.multi_brain:
                        pm_entry_consensus = {
                            "aggregated_bias": consensus_extra.get("aggregated_bias", side),
                            "consensus_score": consensus_extra.get("consensus_score", confidence),
                            "voter_count": consensus_extra.get("voter_count", 0),
                            "majority_ratio": consensus_extra.get("majority_ratio", 0.0),
                            "disagreement_score": consensus_extra.get("disagreement_score", 0.0),
                        }
                        pm_supporting = list(consensus_extra.get("supporting_brains", []))
                    else:
                        pm_entry_consensus = {
                            "aggregated_bias": side,
                            "consensus_score": confidence,
                            "voter_count": 1,
                            "majority_ratio": 1.0,
                        }
                        pm_supporting = dispatch_brain_ids or []

                    # ── Build per-model horizon map (reads training_horizon from brain JSON) ──
                    pm_model_horizons: dict[str, int] = {}
                    if config.multi_brain:
                        for bi in brains:
                            bid = bi.get("brain_id", "")
                            pm_model_horizons[bid] = bi.get("training_horizon", _DEFAULT_HORIZON)
                    elif dispatch_brain_ids:
                        for bid in dispatch_brain_ids:
                            horizon = _DEFAULT_HORIZON
                            for bi in brains:
                                if bi.get("brain_id") == bid:
                                    horizon = bi.get("training_horizon", _DEFAULT_HORIZON)
                                    break
                            pm_model_horizons[bid] = horizon

                    # Derive strategy name for gamma-based EV trajectory
                    _pm_strat_name = ""
                    if not _pm_strat_name and config.multi_brain and pm_supporting:
                        for _bi in brains:
                            if _bi.get("brain_id") in pm_supporting:
                                _pm_strat_name = _bi.get("contract_group", "")
                                break
                    if not _pm_strat_name and dispatch_magic:
                        try:
                            from core.contracts.strategy_magic import MAGIC_TO_STRATEGY as _M2S

                            _pm_strat_name = _M2S.get(dispatch_magic, "")
                        except Exception:
                            pass

                    state.position_manager.register_position(
                        ticket=pm_ticket,
                        side=side,
                        entry_price=ref_for_guard,
                        volume=config.volume or 0.01,
                        initial_sl=stop_loss,
                        initial_tp=take_profit,
                        entry_atr=current_atr,
                        entry_cycle=state.loop_iteration,
                        entry_z_score=0.0,
                        entry_consensus=pm_entry_consensus,
                        supporting_brain_ids=pm_supporting,
                        model_horizons=pm_model_horizons,
                        current_high=ref_for_guard,
                        strategy_name=_pm_strat_name,
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
                                    # Enrich with strategy name from magic (journal entry lacks this)
                                    if "strategy" not in rec:
                                        from core.contracts.strategy_magic import (
                                            MAGIC_TO_STRATEGY as _M,
                                        )

                                        _j_magic = (
                                            rec.get("detail", {}).get("request", {}).get("magic", 0)
                                        )
                                        if not _j_magic:
                                            _j_magic = rec.get("magic", 0)
                                        rec["strategy"] = _M.get(_j_magic, "")
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

    # ── Circuit breaker: track consecutive degraded cycles ──
    if degraded_wakeup:
        state._consecutive_degraded_cycles += 1
        if state._consecutive_degraded_cycles >= 3:
            state._circuit_breaker_tripped = True
            print(
                json.dumps(
                    {
                        "event": "circuit_breaker_tripped",
                        "time": _utc_iso(),
                        "consecutive_degraded": state._consecutive_degraded_cycles,
                        "action": "suspend_new_entries",
                        "mode": "management_only",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    _log_cycle_end(state.loop_iteration)
    return state, not config.once
