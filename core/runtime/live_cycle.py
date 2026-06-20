"""Live trading cycle execution — one iteration of the intent loop.

Extracted from scripts/live_intent_loop.py to keep the CLI script thin
(CLI + init + main loop shell) while housing the cycle logic in core/runtime/.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np

from core.execution.execution_queue import ExecutionQueue
from core.execution.portfolio_risk import PortfolioRiskController
from core.execution.regime_gate import RegimeGate
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

# ── Strategy line imports ──
# ── Extracted sub-modules (P2 refactor) ──
from core.runtime.mia_close import build_mia_close_entry, enrich_mia_from_deals
from core.runtime.ou_hurst import compute_tf_ou_hurst
from core.runtime.position_ownership import resolve_position_owner

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
    protection_flag_path: str = "live_dispatch_block.flag"
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
    adapter_name: str = "mt5"  # FIX-20260613-059: transport adapter (mt5=file, mt5_zmq=ZMQ)
    zmq_order_endpoint: str = ""  # FIX-20260613-059c: per-symbol ZMQ routing
    zmq_ack_endpoint: str = ""

    # ── FIX-20260613-048: Staleness Contract ──
    # Maximum allowed age of the latest tick before the cycle is skipped.
    # 120s for BTC (crypto 24/7, tick expected every few seconds).
    # XAU would use 60s (forex 24/5, tick expected sub-second).
    max_data_age_seconds: float = 120.0
    close_price_max_age_seconds: float = 60.0  # refuse close dispatch if price older than this
    # Phase B: maximum silence from MT5 bridge before circuit breaker trip.
    # 600s (10 min) — 2 M5 bars tolerance; avoids boundary-flapping on single-bar gaps.
    max_bridge_silence_seconds: float = 600.0
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
    _last_tick_age: float = 0.0  # FIX-20260613-052: resolved placeholder: age of latest tick (seconds) for staleness guard
    _cooldown_registry: Any = None  # CooldownRegistry (Cut 1: Absolute Refractory Period)
    _family_entry_tracker: Any = None  # FamilyEntryTracker (Cut 2: Cross-Strategy Spacing)
    _strategies: dict[str, Any] | None = None  # FIX-072: cached strategy_lines for persistence
    _meta_filter_gate: Any = None  # MetaFilterGate (LightGBM 47-dim OU signal quality filter)
    _conformal_ou_gate: Any = None  # ConformalOUGate (physics-based OU signal quality gate)
    _conformal_calibrator: Any = None  # FIX-20260611-022: shared calibrator for live updates
    _mtf_price_service: Any = None  # MTFPriceService — M15 bar reconstruction from M5 tick history
    _last_ou_params: dict[str, float] | None = None  # {z_score, half_life, theta} for meta labeler
    _btc_augmenter: Any = None  # BTCFeatureAugmenter — FIX-134 lazy-init for BTC feature pipeline
    # ── FIX-20260610-010: main eval decisions for Phase 10 gate alignment ──
    _last_eval_decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    # ── FIX-20260610-003: watchdog heartbeat ──
    last_heartbeat: float = 0.0
    # MIA close entries collected by _execute_management_phase, consumed by caller
    _pending_mia_closes: list[dict[str, Any]] = field(default_factory=list)

    # Circuit breaker: 3 consecutive degraded cycles → management-only mode
    _consecutive_degraded_cycles: int = 0
    _circuit_breaker_tripped: bool = False
    _circuit_breaker_tripped_at: float = (
        0.0  # Unix ts when breaker last tripped (for cooldown reset)
    )
    _circuit_breaker_trip_reason: str = ""  # DQAF-20260608-003: which path tripped the breaker

    # FIX-20260613-048: Staleness Contract — consecutive cycles with stale data
    # triggers circuit breaker (data pipeline freeze → fail-closed).
    _consecutive_stale_cycles: int = 0
    _consecutive_stale_features: int = 0  # Phase B: feature store staleness → circuit breaker
    # ── DQAF-20260616-002/P0.1: zombie cycle fuse ──
    _consecutive_cycle_errors: int = 0  # consecutive except-cycle errors → sys.exit(1) at 5

    # Regime gate fail-closed: stale counter for fail-open → fail-closed migration
    _regime_gate_stale_counter: int = 0


# ── Helpers ──────────────────────────────────────────────────────────────


from core.runtime.fault_handler import fail_open_guard
from core.runtime.time_utils import _utc_iso  # consolidated from 18 duplicates


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

    Strangler Fig #14: delegation wrapper — implementation extracted to
    ``core/runtime/ou_hurst.py``.  This shim preserves the 4 existing
    call sites inside live_cycle.py while the pure function lives in
    an independently testable module.
    """
    return compute_tf_ou_hurst(mid_prices)


def _save_recent_prices(state: Any, base_dir: str) -> None:
    """Persist _recent_mid_prices so physics override is warm after restart.

    FIX-20260613-090: Without this, _compute_tf_ou_hurst() needs 21 M5 bars
    (~105 min) to produce valid OU Theta + Hurst values after every restart.
    Persisting the rolling buffer eliminates this cold-start entirely.
    """
    with fail_open_guard("RecentPricesSave"):
        _rp = state._recent_mid_prices
        if len(_rp) < 3:
            return
        _path = Path(base_dir) / "state" / "recent_prices.json"
        _tmp = _path.with_suffix(_path.suffix + ".tmp")
        _tmp.write_text(
            json.dumps({"prices": [round(float(x), 2) for x in _rp[-50:]]}),
            encoding="utf-8",
        )
        os.replace(_tmp, _path)


# ── Daily ops auto-scheduler ────────────────────────────────────────────

# FIX-20260531-009: state paths derived from config.base_dir at call site
DAILY_OPS_STATE_PATH = "data/state/daily_ops_state.json"  # legacy default; overridden by base_dir


def _load_daily_ops_state(base_dir: str) -> float:
    """Restore last daily_ops timestamp from disk. Returns 0.0 if not found."""
    # ── DQAF-20260616-101/P1.3: BLE001 → log_and_continue ──
    with log_and_continue("DailyOpsStateRead"):
        state_path = os.path.join(base_dir, "state", "daily_ops_state.json")
        if os.path.exists(state_path):
            with open(state_path) as f:
                data = json.load(f)
            return float(data.get("last_daily_ops_utc", 0.0))
    return 0.0


def _run_scheduled_daily_ops(config: LiveCycleConfig, state: LiveCycleState) -> None:
    """Execute daily_ops pipeline synchronously within the current cycle."""
    from core.runtime.daily_ops_scheduler import run_scheduled_daily_ops

    run_scheduled_daily_ops(config, state)


