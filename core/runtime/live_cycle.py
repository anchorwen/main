"""Live trading cycle execution — one iteration of the intent loop.

Extracted from scripts/live_intent_loop.py to keep the CLI script thin
(CLI + init + main loop shell) while housing the cycle logic in core/runtime/.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np

from core.brains.brain_registry import BrainRegistry
from core.execution.execution_queue import ExecutionQueue
from core.execution.portfolio_risk import PortfolioRiskController
from core.execution.regime_gate import RegimeGate

# ── Strategy line imports ──
from core.execution.trail_stop_engine import TrailPolicy

# ── Extracted sub-modules (P2 refactor) ──
from core.market.mtf_price_service import MTFPriceService
from core.parliament.contract_groups import (
    ARB_GROUP,
    MICRO_GROUP,
    MICRO_H1_GROUP,
    MICRO_H4_GROUP,
    MICRO_M15_GROUP,
)
from core.runtime.fault_handler import (
    FaultLevel,
    FaultTolerantContext,
    fail_open_guard,
    log_and_continue,
)

logger = logging.getLogger(__name__)
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
    market_type: str = "forex_24_5"  # FIX-082: "crypto_24_7" for BTC, "forex_24_5" for gold

    # ── FIX-20260607-XXX: Staleness Contract ──
    # Maximum allowed age of the latest tick before the cycle is skipped.
    # 120s for BTC (crypto 24/7, tick expected every few seconds).
    # XAU would use 60s (forex 24/5, tick expected sub-second).
    max_data_age_seconds: float = 120.0
    close_price_max_age_seconds: float = 60.0  # refuse close dispatch if price older than this
    # Phase B: maximum silence from MT5 bridge before circuit breaker trip.
    # 300s (5 min) — if no successful price fetch for 5 minutes, MT5 is dead.
    max_bridge_silence_seconds: float = 300.0
    # Phase B: single-cycle duration above this threshold increments degraded counter.
    cycle_stall_threshold_seconds: float = 180.0
    circuit_breaker_cooldown_seconds: float = (
        600.0  # 10min — breaker auto-reset after cooldown + conditions clear
    )

    # ── Multi-strategy mode ──
    multi_strategy_enabled: bool = True  # False → fallback to old CapitalAllocator
    strategy_stagger_seconds: float = 20.0  # delay between strategy dispatches
    portfolio_max_gross: float = 0.10
    portfolio_max_net: float = 0.05
    portfolio_max_same_dir: int = 2
    portfolio_netting_mode: str = "net_out"  # "net_out" | "allow_coexist"

    # ── FIX-20260605-120: Asset-specific reentry thresholds ──
    reentry_sl_cooldown: float = 180.0
    reentry_sl_penalty: float = 0.10
    reentry_bleed_cooldown: float = 180.0
    reentry_bleed_penalty: float = 0.10

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

    # FIX-20260531-008: contract_size from ASSET_REGISTRY (Defense 1 & 2)
    contract_size: float = 100.0  # XAU default; overridden for BTC

    def __post_init__(self) -> None:
        """Defense 2: fail-fast on contract_size mismatch + field sanity."""
        from core.config.asset_registry import ASSET_REGISTRY

        if self.symbol in ASSET_REGISTRY:
            expected = ASSET_REGISTRY[self.symbol].contract_size
            if self.contract_size != expected:
                raise ValueError(
                    f"LiveCycleConfig: contract_size mismatch for '{self.symbol}' "
                    f"(got {self.contract_size}, expected {expected})"
                )

        # ── FIX-20260605-120: basic field sanity checks ──
        if self.interval_seconds <= 0:
            raise ValueError(
                f"LiveCycleConfig: interval_seconds must be > 0, got {self.interval_seconds}"
            )
        if self.max_positions < 0:
            raise ValueError(
                f"LiveCycleConfig: max_positions must be >= 0, got {self.max_positions}"
            )
        if self.sl_atr_mult <= 0:
            raise ValueError(f"LiveCycleConfig: sl_atr_mult must be > 0, got {self.sl_atr_mult}")
        if self.tp_atr_mult <= 0:
            raise ValueError(f"LiveCycleConfig: tp_atr_mult must be > 0, got {self.tp_atr_mult}")
        if self.lot_step <= 0:
            raise ValueError(f"LiveCycleConfig: lot_step must be > 0, got {self.lot_step}")
        if self.reentry_sl_cooldown < 0:
            raise ValueError(
                f"LiveCycleConfig: reentry_sl_cooldown must be >= 0, got {self.reentry_sl_cooldown}"
            )
        if not (0.0 <= self.reentry_sl_penalty <= 1.0):
            raise ValueError(
                f"LiveCycleConfig: reentry_sl_penalty must be in [0,1], got {self.reentry_sl_penalty}"
            )


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
    _bootstrap_degraded: bool = False  # FIX-138: set True when restart bootstrap fails
    # ── FIX-20260607-007: Kalman velocity for exit management ──
    _last_kalman_velocity_bps: float | None = None
    # ── FIX-20260606-138 Phase 2: cross-cycle exit retry cooldown ──
    _exit_reject_streak: dict[int, int] = field(
        default_factory=dict
    )  # ticket → consecutive rejects
    _exit_reject_cooldown: dict[int, float] = field(
        default_factory=dict
    )  # ticket → cooldown_until ts
    # ── FIX-20260603-067 P2: gate telemetry funnel ──
    _gate_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    _gate_stats_cycles: int = 0
    position_manager: Any = None  # ActivePositionManager (set by caller)
    correlation_tracker: Any = None  # GroupCorrelationTracker (set by caller)
    shadow_verification_pending: dict[str, Any] | None = (
        None  # prev-cycle shadow decision for counterfactual settlement
    )
    regime_gate: Any = None  # RegimeGate (persisted across cycles for ADX accumulation)
    intraday_dd_kill: Any = None  # IntradayDrawdownKill (persisted across cycles)
    block_new_entries: bool = False  # FIX-080: circuit-breaker — set True when DD kill blocks, checked before strategy eval
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
    alert_hub: Any = None  # LiveAlertHub instance (FIX-20260529-040)
    _last_bridge_ack_time: float = 0.0  # Unix ts of last successful broker.fetch_prices()
    _last_cycle_start_time: float = 0.0  # wall clock at start of current cycle
    _last_tick_age: float = (
        0.0  # FIX-20260607-XXX: age of latest tick (seconds) for staleness guard
    )
    _cooldown_registry: Any = None  # CooldownRegistry (Cut 1: Absolute Refractory Period)
    _family_entry_tracker: Any = None  # FamilyEntryTracker (Cut 2: Cross-Strategy Spacing)
    _strategies: dict[str, Any] | None = None  # FIX-072: cached strategy_lines for persistence
    _meta_filter_gate: Any = None  # MetaFilterGate (LightGBM 47-dim OU signal quality filter)
    _conformal_ou_gate: Any = None  # ConformalOUGate (physics-based OU signal quality gate)
    _mtf_price_service: Any = None  # MTFPriceService — M15 bar reconstruction from M5 tick history
    _last_ou_params: dict[str, float] | None = None  # {z_score, half_life, theta} for meta labeler
    _btc_augmenter: Any = None  # BTCFeatureAugmenter — FIX-134 lazy-init for BTC feature pipeline
    # MIA close entries collected by _execute_management_phase, consumed by caller
    _pending_mia_closes: list[dict[str, Any]] = field(default_factory=list)

    # Circuit breaker: 3 consecutive degraded cycles → management-only mode
    _consecutive_degraded_cycles: int = 0
    _circuit_breaker_tripped: bool = False
    _circuit_breaker_tripped_at: float = (
        0.0  # Unix ts when breaker last tripped (for cooldown reset)
    )

    # FIX-20260607-XXX: Staleness Contract — consecutive cycles with stale data
    # triggers circuit breaker (data pipeline freeze → fail-closed).
    _consecutive_stale_cycles: int = 0
    _consecutive_stale_features: int = 0  # Phase B: feature store staleness → circuit breaker

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


def _compute_tf_ou_hurst(mid_prices: list[float]) -> tuple[float, float]:
    """Compute TF_OU_Theta and TF_Hurst from rolling M5 mid prices.

    Mirrors build_swing_enhanced_dataset.py:_ou_theta() and _hurst().
    Uses the most recent 21 M5 close prices (≈105 min history).
    Returns (ou_theta, hurst) — defaults (0.0, 0.5) on insufficient data.
    """
    if len(mid_prices) < 21:
        return 0.0, 0.5
    window = np.array(mid_prices[-21:], dtype=np.float64)
    # OU Theta: AR(1) mean-reversion coefficient
    y = window[1:]
    x = window[:-1]
    x_mean = float(np.mean(x))
    y_mean = float(np.mean(y))
    beta_num = float(np.sum((x - x_mean) * (y - y_mean)))
    beta_den = float(np.sum((x - x_mean) ** 2))
    if beta_den == 0:
        ou_theta = 0.0
    else:
        beta = np.clip(beta_num / beta_den, 1e-8, 0.99999999)
        ou_theta = float(-math.log(beta))
    # Hurst: R/S exponent
    s = float(np.std(window))
    if s == 0:
        hurst = 0.5
    else:
        mean_v = float(np.mean(window))
        z = np.cumsum(window - mean_v)
        r = float(np.max(z) - np.min(z))
        rs = r / s
        hurst = float(math.log(rs) / math.log(20)) if rs > 0 else 0.5
    return ou_theta, hurst


# ── Daily ops auto-scheduler ────────────────────────────────────────────

# FIX-20260531-009: state paths derived from config.base_dir at call site
DAILY_OPS_STATE_PATH = "data/state/daily_ops_state.json"  # legacy default; overridden by base_dir


def _load_daily_ops_state(base_dir: str) -> float:
    """Restore last daily_ops timestamp from disk. Returns 0.0 if not found."""
    try:
        state_path = os.path.join(base_dir, "state", "daily_ops_state.json")
        if os.path.exists(state_path):
            with open(state_path) as f:
                data = json.load(f)
            return float(data.get("last_daily_ops_utc", 0.0))
    except Exception as _exc:  # noqa: BLE001
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "Failed to read daily_ops_state: %s", _exc, exc_info=True
        )
    return 0.0


def _run_scheduled_daily_ops(config: LiveCycleConfig, state: LiveCycleState) -> None:
    """Execute daily_ops pipeline synchronously within the current cycle."""
    from core.runtime.daily_ops_scheduler import run_scheduled_daily_ops

    run_scheduled_daily_ops(config, state)


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
        with log_and_continue(component="MagicAttribution:trail"):
            from core.contracts.strategy_magic import STRATEGY_TO_MAGIC

            _strat_magic = STRATEGY_TO_MAGIC.get(strategy_name, 0)
            if _strat_magic:
                payload["magic"] = _strat_magic
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
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "event": "trail_dispatch_error",
                    "time": _utc_iso(),
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "level": "DEGRADE",
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
    exit_urgency: float = 0.5,
    factor_breakdown: dict[str, float] | None = None,
) -> bool:
    """Issue a close order for a managed position and record exit for re-entry guard.

    FIX-20260607-XXX: Price Age Guard — refuses to dispatch a close order
    when the latest price tick is older than close_price_max_age_seconds.
    Sending a close at a stale price guarantees rejection (deviation exceeded)
    and feeds the retry avalanche.  Better to let MT5's server-side SL/TP
    handle the exit.
    """
    # ── Price age guard ──
    _tick_age = getattr(state, "_last_tick_age", 0.0) if state is not None else 0.0
    if _tick_age > config.close_price_max_age_seconds:
        print(
            json.dumps(
                {
                    "event": "close_rejected_stale_price",
                    "time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "ticket": getattr(pos, "ticket", 0),
                    "tick_age_seconds": round(_tick_age, 1),
                    "max_allowed_seconds": config.close_price_max_age_seconds,
                    "reason": reason[:80],
                    "action": "refuse_dispatch_let_mt5_sltp_handle",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return False  # refuse dispatch — let MT5 server-side SL/TP handle it

    from core.execution.managed_close import dispatch_managed_close as _impl

    return _impl(
        config=config,
        pos=pos,
        reason=reason,
        mid=mid,
        state=state,
        strategy_name=strategy_name,
        exit_confidence=exit_confidence,
        exit_watchdog=exit_watchdog,
        mt5_worker=mt5_worker,
        exit_urgency=exit_urgency,
        factor_breakdown=factor_breakdown,
    )


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
    pnl_ledger: Any = None,
    ticket: int | None = None,
    micro_feature_dict: dict[str, float] | None = None,
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
        # MT5 IPC — FTC(CRASH) lets the crash propagate (no outer try/except)
        _mt5_pos = None
        with FaultTolerantContext(
            level=FaultLevel.CRASH,
            component="MT5_IPC:positions_get:MIA_guard",
        ):
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
                symbol=config.symbol,
            )
            # Enrich with MT5 deal history (close_price, reason)
            with FaultTolerantContext(
                level=FaultLevel.CRASH,
                component="MT5_IPC:history_deals_get:MIA_enrich",
            ):
                _deals = mt5_worker.history_deals_get(position=pos.ticket)
                if _deals:
                    _enrich_mia_from_deals(_mia_entry, _deals)
            state._pending_mia_closes.append(_mia_entry)
            pm.clear_position(ticket=pos.ticket)
            state.known_open_tickets.pop(pos.ticket, None)
            # Save position state immediately — don't wait for periodic save
            with FaultTolerantContext(
                level=FaultLevel.CRASH,
                component="pos_state_save_mia_close",
            ):
                pm.save_state(config.position_state_path)
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
    _price_degraded = True  # pre-init: assume degraded
    mid = bid = ask = float(getattr(pos, "entry_price", 0.0) or 0.0)  # fallback prices
    if broker is not None:
        with FaultTolerantContext(
            level=FaultLevel.DEGRADE,
            component="ManagementPhase:price_fetch",
        ):
            mid, bid, ask = broker.fetch_prices(config.symbol)
            state._last_bridge_ack_time = time.time()  # bridge liveness heartbeat
            _price_degraded = False
    else:
        # _mid_and_prices has internal FTC(CRASH) — let it propagate
        mid, bid, ask, _tick_time = _mid_and_prices(mt5_worker, config.symbol)
        if mid > 0:
            state._last_bridge_ack_time = time.time()
        _price_degraded = False
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

    # ── 1c. Alert evaluation (FIX-20260529-040: LiveAlertHub wiring) ──
    _ah = getattr(state, "alert_hub", None)
    if _ah is not None:
        with log_and_continue(component="AlertHub:dispatch"):
            # Build context from in-memory state only (Guardrail 3)
            _ctx_error_rate = 0.0
            _ctx_frozen = 0
            _ctx_pos_util = 0.0
            _ctx_bridge_last_ack = time.time() - getattr(
                state, "_last_bridge_ack_time", time.time()
            )

            # Position utilization
            if pm.has_position() if pos is not None else False:
                _ctx_pos_util = min(
                    1.0,
                    len(getattr(pm, "positions", []) if hasattr(pm, "positions") else [])
                    / max(1, config.max_positions),
                )

            # Cycle duration
            _ctx_cycle_duration = time.time() - getattr(
                state, "_last_cycle_start_time", time.time()
            )

            _ctx: dict[str, Any] = {
                "error_rate": _ctx_error_rate,
                "circuit_state": _ah.circuit_breaker.state.value,
                "frozen_brain_count": _ctx_frozen,
                "position_utilization": _ctx_pos_util,
                "bridge_last_ack_seconds": _ctx_bridge_last_ack,
                "cycle_duration_seconds": _ctx_cycle_duration,
            }

            # ── Phase B: PnL fund-safety context injection ──
            # FIX-20260603-066 P0: compute daily PnL from journal (SSOT),
            # not from in-memory accumulators that drift on restart.
            # FIX-20260606-138 Phase 0: filter by ack_status + dedup by
            # position_ticket to prevent retry/rejected entries from
            # polluting the rolling window (DQAF-20260606-005).
            _daily_pnl = 0.0
            _consec_losses = 0
            _win_count = 0
            _trade_count = 0
            try:
                from datetime import UTC
                from datetime import datetime as _dt

                _today = _dt.now(UTC).date()
                _jp = Path(config.base_dir) / "live_trade_journal.jsonl"
                if _jp.exists():
                    # Read backwards from end — journal grows, only need today.
                    # Backwards scan + seen_positions ensures we only count
                    # the MOST RECENT (final) close entry per position.
                    _lines = []
                    with open(_jp, encoding="utf-8") as _jf:
                        _lines = _jf.readlines()
                    _seen_positions: set[int] = set()
                    for _line in reversed(_lines):
                        _line = _line.strip()
                        if not _line:
                            continue
                        try:
                            _e = json.loads(_line)
                        except json.JSONDecodeError:
                            continue
                        if _e.get("action") != "close":
                            continue
                        # ── Phase 0 filter 1: skip rejected/retry entries ──
                        _ack = _e.get("ack_status", "")
                        if _ack not in ("accepted", "closed"):
                            continue
                        _ts = _e.get("recorded_at", "")
                        try:
                            _d = _dt.fromisoformat(_ts.replace("Z", "+00:00")).date()
                        except (ValueError, TypeError, OSError):
                            continue
                        if _d != _today:
                            continue
                        # ── Phase 0 filter 2: dedup by open position ──
                        # Prefer detail.request.position (the actual MT5
                        # position ticket being closed).  Fall back to the
                        # close order's own position_ticket.
                        _pos_tkt = _e.get("detail", {}).get("request", {}).get(
                            "position"
                        ) or _e.get("position_ticket")
                        if _pos_tkt is not None:
                            _pos_tkt = int(_pos_tkt)
                            if _pos_tkt in _seen_positions:
                                continue  # already counted a more recent close
                            _seen_positions.add(_pos_tkt)
                        _pnl = _e.get("pnl")
                        if _pnl is None:
                            continue
                        _pnl = float(_pnl)
                        _daily_pnl += _pnl
                        _trade_count += 1
                        if _pnl > 0:
                            _win_count += 1
                            _consec_losses = 0
                        elif _pnl < 0:
                            _consec_losses += 1
                _ctx["daily_pnl_usd"] = round(_daily_pnl, 2)
                _ctx["consecutive_losses"] = _consec_losses
                _ctx["rolling_win_rate"] = round(_win_count / max(1, _trade_count), 4)
                _ctx["total_trades_window"] = _trade_count
            except Exception:  # noqa: BLE001
                pass  # journal read is best-effort for alerts

            if pnl_ledger is not None:
                with log_and_continue(component="AlertHub:PnL_context"):
                    # FIX-20260607-XXX: "Frankenstein" logic fix.
                    # Previously _worst_pnl and _worst_wr were independently
                    # min()'d across all brains — they could come from two
                    # DIFFERENT brains (e.g. pnl from V4, wr from LGB_V1),
                    # producing a misleading "strategy" metric that describes
                    # no actual brain.  Now: find the single brain with the
                    # worst cumulative_pnl, and use ITS win_rate too.
                    _all_m = pnl_ledger.get_all_metrics()
                    if _all_m:
                        _worst_m = min(
                            _all_m.values(),
                            key=lambda m: getattr(m, "cumulative_pnl", 0.0),
                        )
                        _ctx["strategy_pnl"] = round(getattr(_worst_m, "cumulative_pnl", 0.0), 2)
                        _ctx["strategy_win_rate"] = round(getattr(_worst_m, "win_rate", 1.0), 4)
                        _ctx["worst_brain_id"] = getattr(_worst_m, "brain_id", "")
                    else:
                        _ctx["strategy_pnl"] = 0.0
                        _ctx["strategy_win_rate"] = 1.0
                        _ctx["worst_brain_id"] = ""

            _ah.evaluate_and_dispatch(_ctx)

    # ── 2. Update regime detector ──
    regime_info: dict[str, Any] = {}
    if regime_detector is not None:
        with log_and_continue(component="RegimeDetector:update"):
            regime_info = regime_detector.update(current_atr)

    # ── 3. Update position tracking ──
    # Pillar 1: Fetch current M5 bar for OHLC-calibrated extreme tracking.
    # M5 covers the full inter-cycle window; graceful degradation on failure.
    _m5_high, _m5_low, _m5_spread = None, None, 0
    if mt5_worker is not None:
        with FaultTolerantContext(
            level=FaultLevel.CRASH,
            component="MT5_IPC:copy_rates_from_pos:M5_OHLC_tracking",
        ):
            _m5_rates = mt5_worker.copy_rates_from_pos(config.symbol, 5, 0, 1)  # TIMEFRAME_M5
            if _m5_rates is not None and len(_m5_rates) > 0:
                _m5_bar = _m5_rates[0]
                _m5_high = float(_m5_bar["high"])
                _m5_low = float(_m5_bar["low"])
                try:
                    _m5_spread = int(_m5_bar["spread"])
                except (KeyError, ValueError, IndexError):
                    _m5_spread = 0
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
    # ── Phase 2: Position snapshot for meta-classifier training ──
    # Records per-cycle state: bars_held, unrealized PnL in R-units,
    # volatility change, trailing SL distance.  Used by MetaExit to
    # learn dynamic exit timing from real position trajectories.
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
        # Phase C: If normal R-multiple partial TP didn't fire, try microstructure-aware trigger
        if not should_ptp and micro_feature_dict is not None:
            _ofi_z = float(micro_feature_dict.get("OFI", 0.0) or 0.0)
            should_ptp, ptp_close_vol, ptp_remain_vol = pm.should_micro_partial_tp(
                mid, _ofi_z, ticket=pos.ticket
            )
            if should_ptp:
                _ofi_reason = f"ofi_{_ofi_z:.1f}z"
            else:
                _ofi_reason = ""
        else:
            _ofi_reason = ""
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
                except Exception:  # noqa: BLE001
                    logger.warning("Magic resolution failed for partial close — using default")
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
            except Exception as _ptp_exc:  # noqa: BLE001
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
                            "trigger": "ofi" if _ofi_reason else "r_milestone",
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

    # ── 6.7 Pending Close Lock (FIX-20260607-XXX) ──
    # Prevents cross-cycle retry avalanche: when ExitWatchdog is already
    # trying to close this position, subsequent management cycles must NOT
    # spawn fresh watchdog batches.  The lock auto-expires after
    # ActivePositionManager.PENDING_CLOSE_MAX_CYCLES to allow retry.
    if pm.is_pending_close(pos.ticket, state.loop_iteration):
        print(
            json.dumps(
                {
                    "event": "pending_close_skipped",
                    "time": _utc_iso(),
                    "ticket": pos.ticket,
                    "loop_iteration": state.loop_iteration,
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
            except Exception as _seq_exc:  # noqa: BLE001
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
            prop = None  # pre-initialise for DEGRADE
            with FaultTolerantContext(
                level=FaultLevel.DEGRADE,
                component="ManagementBrainInference",
            ):
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
                elif "swing" in schema_id or "daily" in schema_id:
                    # FIX-20260531-021: Data-driven assembly via schema registry
                    if daily_feature_provider is not None:
                        with FaultTolerantContext(
                            level=FaultLevel.DEGRADE,
                            component="ManagementBrainInference:DailyFeature",
                        ):
                            fv_24 = daily_feature_provider.get_latest()
                            tf_ou, tf_hurst = _compute_tf_ou_hurst(state._recent_mid_prices)
                            from core.features.schemas.registry import assemble_swing_features

                            fv = assemble_swing_features(
                                schema_id,
                                daily_features=fv_24,
                                tf_ou=tf_ou,
                                tf_hurst=tf_hurst,
                            )
                            raw = b_info["adapter"].infer(fv)
                            prop = b_info["adapter"].get_signal(raw)
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
                    # Direction is neutral — the entry brains still exist
                    # in the group even when votes deadlock.  Use brain_ids
                    # (all brains) NOT [] so flip calculation in
                    # evaluate_brain_exit() doesn't misinterpret a neutral
                    # deadlock as "100% of entry brains flipped".
                    # Confidence-drop exits remain proportional to group
                    # confidence.  Same union-mode all-neutral pattern as
                    # contract_groups.py.
                    _l2_direction = "neutral"
                    _l2_confidence = _entry_group_signal.confidence
                    _l2_supporting = _entry_group_signal.brain_ids
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
                # ── FIX-20260607-XXX: H4 Trend Protection Umbrella ──
                # When the H4/H1 macro trend still supports the position
                # direction, M5-level noise exits (brain_flip, confidence_decay,
                # bleed_stop) are PHYSICALLY BLOCKED.  The position gets room
                # to breathe — closing on a 5-minute wobble when the 4-hour
                # trend is still in your favor is a structural error.
                #
                # Trend hierarchy: H4 > H1 > M5 (same as entry counter_trend gate).
                # If H4 agrees with position → full protection.
                # If H4 neutral but H1 agrees → mild protection.
                # If both disagree or neutral → no protection (normal M5 exits).
                _trend_protected = False
                _trend_mild_protected = False
                _h4_dir = "neutral"
                _h1_dir = "neutral"
                if hasattr(state, "regime_gate") and state.regime_gate is not None:
                    try:
                        _h4_dir = state.regime_gate.h4_trend_direction
                        _h1_dir = state.regime_gate.h1_trend_direction
                        if _h4_dir != "neutral" and _h4_dir == pos.side:
                            _trend_protected = True  # H4 supports position
                        elif _h4_dir == "neutral" and _h1_dir == pos.side:
                            _trend_mild_protected = True  # H1 supports, H4 silent
                    except Exception:  # noqa: BLE001
                        pass

                # ── FIX-20260607-144: Override trend protection when losing ──
                # Trailing SL (Chandelier) only tightens in the PROFIT direction.
                # For a losing position, the SL stays frozen at entry — trend
                # protection becomes a trap, letting losses grow with no defense.
                # If unrealized PnL is below -1.0R, override protection:
                # the H4 "support" is either wrong or lagging.  Let M5 exits work.
                if _trend_protected or _trend_mild_protected:
                    _r_check = pm._compute_r_multiple(mid, ticket=pos.ticket) if mid else 0.0
                    if _r_check < -1.0:
                        _trend_protected = False
                        _trend_mild_protected = False
                        print(
                            json.dumps(
                                {
                                    "event": "trend_protection_overridden_loss",
                                    "time": _utc_iso(),
                                    "ticket": pos.ticket,
                                    "r": round(_r_check, 3),
                                    "reason": "position_underwater_despite_trend_support",
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )

                # Diagnostic: one-shot log when trend protection activates
                if (_trend_protected or _trend_mild_protected) and getattr(
                    pos, "cycles_held", 0
                ) <= 3:
                    print(
                        json.dumps(
                            {
                                "event": "trend_protection_active",
                                "time": _utc_iso(),
                                "ticket": pos.ticket,
                                "side": pos.side,
                                "h4_trend": _h4_dir,
                                "h1_trend": _h1_dir,
                                "protection_level": "full" if _trend_protected else "mild",
                                "action": "blocking_M5_noise_exits",
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

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
                    # Option B: trend-aligned → 5 bars tolerance (was 3)
                    if _trend_protected:
                        _bleed_bars = max(5, _bleed_bars)
                    elif _trend_mild_protected:
                        _bleed_bars = max(4, _bleed_bars)
                    _min_hold = max(2, _bleed_bars)
                    # Option A: trend-protected → double min_hold
                    if _trend_protected:
                        _min_hold = _min_hold * 2
                    if getattr(pos, "cycles_held", 0) < _min_hold:
                        _should_bleed, _bleed_reason = False, ""
                    else:
                        _r_now = pm._compute_r_multiple(mid, ticket=pos.ticket)
                        _should_bleed, _bleed_reason = pm.should_exit_bleed(
                            pos, _r_now, bleed_bars=_bleed_bars
                        )
                    if _should_bleed:
                        pm.mark_pending_close(pos.ticket, state.loop_iteration)
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
                                    pm.mark_pending_close(pos.ticket, state.loop_iteration)
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
                            except Exception:  # noqa: BLE001
                                logger.warning("OU brain exit failed — continuing management")
                            break  # only one OU brain

                should_exit = False
                exit_reason = ""
                if _flip_enabled:
                    # ── FIX-20260607-XXX: Trend Protection — block M5 brain_flip ──
                    # When H4 trend supports the position, physically block
                    # brain_flip and confidence_decay exits.  The brain changing
                    # its mind on a 5-minute bar is NOT a valid exit signal when
                    # the 4-hour trend is still in your favor.
                    if _trend_protected:
                        # Full protection: skip brain_flip/confidence_decay entirely.
                        # Only Trailing SL and MetaExit (which has its own trend
                        # awareness) can close the position.
                        should_exit = False
                        exit_reason = ""
                    else:
                        should_exit, exit_reason = pm.evaluate_brain_exit(
                            current_consensus,
                            current_supporting,
                            mid=mid,
                            ticket=pos.ticket,
                            kalman_velocity_bps=getattr(state, "_last_kalman_velocity_bps", None),
                        )
                        # Option B: mild protection — require higher confidence
                        if should_exit and _trend_mild_protected:
                            _exit_conf = float(current_consensus.get("consensus_score", 0.5))
                            if "brain_flip" in exit_reason and _exit_conf < 0.80:
                                should_exit = False
                                exit_reason = (
                                    f"brain_flip_shielded_trend_mild_conf_{_exit_conf:.2f}"
                                )
                            elif "confidence_decay" in exit_reason:
                                should_exit = False
                                exit_reason = "confidence_decay_shielded_trend_mild"
                # ── Phase C Fix 3: Price-Confirmation Shield ──
                # When confidence_decay triggers but price action confirms the
                # trade direction, veto the time-based exit and let Trailing SL
                # manage the position.
                if should_exit and "confidence_decay" in exit_reason and mid is not None:
                    _r_now = pm._compute_r_multiple(mid, ticket=pos.ticket)
                    _sl_trailing = (pos.side == "short" and pos.current_sl < pos.initial_sl) or (
                        pos.side == "long" and pos.current_sl > pos.initial_sl
                    )
                    if _r_now > 0.5 and _sl_trailing:
                        should_exit = False
                        exit_reason = f"confidence_decay_shielded_r{_r_now:.2f}_trailing"
                if should_exit:
                    _bf_confidence = float(
                        current_consensus.get("consensus_score", _exit_confidence)
                    )
                    pm.mark_pending_close(pos.ticket, state.loop_iteration)
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
            except Exception as exc:  # noqa: BLE001
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

            evaluation = pm.evaluate_meta_exit(
                mid=mid,
                current_atr=current_atr,
                regime_info=_regime_with_side,
                current_consensus=_meta_cons,
                current_supporting=_meta_sup,
                ticket=pos.ticket,
            )
            if evaluation is not None:
                # ── FIX-20260608-XXX: MetaExit demoted to TELEMETRY ONLY ──
                # The MetaExit ML model (data/models/meta_exit_model.txt) was
                # trained on only 833 samples with 8 journal-level features.
                # It has a 27.9% baseline WR, circular SL-distance dependency,
                # and a train-serve feature gap (training features ≠ runtime
                # ExitFeatureSnapshot features).  As of 2026-06-08, only 16
                # additional XAU trades are available — statistically zero.
                #
                # MetaExit is now SHADOW MODE: evaluate + log, but NEVER
                # dispatch a close.  Layer 1 (Trail SL) + Layer 2 (Brain Flip)
                # handle exits.  TODO: re-enable when >=500 XAU trades with
                # ExitFeatureSnapshot-level features are available for retraining.
                print(
                    json.dumps(
                        {
                            "event": "meta_exit_shadow_telemetry",
                            "time": _utc_iso(),
                            "ticket": pos.ticket,
                            "exit_urgency": round(evaluation.exit_urgency, 3),
                            "p_win": evaluation.p_win,
                            "exit_reason": evaluation.exit_reason,
                            "factor_breakdown": evaluation.factor_breakdown,
                            "action": "BLOCKED — telemetry only, close NOT dispatched",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001
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
            pm.mark_pending_close(pos.ticket, state.loop_iteration)
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
            pm.mark_pending_close(pos.ticket, state.loop_iteration)
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
    """Replay recent journal close entries to restore runtime guard state."""
    from core.runtime.restart_state import bootstrap_restart_state as _impl

    _impl(state=state, journal_path=journal_path, config=config)


def _reconcile_closed_positions(
    mt5_worker: Any,
    symbol: str,
    journal_path: str,
    known_tickets: dict[int, dict[str, Any]],
    state: Any = None,
) -> list[dict[str, Any]]:
    """Detect positions closed by SL/TP and return close journal entries."""
    from core.runtime.reconciliation import reconcile_closed_positions as _impl

    return _impl(
        mt5_worker=mt5_worker,
        symbol=symbol,
        journal_path=journal_path,
        known_tickets=known_tickets,
        state=state,
    )


def _build_mia_close_entry(
    pos: Any, known_entry: dict[str, Any], *, symbol: str = "XAUUSDc"
) -> dict[str, Any]:
    """Build a close journal entry for a position detected MIA in MT5.

    Called by _execute_management_phase when positions_get returns empty
    for a tracked ticket.  Uses all available engine-side info since MT5
    deal history may not be available yet (position just closed).

    symbol is a keyword-only parameter; caller MUST pass config.symbol.
    The default "XAUUSDc" exists only for backward compatibility.
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
        with fail_open_guard("MIA_MagicResolution"):
            from core.contracts.strategy_magic import MAGIC_TO_STRATEGY

            _resolved_strategy = MAGIC_TO_STRATEGY.get(int(_resolved_magic), "")

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
            "entry_price": entry_price,  # FIX-20260602-058: for PnL recalculation
        },
        "symbol": known_entry.get("symbol") or symbol,
        "action": "close",
        "side": side,
        "volume": close_volume,
        "entry_price": entry_price,  # FIX-20260602-058: for PnL recalculation
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
                # FIX-20260602-058: sync detail.pnl with recomputed PnL
                if isinstance(mia_entry.get("detail"), dict):
                    mia_entry["detail"]["pnl"] = mia_entry["pnl"]
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
    from core.parliament.group_consensus import compute_contract_group_consensus as _impl

    return _impl(
        raw_proposals=raw_proposals,
        brains=brains,
        tracker=tracker,
        pnl_ledger=pnl_ledger,
        correlation_tracker=correlation_tracker,
        base_volume=base_volume,
        current_atr=current_atr,
        regime_info=regime_info,
        total_budget=total_budget,
        lot_value=lot_value,
    )