def _check_pre_close(config: LiveCycleConfig, state: LiveCycleState) -> dict[str, Any]:
    """Check if we are approaching a market close and return action flags.

    Strangler Fig #23 — delegation wrapper.  Pure calendar logic extracted to
    core.runtime.pre_close_check.  Side effects (position flattening) remain here.
    """
    from core.runtime.pre_close_check import check_pre_close as _impl

    result = _impl(
        now_utc=datetime.now(UTC),
        symbol=config.symbol,
        calendar_path=config.calendar_path,
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
            _mid = None
            if hasattr(state, "_recent_mid_prices") and state._recent_mid_prices:
                try:
                    _mid = float(state._recent_mid_prices[-1])
                except (IndexError, TypeError, ValueError):
                    _mid = None
            from core.contracts.domain.dispatch_context import build_dispatch_context

            _dispatch_managed_close(
                config,
                build_dispatch_context(config),
                pos,
                reason=f"pre_close_flatten:{result['close_label']}",
                mid=_mid,
                state=state,
                exit_watchdog=state.exit_watchdog,
            )

    return result


def cooldown_blocks_fire(now: float, last_fire: float, cooldown_seconds: float) -> bool:
    """Strangler Fig #24 — delegation wrapper.  Implementation in core.runtime.cooldown."""
    from core.runtime.cooldown import cooldown_blocks_fire as _impl

    return _impl(now, last_fire, cooldown_seconds)


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
    """Issue a modify_sltp through the outbox pipeline.

    Strangler Fig #25 — delegation wrapper.  Implementation extracted to
    core.runtime.modify_trail_dispatch.
    """
    from core.runtime.modify_trail_dispatch import dispatch_modify_trail as _impl

    _open_msg_id = ""
    if state is not None:
        _open_entry = state.known_open_tickets.get(pos.ticket, {})
        _open_msg_id = _open_entry.get("message_id", "")

    _impl(
        base_dir=config.base_dir,
        symbol=config.symbol,
        adapter_name=config.adapter_name,
        mt5_terminal_path=config.mt5_terminal_path,
        ignore_protection_flag=config.ignore_protection_flag,
        protection_flag_path=config.protection_flag_path,
        pos_side=pos.side,
        pos_ticket=pos.ticket,
        new_sl=new_sl,
        new_tp=new_tp,
        open_message_id=_open_msg_id,
        reason=reason,
        brain_ids=brain_ids,
        strategy_name=strategy_name,
    )


def _dispatch_managed_close(
    config: LiveCycleConfig,
    ctx: Any,  # DispatchContext — immutable routing bundle (DQAF-20260615-010/Phase1)
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

    FIX-20260613-052: resolved placeholder: Price Age Guard — refuses to dispatch a close order
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
        ctx=ctx,
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
    regime_detector: Any,
    feature_service: Any,
    micro_feature_computer: Any,
    micro_feature_adapter: Any,
    daily_feature_provider: Any = None,
    pnl_ledger: Any = None,
    ticket: int | None = None,
    micro_feature_dict: dict[str, float] | None = None,
) -> Any:
    """Manage open position: trail stop, re-evaluate brains, check exits.

    Strangler Fig #26 — delegation wrapper.
    Implementation extracted to core.runtime.management_phase.execute_management_phase.
    """
    from core.runtime.management_phase import execute_management_phase as _impl

    return _impl(
        config=config,
        state=state,
        mt5_worker=mt5_worker,
        broker=broker,
        brains=brains,
        regime_detector=regime_detector,
        feature_service=feature_service,
        micro_feature_computer=micro_feature_computer,
        micro_feature_adapter=micro_feature_adapter,
        daily_feature_provider=daily_feature_provider,
        pnl_ledger=pnl_ledger,
        ticket=ticket,
        micro_feature_dict=micro_feature_dict,
    )



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
    "trail_activation_atr",
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
    """Strangler Fig #20: delegation wrapper — implementation in mia_close.py."""
    return build_mia_close_entry(pos, known_entry, symbol=symbol)


# ── MIA helpers extracted to core/runtime/mia_close.py (Strangler Fig #20) ──


def _enrich_mia_from_deals(
    mia_entry: dict[str, Any],
    deals: list[Any],
) -> None:
    """Strangler Fig #20: delegation wrapper — implementation in mia_close.py."""
    enrich_mia_from_deals(mia_entry, deals)
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
    btc_augment: Any = None,  # FIX-20260613-046: pre-computed 37-dim BTC vector
    # ── FIX-20260609-011: governance degradation gate ──
    governance_state: dict[str, Any] | None = None,
    degradation_constraints: Any | None = None,  # FIX-20260611-022
    base_dir: str = "",  # FIX-20260615-006/C8
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
        btc_augment=btc_augment,  # FIX-20260613-052: resolved placeholder
        governance_state=governance_state,
        degradation_constraints=degradation_constraints,
        base_dir=base_dir,
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

    # ── DQAF-20260615-010/Phase1: Immutable dispatch routing context ──
    # Built once at cycle start, reused by all dispatch calls.  Eliminates
    # the 7-scattered-kwargs anti-pattern that caused P0-1 (closure forgot
    # adapter_name → TypeError in net_out close path).
    from core.contracts.domain.dispatch_context import build_dispatch_context
    from core.runtime.fault_handler import (  # DQAF-20260616-101/P1.1: MT5 timeout for all call sites
        _MT5_TIMEOUT_SENTINEL,
        mt5_call_with_timeout,
    )

    dispatch_ctx = build_dispatch_context(config)

    # ── DQAF-20260616-002/P2: Phase telemetry file writer ────────────────
    # Writes phase_transition events to both stdout (for live tail -f) and
    # data_btc/logs/phase_telemetry.jsonl (for post-mortem baseline analysis).
    # Single-line JSON append — ~80 bytes/event, zero measurable overhead.
    _telemetry_path = Path(config.base_dir) / "logs" / "phase_telemetry.jsonl"

    def _log_phase_transition(phase: str, phase_label: str) -> None:
        _entry = json.dumps(
            {
                "event": "phase_transition",
                "phase": phase,
                "phase_label": phase_label,
                "time": _utc_iso(),
                "cycle": state.cycle_count,
            },
            ensure_ascii=False,
        )
        print(_entry, flush=True)
        try:
            with open(_telemetry_path, "a", encoding="utf-8") as _ptf:
                _ptf.write(_entry + "\n")
        except OSError:
            pass  # disk full / permission — non-fatal, stdout still works

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

    # ── Phase B: Bridge silence check (FIX-20260608-008) ──
    # Bridge silence no longer trips the breaker immediately.  Instead it
    # increments the degraded counter — same escalation path as cycle_stall.
    # Only 3 consecutive degraded cycles (across ANY degradation source)
    # actually trip the breaker.  This prevents single-bar MT5 micro-outages
    # from triggering a 10-minute cooldown.
    _bridge_silence = time.time() - state._last_bridge_ack_time
    if _bridge_silence > config.max_bridge_silence_seconds:
        state._consecutive_degraded_cycles += 1
        print(
            json.dumps(
                {
                    "event": "bridge_silence_degraded",
                    "time": _utc_iso(),
                    "bridge_silence_seconds": round(_bridge_silence, 1),
                    "max_allowed_seconds": config.max_bridge_silence_seconds,
                    "consecutive_degraded": state._consecutive_degraded_cycles,
                    "action": "degraded — trip at >=3",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if state._consecutive_degraded_cycles >= 3 and not state._circuit_breaker_tripped:
            state._circuit_breaker_tripped = True
            state._circuit_breaker_tripped_at = time.time()
            state._circuit_breaker_trip_reason = "bridge_silence"
            print(
                json.dumps(
                    {
                        "event": "circuit_breaker_bridge_silence_trip",
                        "time": _utc_iso(),
                        "consecutive_degraded": state._consecutive_degraded_cycles,
                        "bridge_silence_seconds": round(_bridge_silence, 1),
                        "trip_reason": state._circuit_breaker_trip_reason,
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
            # ── DQAF-20260616-002/P0.2: Circuit breaker MT5 calls with timeout ──
            # Circuit breaker triggers during extreme market conditions (high
            # volatility, server lag).  Use 10s for positions_get (query) and
            # 15s for order_send (execution) — tight enough to avoid indefinite
            # block, loose enough to tolerate stressed MT5 server response.
            from core.runtime.fault_handler import (
                _MT5_TIMEOUT_SENTINEL,
                mt5_call_with_timeout,
            )
            # ── DQAF-20260616-101/P1.3: BLE001 → fail_open_guard ──
            with fail_open_guard("CircuitBreakerCloseAll"):
                _open_positions = mt5_call_with_timeout(
                    mt5_worker.positions_get, symbol=config.symbol, timeout=10.0
                )
                if _open_positions is _MT5_TIMEOUT_SENTINEL:
                    _open_positions = None  # timeout → skip close, retry next breaker cycle
                    print(
                        json.dumps(
                            {
                                "event": "circuit_breaker_positions_get_timeout",
                                "time": _utc_iso(),
                                "timeout_s": 10.0,
                                "reason": "MT5 positions_get blocked >10s — skip close this cycle",
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                else:
                    _open_positions = _open_positions or []
                # ── FIX-20260608-005: bridge liveness heartbeat ──
                # positions_get() success proves MT5 bridge is alive even
                # during management-only mode.  Update heartbeat so the
                # cooldown-based auto-reset can detect bridge recovery.
                if _open_positions is not None:
                    state._last_bridge_ack_time = time.time()
                if _open_positions:
                    _closed_any = False
                    for _pos in _open_positions:
                        _ticket = _pos.ticket
                        _side = "short" if _pos.type == 1 else "long"
                        _vol = float(getattr(_pos, "volume", 0) or 0)
                        _close_side = "buy" if _side == "short" else "sell"
                        with fail_open_guard("CircuitBreakerClose"):
                            _cb_result = mt5_call_with_timeout(
                                mt5_worker.order_send,
                                {
                                    "symbol": config.symbol,
                                    "order_type": 1,  # Market
                                    "volume": _vol,
                                    "side": _close_side,
                                    "ticket": _ticket,
                                    "magic": getattr(_pos, "magic", 0),
                                },
                                timeout=15.0,
                            )
                            if _cb_result is _MT5_TIMEOUT_SENTINEL:
                                print(
                                    json.dumps(
                                        {
                                            "event": "circuit_breaker_order_send_timeout",
                                            "time": _utc_iso(),
                                            "ticket": _ticket,
                                            "side": _side,
                                            "timeout_s": 15.0,
                                            "reason": "MT5 order_send blocked >15s",
                                        },
                                        ensure_ascii=False,
                                    ),
                                    flush=True,
                                )
                            else:
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
            # (BLE001 replaced with fail_open_guard("CircuitBreakerCloseAll") — DQAF-20260616-101/P1.3)
    # ── Phase B: Cycle stall detection (FIX-20260613-052: resolved placeholder) ──
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
            state._circuit_breaker_trip_reason = "cycle_stall"
            print(
                json.dumps(
                    {
                        "event": "circuit_breaker_cycle_stall_trip",
                        "time": _utc_iso(),
                        "consecutive_degraded": state._consecutive_degraded_cycles,
                        "trip_reason": state._circuit_breaker_trip_reason,
                        "action": "management_only_mode",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    # ── Circuit breaker auto-reset ──
    # Strangler Fig #31 — extracted to core.runtime.circuit_breaker_reset
    from core.runtime.circuit_breaker_reset import auto_reset_circuit_breaker

    auto_reset_circuit_breaker(
        config=config,
        state=state,
        degraded_wakeup=degraded_wakeup,
        cycle_duration=_cycle_duration,
    )

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
            level=FaultLevel.DEGRADE,
            component="MT5_IPC:positions_get:startup_reconciliation",
        ):
            # ── DQAF-20260616-101/P1.1: timeout-wrapped MT5 call ──
            _positions = mt5_call_with_timeout(
                mt5_worker.positions_get, symbol=config.symbol, timeout=5.0
            )
            if _positions is _MT5_TIMEOUT_SENTINEL:
                _positions = []  # timeout → skip reconciliation this cycle
            else:
                _positions = _positions or []
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
    # ── DQAF-20260616-002/P0.3: Phase 1 boundary log ──
    _log_phase_transition("1_startup_bootstrap", "Startup & restart bootstrap")
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
            # ── DQAF-20260616-101/P1.1: timeout-wrapped MT5 call ──
            _mt5_positions = mt5_call_with_timeout(
                mt5_worker.positions_get, symbol=config.symbol, timeout=5.0
            )
            if _mt5_positions is _MT5_TIMEOUT_SENTINEL:
                _mt5_positions = []  # timeout → skip orphan detection this cycle
            else:
                _mt5_positions = _mt5_positions or []
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
        except Exception as _exc:  # BLE001:FOG_DEFERRED (logged, Phase 3b)
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
    # ── DQAF-20260616-002/P0.3: Phase 2 boundary log ──
    _log_phase_transition("2_reconcile_positions", "Reconcile closed positions")
    # first scheduled reconciliation detects a stop-loss cascade.
    _run_reconciliation = state.loop_iteration % config.reconciliation_interval == 0
    if not state._initial_reconciliation_done and state.known_open_tickets:
        _run_reconciliation = True
        state._initial_reconciliation_done = True

    if not config.no_mt5 and state.known_open_tickets and _run_reconciliation:
        try:
            # ── FIX-20260611-005 Phase 2: Strangler Fig #11 ──
            from core.runtime.position_close_adapter import reconcile_and_record_closes

            _events = reconcile_and_record_closes(
                state.known_open_tickets,
                mt5_worker,
                config.symbol,
                str(journal_path),
                state,
            )

            if _events:
                # ── Update per-strategy losing-streak tracker ──
                for _evt in _events:
                    _label = _evt.label
                    _strategy = _evt.strategy or _strategy_from_brain_ids(list(_evt.brain_ids))
                    _curr = state.consecutive_sl_hits.get(_strategy, 0)
                    if _label in ("sl_hit_first", "loss"):
                        _curr += 1
                        state._pending_sl_records.append(
                            {"strategy": _strategy, "timestamp": time.time()}
                        )
                    elif _label in ("tp_hit_first", "win"):
                        _curr = 0
                    state.consecutive_sl_hits[_strategy] = _curr

                    # ── FIX-20260611-022: Feed conformal calibrator ──
                    _calib = getattr(state, "_conformal_calibrator", None)
                    if _calib is not None:
                        with fail_open_guard("ConformalCalibratorUpdate"):
                            _label_int = 1 if _evt.pnl > 0 else 0
                            for _brain_id in _evt.brain_ids:
                                _calib.update(0.5, _label_int)

                    # ── SignalSettled — real trade PnL ──
                    # Strangler Fig #32 — extracted to core.runtime.signal_settlement
                    from core.runtime.signal_settlement import settle_closed_trade_signals

                    settle_closed_trade_signals(evt=_evt, base_dir=config.base_dir)

                    # Update portfolio risk
                    if state.portfolio_risk_controller is not None:
                        import contextlib as _ctxlib_pf

                        with _ctxlib_pf.suppress(Exception):
                            state.portfolio_risk_controller.update_returns(_strategy, _evt.pnl)

                    # ── Budget recording ──
                    if mt5_worker is not None:
                        with FaultTolerantContext(
                            level=FaultLevel.DEGRADE,
                            component="MT5_IPC:account_info:PnL_to_equity",
                        ):
                            _acc = mt5_worker.account_info()
                            _eq = float(getattr(_acc, "equity", 0)) if _acc is not None else 0.0
                        _pnl_pct = _evt.pnl / _eq if _eq > 0 else 0.0
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
                for _evt in _events:
                    if _evt.remaining_volume <= 0:
                        state.known_open_tickets.pop(_evt.position_ticket, None)

                # Sync position_manager
                if state.position_manager is not None and state.position_manager.has_position():
                    for _pm_pos in list(state.position_manager.get_all_positions()):
                        if _pm_pos.ticket not in state.known_open_tickets:
                            state.position_manager.clear_position(ticket=_pm_pos.ticket)

                print(
                    json.dumps(
                        {
                            "event": "positions_closed",
                            "time": _utc_iso(),
                            "count": len(_events),
                            "tickets": [e.position_ticket for e in _events],
                            "sl_streak_by_strategy": dict(state.consecutive_sl_hits),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        except Exception:  # BLE001:AUDITED — complex nested block, not suitable for fog
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
    _tick_time: float = 0.0  # FIX-20260613-048: for staleness detection
    if broker is not None:
        with FaultTolerantContext(
            level=FaultLevel.DEGRADE,
            component="PriceFetch:broker",
        ):
            mid_price, _bid, _ask = broker.fetch_prices(config.symbol)
    # ── FIX-20260619-001: L3 fallback — broker→direct MT5 ──
    # When the broker ZMQ tick channel fails (single point of failure),
    # fall back to direct MT5 via mt5_worker.  The worker has built-in
    # reconnect() + CRASH-level FaultTolerantContext — self-healing.
    # This eliminates the failure mode where broker is disconnected but
    # MT5 has live prices (observed: 60+ min XAU outage, 2026-06-18).
    if mid_price is None and not config.no_mt5:
        mid_price, _bid, _ask, _tick_time = _mid_and_prices(mt5_worker, config.symbol)

    # ── FIX-20260613-048: Staleness Contract (Iron Law #11 Data Analytics) ──
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
                state._circuit_breaker_trip_reason = "data_staleness"
                print(
                    json.dumps(
                        {
                            "event": "circuit_breaker_staleness_trip",
                            "time": _utc_iso(),
                            "consecutive_stale_cycles": state._consecutive_stale_cycles,
                            "data_age_seconds": round(_data_age, 1),
                            "trip_reason": state._circuit_breaker_trip_reason,
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
            if state.loop_iteration % 5 == 0:
                _save_recent_prices(state, config.base_dir)
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
        # FIX-20260613-090: persist warm prices so physics override is
        # immediately available after restart (no ~105min cold-start).
        if state.loop_iteration % 5 == 0:
            _save_recent_prices(state, config.base_dir)

    # ── MTF Price Service: M15 bar reconstruction from M5 tick history ──
    if not hasattr(state, "_mtf_price_service") or state._mtf_price_service is None:
        state._mtf_price_service = MTFPriceService()
        # Bootstrap from historical M5 closes so M15 bars are available immediately.
        # FIX-20260613-090: also hydrate _recent_mid_prices from MT5 so the
        # physics override (OU Theta + Hurst) is warm on cycle 1.  MT5 is the
        # Single Source of Truth for market data — unlike disk persistence,
        # this guarantees continuous, gap-free price series regardless of how
        # long the system was offline.
        if not config.no_mt5 and mt5_worker is not None:
            with FaultTolerantContext(
                level=FaultLevel.DEGRADE,
                component="MT5_IPC:copy_rates_from_pos:bootstrap",
            ):
                # ── DQAF-20260616-101/P1.1: timeout-wrapped MT5 call ──
                _hist_rates = mt5_call_with_timeout(
                    mt5_worker.copy_rates_from_pos, config.symbol, 5, 0, 200, timeout=5.0
                )  # TIMEFRAME_M5
                if _hist_rates is _MT5_TIMEOUT_SENTINEL:
                    _hist_rates = None  # timeout → skip bootstrap this cycle
                if _hist_rates is not None and len(_hist_rates) >= 6:
                    _closes = [float(r[4]) for r in _hist_rates]
                    state._mtf_price_service.bootstrap(_closes)
                    # Hydrate physics indicators: use mid=(H+L)/2 for OU/Hurst
                    if len(state._recent_mid_prices) < 21:
                        _hydrated = [(float(r[2]) + float(r[3])) / 2.0 for r in _hist_rates[-50:]]
                        state._recent_mid_prices = _hydrated
                        print(
                            json.dumps(
                                {
                                    "event": "physics_hydrated",
                                    "time": _utc_iso(),
                                    "source": "mt5_copy_rates",
                                    "prices_loaded": len(_hydrated),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
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
                # ── Blind Spot 1b: hard-block the cycle on dirty tick data ──
                # Previously this was log-only — the cycle continued with
                # potentially poisoned prices.  Now we nullify bid/ask so
                # ALL downstream trading logic (spread gate, feature assembly,
                # strategy evaluate, order dispatch) naturally bails out.
                _bid = None
                _ask = None
                mid_price = None
                print(
                    json.dumps(
                        {
                            "event": "tick_sanity_blocked_cycle",
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
                    market_type=getattr(config, "market_type", "forex_24_5"),
                    tick_time=_tick_time,
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

        # ── FIX-20260615-009d: Cold-start race condition fix ──
        # BTCFeatureAugmenter MUST be initialised BEFORE _execute_management_phase
        # because management-phase brain inference for btc_macro schema brains
        # needs the augmenter to assemble 41-dim feature vectors.  Previously the
        # augmenter was created 1300 lines later during main eval, causing a
        # deterministic RuntimeError on cycle 1 of every cold start with open positions.
        if config.symbol == "BTCUSDc" and feature_service is not None:
            _aug = getattr(state, "_btc_augmenter", None)
            if _aug is None:
                from core.features.computers.btc_feature_augmenter import (
                    BTCFeatureAugmenter,
                )

                _aug = BTCFeatureAugmenter(feature_service, mt5_worker=mt5_worker)
                state._btc_augmenter = _aug

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
                        regime_detector=regime_detector,
                        feature_service=feature_service,
                        micro_feature_computer=micro_feature_computer,
                        micro_feature_adapter=micro_feature_adapter,
                        daily_feature_provider=daily_feature_provider,
                        pnl_ledger=pnl_ledger,
                        ticket=_pm_pos.ticket,
                        micro_feature_dict=micro_feature_dict,
                    )
            except Exception:  # BLE001:FOG_DEFERRED
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
            # ── FIX-20260610-002: in-memory dedup (F2) ──────────────────
            # Track processed tickets within this session so that if
            # clear_position() fails and the same ticket is re-detected
            # as MIA in a later cycle, we skip the duplicate journal write.
            _mia_seen: set[int] = getattr(state, "_mia_processed_tickets", set())
            _mia_closed = [
                e for e in _mia_closed if int(e.get("position_ticket", 0) or 0) not in _mia_seen
            ]
            for _e in _mia_closed:
                _t = int(_e.get("position_ticket", 0) or 0)
                if _t:
                    _mia_seen.add(_t)
            state._mia_processed_tickets = _mia_seen  # type: ignore[attr-defined]
            if not _mia_closed:
                _mia_closed = []  # all deduped — skip journal write
            if _mia_closed:
                # ── FIX-20260612-024: journal-based dedup ──
                # Before writing MIA close entries, check if the journal
                # already has a close entry for this ticket (written by
                # the bridge).  If yes, skip — prevents duplicate close
                # entries that cause JOURNAL_SLA_VIOLATION dupes.
                _existing_close_tickets: set[int] = set()
                try:
                    _jp = str(journal_path)
                    if Path(_jp).exists():
                        for _line in Path(_jp).read_text(encoding="utf-8").splitlines():
                            if not _line.strip():
                                continue
                            if '"action": "close"' not in _line:
                                continue
                            # Fast substring extraction of position_ticket
                            _pt = (
                                _line.split('"position_ticket":')[1].split(",")[0].strip()
                                if '"position_ticket":' in _line
                                else ""
                            )
                            if _pt and _pt.isdigit():
                                _existing_close_tickets.add(int(_pt))
                except Exception:  # BLE001:FOG_DEFERRED
                    pass  # Non-blocking — skip dedup on read error
                _mia_closed = [
                    e
                    for e in _mia_closed
                    if int(e.get("position_ticket", 0) or 0) not in _existing_close_tickets
                ]
                if not _mia_closed:
                    pass  # all tickets already have close entries — skip
            if _mia_closed:
                # ── FIX-20260611-005 Phase 2: Strangler Fig #12 ──
                with log_and_continue(component="MIA_Close:journal_write"):
                    from core.runtime.position_close_adapter import record_mia_closes

                    record_mia_closes(
                        _mia_closed,
                        mt5_worker,
                        config.symbol,
                        str(journal_path),
                        state,
                    )
            # ── Record exit for reentry guard ──
            # Strangler Fig #30 — extracted to core.runtime.reentry_recording
            from core.runtime.reentry_recording import record_mia_exits_for_reentry

            record_mia_exits_for_reentry(mia_closed=state._pending_mia_closes, state=state)

            # ── DingTalk notification + ghost position cleanup ──
            for _entry in state._pending_mia_closes:
                _exit_strategy = _entry.get("strategy", "")
                _exit_side = _entry.get("side", "")
                _exit_price = float(_entry.get("detail", {}).get("close_price", 0) or 0)
                _emit_close_notification(
                    _ah=getattr(state, "alert_hub", None),
                    _sym=_entry.get("symbol", config.symbol),
                    _side=_exit_side if _exit_side in ("long", "short") else _entry.get("side", ""),
                    _vol=float(_entry.get("volume", 0) or 0),
                    _price=_exit_price,
                    _pnl=_entry.get("pnl"),
                )
                # ── FIX-20260610-002: Clean up ghost position ──
                _mia_ticket = _entry.get("position_ticket")
                if _mia_ticket and state.position_manager is not None:
                    with log_and_continue(component="MIA_Close:clear_position"):
                        state.position_manager.clear_position(int(_mia_ticket))
                        # SF #26 FIX: _emit extracted to management_phase.py — inline here
                        print(json.dumps({"event": "mia_position_cleared", "time": _utc_iso(), "ticket": _mia_ticket}, ensure_ascii=False), flush=True)
                # ── FIX-20260610-002: Record budget for MIA close (F3) ──────
                # Budget was never updated for MIA-detected closes — daily
                # PnL, consecutive loss counters, and all cumulative circuit
                # breakers missed every MIA close.  Feed through the same
                # pending-records pipeline as reconciliation closes.
                # ── FIX-20260620-001: MIA pnl is raw USD from deal.profit,
                # NOT a percentage.  Must divide by account equity to match
                # the reconciliation path (_evt.pnl / _eq).  Previous code
                # passed raw USD as percentage → $5 loss = -500% daily PnL
                # → immediate budget_breached false positive.
                _mia_pnl = _entry.get("pnl")
                if _mia_pnl is not None and _exit_strategy:
                    _is_win = float(_mia_pnl) > 0
                    _eq = 1000.0  # fallback equity
                    try:
                        if mt5_worker is not None:
                            _acc = mt5_worker.account_info()
                            _eq = float(getattr(_acc, "equity", 1000.0)) if _acc is not None else 1000.0
                    except Exception:  # BLE001:FOG_DEFERRED (Sev 4, Phase 3b — MT5 account_info fallback)
                        pass  # graceful fallback — keep _eq at 1000.0
                    _pnl_pct = float(_mia_pnl) / _eq if _eq > 0 else 0.0
                    if not hasattr(state, "_pending_budget_records"):
                        state._pending_budget_records = []
                    state._pending_budget_records.append(
                        {
                            "strategy": _exit_strategy,
                            "pnl": _pnl_pct,
                            "is_win": _is_win,
                        }
                    )
                    print(
                        json.dumps(
                            {
                                "event": "mia_budget_queued",
                                "time": _utc_iso(),
                                "ticket": _mia_ticket,
                                "pnl": float(_mia_pnl),
                                "is_win": _is_win,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            # ── Save position state immediately ──
            if state.position_manager is not None:
                with FaultTolerantContext(
                    level=FaultLevel.DEGRADE,
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
    # ── DQAF-20260616-002/P0.3: Phase 4 boundary log ──
    _log_phase_transition("4_feature_computation", "Feature computation")
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
                # ── Persist micro features ──
                # Strangler Fig #34 — extracted to core.runtime.micro_persist
                from core.runtime.micro_persist import persist_micro_features

                persist_micro_features(config=config, micro_features=micro_features)

            micro_feature_vector = np.zeros(_schema_dim("v4.3_microstructure_9"), dtype=np.float64)

    # ── Build entry_context for journal (Phase 1: 40-dim feature snapshot) ──
    # Guardrail 1: schema versioning — V9 vs future V10 prevents feature drift
    # Guardrail 2: immutability — tuple deep-copy prevents async mutation
    # Guardrail 3: NaN safety — nan_to_num prevents JSON serialization failures
    _entry_features_snapshot: dict[str, Any] = {
        "schema_version": "v9_institutional",
        "vector": tuple(np.nan_to_num(np.asarray(feature_vector, dtype=np.float64)).tolist()),
        # FIX-20260613-087: entry_spread must propagate to journal for accurate EV
        "entry_spread": float(round(_ask - _bid, 2))
        if (_bid is not None and _ask is not None and _ask > _bid)
        else 0.0,
        "bid": _bid,
        "ask": _ask,
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
            # ── FIX-20260611-022: Make calibrator accessible for live updates ──
            state._conformal_calibrator = _cal
        except Exception:  # BLE001:FOG_DEFERRED
            with fail_open_guard("ConformalCalibratorInit"):
                raise

        # ── MetaFilterGate (47-dim LGB, for non-OU strategies if any) ──
        try:
            from core.execution.meta_filter_gate import MetaFilterGate

            _mg = MetaFilterGate(
                model_dir=f"{config.base_dir}/models/meta_filter_v3",
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
        except Exception:  # BLE001:FOG_DEFERRED
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
        except Exception as _oug_exc:  # BLE001:FOG_DEFERRED (logged, Phase 3b)
            with fail_open_guard("ConformalOUGateInit"):
                raise  # Re-raise inside guard for structured traceback logging

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

    # ── Feature freshness check ──
    # Strangler Fig #29 — extracted to core.runtime.feature_freshness
    from core.runtime.feature_freshness import check_feature_freshness

    check_feature_freshness(config=config, state=state, feature_store=feature_store)

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
        except Exception:  # BLE001:FOG_DEFERRED
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
    # ── DQAF-20260616-002/P0.3: Phase 6 boundary log ──
    _log_phase_transition("6_risk_budget", "Account equity & risk budget")
    _account_equity: float | None = None
    try:
        if broker is not None:
            _account_equity = broker.get_account_equity()
    except Exception:  # BLE001:FOG_DEFERRED
        logger.warning("Broker equity fetch failed — falling back to MT5 direct query")
    if _account_equity is None and mt5_worker is not None:
        with FaultTolerantContext(
            level=FaultLevel.DEGRADE,
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

        # ── FIX-20260609-010: Restore budget state EVERY cycle ────────────────
        # _build_strategy_lines() above creates fresh StrategyBudget objects
        # with zeroed counters.  Without per-cycle restoration the persisted
        # cumulative state (daily PnL, consecutive losses, SL cooldown) is
        # lost after cycle 1, permanently disabling all budget-based circuit
        # breakers (daily_loss_limit, max_consecutive_losses, intraday DD).
        # The restore MUST happen before pending budget records are fed so
        # those records are ADDED to the restored cumulative state rather
        # than overwritten by it.
        with fail_open_guard("BudgetStateRestore"):
            from core.runtime.execution_state import load_execution_state as _load_exec

            _exec_snap = _load_exec(Path(config.base_dir) / "state" / "execution_state.json")
            if _exec_snap is not None:
                _budgets_data: dict[str, Any] = _exec_snap.get("budgets", {})
                for _sname, _snap in _budgets_data.items():
                    _strat = strategies.get(_sname)
                    if _strat is not None:
                        _budget = getattr(_strat, "budget", None)
                        if _budget is not None and hasattr(_budget, "load_state"):
                            import contextlib

                            with contextlib.suppress(Exception):
                                _budget.load_state(_snap)

        # ── Feed pending budget records from reconciliation ──
        if state._pending_budget_records:
            for _rec in state._pending_budget_records:
                _sname = _rec.get("strategy", "")
                _strat = strategies.get(_sname)
                if _strat is not None and getattr(_strat, "budget", None) is not None:
                    with log_and_continue(component="Budget:record_trade"):
                        # FIX-20260615-009g: defensive defaults for breakeven (PnL=0)
                        # edge case — some close paths may omit is_win when PnL=0.
                        _strat.budget.record_trade(
                            _rec.get("pnl", 0.0),
                            _rec.get("is_win", False),
                        )
            state._pending_budget_records.clear()

        # ── Feed pending SL records for graduated per-SL cooldown ──
        if state._pending_sl_records:
            for _rec in state._pending_sl_records:
                _sname = _rec["strategy"]
                _strat = strategies.get(_sname)
                if _strat is not None and getattr(_strat, "budget", None) is not None:
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
                    # ── FIX-20260613-090: inject physics-based regime indicators ──
                    # OU Theta + Hurst provide a mean-reversion override that is
                    # independent of ADX.  When both confirm strong mean-reversion
                    # (Theta > 0.5, Hurst < 0.48), the gate treats the market as
                    # "ranging" regardless of ADX — counter-trend signals are
                    # physically justified.
                    # Target: phase out ADX gating entirely once brains are
                    # retrained with V9_Micro features.
                    try:
                        _phys_ou, _phys_hurst = _compute_tf_ou_hurst(state._recent_mid_prices)
                        regime_info["ou_theta_m5"] = float(_phys_ou)
                        regime_info["hurst_m5"] = float(_phys_hurst)
                    except Exception:  # BLE001:FOG_DEFERRED
                        pass  # fail-safe: gate falls back to ADX-only logic
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
                            "ou_theta_m5": regime_info.get("ou_theta_m5") if regime_info else None,
                            "hurst_m5": regime_info.get("hurst_m5") if regime_info else None,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as _rg_exc:  # BLE001:FOG_DEFERRED
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
        # Strangler Fig #27 — extracted to core.runtime.session_guards
        from core.runtime.session_guards import run_session_guards

        _skip_cycle, session_info = run_session_guards(
            config=config, state=state, mt5_worker=mt5_worker
        )
        if _skip_cycle:
            _log_cycle_end(state.loop_iteration)
            return state, True  # skip cycle


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
            with fail_open_guard("LiveCycle:RestoreExecutionState"):
                from core.runtime.execution_state import restore_execution_state
                restore_execution_state(state, strategies, data_dir=config.base_dir)

        # ── FIX-20260613-090: disk fallback for _recent_mid_prices ──
        # MT5 backfill (above) is the primary hydration source.  Disk persistence
        # serves as a fallback when MT5 is slow/unavailable during startup.
        # Freshness guard: reject persisted data older than 5 minutes (1 M5 bar)
        # to prevent feeding discontinuous price series into OU/Hurst formulas.
        if state.loop_iteration == 1 and len(state._recent_mid_prices) < 21:
            _rp_path = Path(config.base_dir) / "state" / "recent_prices.json"
            try:
                if _rp_path.exists():
                    _age_s = time.time() - _rp_path.stat().st_mtime
                    if _age_s < 300:  # 5 min = 1 M5 bar — tolerate at most 1 missing bar
                        _rp_data = json.loads(_rp_path.read_text(encoding="utf-8"))
                        _rp_list = _rp_data.get("prices", [])
                        if isinstance(_rp_list, list) and len(_rp_list) >= 3:
                            state._recent_mid_prices = [
                                float(x) for x in _rp_list[-50:] if isinstance(x, int | float)
                            ]
                            print(
                                json.dumps(
                                    {
                                        "event": "physics_hydrated",
                                        "time": _utc_iso(),
                                        "source": "disk_fallback",
                                        "prices_loaded": len(state._recent_mid_prices),
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                    # else: stale >5min — skip, fall through to cold warm-up
            except Exception:  # BLE001:FOG
                with fail_open_guard("live_cycle:_emit_close_notification"):
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
                level=FaultLevel.DEGRADE,
                component="MT5_IPC:positions_get:portfolio_risk",
            ):  # fmt: skip
                from core.contracts.strategy_magic import MAGIC_TO_STRATEGY as _MAGIC_TO_STRATEGY

                # ── DQAF-20260616-101/P1.1: timeout-wrapped MT5 call ──
                _mt5_positions = mt5_call_with_timeout(
                    mt5_worker.positions_get, symbol=config.symbol, timeout=5.0
                )
                if _mt5_positions is _MT5_TIMEOUT_SENTINEL:
                    _mt5_positions = None  # timeout → skip portfolio mapping this cycle
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
                # Strangler Fig #15: ownership resolution extracted to
                # core/runtime/position_ownership.py
                owner = resolve_position_owner(
                    pos.supporting_brain_ids or [],
                    brains=brains,
                    micro_m15_types=MICRO_M15_GROUP["brain_types"],
                    micro_h1_types=MICRO_H1_GROUP["brain_types"],
                    micro_h4_types=MICRO_H4_GROUP["brain_types"],
                    micro_3bar_types=MICRO_GROUP["brain_types"],
                    statarb_types=ARB_GROUP["brain_types"],
                    default_owner="barrier_12bar",
                )
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
                # FIX-20260613-B: Record OU z-score/half-life in entry_context for audit trail
                _entry_features_snapshot["ou_z_score"] = round(
                    float(_ou_parms.get("z_score", 0.0)), 4
                )
                _entry_features_snapshot["ou_half_life"] = round(
                    float(_ou_parms.get("half_life", 0.0)), 1
                )
                _entry_features_snapshot["ou_theta"] = round(float(_ou_parms.get("theta", 0.0)), 4)

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
        except Exception as _gm_exc:  # Iron Law #10: BLE001→fail_open_guard
            import logging as _gm_log

            _gm_log.getLogger(__name__).warning(
                "Golden Master record_cycle_inputs failed: %s", _gm_exc
            )

        # ── FIX-20260613-052: resolved placeholder: BTC 37-dim feature augmentation ──
        # Compute btc_augment for BTC brains using btc_macro_enhanced_41 schema.
        # Must be computed BEFORE strategy evaluation so SwingStrategy._run_inference()
        # can pass it to assemble_features_by_schema(), avoiding the legacy
        # XAU-centric fallback path (which has incorrect cross-asset slots).
        _btc_aug: Any = None
        if config.symbol == "BTCUSDc" and daily_feature_vector is not None:
            with fail_open_guard("BTCFeatureAugment"):
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

        # ── FIX-20260609-011: load governance state for degradation gate ──
        # Read once per cycle so the governance degradation gate sees the
        # latest brain status transitions (daily_ops updates this file).
        _gov_state: dict[str, Any] | None = None
        with fail_open_guard("GovernanceStateLoad"):
            _gov_path = Path(config.base_dir) / "governance_state.json"
            if _gov_path.exists():
                import json as _json_gov

                _gov_raw = _json_gov.loads(_gov_path.read_text(encoding="utf-8"))
                _gov_state = _gov_raw.get("brain_states", {})

        # ── FIX-20260611-022: Evaluate data-health degradation ──
        # Progressive risk reduction based on data quality.
        # Staleness-based: if key sources haven't updated recently, reduce exposure.
        _degrade_constraints: Any = None
        with fail_open_guard("DataHealthDegradationEval"):
            from core.observability.degradation import (
                DegradationConstraints,
                evaluate_staleness,
            )

            _dh_path = Path(config.base_dir) / "state" / "data_health_state.json"
            if _dh_path.exists():
                import json as _json_dh

                _dh_raw = _json_dh.loads(_dh_path.read_text(encoding="utf-8"))
                _sources = _dh_raw.get("sources", {})
                _stale_level = evaluate_staleness(_sources)
                if _stale_level is not None:
                    _degrade_constraints = DegradationConstraints.for_level(
                        _stale_level,
                        reason=f"Data staleness detected (level={_stale_level.name})",
                    )

        # ── DQAF-20260614-002: Per-cycle calibrator heartbeat ──
        # compute_threshold() updates the adaptive Q10 from rolling history.
        # Must be called at least once per cycle so total_computations > 0
        # and the MetaFilter can transition from fixed → adaptive threshold.
        # Previously, MetaFilterGate had a calibrator but evaluate() was never
        # called; MetaSignalFilter was called but had no calibrator.  This
        # heartbeat bypasses the gate wiring gap — the calibrator computes
        # regardless of which gate consumes the threshold.
        with contextlib.suppress(Exception):
            _heartbeat_cal = getattr(state, "_conformal_calibrator", None)
            if _heartbeat_cal is not None and _heartbeat_cal.is_warm:
                _heartbeat_cal.compute_threshold()
                _heartbeat_cal._save_state()  # DQAF-002c: persist immediately

        # ── DQAF-20260616-002/P0.3: Phase 7 boundary log ──
        _log_phase_transition("7_strategy_evaluation", "Multi-strategy evaluation")
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
            btc_augment=_btc_aug,  # FIX-20260613-052: resolved placeholder
            governance_state=_gov_state,
            degradation_constraints=_degrade_constraints,
            base_dir=config.base_dir,  # FIX-20260615-006/C8
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
            except Exception as _gm_exc:  # BLE001:FOG_DEFERRED
                import logging as _gm_log

                _gm_log.getLogger(__name__).warning(
                    "Golden Master record_cycle_outputs failed: %s", _gm_exc
                )

        # ── FIX-20260610-010: Persist main eval decisions for Phase 10 gate alignment ──
        _last_decisions: dict[str, dict[str, Any]] = {}
        for _sr in eval_summary.get("strategy_results", []):
            _sname = _sr.get("strategy", "")
            if _sname:
                _last_decisions[_sname] = {
                    "should_trade": _sr.get("should_trade", False),
                    "reason": _sr.get("reason", ""),
                    "direction": _sr.get("direction", "neutral"),
                }
        state._last_eval_decisions = _last_decisions

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

        # ── FIX-20260613-090: Budget breach → global circuit breaker ──
        # Per-strategy budget pauses now cascade to the global block_new_entries
        # flag, implementing the fail-closed latch pattern.  Any strategy that
        # breaches its daily_loss_limit or max_consecutive_losses blocks ALL
        # new entries for the rest of the day (cross-day reset via _reset_daily).
        _budget_breached = False
        for _sr in eval_summary.get("strategy_results", []):
            if _sr.get("reason") == "budget_paused":
                _budget_breached = True
                break
        if _budget_breached and not state._circuit_breaker_tripped:
            state._circuit_breaker_tripped = True
            state._circuit_breaker_tripped_at = time.time()
            state._circuit_breaker_trip_reason = "budget_breached"
            state.block_new_entries = True
            _breached_names = [
                _sr.get("strategy", "?")
                for _sr in eval_summary.get("strategy_results", [])
                if _sr.get("reason") == "budget_paused"
            ]
            print(
                json.dumps(
                    {
                        "event": "circuit_breaker_budget_breach_trip",
                        "time": _utc_iso(),
                        "strategies": _breached_names,
                        "trip_reason": "budget_breached",
                        "severity": "CRITICAL",
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

        # ── FIX-20260606-128: reentry block streak alert ──
        # Strangler Fig #28 — extracted to core.runtime.reentry_alert
        from core.runtime.reentry_alert import check_reentry_block_streaks

        check_reentry_block_streaks(eval_summary=eval_summary, state=state)


        # Flush execution queue → dispatch to MT5
        if exec_queue.queue_size > 0 and not config.no_mt5:
            from core.execution.live_order_sender import dispatch_live_open_order

            # 陷阱三: net-out close orders intercepted at upper layer for Watchdog wrapping
            # Strangler Fig extraction: handle_net_out_close() in net_out_close_handler.py
            _net_out_close_dispatch_fn = None
            if exit_watchdog is not None:
                from core.execution.net_out_close_handler import handle_net_out_close

                def _net_out_close_dispatch_fn(payload: dict) -> dict:
                    _result, state._exit_reject_streak, state._exit_reject_cooldown = (
                        handle_net_out_close(
                            ctx=dispatch_ctx,
                            payload=payload,
                            exit_reject_streak=state._exit_reject_streak,
                            exit_reject_cooldown=state._exit_reject_cooldown,
                            known_open_tickets=state.known_open_tickets,
                            mid_price=mid_price,
                            exit_watchdog=exit_watchdog,
                            utc_iso_fn=_utc_iso,
                        )
                    )
                    return _result

            dispatch_results = exec_queue.flush(
                partial(
                    dispatch_live_open_order,
                    ctx=dispatch_ctx,
                    entry_context=_entry_features_snapshot,
                ),
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

                # ── Real-time trade notification ──
                # Strangler Fig #35 — extracted to core.runtime.trade_notify
                from core.runtime.trade_notify import notify_dispatched_trades

                notify_dispatched_trades(
                    dispatch_results=dispatch_results, state=state, symbol=config.symbol,
                    _emit_close_notification=_emit_close_notification,
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

            # ── Dispatch post-processing ──
            # Strangler Fig #33 — extracted to core.runtime.dispatch_post
            from core.runtime.dispatch_post import process_dispatch_results

            process_dispatch_results(
                dispatch_results=dispatch_results,
                state=state,
                strategies=strategies,
                feature_vector=feature_vector,
                micro_feature_vector=micro_feature_vector,
                mid_price=mid_price,
                daily_feature_vector=daily_feature_vector,
                tracker=tracker,
                symbol=config.symbol,
            )

            # ── Register opened positions for dynamic exit management ──
            # Strangler Fig #10: extracted to core/runtime/position_registration.py
            from core.runtime.position_registration import register_dispatched_positions

            _reg_result = register_dispatched_positions(
                config=config,
                position_manager=state.position_manager,
                known_open_tickets=state.known_open_tickets,
                loop_iteration=state.loop_iteration,
                limit_monitor=state.limit_monitor,
                dispatch_results=dispatch_results,
                eval_summary=eval_summary,
                brains=brains,
                journal_path=journal_path,
                current_atr=current_atr,
                mid_price=mid_price,
                bid=_bid,
                ask=_ask,
                mt5_worker=mt5_worker,
                _utc_iso_fn=_utc_iso,
                _DEFAULT_HORIZON=_DEFAULT_HORIZON,
            )

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
                        level=FaultLevel.DEGRADE,
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
                                                adapter_name=config.adapter_name,
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
                                        except Exception as _fc_exc:  # BLE001:FOG_DEFERRED
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
                    except Exception:  # BLE001:FOG_DEFERRED
                        logger.warning("Intraday drawdown recovery check failed")

                _fv = check_feature_vector(feature_vector)
                if not _fv.get("passed"):
                    _log_cycle_end(state.loop_iteration)
                    return state, not config.once
            except Exception:  # BLE001:FOG
                with fail_open_guard("live_cycle:_net_out_close_dispatch_fn"):
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
                    except Exception:  # BLE001:FOG_DEFERRED
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
                        except Exception:  # BLE001:FOG_DEFERRED
                            prop = None
                else:
                    prop = None
            elif "swing" in schema_id or "daily" in schema_id or "btc_macro" in schema_id:
                # FIX-20260531-021 / FIX-20260610-009: Data-driven assembly via schema registry
                # btc_macro added 2026-06-10 — the 4th hardcoded schema check that
                # FIX-022 missed.  BTC brains (btc_macro_enhanced_41) fell through
                # to the else branch → raw 40-dim feature_vector → dimension mismatch.
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
                            except Exception:  # BLE001:FOG_DEFERRED
                                import logging as _btc_log
                                import traceback as _btc_tb

                                _btc_log.getLogger(__name__).error(
                                    "BTCFeatureAugmenter.augment() CRASHED — "
                                    "BTC cross-asset slots [12][30][35][36] will be "
                                    "zero-filled.  Train-serve skew is ACTIVE.  "
                                    "Fix the augmenter before trusting brain inference.\n%s",
                                    _btc_tb.format_exc(),
                                )
                                # FIX-20260613-052: resolved placeholder: Do NOT silently fall back.
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

    # ── Record counterfactual signals ──
    # Strangler Fig #36 — extracted to core.runtime.pnl_recording
    from core.runtime.pnl_recording import record_counterfactual_signals

    record_counterfactual_signals(
        config=config, pnl_ledger=pnl_ledger, raw_proposals=raw_proposals,
        proposal=proposal, mid_price=mid_price, bid=_bid, ask=_ask,
    )

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

    # ── FIX-20260610-010: Phase 10 is LEGACY (rollback reference) ──
    # When multi_strategy_enabled=True, the main eval path (L4175-L5394)
    # already handles strategy evaluation, safety gates, AND dispatch via
    # the execution queue.  Phase 10 is retained ONLY for rollback to
    # multi_strategy_enabled=False.  It must NOT dispatch when the main
    # eval path is active — one cycle, one dispatch path.
    if config.multi_strategy_enabled:
        # Main eval already dispatched via exec queue.  Phase 10 runs
        # PnL ledger recording + shadow verification but NEVER dispatches.
        direction = "neutral"
        confidence = 0.0

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
        except Exception:  # BLE001:FOG_DEFERRED
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
            _record_brain_outcomes(proposals, direction, "consensus_skip", tracker, symbol=config.symbol)
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
                    "dimensions": {
                        "brain_confidence": round(confidence, 4),
                        "brain_direction": direction,
                        "consensus_direction": direction,
                        "vote_matched": False,
                        "symbol": config.symbol,
                    },
                },
            )
        print(json.dumps(skip_event, ensure_ascii=False), flush=True)
        _log_cycle_end(state.loop_iteration)
        return state, not config.once  # break if --once, else continue
    # ── Circuit breaker: track consecutive degraded cycles ──
    if degraded_wakeup:
        state._consecutive_degraded_cycles += 1
        if state._consecutive_degraded_cycles >= 3:
            state._circuit_breaker_tripped = True
            state._circuit_breaker_tripped_at = time.time()
            state._circuit_breaker_trip_reason = "degraded_wakeup"
            print(
                json.dumps(
                    {
                        "event": "circuit_breaker_tripped",
                        "time": _utc_iso(),
                        "consecutive_degraded": state._consecutive_degraded_cycles,
                        "trip_reason": state._circuit_breaker_trip_reason,
                        "action": "suspend_new_entries",
                        "mode": "management_only",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    _log_cycle_end(state.loop_iteration)
    return state, not config.once