# ── Multi-strategy evaluation ────────────────────────────────────────────


def _build_strategy_lines(
    brains: list[dict[str, Any]],
    config: LiveCycleConfig,
) -> dict[str, Any]:
    """Partition brains into contract groups and create strategy line objects."""
    from core.runtime.strategy_builder import build_strategy_lines as _impl

    return _impl(brains=brains, config=config)


def _build_meta_feature_vector(
    *,
    brains: list[dict[str, Any]],
    feature_store: Any,
    mid_price: float | None,
    symbol: str,
) -> tuple[Any, dict[str, float] | None]:
    """Build 40-dim raw feature vector for meta-labeling binary classifier."""
    from core.features.meta_feature_builder import build_meta_feature_vector as _impl

    return _impl(
        brains=brains,
        feature_store=feature_store,
        mid_price=mid_price,
        symbol=symbol,
    )


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
    # ── FIX-20260607-007: trend maturity signals ──
    hurst: float | None = None,
    kalman_velocity_bps: float | None = None,
    meta_filter_gate: Any = None,
    conformal_ou_gate: Any = None,
    micro_feature_dict: dict[str, float] | None = None,
    cooldown_registry: Any = None,
    family_entry_tracker: Any = None,
    mtf_price_service: Any = None,
    meta_feature_vector: Any = None,
    # ── FIX-20260606-131: reentry guard front-placement (P2.6) ──
    reentry_states: dict[str, Any] | None = None,
    reentry_sl_cooldown: float | None = None,
    reentry_sl_penalty: float | None = None,
    reentry_bleed_cooldown: float | None = None,
    reentry_bleed_penalty: float | None = None,
    # ── FIX-20260606-138: Fail-Closed on bootstrap degradation ──
    bootstrap_degraded: bool = False,
    btc_augment: Any = None,  # FIX-20260607-XXX: pre-computed 37-dim BTC vector
) -> dict[str, Any]:
    """Run independent strategy evaluations + portfolio risk + execution queue."""
    from core.runtime.strategy_evaluator import evaluate_strategy_lines as _impl

    return _impl(
        strategy_lines=strategy_lines,
        feature_vector=feature_vector,
        micro_feature_vector=micro_feature_vector,
        mid_price=mid_price,
        bid=bid,
        ask=ask,
        current_atr=current_atr,
        regime_info=regime_info,
        regime_gate=regime_gate,
        regime_modulation=regime_modulation,
        trend_direction=trend_direction,
        trend_strength=trend_strength,
        h4_trend_strength=h4_trend_strength,
        macro_regime=macro_regime,
        risk_budget_usd=risk_budget_usd,
        sl_streak_blocked_until=sl_streak_blocked_until,
        portfolio_risk=portfolio_risk,
        execution_queue=execution_queue,
        tracker=tracker,
        pnl_ledger=pnl_ledger,
        current_positions=current_positions,
        session_volume_mult=session_volume_mult,
        health_volume_mult=health_volume_mult,
        micro_sequences=micro_sequences,
        daily_feature_vector=daily_feature_vector,
        account_equity=account_equity,
        cycle_count=cycle_count,
        meta_signal_filter=meta_signal_filter,
        meta_filter_gate=meta_filter_gate,
        conformal_ou_gate=conformal_ou_gate,
        micro_feature_dict=micro_feature_dict,
        cooldown_registry=cooldown_registry,
        family_entry_tracker=family_entry_tracker,
        mtf_price_service=mtf_price_service,
        meta_feature_vector=meta_feature_vector,
        # ── FIX-20260607-007 ──
        hurst=hurst,
        kalman_velocity_bps=kalman_velocity_bps,
        reentry_states=reentry_states,
        reentry_sl_cooldown=reentry_sl_cooldown,
        reentry_sl_penalty=reentry_sl_penalty,
        reentry_bleed_cooldown=reentry_bleed_cooldown,
        reentry_bleed_penalty=reentry_bleed_penalty,
        bootstrap_degraded=bootstrap_degraded,
        btc_augment=btc_augment,  # FIX-20260607-XXX
    )


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
    alert_hub: Any = None,
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

    .. rubric:: Architectural Roadmap

    This function is a ~3,450-line monolithic state machine with **40 mutable
    state fields** read/written across **13 logical phases**.  Big-bang
    refactoring is FORBIDDEN — use the Strangler Fig pattern instead: extract
    ONE phase at a time, only when that phase is next modified.

    **Pipeline flow** (line numbers are section markers, not exact boundaries)::

        ── STARTUP ──
        Phase  0   L2232   Resolve MT5, circuit breaker init, heartbeat
        Phase  1   L2312   First-cycle reconciliation, restart bootstrap
        Phase  1a  L2388   Daily ops auto-scheduler (Highlander Rule)
        Phase  1b  L2428   Startup orphan detection (MT5 vs state file)

        ── DATA ACQUISITION & EXIT MANAGEMENT ──
        Phase  2   L2525   Reconcile closed positions, SL streak tracking    [资金高危]
        Phase  2a  L2691   Protection flag, mid-price, tick sanity
        Phase  3   L2821   Shadow verification settlement                    [资金高危]
        Phase  3a  L2866   Cooldown check, SL streak circuit breaker
        Phase  3b  L3031   Market-closed guard, P&L settlement anchor
        Phase  3c  L3062   Dynamic exit management (_execute_management_phase)
        Phase  3d  L3104   MIA close processing, journal write

        ── FEATURES & GATES ──
        Phase  4   L3216   Feature computation, entry_context build          [最易剥离]
        Phase  4a  L3264   MetaFilter gate + Conformal OU gate (lazy init)
        Phase  4b  L3379   Daily D1 features, feature persistence
        Phase  5   L3445   Market regime detection, feature gate, inference

        ── RISK & BUDGET ──
        Phase  6   L3502   Account equity, risk budget, vol-targeted sizing

        ── ═══════ ROUTING FORK ═══════
        │ if config.multi_brain and config.multi_strategy_enabled (default True):
        │
        ├─ ✅ NEW PATH (always taken with defaults) ─────────────────────
        │
        Phase  7   L3543   Multi-strategy evaluation, regime gate propagation
        Phase  7a  L3673   Pre-close check, session detection
        Phase  8   L3912   Cooldown registry, execution guard restore       [资金高危]
        Phase  8a  L3951   MT5 position query, quarantine auto-clear
        Phase  8b  L4053   Circuit breaker drawdown kill, re-entry quality
        Phase  8c  L4300   Quarantine check, NET_OUT reassignment
        Phase  8d  L4367   Trade notification, gate telemetry, family entry
        Phase  8e  L4481   Position registration, shadow record
        │
        │   → L4725  return (early exit — new path complete)
        │
        └─ ⚠️  LEGACY PATH (unreachable with default config) ───────────
             FIX-20260517-018: dead code — retained as rollback reference only.
             Phase  9   L4730   Contract-group consensus
             Phase 10   L5098   Shadow verification, risk eval, dispatch

        ── SHARED TAIL ──
        Phase 11   L5440   Per-model horizon map, position registration
        Phase 12   L5634   Circuit breaker degraded-cycle tracking

    **Immutability guardrail**: When extracting any phase, do NOT pass the
    entire ``state`` object.  Extract as a pure function that receives only
    the fields it reads, and returns results to be explicitly merged back
    into ``state`` by the caller.  This is how ``_execute_management_phase``
    and ``_reconcile_closed_positions`` were already extracted — follow that
    precedent.
    """
    import numpy as np  # ensure np available in all code paths (local imports at L4506/L6503 may not execute)

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
    if alert_hub is not None:
        state.alert_hub = alert_hub

    # ── Cycle-start heartbeat ──
    _cycle_start_wall = time.time()
    # ── FIX-068 debug: dump reentry state on cycle 1 ──
    if state.loop_iteration == 1 and state._reentry_states:
        for _sname, _rs in state._reentry_states.items():
            _le = _rs.last_exit
            print(
                json.dumps(
                    {
                        "event": "reentry_state_debug",
                        "step": "cycle1_start",
                        "strategy": _sname,
                        "has_last_exit": _le is not None,
                        "exit_timestamp": _le.timestamp if _le else None,
                        "exit_confidence": _le.confidence if _le else None,
                        "exit_reason": _le.reason if _le else None,
                        "consecutive_same_dir": _rs.consecutive_same_direction,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
                flush=True,
            )
    print(
        json.dumps(
            {"event": "cycle_start", "time": _utc_iso(), "iteration": state.loop_iteration},
            ensure_ascii=False,
        ),
        flush=True,
    )
    # Track cycle start time for bridge liveness proxy + cycle stall detection
    state._last_cycle_start_time = _cycle_start_wall
    if getattr(state, "_last_bridge_ack_time", 0) == 0:
        state._last_bridge_ack_time = _cycle_start_wall

    # ── Phase B: Bridge silence check (FIX-20260607-XXX) ──
    # If the MT5 bridge has not returned a successful price fetch for longer
    # than max_bridge_silence_seconds, the bridge is dead.  Trip the circuit
    # breaker immediately (no 3-cycle grace period — bridge death is binary).
    _bridge_silence = time.time() - state._last_bridge_ack_time
    if _bridge_silence > config.max_bridge_silence_seconds and not state._circuit_breaker_tripped:
        state._circuit_breaker_tripped = True
        state._circuit_breaker_tripped_at = time.time()
        print(
            json.dumps(
                {
                    "event": "circuit_breaker_bridge_silence_trip",
                    "time": _utc_iso(),
                    "bridge_silence_seconds": round(_bridge_silence, 1),
                    "max_allowed_seconds": config.max_bridge_silence_seconds,
                    "action": "management_only_mode",
                },
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
        # ── FIX-20260607-142: Fail-Safe Exit Gateway ─────────────────
        # When the circuit breaker is tripped (e.g. dispatch pipeline crash,
        # consecutive degraded cycles), attempt to close ALL open MT5
        # positions directly.  This is a cold-blooded last-resort mechanism
        # that bypasses the brain/queue/execution pipeline entirely.
        # Market close-all protects capital when the system is in an
        # unknown or degraded state.
        if not config.no_mt5 and mt5_worker is not None:
            try:
                _open_positions = mt5_worker.positions_get(symbol=config.symbol) or []
                # ── FIX-20260608-005: bridge liveness heartbeat ──
                # positions_get() success proves MT5 bridge is alive even
                # during management-only mode.  Update heartbeat so the
                # cooldown-based auto-reset can detect bridge recovery.
                state._last_bridge_ack_time = time.time()
                if _open_positions:
                    _closed_any = False
                    for _pos in _open_positions:
                        _ticket = _pos.ticket
                        _side = "short" if _pos.type == 1 else "long"
                        _vol = float(getattr(_pos, "volume", 0) or 0)
                        _close_side = "buy" if _side == "short" else "sell"
                        with fail_open_guard("CircuitBreakerClose"):
                            _cb_result = mt5_worker.order_send(
                                symbol=config.symbol,
                                order_type=1,  # Market
                                volume=_vol,
                                side=_close_side,
                                ticket=_ticket,
                                magic=getattr(_pos, "magic", 0),
                            )
                            _closed_any = True
                            print(
                                json.dumps(
                                    {
                                        "event": "circuit_breaker_close",
                                        "time": _utc_iso(),
                                        "ticket": _ticket,
                                        "side": _side,
                                        "volume": _vol,
                                        "result": str(_cb_result)[:200],
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                    if _closed_any:
                        # After emergency close, reset the circuit breaker
                        state._circuit_breaker_tripped = False
                        state.block_new_entries = True  # keep blocked — manual review needed
            except Exception as _cb_exc:  # noqa: BLE001
                print(
                    json.dumps(
                        {
                            "event": "circuit_breaker_close_all_failed",
                            "time": _utc_iso(),
                            "error": str(_cb_exc)[:200],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    # ── Phase B: Cycle stall detection (FIX-20260607-XXX) ──
    # A single cycle taking longer than cycle_stall_threshold_seconds is a
    # strong signal of pipeline blockage (MT5 hang, feature computation stall,
    # IPC deadlock).  Increment the degraded counter so that 3 consecutive
    # stalled cycles trip the circuit breaker.
    _cycle_duration = time.time() - state._last_cycle_start_time
    if _cycle_duration > config.cycle_stall_threshold_seconds:
        state._consecutive_degraded_cycles += 1
        print(
            json.dumps(
                {
                    "event": "cycle_stall_detected",
                    "time": _utc_iso(),
                    "cycle_duration_seconds": round(_cycle_duration, 1),
                    "threshold_seconds": config.cycle_stall_threshold_seconds,
                    "consecutive_degraded": state._consecutive_degraded_cycles,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if state._consecutive_degraded_cycles >= 3:
            state._circuit_breaker_tripped = True
            state._circuit_breaker_tripped_at = time.time()
            print(
                json.dumps(
                    {
                        "event": "circuit_breaker_cycle_stall_trip",
                        "time": _utc_iso(),
                        "consecutive_degraded": state._consecutive_degraded_cycles,
                        "action": "management_only_mode",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    # ── Circuit breaker unified auto-reset (DQAF-20260608-001) ──
    # Old logic (line 2774): `if not degraded_wakeup and _consecutive_degraded_cycles > 0`
    # only covered the cycle-stall/degraded-wakeup trip paths.  Bridge-silence and
    # ExecutionQueueFatalError trips never incremented _consecutive_degraded_cycles,
    # so the auto-reset condition was permanently false → breaker stuck forever.
    #
    # New logic: cooldown-based unified reset.  After circuit_breaker_cooldown_seconds,
    # if ALL triggering conditions have cleared, reset the breaker unconditionally.
    if state._circuit_breaker_tripped:
        _cooldown_elapsed = (
            time.time() - state._circuit_breaker_tripped_at
        ) > config.circuit_breaker_cooldown_seconds
        _bridge_alive = _bridge_silence <= config.max_bridge_silence_seconds
        _not_stalled = _cycle_duration <= config.cycle_stall_threshold_seconds
        _not_degraded = not degraded_wakeup
        if _cooldown_elapsed and _bridge_alive and _not_stalled and _not_degraded:
            print(
                json.dumps(
                    {
                        "event": "circuit_breaker_reset",
                        "time": _utc_iso(),
                        "reason": "cooldown_elapsed_all_conditions_clear",
                        "tripped_duration_seconds": round(
                            time.time() - state._circuit_breaker_tripped_at, 1
                        ),
                        "previous_consecutive_degraded": state._consecutive_degraded_cycles,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            state._circuit_breaker_tripped = False
            state._circuit_breaker_tripped_at = 0.0
            state._consecutive_degraded_cycles = 0
    elif not degraded_wakeup and state._consecutive_degraded_cycles > 0:
        # Breaker NOT tripped but degraded counter > 0 → reset counter on clean cycle
        state._consecutive_degraded_cycles = 0

    # ── FIX-20260603-074: On first cycle, reconcile positions closed during
    # downtime BEFORE the restart state bootstrap.  Positions in known_open_tickets
    # that are no longer open in MT5 were closed (by SL/TP/external) while the
    # process was down.  Run reconciliation before bootstrap so that
    # known_open_tickets only contains ACTUALLY open positions.  Otherwise the
    # bootstrap skips the most recent close entries (their open_message_id is
    # still in the stale known_open_tickets) and falls back to ancient exits →
    # stale_exit_allowed bypasses the reentry guard → restart-immediate-trade.
    # ── On first cycle, reconcile positions closed during downtime ──
    if state.loop_iteration == 1 and state.known_open_tickets and not config.no_mt5:
        with FaultTolerantContext(
            level=FaultLevel.CRASH,
            component="MT5_IPC:positions_get:startup_reconciliation",
        ):
            _positions = mt5_worker.positions_get(symbol=config.symbol) or []
        with log_and_continue(component="StartupReconciliation"):
            _open_tickets = {p.ticket for p in _positions}
            _gone_tickets = set(state.known_open_tickets.keys()) - _open_tickets
            if _gone_tickets:
                _gone_dict = {
                    t: state.known_open_tickets[t]
                    for t in _gone_tickets
                    if t in state.known_open_tickets
                }
                with log_and_continue(component="StartupReconciliation:journal_write"):
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
            # Filter to only currently-open positions AFTER reconciliation
            state.known_open_tickets = {
                t: r for t, r in state.known_open_tickets.items() if t in _open_tickets
            }

    # ── Restart state bootstrap: replay recent journal closes ──
    # FIX-20260603-074: MUST run AFTER reconciliation so known_open_tickets
    # only contains actually-open positions.  Otherwise _active_open_mids
    # includes stale entries → bootstrap skips the most recent close →
    # falls back to ancient exit → stale_exit_allowed → restart-immediate-trade.
    if state.loop_iteration == 1 and not config.no_mt5:
        _bootstrap_restart_state(state, str(journal_path), config)
        for _rs in state._reentry_states.values():
            _rs.consecutive_same_direction = 0

    # ── Daily ops auto-scheduler (The Highlander Rule) ──
    # Primary: Fixed UTC 22:00–23:00 window (= 06:00–07:00 CST).
    # FIX-20260604-078: Fallback — if >24h since last run AND not today,
    # trigger immediately regardless of time.  Prevents daily_ops from
    # never executing when the system never survives to 22:00 UTC.
    if state.loop_iteration == 1 and state._last_daily_ops_utc == 0:
        state._last_daily_ops_utc = _load_daily_ops_state(config.base_dir)
    with log_and_continue(component="DailyOps:scheduling"):
        _now_utc = datetime.now(UTC)
        _today_22z = _now_utc.replace(hour=22, minute=0, second=0, microsecond=0)
        _window_end = _today_22z + timedelta(hours=1)
        _last_date = (
            datetime.fromtimestamp(state._last_daily_ops_utc, UTC).date()
            if state._last_daily_ops_utc > 0
            else None
        )
        _already_ran_today = _last_date == _now_utc.date()
        _in_primary_window = _today_22z <= _now_utc < _window_end
        _fallback_overdue = (
            state._last_daily_ops_utc > 0
            and (time.time() - state._last_daily_ops_utc) > 86400
            and not _already_ran_today
        )
        if (_in_primary_window or _fallback_overdue) and not _already_ran_today:
            if _fallback_overdue and not _in_primary_window:
                print(
                    json.dumps(
                        {
                            "event": "daily_ops_fallback_triggered",
                            "time": _utc_iso(),
                            "hours_since_last": round(
                                (time.time() - state._last_daily_ops_utc) / 3600, 1
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            _run_scheduled_daily_ops(config, state)

        # ── Startup orphan detection: MT5 vs active_position.json ──
        try:
            _ap_path = os.path.join(config.base_dir, config.position_state_path)
            _ap_tickets: set[int] = set()
            if os.path.exists(_ap_path):
                with open(_ap_path) as _f:
                    _ap = json.load(_f)
                if isinstance(_ap, dict):
                    # FIX-20260601-040: support both v2 ("tickets": [int]) and
                    # v3 ("positions": [{"ticket": int}]) state file formats.
                    # v2 → direct list of ticket IDs; v3 → list of position dicts.
                    _ap_tickets = set()
                    _v2_tickets = _ap.get("tickets", [])
                    if isinstance(_v2_tickets, list):
                        for t in _v2_tickets:
                            try:  # noqa: SIM105
                                _ap_tickets.add(int(t))
                            except (TypeError, ValueError):
                                pass
                    _v3_positions = _ap.get("positions", [])
                    if isinstance(_v3_positions, list):
                        for p in _v3_positions:
                            if isinstance(p, dict):
                                try:  # noqa: SIM105
                                    _ap_tickets.add(int(p.get("ticket", 0)))
                                except (TypeError, ValueError):
                                    pass
                    _ap_tickets.discard(0)  # remove any zero placeholder
            _mt5_positions = mt5_worker.positions_get(symbol=config.symbol) or []
            _mt5_tickets = {p.ticket for p in _mt5_positions}
            _orphans = _mt5_tickets - _ap_tickets - set(state.known_open_tickets.keys())
            if _orphans:
                # FIX-20260601-040: HARD_BLOCK → adopt-and-continue.
                # Previously the system refused to start when it found a
                # position in MT5 that wasn't in the state file or known_open
                # tracker.  This caused crash-loops whenever the v2→v3 state
                # migration left orphan detection blind (see FIX-036/040).
                # Now we adopt the orphan into managed tracking — the exit
                # watchdog will handle it normally.  If it's a ghost, it gets
                # closed.  If it's real, management resumes.
                # ── FIX-20260607-141: enrich orphan adoption with MT5 data ──
                # Previously, orphan adoption stored only minimal metadata
                # (source + adopted_at).  The exit watchdog needs SL, TP, entry
                # price, direction, and volume to manage the position properly.
                # Without enrichment, the watchdog may ignore or mismanage
                # adopted positions.
                for _ot in sorted(_orphans):
                    _pos_data: dict[str, Any] = {
                        "source": "orphan_adopted",
                        "adopted_at": _utc_iso(),
                    }
                    # Enrich from MT5 position data
                    for _p in _mt5_positions:
                        if _p.ticket == _ot:
                            _pos_data.update(
                                {
                                    "ticket": _ot,
                                    "direction": "short" if _p.type == 1 else "long",
                                    "entry_price": float(getattr(_p, "price_open", 0) or 0),
                                    "current_sl": float(getattr(_p, "sl", 0) or 0),
                                    "current_tp": float(getattr(_p, "tp", 0) or 0),
                                    "volume": float(getattr(_p, "volume", 0) or 0),
                                    "enriched_from_mt5": True,
                                }
                            )
                            break
                    state.known_open_tickets[_ot] = _pos_data
                print(
                    json.dumps(
                        {
                            "event": "orphan_position_adopted",
                            "time": _utc_iso(),
                            "severity": "WARNING",
                            "adopted_tickets": sorted(_orphans),
                            "mt5_tickets": sorted(_mt5_tickets),
                            "active_position_tickets": sorted(_ap_tickets),
                            "known_open_tickets": sorted(state.known_open_tickets.keys()),
                            "action": "adopted_into_managed_tracking",
                            "note": (
                                "Orphan positions from MT5 adopted. "
                                "Exit watchdog will manage them normally. "
                                "If positions are already closed on MT5, "
                                "they will be cleaned up on the next cycle."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
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
        except Exception as _exc:  # noqa: BLE001
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
                            except Exception:  # noqa: BLE001
                                logger.warning(
                                    "PortfolioRiskController.update_returns failed "
                                    "strategy=%s pnl=%s",
                                    _strategy,
                                    _pnl,
                                )

                    # ── Collect for per-strategy budget recording (processed after
                    #     strategies are built, since budgets live on StrategyLine) ──
                    _pnl_val = _entry.get("pnl")
                    if _pnl_val is not None:
                        # Fetch equity from MT5 (must succeed or crash)
                        _eq = 0.0
                        if mt5_worker is not None:
                            with FaultTolerantContext(
                                level=FaultLevel.CRASH,
                                component="MT5_IPC:account_info:PnL_to_equity",
                            ):
                                _acc = mt5_worker.account_info()
                                _eq = float(getattr(_acc, "equity", 0)) if _acc is not None else 0.0
                        # Convert dollar PnL to percentage of account equity
                        _pnl_pct = float(_pnl_val) / _eq if _eq > 0 else 0.0
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
        except Exception:  # noqa: BLE001
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
    _tick_time: float = 0.0  # FIX-20260607-XXX: for staleness detection
    if broker is not None:
        with FaultTolerantContext(
            level=FaultLevel.DEGRADE,
            component="PriceFetch:broker",
        ):
            mid_price, _bid, _ask = broker.fetch_prices(config.symbol)
    elif not config.no_mt5:
        # _mid_and_prices has internal FTC(CRASH) — let it propagate
        mid_price, _bid, _ask, _tick_time = _mid_and_prices(mt5_worker, config.symbol)

    # ── FIX-20260607-XXX: Staleness Contract (Iron Law #11 Data Analytics) ──
    # Data pipeline freeze detection: if MT5 returns the same stale tick for
    # multiple cycles, the system is "blind" — all trading decisions based on
    # this data are invalid.  Fail-closed: skip the cycle, and trip the
    # circuit breaker after 3 consecutive stale cycles.
    _stale_this_cycle = False
    if _tick_time > 0:
        _data_age = time.time() - _tick_time
        state._last_tick_age = round(_data_age, 3)  # for diagnostics
        if _data_age > config.max_data_age_seconds:
            _stale_this_cycle = True
            state._consecutive_stale_cycles += 1
            print(
                json.dumps(
                    {
                        "event": "data_stale",
                        "time": _utc_iso(),
                        "data_age_seconds": round(_data_age, 1),
                        "max_allowed_seconds": config.max_data_age_seconds,
                        "consecutive_stale_cycles": state._consecutive_stale_cycles,
                        "tick_time_unix": _tick_time,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if state._consecutive_stale_cycles >= 3:
                state._circuit_breaker_tripped = True
                state._circuit_breaker_tripped_at = time.time()
                print(
                    json.dumps(
                        {
                            "event": "circuit_breaker_staleness_trip",
                            "time": _utc_iso(),
                            "consecutive_stale_cycles": state._consecutive_stale_cycles,
                            "data_age_seconds": round(_data_age, 1),
                            "action": "management_only_mode",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        else:
            state._consecutive_stale_cycles = 0

    # ── Stale-cycle early return: skip all trading decisions ──
    # When data is stale but the circuit breaker has NOT yet tripped (first
    # 1-2 stale cycles), skip the remainder of this cycle.  The rolling
    # buffer and MTF service will still process on the next cycle when fresh
    # data arrives.  If the circuit breaker HAS tripped, the management-only
    # path at the top of the next cycle handles position close-out.
    if _stale_this_cycle and not state._circuit_breaker_tripped:
        # Still update the rolling buffer so it stays warm
        if mid_price is not None and mid_price > 0:
            state._recent_mid_prices.append(mid_price)
            if len(state._recent_mid_prices) > 50:
                state._recent_mid_prices.pop(0)
        # MTF service needs fresh ticks — skip when stale
        print(
            json.dumps(
                {
                    "event": "stale_cycle_skipped",
                    "time": _utc_iso(),
                    "consecutive_stale_cycles": state._consecutive_stale_cycles,
                    "action": "skip_trading_keep_buffers_warm",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return state, True  # continue running, skip this cycle's decisions
    if mid_price is not None and mid_price > 0:
        state._recent_mid_prices.append(mid_price)
        if len(state._recent_mid_prices) > 50:
            state._recent_mid_prices.pop(0)

    # ── MTF Price Service: M15 bar reconstruction from M5 tick history ──
    if not hasattr(state, "_mtf_price_service") or state._mtf_price_service is None:
        state._mtf_price_service = MTFPriceService()
        # Bootstrap from historical M5 closes so M15 bars are available immediately
        if not config.no_mt5 and mt5_worker is not None:
            with FaultTolerantContext(
                level=FaultLevel.CRASH,
                component="MT5_IPC:copy_rates_from_pos:MTF_bootstrap",
            ):
                _hist_rates = mt5_worker.copy_rates_from_pos(
                    config.symbol, 5, 0, 200
                )  # TIMEFRAME_M5
                if _hist_rates is not None and len(_hist_rates) >= 6:
                    _closes = [float(r[4]) for r in _hist_rates]
                    state._mtf_price_service.bootstrap(_closes)
    if mid_price is not None and mid_price > 0 and state._mtf_price_service is not None:
        with log_and_continue(component="MTFPrice:feed_tick"):
            _now_s = int(datetime.now(UTC).timestamp())
            state._mtf_price_service.feed_tick(_now_s, mid_price)

    # ── Tick sanity check ──
    if _bid is not None and _ask is not None and _bid > 0:
        with log_and_continue(component="TickSanity:check"):
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

    # ── Limit order monitor: check pending orders for spread-aware fills ──
    if state.limit_monitor is not None and state.limit_monitor.has_pending():
        with log_and_continue(component="LimitMonitor:check_fill"):
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

    # ── Shadow verification: settle previous cycle's consensus decision ──
    if mid_price is not None and mid_price > 0 and state.shadow_verification_pending:
        try:
            with log_and_continue(component="ShadowVerify"):
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
            with log_and_continue(component="SLStreak:market_normalization"):
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
        # _position_count internally wraps MT5 IPC in FTC(CRASH) — let it propagate
        pos_count = (
            broker.count_positions(config.symbol)
            if broker is not None
            else _position_count(mt5_worker, config.symbol)
        )

        # -1 means MT5 connection is dead — attempt reconnect once
        if pos_count < 0 and mt5_worker is not None:
            mt5_worker.reconnect()
            pos_count = (
                broker.count_positions(config.symbol)
                if broker is not None
                else _position_count(mt5_worker, config.symbol)
            )

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
            with log_and_continue(component="MarketGuard:session_detect"):
                from core.execution.pre_trade_guards import detect_session

                _pre_session = detect_session(
                    market_type=getattr(config, "market_type", "forex_24_5")
                )
                if _pre_session.get("risk_tier") == "off":
                    _log_cycle_end(state.loop_iteration)
                    return state, True  # market closed — skip entire cycle

        # Phase C: micro_feature_dict needed by management phase (OFI partial TP).
        # Initialised None here; populated by feature computation below on each cycle.
        micro_feature_dict: dict[str, float] | None = None

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
            with log_and_continue(component="PnLLedger:settle"):
                _live_spread = float(_ask - _bid) if (_bid and _ask and _ask > _bid) else 0.0
                pnl_ledger.update_pending(mid_price)
                pnl_ledger.settle_all(mid_price, spread=_live_spread, slippage=0.10)

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
                        pnl_ledger=pnl_ledger,
                        ticket=_pm_pos.ticket,
                        micro_feature_dict=micro_feature_dict,
                    )
            except Exception:
                logger.exception(
                    "Management phase aborted for ticket=%s — position state not updated",
                    _pm_pos.ticket,
                )
                _ah = getattr(state, "alert_hub", None)
                if _ah is not None:
                    _ah.send_critical(
                        "management_phase_failure",
                        {"ticket": _pm_pos.ticket, "cycle": state.loop_iteration},
                    )
            # Persist position state every N cycles (trail steps, breakeven, etc.)
            if state.loop_iteration % 5 == 0 and state.position_manager is not None:
                with log_and_continue(component="PositionState:periodic_save"):
                    state.position_manager.save_state(config.position_state_path)

        # ── Process MIA close entries collected by _execute_management_phase ──
        # FIX-20260525-024: When a position disappears from MT5 between
        # reconciliation cycles, the management phase detects it and stores
        # a close entry in _pending_mia_closes.  We must write these to the
        # journal and record them for reentry guard, otherwise:
        #   - Journal has no close entry → PnL hole
        #   - Reentry guard gets unknown_exit → permanent block
        #   - Position state file stays stale
        # ── Single-point-of-exit: notify DingTalk for ALL position closes ──
        def _emit_close_notification(
            _ah: Any, _sym: str, _side: str, _vol: float, _price: float | None, _pnl: float | None
        ) -> None:
            """Notify DingTalk when a position is closed, regardless of close path.

            Called from MIA-detected closes AND dispatch-driven closes.
            Fire-and-forget — never blocks the main loop.
            """
            import contextlib

            if _ah is None:
                return
            with contextlib.suppress(Exception):
                _ah.notify_trade(
                    action="close",
                    symbol=_sym,
                    side=_side,
                    volume=_vol,
                    price=_price,
                    pnl=_pnl,
                )

        if state._pending_mia_closes:
            _mia_closed = state._pending_mia_closes
            state._pending_mia_closes = []
            # ── Write to journal (same FileLock pattern as reconciliation) ──
            with log_and_continue(component="MIA_Close:journal_write"):
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
            # ── Record exit for reentry guard ──
            for _entry in _mia_closed:
                _exit_strategy = _entry.get("strategy", "")
                _exit_side = _entry.get("side", "")
                _exit_price = float(_entry.get("detail", {}).get("close_price", 0) or 0)
                _exit_ts_str = _entry.get("recorded_at", "")
                _exit_ts = time.time()
                if _exit_ts_str:
                    with log_and_continue(component="MIA_Close:parse_timestamp"):
                        _parsed = datetime.fromisoformat(_exit_ts_str.replace("Z", "+00:00"))
                        _exit_ts = _parsed.timestamp()
                _exit_confidence = (
                    _entry.get("entry_consensus", {}).get("consensus_score", 0.5)
                    if isinstance(_entry.get("entry_consensus"), dict)
                    else 0.5
                )
                _exit_reason = _entry.get("detail", {}).get("reason", "mia_close")
                if _exit_strategy and _exit_side in ("long", "short"):
                    with log_and_continue(component="MIA_Close:reentry_guard"):
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
                # ── Notify DingTalk: MIA-detected close ──
                _emit_close_notification(
                    _ah=getattr(state, "alert_hub", None),
                    _sym=_entry.get("symbol", config.symbol),
                    _side=_exit_side if _exit_side in ("long", "short") else _entry.get("side", ""),
                    _vol=float(_entry.get("volume", 0) or 0),
                    _price=_exit_price,
                    _pnl=_entry.get("pnl"),
                )
            # ── Save position state immediately ──
            if state.position_manager is not None:
                with FaultTolerantContext(
                    level=FaultLevel.CRASH,
                    component="PositionState:save_after_mia_close",
                ):
                    state.position_manager.save_state(config.position_state_path)

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
    # micro_feature_dict initialised above (before management phase) — reused here
    from core.features.schemas.registry import get_schema_dimension as _schema_dim

    if config.no_mt5:
        feature_vector: Any = np.zeros(_schema_dim("v9_institutional_40"), dtype=np.float64)
        micro_feature_vector: Any = np.zeros(_schema_dim("v4.3_microstructure_9"), dtype=np.float64)
    else:
        trigger = {"symbol": config.symbol, "venue": "MT5"}
        feature_vector = feature_service.build_feature_vector(trigger)

        # Compute microstructure 9-feature vector for Transformer/XGBoost brains
        if micro_feature_computer is not None and micro_feature_adapter is not None:
            micro_sequences = {}
            with FaultTolerantContext(
                level=FaultLevel.DEGRADE,
                component="FeatureCompute:micro_sequences",
            ):
                micro_sequences = micro_feature_computer.compute_all_sequences(32)
            micro_feature_dict = {}
            micro_feature_vector = np.zeros(_schema_dim("v4.3_microstructure_9"), dtype=np.float64)
            with FaultTolerantContext(
                level=FaultLevel.DEGRADE,
                component="FeatureCompute:micro_features",
            ):
                micro_features = micro_feature_computer.compute_all()
                micro_feature_dict = micro_features
                micro_feature_vector = micro_feature_adapter.build_model_input(
                    micro_features
                ).ravel()
        else:
            micro_feature_vector = np.zeros(_schema_dim("v4.3_microstructure_9"), dtype=np.float64)

    # ── Build entry_context for journal (Phase 1: 40-dim feature snapshot) ──
    # Guardrail 1: schema versioning — V9 vs future V10 prevents feature drift
    # Guardrail 2: immutability — tuple deep-copy prevents async mutation
    # Guardrail 3: NaN safety — nan_to_num prevents JSON serialization failures
    _entry_features_snapshot: dict[str, Any] = {
        "schema_version": "v9_institutional",
        "vector": tuple(np.nan_to_num(np.asarray(feature_vector, dtype=np.float64)).tolist()),
    }

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
                state_path=f"{config.base_dir}/conformal_calibrator_state.json",
            )
            _cal.cold_start_from_journal(f"{config.base_dir}/live_trade_journal.jsonl")
        except Exception:
            with fail_open_guard("ConformalCalibratorInit"):
                raise

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
        except Exception:
            with fail_open_guard("MetaFilterGateInit"):
                raise

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
        except Exception as _oug_exc:  # noqa: BLE001
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
    daily_feature_vector: Any = None  # pre-initialised for DEGRADE
    if daily_feature_provider is not None:
        with FaultTolerantContext(
            level=FaultLevel.DEGRADE,
            component="FeatureCompute:daily_feature",
        ):
            daily_feature_vector = daily_feature_provider.get_latest()

    # ── Persist features to LocalFeatureStore ──
    if not config.disable_feature_store and not config.no_mt5:
        with log_and_continue(component="FeatureStore:write"):
            from core.deployment.feature_update_producer import produce_from_live_computer

            for record in produce_from_live_computer(
                feature_computer, feature_schema, config.symbol
            ):
                feature_store.write_records([record])

    # ── Feature freshness check (cycle-level visible alert) ──
    if not config.no_mt5 and feature_store is not None:
        with log_and_continue(component="FeatureCheck:freshness"):
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
                        state._consecutive_stale_features += 1
                        print(
                            json.dumps(
                                {
                                    "event": "feature_stale_warning",
                                    "time": _utc_iso(),
                                    "age_seconds": freshness.get("age_seconds"),
                                    "max_age_seconds": freshness["max_age_seconds"],
                                    "consecutive_stale_features": state._consecutive_stale_features,
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        if state._consecutive_stale_features >= 3:
                            state._circuit_breaker_tripped = True
                            state._circuit_breaker_tripped_at = time.time()
                            print(
                                json.dumps(
                                    {
                                        "event": "circuit_breaker_feature_staleness_trip",
                                        "time": _utc_iso(),
                                        "consecutive_stale_features": state._consecutive_stale_features,
                                        "action": "management_only_mode",
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                    else:
                        state._consecutive_stale_features = 0

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
        # _get_current_atr internally wraps MT5 IPC in FTC(CRASH) — let it propagate
        current_atr = (
            broker.fetch_current_atr(config.symbol)
            if broker is not None
            else _get_current_atr(mt5_worker, config.symbol)
        )
        try:
            if current_atr > 0:
                regime_info = regime_detector.update(current_atr)
                # Rolling ATR buffer for adaptive circuit breaker
                state._recent_atr_values.append(current_atr)
                if len(state._recent_atr_values) > 50:
                    state._recent_atr_values.pop(0)
        except Exception:  # noqa: BLE001
            logger.warning("Regime detector update failed — using stale regime values")

    # ── Feature gate: block garbage-in before it becomes garbage-out ──
    if not config.no_mt5:
        with log_and_continue(component="FeatureGate:check"):
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
    except Exception:  # noqa: BLE001
        logger.warning("Broker equity fetch failed — falling back to MT5 direct query")
    if _account_equity is None and mt5_worker is not None:
        with FaultTolerantContext(
            level=FaultLevel.CRASH,
            component="MT5_IPC:account_info:equity_risk_budget",
        ):
            _acc = mt5_worker.account_info()
            _account_equity = float(getattr(_acc, "equity", 0)) if _acc is not None else 0.0

    # ── Equity-based risk budget: overrides fixed risk_budget_usd when equity_risk_pct > 0 ──
    _effective_risk_budget = config.risk_budget_usd
    if config.equity_risk_pct > 0 and _account_equity is not None and _account_equity > 0:
        _effective_risk_budget = round(_account_equity * config.equity_risk_pct, 2)

    # Vol-targeted position sizing — override fixed volume when risk_budget_usd > 0
    if _effective_risk_budget > 0 and current_atr > 0:
        with FaultTolerantContext(
            level=FaultLevel.DEGRADE,
            component="VolumeTarget:compute_position_size",
        ):
            from core.execution.pre_trade_guards import compute_position_size

            dynamic_volume = compute_position_size(
                risk_budget_usd=_effective_risk_budget,
                atr=current_atr,
                sl_atr_mult=config.sl_atr_mult,
                min_lot=config.min_lot,
                max_lot=config.max_lot,
                lot_step=config.lot_step,
                symbol=config.symbol,
            )
            _vol_targeted = True

    if config.multi_brain and config.multi_strategy_enabled:
        # ── NEW: Multi-strategy independent evaluation ──
        # Each contract group runs independently → portfolio risk → staggered dispatch

        # Partition brains into contract groups and build strategy lines
        strategies = _build_strategy_lines(brains, config)
        state._strategies = strategies  # FIX-072: stash for execution state persistence

        # ── Feed pending budget records from reconciliation ──
        if state._pending_budget_records:
            for _rec in state._pending_budget_records:
                _sname = _rec["strategy"]
                _strat = strategies.get(_sname)
                if _strat is not None and _strat.budget is not None:
                    with log_and_continue(component="Budget:record_trade"):
                        _strat.budget.record_trade(_rec["pnl"], _rec["is_win"])
            state._pending_budget_records.clear()

        # ── Feed pending SL records for graduated per-SL cooldown ──
        if state._pending_sl_records:
            for _rec in state._pending_sl_records:
                _sname = _rec["strategy"]
                _strat = strategies.get(_sname)
                if _strat is not None and _strat.budget is not None:
                    with log_and_continue(component="Budget:record_sl"):
                        _result = _strat.budget.record_sl(_rec.get("timestamp"))
                        if _result.get("event") != "sl_recorded":
                            print(
                                json.dumps(_result, ensure_ascii=False),
                                flush=True,
                            )
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
        # FIX-20260607-007: trend maturity signals for sizing + exit
        _m5_hurst: float | None = None
        _h1_kalman_velocity_bps: float | None = None

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
                # FIX-20260607-007: extract trend maturity signals for sizing + exit
                _m5_hurst = regime_gate_result.get("m5_hurst")
                _h1_ema_slope_raw = regime_gate_result.get("h1_ema_slope")
                if _h1_ema_slope_raw is not None:
                    # h1_ema_slope = velocity_scaled / 10000 ≈ velocity in % per bar
                    # Convert to bps: multiply by 10000
                    _h1_kalman_velocity_bps = round(float(_h1_ema_slope_raw) * 10000, 2)
                # FIX-20260607-007: persist for next cycle's exit management
                state._last_kalman_velocity_bps = _h1_kalman_velocity_bps
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
            except Exception as _rg_exc:  # noqa: BLE001
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

                session_info = detect_session(
                    market_type=getattr(config, "market_type", "forex_24_5")
                )
                if session_info.get("risk_tier") == "off":
                    _log_cycle_end(state.loop_iteration)
                    return state, True  # market closed, skip cycle

                # Intraday drawdown kill switch — tracks equity peak-to-trough
                if config.intraday_drawdown_kill_enabled:
                    # Fetch current equity from MT5 account (must succeed or crash)
                    with FaultTolerantContext(
                        level=FaultLevel.CRASH,
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
                                state.block_new_entries = True  # FIX-080: CB trip
                                print(
                                    json.dumps(
                                        {
                                            "event": "intraday_drawdown_kill",
                                            "time": _utc_iso(),
                                            "drawdown_pct": dd_result["drawdown_pct"],
                                            "high_watermark": dd_result["high_watermark"],
                                            "current_equity": dd_result["current_equity"],
                                            "force_close": dd_result.get("force_close", False),
                                            "circuit_breaker": "OPEN — new entries blocked",
                                        },
                                        ensure_ascii=False,
                                    ),
                                    flush=True,
                                )
                            elif state.block_new_entries:
                                # DD recovered — clear the block
                                state.block_new_entries = False
                                print(
                                    json.dumps(
                                        {
                                            "event": "intraday_drawdown_recovered",
                                            "time": _utc_iso(),
                                            "circuit_breaker": "CLOSED — new entries allowed",
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
                                                except Exception:  # noqa: BLE001
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
                                        except Exception as _fc_exc:  # noqa: BLE001
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
                        logger.exception(
                            "Force-close dispatch orchestration failed ticket=%s",
                            _pos.ticket,
                        )
                        _ah = getattr(state, "alert_hub", None)
                        if _ah is not None:
                            _ah.send_critical(
                                "force_close_dispatch_failed",
                                {"ticket": _pos.ticket, "cycle": state.loop_iteration},
                            )
                        # Continue to next position — do not abandon the cycle

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
                logger.exception("Feature vector quality check crashed — halting cycle")
                _ah = getattr(state, "alert_hub", None)
                if _ah is not None:
                    _ah.send_critical(
                        "feature_vector_check_crash",
                        {"cycle": state.loop_iteration},
                    )
                _log_cycle_end(state.loop_iteration)
                return state, True  # skip cycle — garbage features would corrupt inference

        # ── Cut 1 + 2: Initialize cooldown registry & family entry tracker ──
        if state._cooldown_registry is None:
            from core.execution.pre_trade_guards import CooldownRegistry

            state._cooldown_registry = CooldownRegistry()
        if state._family_entry_tracker is None:
            from core.execution.pre_trade_guards import FamilyEntryTracker

            state._family_entry_tracker = FamilyEntryTracker()

        # ── FIX-20260603-072: restore execution guard state from disk ──
        # Runs once after lazy-init above.  Silently passes if no persisted
        # state exists (first run or stale >24h snapshot).
        if state.loop_iteration == 1 and state._cooldown_registry is not None:
            try:
                from core.runtime.execution_state import restore_execution_state

                restore_execution_state(state, strategies, data_dir=config.base_dir)
            except Exception:  # noqa: BLE001
                pass

        # Portfolio risk controller (persist for VaR/correlation tracking) + execution queue
        if state.portfolio_risk_controller is None:
            state.portfolio_risk_controller = PortfolioRiskController(
                max_gross_exposure=config.portfolio_max_gross,
                max_net_exposure=config.portfolio_max_net,
                max_same_direction=config.portfolio_max_same_dir,
                netting_mode=config.portfolio_netting_mode,
                symbol_contract_size=config.contract_size,  # FIX-20260601-037: BTC=1.0, XAU=100.0
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
            with FaultTolerantContext(
                level=FaultLevel.CRASH,
                component="MT5_IPC:positions_get:portfolio_risk",
            ):  # fmt: skip
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

        # ── Circuit breaker: skip all new entries when drawdown kill is active ──
        # FIX-080: intraday drawdown kill now physically blocks new entries via
        # block_new_entries flag (set in drawdown kill blocks above).
        if state.block_new_entries:
            print(
                json.dumps(
                    {
                        "event": "circuit_breaker_entries_blocked",
                        "time": _utc_iso(),
                        "reason": "intraday_drawdown_kill_active",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            _log_cycle_end(state.loop_iteration)
            return state, True  # skip entry logic, continue management

        # ── Golden Master recording: capture inputs before evaluation ──
        _gm_capture = None
        try:
            from core.runtime.golden_master import record_cycle_inputs

            _fv_sample = None
            if feature_vector is not None:
                import numpy as np

                _fv_arr = np.asarray(feature_vector, dtype=np.float64).ravel()
                _fv_sample = _fv_arr
            _gm_capture = record_cycle_inputs(
                cycle_count=state.loop_iteration,
                mid_price=mid_price,
                bid=_bid,
                ask=_ask,
                current_atr=current_atr if current_atr else 0.0,
                regime_info=regime_info,
                trend_direction=trend_direction,
                trend_strength=trend_strength if trend_strength is not None else 0.0,
                macro_regime=macro_regime,
                risk_budget_usd=_effective_risk_budget,
                session_volume_mult=session_info.get("volume_mult", 1.0),
                health_volume_mult=state._last_health_volume_mult or 1.0,
                hurst=_m5_hurst,  # FIX-20260607-143: trend maturity observability
                feature_vector_sample=_fv_sample,
                data_dir=config.base_dir,
            )
        except Exception as _gm_exc:  # noqa: BLE001
            import logging as _gm_log

            _gm_log.getLogger(__name__).warning(
                "Golden Master record_cycle_inputs failed: %s", _gm_exc
            )

        # ── FIX-20260607-XXX: BTC 37-dim feature augmentation ──
        # Compute btc_augment for BTC brains using btc_macro_enhanced_37 schema.
        # Must be computed BEFORE strategy evaluation so SwingStrategy._run_inference()
        # can pass it to assemble_features_by_schema(), avoiding the legacy
        # XAU-centric fallback path (which has incorrect cross-asset slots).
        _btc_aug: Any = None
        if config.symbol == "BTCUSDc" and daily_feature_vector is not None:
            try:
                _aug = getattr(state, "_btc_augmenter", None)
                if _aug is None:
                    from core.features.computers.btc_feature_augmenter import (
                        BTCFeatureAugmenter,
                    )

                    _aug = BTCFeatureAugmenter(feature_store, mt5_worker=mt5_worker)
                    state._btc_augmenter = _aug
                tf_ou, tf_hurst = _compute_tf_ou_hurst(state._recent_mid_prices)
                _btc_aug = _aug.augment(
                    daily_feature_vector,
                    micro_feature_vector,
                    btc_price=mid_price or 0.0,
                    tf_ou=tf_ou,
                    tf_hurst=tf_hurst,
                )
            except Exception:  # noqa: BLE001
                import logging as _btc_log2
                import traceback as _btc_tb2

                _btc_log2.getLogger(__name__).error(
                    "BTCFeatureAugmenter failed in main eval — "
                    "V6 brains using btc_macro_enhanced_37 will get "
                    "legacy XAU-centric features (slots [12][30][35][36] incorrect).\n%s",
                    _btc_tb2.format_exc(),
                )

        # Evaluate all strategy lines
        eval_summary = _evaluate_strategy_lines(
            strategy_lines=strategies,
            feature_vector=feature_vector,
            micro_feature_vector=micro_feature_vector,
            mid_price=mid_price,
            bid=_bid,  # FIX-20260529-038: wire real bid price for Max_Spread_Gate
            ask=_ask,  # FIX-20260529-038: wire real ask price for Max_Spread_Gate
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
            # ── FIX-20260607-007: trend maturity signals ──
            hurst=_m5_hurst,
            kalman_velocity_bps=_h1_kalman_velocity_bps,
            # ── FIX-20260606-131: reentry guard front-placement ──
            reentry_states=state._reentry_states,
            reentry_sl_cooldown=config.reentry_sl_cooldown,
            reentry_sl_penalty=config.reentry_sl_penalty,
            reentry_bleed_cooldown=config.reentry_bleed_cooldown,
            reentry_bleed_penalty=config.reentry_bleed_penalty,
            # ── FIX-20260606-138: Fail-Closed on bootstrap degradation ──
            bootstrap_degraded=getattr(state, "_bootstrap_degraded", False),
            btc_augment=_btc_aug,  # FIX-20260607-XXX
        )

        # ── Golden Master recording: capture outputs after evaluation ──
        if _gm_capture is not None:
            try:
                from core.runtime.golden_master import record_cycle_outputs

                _decisions_map = eval_summary.get("decisions_map", {})
                _strategy_outputs: dict[str, Any] = {}
                for _sn, _sd in _decisions_map.items():
                    _strategy_outputs[_sn] = {
                        "direction": getattr(_sd, "direction", "neutral"),
                        "confidence": getattr(_sd, "confidence", 0.0),
                        "should_trade": getattr(_sd, "should_trade", False),
                        "reason": getattr(_sd, "reason", ""),
                        "volume": getattr(_sd, "volume", 0.0),
                        "sl": getattr(_sd, "sl", 0.0),
                        "tp": getattr(_sd, "tp", 0.0),
                    }
                record_cycle_outputs(
                    _gm_capture,
                    strategy_results=eval_summary.get("strategy_results", {}),
                    decisions_map=_decisions_map,
                    trade_decisions=eval_summary.get("trade_decisions", 0),
                    queued=eval_summary.get("queued", 0),
                    data_dir=config.base_dir,
                )
            except Exception as _gm_exc:  # noqa: BLE001
                import logging as _gm_log

                _gm_log.getLogger(__name__).warning(
                    "Golden Master record_cycle_outputs failed: %s", _gm_exc
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
                    try:  # noqa: SIM105
                        state.signal_health_monitor.feed_prediction(
                            up_prob=float(bp.get("up_prob", 0.5)),
                            down_prob=float(bp.get("down_prob", 0.5)),
                            confidence=float(bp.get("confidence", 0.5)),
                        )
                    except (TypeError, ValueError):
                        pass

        # ── Volume decay for consecutive same-direction entries ──────────
        # FIX-20260606-131: reentry quality check moved to Cut 3 in
        # strategy_evaluator.evaluate_strategy_lines().  This section now
        # only applies volume decay — all queued decisions already passed
        # the reentry guard during evaluation (no more ghost signals).
        if exec_queue.queue_size > 0 and state._reentry_states:
            from core.execution.reentry_guard import (  # noqa: I001
                apply_reentry_volume_scale,
                ensure_reentry_state,
            )

            for _qd in exec_queue._queue:
                _rs = ensure_reentry_state(state._reentry_states, _qd.strategy_name)
                _cons = _rs.consecutive_same_direction
                if _cons > 0:
                    _scaled_vol, _should_block = apply_reentry_volume_scale(
                        _qd.decision.volume, _cons
                    )
                    if _should_block:
                        _qd.decision.volume = 0.0
                        _qd.decision.should_trade = False
                    else:
                        _qd.decision.volume = _scaled_vol

        # ── FIX-20260606-128: reentry block streak alert ──────────────────
        # Scans strategy results for reentry-blocked strategies (Cut 3 in
        # strategy_evaluator).  When a strategy is blocked for ≥ 5
        # consecutive cycles, fires a warning via alert hub.
        _strat_results = eval_summary.get("strategy_results", [])
        _ah_reentry = getattr(state, "alert_hub", None)
        for _sr in _strat_results:
            _sname = _sr.get("strategy", "")
            _reason = _sr.get("reason", "")
            if not _sr.get("should_trade") and (
                "brain_flip" in _reason
                or "meta_exit" in _reason
                or "sl_" in _reason
                or "ou_revert" in _reason
                or "unknown" in _reason
                or "bleed" in _reason
                or "momentum" in _reason
                or "hesitation" in _reason
            ):
                _streak_key = f"_reentry_block_streak_{_sname}"
                _streak = getattr(state, _streak_key, 0) + 1
                setattr(state, _streak_key, _streak)
                if _streak >= 5 and _streak % 5 == 0 and _ah_reentry is not None:
                    _alert = {
                        "rule_name": "reentry_persistent_block",
                        "rule_id": f"reentry_block_{_sname}_{int(time.time())}",
                        "severity": "warning",
                        "title": f"Reentry Block: {_sname} ({_streak} cycles)",
                        "text": (
                            f"## {_sname} 重入守卫持续拦截\n\n"
                            f"- 连续拦截: **{_streak}** 个周期\n"
                            f"- 拦截原因: {_reason}\n"
                            f"- 时间: {_utc_iso()}\n\n"
                            f"> 请检查退出类型和历史置信度。"
                        ),
                        "timestamp_utc": _utc_iso(),
                        "context": {
                            "strategy": _sname,
                            "consecutive_blocks": _streak,
                            "reason": _reason,
                        },
                    }
                    import contextlib

                    with contextlib.suppress(Exception):
                        _ah_reentry._alert_queue.put_nowait(_alert)
            else:
                # Reset streak when strategy passes or isn't reentry-blocked
                _streak_key = f"_reentry_block_streak_{_sname}"
                if hasattr(state, _streak_key):
                    setattr(state, _streak_key, 0)

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
                    # ── Phase 2: cross-cycle exit retry cooldown ──
                    # If this position has been rejected ≥3 consecutive
                    # cycles, skip the exit attempt for 10 cycles to
                    # prevent retry storms (DQAF-20260606-005).
                    import time as _cooldown_time

                    _now_ts = _cooldown_time.time()
                    _cd_until = state._exit_reject_cooldown.get(int(_ticket), 0.0)
                    if _now_ts < _cd_until:
                        _remaining = int(_cd_until - _now_ts)
                        print(
                            json.dumps(
                                {
                                    "event": "exit_cooldown_skipped",
                                    "time": _utc_iso(),
                                    "ticket": _ticket,
                                    "reason": _reason,
                                    "cooldown_remaining_s": _remaining,
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        return {
                            "dispatched": False,
                            "intent_id": "",
                            "reason": "exit_cooldown_active",
                        }
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
                    # ── Phase 2: update reject streak / cooldown ──
                    _tkt_key = int(_ticket)
                    if _wd.success:
                        state._exit_reject_streak.pop(_tkt_key, None)
                        state._exit_reject_cooldown.pop(_tkt_key, None)
                    else:
                        _streak = state._exit_reject_streak.get(_tkt_key, 0) + 1
                        state._exit_reject_streak[_tkt_key] = _streak
                        if _streak >= 3:
                            _cooldown_s = 300  # 10 cycles × 30s
                            _cd_deadline = _now_ts + _cooldown_s
                            state._exit_reject_cooldown[_tkt_key] = _cd_deadline
                            print(
                                json.dumps(
                                    {
                                        "event": "exit_cooldown_activated",
                                        "time": _utc_iso(),
                                        "ticket": _ticket,
                                        "consecutive_rejects": _streak,
                                        "cooldown_seconds": _cooldown_s,
                                        "message": (
                                            "Position exit has been rejected "
                                            f"{_streak} times consecutively. "
                                            "Cooling down for 10 cycles to "
                                            "prevent retry storm."
                                        ),
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                    return {
                        "dispatched": _wd.success,
                        "intent_id": "",
                        "pnl": _net_pnl,  # FIX-138-Phase3: pass PnL through to notify_trade
                    }

                _net_out_close_dispatch_fn = _net_out_close_dispatch_fn

            dispatch_results = exec_queue.flush(
                partial(dispatch_live_open_order, entry_context=_entry_features_snapshot),
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

                # ── FIX-20260602-059: real-time trade notification ──
                # FIX-138-Phase3: pass PnL + dedup per position_ticket to
                # prevent retry storms from flooding DingTalk (DQAF-006).
                _notified_tickets: set[int] = set()
                for dr in dispatch_results:
                    if not dr.dispatched:
                        continue
                    _ah = getattr(state, "alert_hub", None)
                    if _ah is None:
                        continue

                    _action = "open" if dr.reason != "net_out_close" else "close"
                    _tkt = (
                        dr.net_out_ticket_update.get("old_ticket", 0)
                        if dr.net_out_ticket_update
                        else 0
                    )
                    # Dedup: only one close notification per ticket per cycle
                    if _action == "close" and _tkt:
                        if _tkt in _notified_tickets:
                            continue
                        _notified_tickets.add(_tkt)
                    if _action == "close":
                        # Single-point-of-exit: unified close notification
                        _emit_close_notification(
                            _ah=_ah,
                            _sym=config.symbol,
                            _side=dr.direction,
                            _vol=dr.volume,
                            _price=dr.price if hasattr(dr, "price") else None,
                            _pnl=dr.pnl,
                        )
                    else:
                        # Open notification — fire-and-forget
                        import contextlib

                        with contextlib.suppress(Exception):
                            _ah.notify_trade(
                                action="open",
                                symbol=config.symbol,
                                side=dr.direction,
                                volume=dr.volume,
                                price=dr.price if hasattr(dr, "price") else None,
                                pnl=dr.pnl,
                            )

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

            # ── FIX-20260603-067 P2: gate telemetry per dispatch result ──
            for dr in dispatch_results:
                _sname = dr.strategy_name
                _reason = dr.reason if not dr.dispatched else "dispatched"
                if _sname not in state._gate_stats:
                    state._gate_stats[_sname] = {}
                state._gate_stats[_sname][_reason] = state._gate_stats[_sname].get(_reason, 0) + 1
            state._gate_stats_cycles += 1
            if state._gate_stats_cycles >= 12:
                _tp = Path(config.base_dir) / "reports" / "telemetry_gates.jsonl"
                try:
                    _tp.parent.mkdir(parents=True, exist_ok=True)
                    _payload = {
                        "event": "gate_telemetry",
                        "time": _utc_iso(),
                        "cycles": state._gate_stats_cycles,
                        "symbol": config.symbol,
                        "gates": state._gate_stats,
                    }
                    with open(_tp, "a", encoding="utf-8") as _tf:
                        _tf.write(json.dumps(_payload, ensure_ascii=False) + "\n")
                except OSError:
                    pass
                state._gate_stats.clear()
                state._gate_stats_cycles = 0

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
                            daily_feature_vector=daily_feature_vector,
                        )
                        _record_brain_outcomes(strategy_proposals, dr.direction, "pending", tracker)
                    except Exception as _bi_exc:  # noqa: BLE001
                        print(
                            json.dumps(
                                {
                                    "event": "brain_inference_failed",
                                    "time": _utc_iso(),
                                    "strategy": dr.strategy_name,
                                    "error": f"{type(_bi_exc).__name__}: {str(_bi_exc)[:200]}",
                                    "level": "DEGRADE",
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )

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
                                    except Exception:  # noqa: BLE001
                                        pass  # malformed journal line → skip
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
                            # Phase C: Microstructure-aware partial TP (OFI-based)
                            ofi_partial_tp_threshold=_tp_cfg.get("ofi_partial_tp_threshold", 0.0),
                            ofi_partial_tp_r_mult=_tp_cfg.get("ofi_partial_tp_r_mult", 0.5),
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
                        with FaultTolerantContext(
                            level=FaultLevel.CRASH,
                            component="PositionState:save_after_register",
                        ):
                            state.position_manager.save_state(config.position_state_path)
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
                            except Exception:  # noqa: BLE001
                                pass
                    except Exception as _reg_exc:  # noqa: BLE001
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
                        bid=_bid,  # FIX-20260529-038: wire real bid for Max_Spread_Gate
                        ask=_ask,  # FIX-20260529-038: wire real ask for Max_Spread_Gate
                        current_atr=current_atr,
                        regime_info=regime_info,
                        regime_gate_mode="full",
                        trend_direction=trend_direction,
                        trend_strength=trend_strength,
                        h4_trend_strength=h4_trend_strength,
                        hurst=_m5_hurst,  # FIX-20260607-007
                        kalman_velocity_bps=_h1_kalman_velocity_bps,  # FIX-20260607-007
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
                except Exception:  # noqa: BLE001
                    logger.warning("Shadow verification registration failed strategy=%s", sname)

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

                _s = detect_session(market_type=getattr(config, "market_type", "forex_24_5"))
                if _s.get("risk_tier") == "off":
                    _log_cycle_end(state.loop_iteration)
                    return state, not config.once
                float(_s.get("volume_mult", 1.0))

                # Intraday drawdown kill for legacy path
                if config.intraday_drawdown_kill_enabled:
                    # MT5 IPC — FTC(CRASH), extracted from legacy try/except
                    _acc = None
                    with FaultTolerantContext(
                        level=FaultLevel.CRASH,
                        component="MT5_IPC:account_info:drawdown_kill_legacy",
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
                            _dd = state.intraday_dd_kill.update(_eq)
                            if _dd.get("blocked"):
                                state.block_new_entries = True  # FIX-080
                                print(
                                    json.dumps(
                                        {
                                            "event": "intraday_drawdown_kill_legacy",
                                            "time": _utc_iso(),
                                            "drawdown_pct": _dd["drawdown_pct"],
                                            "force_close": _dd.get("force_close", False),
                                            "circuit_breaker": "OPEN",
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
                                        except Exception as _fc_exc:  # noqa: BLE001
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
                            elif state.block_new_entries:
                                # DD recovered — clear the block
                                state.block_new_entries = False
                                print(
                                    json.dumps(
                                        {
                                            "event": "intraday_drawdown_recovered_legacy",
                                            "time": _utc_iso(),
                                            "circuit_breaker": "CLOSED",
                                        },
                                        ensure_ascii=False,
                                    ),
                                    flush=True,
                                )
                    except Exception:  # noqa: BLE001
                        logger.warning("Intraday drawdown recovery check failed")

                _fv = check_feature_vector(feature_vector)
                if not _fv.get("passed"):
                    _log_cycle_end(state.loop_iteration)
                    return state, not config.once
            except Exception:  # noqa: BLE001
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
                    except Exception:  # noqa: BLE001
                        # Fallback: bypass run() pipeline.
                        # Transformer adapters: use infer_sequence() to avoid rolling-buffer corruption.
                        # XGBoost adapters: use infer() with flat ravel (model expects 288-dim).
                        try:
                            if hasattr(b_info["adapter"], "infer_sequence"):
                                seq_batch: np.ndarray = seq.astype(np.float32).reshape(
                                    1, seq.shape[0], 9
                                )
                                raw = b_info["adapter"].infer_sequence(seq_batch)
                            else:
                                raw = b_info["adapter"].infer(seq.ravel().astype(np.float64))
                            prop = b_info["adapter"].get_signal(raw)
                        except Exception:  # noqa: BLE001
                            prop = None
                else:
                    prop = None
            elif "swing" in schema_id or "daily" in schema_id:
                # FIX-20260531-021: Data-driven assembly via schema registry
                if daily_feature_vector is not None:
                    prop = None  # pre-initialise for DEGRADE
                    with FaultTolerantContext(
                        level=FaultLevel.DEGRADE,
                        component="EntryBrain:SwingBrain",
                    ):
                        tf_ou, tf_hurst = _compute_tf_ou_hurst(state._recent_mid_prices)
                        from core.features.schemas.registry import assemble_swing_features

                        # ── FIX-20260606-134: BTC feature augmenter ──
                        _btc_aug = None
                        if config.symbol == "BTCUSDc":
                            try:
                                _aug = getattr(state, "_btc_augmenter", None)
                                if _aug is None:
                                    from core.features.computers.btc_feature_augmenter import (  # noqa: I001
                                        BTCFeatureAugmenter,
                                    )

                                    _fs = getattr(feature_service, "_store", None)
                                    _aug = BTCFeatureAugmenter(_fs, mt5_worker=mt5_worker)
                                    state._btc_augmenter = _aug
                                _btc_aug = _aug.augment(
                                    daily_feature_vector,
                                    micro_feature_vector,
                                    btc_price=mid_price or 0.0,
                                    tf_ou=tf_ou,
                                    tf_hurst=tf_hurst,
                                )
                            except Exception:  # noqa: BLE001
                                import logging as _btc_log
                                import traceback as _btc_tb

                                _btc_log.getLogger(__name__).error(
                                    "BTCFeatureAugmenter.augment() CRASHED — "
                                    "BTC cross-asset slots [12][30][35][36] will be "
                                    "zero-filled.  Train-serve skew is ACTIVE.  "
                                    "Fix the augmenter before trusting brain inference.\n%s",
                                    _btc_tb.format_exc(),
                                )
                                # FIX-20260607-XXX: Do NOT silently fall back.
                                # btc_aug remains None → assemble_swing_features()
                                # will zero-fill slots [35-36] via legacy path.
                                # This is intentionally visible — the operator
                                # must fix the augmenter, not ignore the skew.

                        fv = assemble_swing_features(
                            schema_id,
                            daily_features=daily_feature_vector,
                            micro_features=micro_feature_vector,
                            tf_ou=tf_ou,
                            tf_hurst=tf_hurst,
                            btc_augment=_btc_aug,
                        )
                        raw = b_info["adapter"].infer(fv)
                        prop = b_info["adapter"].get_signal(raw)
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
                except Exception:  # noqa: BLE001
                    logger.warning("PnL ledger signal recording failed (multi-strategy)")
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
            except Exception:  # noqa: BLE001
                logger.warning("PnL ledger signal recording failed (legacy)")

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
        except Exception:  # noqa: BLE001
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
            except Exception as exc:  # noqa: BLE001
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
                mid, bid, ask, _tick_time = _mid_and_prices(mt5_worker, config.symbol)
        except Exception as _price_exc:  # noqa: BLE001
            # MT5 connection may have gone stale during cooldown — attempt reconnect
            try:
                if not config.no_mt5:
                    mt5_worker.reconnect()
                if broker is not None:
                    mid, bid, ask = broker.fetch_prices(config.symbol)
                else:
                    mid, bid, ask, _tick_time = _mid_and_prices(mt5_worker, config.symbol)
            except Exception:  # noqa: BLE001
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
            except Exception as exc:  # noqa: BLE001
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
            except Exception as exc:  # noqa: BLE001
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
            entry_context=_entry_features_snapshot,
        )
        state.last_fire = now

        # ── Publish dispatch event to message broker (best-effort) ──
        with log_and_continue(component="MessageBroker:publish"):
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
                        except Exception:  # noqa: BLE001
                            logger.warning("Intent ID lookup failed for dispatch attribution")
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
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "Magic-to-strategy lookup failed for journal attribution"
                            )

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
            except Exception as _reg_exc:  # noqa: BLE001
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
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "Open ticket journal enrichment failed ticket=%s", ticket
                            )
                        break
            except Exception:  # noqa: BLE001
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
            state._circuit_breaker_tripped_at = time.time()
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
