"""Position management phase — Strangler Fig #26 extraction from live_cycle.py.

Extracted from live_cycle.py:_execute_management_phase() (formerly 1,454 lines).
Manages an open position through all exit layers from Guard through Time Exit.

FIX-20260620-064: Bug fix (line 774 tuple→bool return, dormant since caller discards).
FIX-20260620-065: Strangler Fig #26 — extracted to core.runtime.management_phase.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from core.runtime.fault_handler import (
    FaultLevel,
    FaultTolerantContext,
)
from core.runtime.market_ingress import (
    MT5_TIMEFRAME_M15,
    MT5_TIMEFRAME_M30,
    _get_current_atr,
    _mid_and_prices,
)
from core.runtime.mia_close import build_mia_close_entry as _build_mia_close_entry
from core.runtime.mia_close import enrich_mia_from_deals as _enrich_mia_from_deals
from core.runtime.ou_hurst import compute_tf_ou_hurst as _compute_tf_ou_hurst
from core.runtime.time_utils import _utc_iso
from core.runtime.trail_dispatch import compute_and_dispatch_trail

# ── V6 Shared Trading Infrastructure (FIX-20260629-195) ──
from core.trading.position_lifecycle import ExitPriorityQueue
from core.trading.ratchet_risk import RatchetConfig, RatchetRisk

if TYPE_CHECKING:
    from core.runtime.live_cycle import LiveCycleConfig, LiveCycleState

logger = logging.getLogger(__name__)


def _emit(event: str, /, **fields: Any) -> None:
    """Emit a structured JSON event to stdout (one per line, auto-flush).

    Replaces ~22 inline print(json.dumps({...}), flush=True) blocks.
    """
    payload: dict[str, Any] = {"event": event, "time": _utc_iso()}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _modify_trail(
    config: Any,
    pos: Any,
    new_sl: float,
    new_tp: float,
    *,
    reason: str = "",
    brain_ids: list[str] | None = None,
    strategy_name: str = "",
    state: Any = None,
) -> None:
    """Issue modify_sltp with open_message_id resolved from state.

    Signature-compatible replacement for live_cycle.py:_dispatch_modify_trail.
    """
    from core.runtime.modify_trail_dispatch import dispatch_modify_trail

    _open_msg_id = ""
    if state is not None:
        _open_entry = state.known_open_tickets.get(pos.ticket, {})
        _open_msg_id = _open_entry.get("message_id", "")

    dispatch_modify_trail(
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
    config: Any,
    ctx: Any,
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
    """Issue a close order for a managed position with price age guard.

    Signature-compatible replacement for live_cycle.py:_dispatch_managed_close.
    Adds price age guard before delegating to core.execution.managed_close.
    """
    # Price age guard: refuse dispatch when tick is stale
    _tick_age = getattr(state, "_last_tick_age", 0.0) if state is not None else 0.0
    if _tick_age > config.close_price_max_age_seconds:
        _emit(
            "close_rejected_stale_price",
            ticket=getattr(pos, "ticket", 0),
            tick_age_seconds=round(_tick_age, 1),
            max_allowed_seconds=config.close_price_max_age_seconds,
            reason=reason[:80],
            action="refuse_dispatch_let_mt5_sltp_handle",
        )
        return False

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


def _build_and_dispatch_alert_context(
    config: Any,
    state: Any,
    pos: Any,
    pm: Any,
    pnl_ledger: Any,
) -> None:
    """Build alert hub context and dispatch to LiveAlertHub.

    Extracted from execute_management_phase (Strangler Fig #26, Phase 2).
    Reads the live trade journal for daily PnL, queries the PnL ledger
    for worst-brain metrics, and dispatches to state.alert_hub.
    """
    _ah = getattr(state, "alert_hub", None)
    if _ah is None:
        return

    try:
        # Build context from in-memory state only (Guardrail 3)
        # FIX-20260616-001: error_rate was hardcoded to 0.0 — RULE-001 was
        # permanently silent.  Derive a composite health score from the
        # degradation pipeline (already maintained by the circuit breaker).
        _degraded = getattr(state, "_consecutive_degraded_cycles", 0)
        _stale = getattr(state, "_consecutive_stale_cycles", 0)
        _total = max(1, getattr(state, "cycle_count", 0))
        _ctx_error_rate = round(min(1.0, (_degraded + _stale) / _total), 4)
        # FIX-20260616-001: frozen_brain_count was hardcoded to 0 — RULE-004
        # was permanently silent.  Count frozen brains from governance.
        _ctx_frozen = 0
        try:
            _gv_path = Path(config.base_dir) / "governance_state.json"
            if _gv_path.exists():
                _gv_raw = json.loads(_gv_path.read_text(encoding="utf-8"))
                _brain_states = _gv_raw.get("brain_states", {})
                if isinstance(_brain_states, dict):
                    _ctx_frozen = sum(
                        1
                        for _bs in _brain_states.values()
                        if isinstance(_bs, dict)
                        and str(_bs.get("state", _bs.get("status", ""))).lower() == "frozen"
                    )
        except (json.JSONDecodeError, OSError) as _fz_exc:
            # DQAF-076/BLE001-P0: governance_state.json read can fail on
            # JSONDecodeError (corruption) or OSError (filesystem).
            # Non-critical: frozen count degrades to 0.
            pass
        _ctx_pos_util = 0.0
        _ctx_bridge_last_ack = time.time() - getattr(state, "_last_bridge_ack_time", time.time())

        # Position utilization
        if pm.has_position() if pos is not None else False:
            _ctx_pos_util = min(
                1.0,
                len(getattr(pm, "positions", []) if hasattr(pm, "positions") else [])
                / max(1, config.max_positions),
            )

        # Cycle duration
        _ctx_cycle_duration = time.time() - getattr(state, "_last_cycle_start_time", time.time())

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
                    # [FIX-20260615-012] Filter out synthetic orphan closures
                    # (auto_orphan_rejected/stale/no_ticket) from alert context.
                    # These have pnl=0, position_ticket=None, and are generated
                    # by cleanup_orphan_opens() at startup — not real trades.
                    # Counting them in the rolling win-rate window dilutes the
                    # true win rate and triggers false circuit-breaker trips.
                    if str(_e.get("label", "")).startswith("auto_orphan_"):
                        continue
                    # ── Phase 0 filter 2: dedup by open position ──
                    # Prefer detail.request.position (the actual MT5
                    # position ticket being closed).  Fall back to the
                    # close order's own position_ticket.
                    _pos_tkt = _e.get("detail", {}).get("request", {}).get("position") or _e.get(
                        "position_ticket"
                    )
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
        except (json.JSONDecodeError, OSError, ValueError, TypeError, KeyError) as _je_exc:
            # DQAF-076/BLE001-P0: journal JSONL read + parse can fail on
            # JSONDecodeError (corruption), OSError (filesystem), or
            # ValueError/TypeError on malformed PnL/ticket data.
            # Non-blocking: alert context degrades without journal enrichment.
            pass
        if pnl_ledger is not None:
            try:
                # FIX-20260613-052: resolved placeholder: "Frankenstein" logic fix.
                # Previously _worst_pnl and _worst_wr were independently
                # min()'d across all brains — they could come from two
                # DIFFERENT brains (e.g. pnl from V4, wr from LGB_V1),
                # producing a misleading "strategy" metric that describes
                # no actual brain.  Now: find the single brain with the
                # worst cumulative_pnl, and use ITS win_rate too.
                #
                # FIX-20260615-011: ARCHIVED_BRAIN_ALERT_POLLUTION —
                # Archived/retired brains with years of counterfactual PnL
                # permanently dominate the "worst brain" slot, silencing
                # alerts for active brain degradation.
                # Filter to only OPERATIONAL brains (state ∈ governance,
                # excluding terminal states: retired, frozen, archived,
                # shadow, error).  Reuses the same logic as
                # DataHealthService.check_governance_state().
                _TERMINAL_STATES = {"retired", "frozen", "archived", "shadow", "error"}

                def _is_operational(brain_dict: dict) -> bool:
                    _raw = str(brain_dict.get("state", brain_dict.get("status", ""))).lower()
                    return _raw not in _TERMINAL_STATES and _raw != ""

                _active_brain_ids: set[str] = set()
                try:
                    _gov_path = Path(config.base_dir) / "governance_state.json"
                    if _gov_path.exists():
                        _gov_raw = json.loads(_gov_path.read_text(encoding="utf-8"))
                        _brain_states = _gov_raw.get("brain_states", {})
                        if isinstance(_brain_states, dict):
                            for _bid, _bs in _brain_states.items():
                                if isinstance(_bs, dict) and _is_operational(_bs):
                                    _active_brain_ids.add(_bid)
                        elif isinstance(_brain_states, list):
                            for _b in _brain_states:
                                if isinstance(_b, dict) and _is_operational(_b):
                                    _bid = _b.get("brain_id", _b.get("id", ""))
                                    if _bid:
                                        _active_brain_ids.add(_bid)
                except (json.JSONDecodeError, OSError) as _gv_exc:
                    # DQAF-076/BLE001-P0: governance_state.json read failure.
                    # JSONDecodeError = corruption, OSError = filesystem.
                    # Fail-Close: empty active_brain_ids → worst-brain
                    # computation skipped (Missing > Corrupted).
                    # FIX-20260616-001: Architect's Amendment — Fail-Close for metrics.
                    # If governance is unreadable, we MUST NOT fall back to unfiltered
                    # data (which includes archived/retired ghost brains from FIX-011).
                    # Keep _active_brain_ids as empty set → worst-brain computation
                    # will be skipped (Missing > Corrupted principle).
                    _active_brain_ids = set()
                _all_m = pnl_ledger.get_all_metrics()
                # Filter to active brains only when governance provides them.
                # If _active_brain_ids is empty (governance unreadable), skip
                # worst-brain computation entirely rather than risking ghost
                # brain pollution from unfiltered data.
                if _all_m and _active_brain_ids:
                    _active_m = {
                        _bid: _m for _bid, _m in _all_m.items() if _bid in _active_brain_ids
                    }
                    if _active_m:
                        _all_m = _active_m
                    else:
                        # No active brains have metrics — don't use stale data
                        _all_m = {}
                elif _all_m and not _active_brain_ids:
                    # Governance unavailable — skip worst-brain to avoid pollution
                    _all_m = {}
                    # else: no active brains have metrics → use full set
                    # as fallback (empty metrics would hide genuine issues)

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
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                pass

        _ah.evaluate_and_dispatch(_ctx)
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        pass


def _evaluate_brain_ensemble(
    *,
    config: Any,
    state: Any,
    pm: Any,
    pos: Any,
    mid: float,
    current_atr: float,
    strategy_name: str,
    exit_confidence: float,
    brains: list[dict[str, Any]],
    micro_feature_computer: Any,
    micro_feature_adapter: Any,
    feature_service: Any,
    daily_feature_provider: Any,
    dispatch_ctx: Any,
    exit_watchdog: Any,
    mt5_worker: Any,
    flip_enabled: bool = True,
    zscore_enabled: bool = False,
) -> Any:
    """Re-evaluate brain ensemble and apply exit layers.

    Extracted from execute_management_phase (Strangler Fig #26, Phase 3).
    Returns dict with closed, current_consensus, current_supporting,
    meta_consensus, meta_supporting.
    """
    # Pre-initialize return values at function scope so the fallback
    # return at the end is always safe, even when the re-evaluation
    # guard (multi_brain + should_reeval_brains) is False.
    # DQAF-20260629-194: UnboundLocalError when should_reeval_brains
    # returns False on early cycles — the if-block was skipped but
    # the out-of-block return referenced block-local variables.
    current_consensus: dict[str, Any] = {}
    current_supporting: list[str] = []
    meta_consensus: dict[str, Any] = {}
    meta_supporting: list[str] = []

    if config.multi_brain and pm.should_reeval_brains(state.loop_iteration):
        pm.mark_brains_reevaluated(state.loop_iteration)

        # Re-run all brain inference
        # Compute fresh multi-TF sequences for position re-evaluation
        mgmt_sequences: dict[str, np.ndarray] = {}
        if micro_feature_computer is not None:
            try:
                mgmt_sequences = micro_feature_computer.compute_all_sequences(32)
            except (ValueError, TypeError, KeyError, RuntimeError) as _seq_exc:
                # DQAF-076/BLE001-P0: compute_all_sequences() uses numpy
                # ops. ValueError on shape mismatch, TypeError on dtype,
                # KeyError on missing feature, RuntimeError on compute fail.
                _emit("sequence_compute_error", error=str(_seq_exc))
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
                elif "swing" in schema_id or "daily" in schema_id or "btc_macro" in schema_id:
                    # FIX-20260531-021 / FIX-20260610-009: Data-driven assembly via schema registry
                    # btc_macro added 2026-06-10 — BTC brains fell to else → raw 40-dim.
                    # FIX-20260615-009d: Use state._btc_augmenter for btc_macro schema
                    # to eliminate cold-start RuntimeError on cycle 1 with open positions.
                    if daily_feature_provider is not None:
                        with FaultTolerantContext(
                            level=FaultLevel.DEGRADE,
                            component="ManagementBrainInference:DailyFeature",
                        ):
                            fv_24 = daily_feature_provider.get_latest()
                            tf_ou, tf_hurst = _compute_tf_ou_hurst(state._recent_mid_prices)
                            from core.features.schemas.registry import assemble_swing_features

                            # Compute btc_augment for management-phase inference
                            _mgmt_btc_aug: Any = None
                            if "btc_macro" in str(schema_id):
                                _mgmt_aug = getattr(state, "_btc_augmenter", None)
                                if _mgmt_aug is not None:
                                    try:
                                        # FIX-20260628-059 / DQAF-059: pass zero-filled
                                        # micro features (9-dim) instead of None.
                                        # np.asarray(None, dtype=np.float64) creates
                                        # array(nan) which propagates into the output
                                        # vector and triggers AssertionError.
                                        _mgmt_btc_aug = _mgmt_aug.augment(
                                            fv_24,
                                            np.zeros(9, dtype=np.float64),
                                            btc_price=mid or 0.0,
                                            tf_ou=tf_ou,
                                            tf_hurst=tf_hurst,
                                        )
                                    except (
                                        ValueError,
                                        TypeError,
                                        RuntimeError,
                                        AttributeError,
                                    ) as _btc_exc:
                                        # DQAF-076/BLE001-P0: BTC augment
                                        # computation can fail on ValueError
                                        # (bad input), TypeError (dtype),
                                        # RuntimeError (model), or
                                        # AttributeError (augmenter not init).
                                        # FIX-20260627-058: log the actual failure for diagnosis
                                        _exc_type = type(_btc_exc).__name__
                                        _exc_msg = str(_btc_exc)[:200]
                                        _log = getattr(state, "logger", None)
                                        if _log is not None:
                                            _log.warning(
                                                f"[mgmt] BTCFeatureAugmenter.augment() failed: "
                                                f"{_exc_type}: {_exc_msg} — schema={schema_id}"
                                            )
                                        _mgmt_btc_aug = None  # degrade: fall through to None
                            fv = assemble_swing_features(
                                schema_id,
                                daily_features=fv_24,
                                tf_ou=tf_ou,
                                tf_hurst=tf_hurst,
                                btc_augment=_mgmt_btc_aug,
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
                _entry_group_signal = group_signals.get(strategy_name) if strategy_name else None
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
                    (config.strategy_configs.get(strategy_name, {}) or {}).get("_tf_mult", 1) or 1
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
                # ── FIX-20260613-050: H4 Trend Protection Umbrella ──
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
                    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                        pass
                    # ── FIX-20260715-008: trend protection was dead code ──
                    # The if/elif below was accidentally indented INSIDE the except
                    # block (since FIX-20260613-050).  It only ran when regime_gate
                    # attribute access threw, which never happens in normal operation.
                    # Moved to after try/except so H4/H1 macro trend actually shields
                    # higher-TF positions from M5 noise exits (bleed_stop, etc.).
                    if _h4_dir != "neutral" and _h4_dir == pos.side:
                        _trend_protected = True  # H4 supports position
                    elif _h4_dir == "neutral" and _h1_dir == pos.side:
                        _trend_mild_protected = True  # H1 supports, H4 silent

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
                        _emit(
                            "trend_protection_overridden_loss",
                            ticket=pos.ticket,
                            r=round(_r_check, 3),
                            reason="position_underwater_despite_trend_support",
                        )

                # Diagnostic: one-shot log when trend protection activates
                if (_trend_protected or _trend_mild_protected) and getattr(
                    pos, "cycles_held", 0
                ) <= 3:
                    _emit(
                        "trend_protection_active",
                        ticket=pos.ticket,
                        side=pos.side,
                        h4_trend=_h4_dir,
                        h1_trend=_h1_dir,
                        protection_level="full" if _trend_protected else "mild",
                        action="blocking_M5_noise_exits",
                    )

                # FIX-20260525-020: Mean-reversion (statarb/OU) strategies are
                # EXEMPT from bleed_stop.  They enter at trend extremes — price
                # continuing 3-5 bars in the same direction is normal "rubber band
                # stretching," not thesis failure.  Killing during the stretch is
                # a category error (trend exit applied to mean-reversion position).
                strategy_name_lower = (strategy_name or "").lower()
                if "statarb" not in strategy_name_lower and mid is not None and mid > 0:
                    _strat_cfg = (config.strategy_configs or {}).get(strategy_name, {}) or {}
                    _horizon = int(
                        _strat_cfg.get("horizon_cycles", 0) or _strat_cfg.get("horizon", 0) or 0
                    )
                    # ── FIX-20260715-008: TF-aware bleed bars floor ──
                    # Without a configured horizon, bleed_bars defaulted to 3 M5
                    # bars (15 min) for ALL timeframes.  For H4, 3 M5 bars is
                    # 1/16th of a single H4 bar — pure noise.  Scale the floor
                    # by _tf_mult so each TF gets a minimum observation window
                    # before bleed_stop can fire:
                    #   M5 (mult=1):  3 bars (15 min)
                    #   M15 (mult=3): 3 bars (15 min)
                    #   M30 (mult=6): 6 bars (30 min)
                    #   H1  (mult=12):12 bars (60 min)
                    #   H4  (mult=48):48 bars (240 min = 1 H4 bar)
                    _tf_mult = int((_strat_cfg or {}).get("_tf_mult", 1) or 1)
                    _tf_bleed_base = max(3, _tf_mult)
                    _bleed_bars = (
                        max(_tf_bleed_base, _horizon // 3) if _horizon > 0 else _tf_bleed_base
                    )
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
                            dispatch_ctx,
                            pos,
                            reason=_bleed_reason,
                            mid=mid,
                            state=state,
                            strategy_name=strategy_name,
                            exit_confidence=exit_confidence,
                            exit_watchdog=exit_watchdog,
                            mt5_worker=mt5_worker,
                        )
                        _emit(
                            "bleed_stop_triggered",
                            ticket=pos.ticket,
                            r_now=round(_r_now, 3),
                            reason=_bleed_reason,
                            bleed_bars=_bleed_bars,
                            cycles_held=getattr(pos, "cycles_held", 0),
                            min_hold_cycles=_min_hold,
                            horizon_cycles=_horizon,
                            dispatched=_dispatched,
                        )

                        if _dispatched:
                            pm.clear_position(ticket=pos.ticket)
                        return True

                # ── OU mean-reversion exit (ARB brain) ──
                # Only applies to positions opened by the StatArb strategy
                # (positions whose supporting brains include the OU brain).
                # Gated by per-strategy exit.zscore_exit_enabled config.
                if (
                    zscore_enabled
                    and pos.supporting_brain_ids
                    and any(
                        bid.startswith("OU_") or bid.lower().startswith("ou_")
                        for bid in pos.supporting_brain_ids
                    )
                ):
                    for b_info in brains:
                        if b_info.get("brain_type") == "ou_params_v6":
                            # ── DQAF-20260616-101/P1.3: BLE001 → log_and_continue ──
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
                                        dispatch_ctx,
                                        pos,
                                        reason=ou_reason,
                                        mid=mid,
                                        state=state,
                                        strategy_name=strategy_name,
                                        exit_confidence=exit_confidence,
                                        exit_watchdog=exit_watchdog,
                                        mt5_worker=mt5_worker,
                                    )
                                    _emit(
                                        "ou_exit_triggered",
                                        ticket=pos.ticket,
                                        z_score=round(ou_z, 3),
                                        reason=ou_reason,
                                        dispatched=_dispatched,
                                    )

                                    if _dispatched:
                                        pm.clear_position(ticket=pos.ticket)
                                    return True
                            except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                                pass
                            break  # only one OU brain

                should_exit = False
                exit_reason = ""
                if flip_enabled:
                    # ── FIX-20260613-050: Trend Protection — block M5 brain_flip ──
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
                        current_consensus.get("consensus_score", exit_confidence)
                    )
                    pm.mark_pending_close(pos.ticket, state.loop_iteration)
                    _dispatched = _dispatch_managed_close(
                        config,
                        dispatch_ctx,
                        pos,
                        reason=exit_reason,
                        mid=mid,
                        state=state,
                        strategy_name=strategy_name,
                        exit_confidence=_bf_confidence,
                        exit_watchdog=exit_watchdog,
                        mt5_worker=mt5_worker,
                    )
                    _emit(
                        "brain_exit_triggered",
                        ticket=pos.ticket,
                        reason=exit_reason,
                        dispatched=_dispatched,
                    )

                    if _dispatched:
                        pm.clear_position(ticket=pos.ticket)
                    return True
            except (
                ValueError,
                RuntimeError,
                TypeError,
                KeyError,
                AttributeError,
                ConnectionError,
                TimeoutError,
                OSError,
            ) as exc:
                # DQAF-076/BLE001-P0: brain re-evaluation covers ONNX
                # inference (ValueError/RuntimeError), data access
                # (TypeError/KeyError/AttributeError), and dispatch
                # I/O (ConnectionError/TimeoutError/OSError).
                # Fail-closed: emit error, return consensus with closed=False.
                _emit("brain_reeval_error", error=str(exc))
    # Not closed — return consensus for downstream layers
    return {
        "closed": False,
        "current_consensus": current_consensus,
        "current_supporting": current_supporting,
        "meta_consensus": meta_consensus,
        "meta_supporting": meta_supporting,
    }


def _resolve_stale_position_action(
    pos: Any,
    state: Any,
    mt5_worker: Any,
    config: Any,
) -> tuple[str, dict[str, Any] | None]:
    """Decide what to do with a managed position absent from known_open_tickets.

    The broker's ``positions_get`` is the SSOT for "is this position still
    open?".  A position can transiently vanish from ``known_open_tickets``
    across a market-closed restart (orphan re-adoption runs only at
    loop_iteration==1); treating that absence as a close blind-cleared a
    STILL-OPEN position and produced a stale-clear ↔ restart-readopt ping-pong
    that left the position hedged and unmanaged (DQAF-20260709-002).

    Returns ``(action, entry)``:
      - ``("tracked", None)``  — pos IS tracked; no reconciliation needed.
      - ``("readopt", entry)`` — broker confirms OPEN but untracked; *entry* is
        a fully-formed ``known_open_tickets`` record to re-adopt (self-heal).
      - ``("retain", None)``   — probe inconclusive (MT5 timeout); retain & retry.
      - ``("clear", None)``    — broker confirms GONE (or no_mt5); stale-clear.

    Pure decision function — performs the read-only broker probe but mutates
    nothing, so the caller owns all state changes and the branch table is unit
    testable without the full management phase.
    """
    if not (state.known_open_tickets and pos.ticket not in state.known_open_tickets):
        return ("tracked", None)

    if mt5_worker is None or config.no_mt5:
        # No broker to consult (backtest / no_mt5) — preserve legacy semantics:
        # the local tracker is the only authority available.
        return ("clear", None)

    from core.runtime.fault_handler import _MT5_TIMEOUT_SENTINEL, mt5_call_with_timeout

    _broker_pos: Any = None
    with FaultTolerantContext(
        level=FaultLevel.DEGRADE,
        component="MT5_IPC:positions_get:stale_guard",
    ):
        _broker_pos = mt5_call_with_timeout(
            mt5_worker.positions_get, ticket=pos.ticket, timeout=5.0
        )
    if _broker_pos is _MT5_TIMEOUT_SENTINEL:
        return ("retain", None)
    if not _broker_pos:
        return ("clear", None)

    # Broker confirms the position is still open → build a re-adopt record.
    # Attribution comes from the (richer) position_manager entry; magic is taken
    # from the broker-authoritative position record.
    _bp = _broker_pos[0] if isinstance(_broker_pos, list | tuple) else _broker_pos
    _entry: dict[str, Any] = {
        "entry_price": float(getattr(pos, "entry_price", 0.0) or 0.0),
        "side": str(getattr(pos, "side", "")),
        "strategy": str(getattr(pos, "strategy_name", "")),
        "magic": int(getattr(_bp, "magic", 0) or 0),
        "volume": float(getattr(pos, "volume", 0.0) or 0.0),
        "brain_ids": list(getattr(pos, "supporting_brain_ids", []) or []),
        "message_id": "",
        "sl": float(getattr(pos, "current_sl", 0.0) or getattr(pos, "initial_sl", 0.0) or 0.0),
        "tp": float(getattr(pos, "current_tp", 0.0) or getattr(pos, "initial_tp", 0.0) or 0.0),
        "source": "management_readopt",
        "readopted_at": _utc_iso(),
    }
    return ("readopt", _entry)


def execute_management_phase(
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

    If *ticket* is given, manages that specific position; otherwise manages
    the primary (backward compat).  Returns True if the position was closed.
    """
    from core.contracts.domain.dispatch_context import build_dispatch_context

    dispatch_ctx = build_dispatch_context(config)

    pm = state.position_manager
    if pm is None or not pm.has_position(ticket=ticket):
        return False

    pos = pm.get_position(ticket=ticket)
    if pos is None:
        return False

    # Guard 1: broker-authoritative presence reconciliation (DQAF-20260709-002).
    # "present in position_manager but absent from known_open_tickets" is NOT
    # proof of a close — known_open_tickets can transiently lose a still-open
    # position across a market-closed restart (orphan re-adoption runs only at
    # loop_iteration==1).  Consult the broker (SSOT) before removing anything;
    # re-adopt a still-open position instead of stale-clearing it, so the
    # stale-clear ↔ restart-readopt ping-pong that left a hedged position
    # unmanaged and un-exited cannot recur.
    _stale_action, _readopt_entry = _resolve_stale_position_action(pos, state, mt5_worker, config)
    if _stale_action == "retain":
        # Absence UNCONFIRMED (MT5 timeout) — retain tracking, retry next cycle.
        _emit(
            "position_manager_stale_probe_inconclusive",
            ticket=pos.ticket,
            reason="mt5_timeout_retain",
        )
        return False
    if _stale_action == "clear":
        # Broker CONFIRMS gone (or backtest / no_mt5) → genuine stale close.
        pm.clear_position(ticket=pos.ticket)
        _emit(
            "position_manager_stale_cleared",
            ticket=pos.ticket,
            reason="not_in_known_open_tickets",
        )
        return False
    if _stale_action == "readopt" and _readopt_entry is not None:
        # Broker CONFIRMS still open but untracked → self-heal instead of
        # clearing so the next pm.save_state() re-persists it into
        # active_position.json and breaks the ping-pong.
        state.known_open_tickets[pos.ticket] = _readopt_entry
        _emit(
            "position_manager_readopted",
            ticket=pos.ticket,
            reason="broker_open_but_untracked",
            strategy=str(_readopt_entry.get("strategy", "")),
        )
    # "tracked" / "readopt" → fall through to normal management

    # Guard 2: verify position still exists in MT5 (catches closes between
    # reconciliation cycles — up to 10 min window). Single-ticket query is
    # lightweight and prevents position_not_found dispatches.
    if mt5_worker is not None and not config.no_mt5:
        # MT5 IPC — FTC(CRASH) lets the crash propagate (no outer try/except)
        _mt5_pos = None
        with FaultTolerantContext(
            level=FaultLevel.DEGRADE,
            component="MT5_IPC:positions_get:MIA_guard",
        ):
            from core.runtime.fault_handler import (
                _MT5_TIMEOUT_SENTINEL,
                mt5_call_with_timeout,
            )

            _mt5_pos = mt5_call_with_timeout(
                mt5_worker.positions_get, ticket=pos.ticket, timeout=5.0
            )
            if _mt5_pos is _MT5_TIMEOUT_SENTINEL:
                _mt5_pos = None  # timeout → skip MIA check, retry next cycle
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
            # Enrich with MT5 deal history (close_price, reason).
            # FIX-20260612-004: Retry up to 3× with 1s delay — MT5 deal
            # finalization can lag behind position disappearance by 1-3s.
            # Without retry, ~23% of MIA closes (10/43 BTC) have null PnL
            # because deals aren't available on the first query.
            with FaultTolerantContext(
                level=FaultLevel.DEGRADE,
                component="MT5_IPC:history_deals_get:MIA_enrich",
            ):
                _deals = None
                for _retry in range(3):
                    _deals = mt5_worker.history_deals_get(position=pos.ticket)
                    if _deals:
                        break
                    if _retry < 2:  # don't sleep after last attempt
                        time.sleep(1.0)
                if _deals:
                    _enrich_mia_from_deals(_mia_entry, _deals)
            state._pending_mia_closes.append(_mia_entry)
            pm.clear_position(ticket=pos.ticket)
            state.known_open_tickets.pop(pos.ticket, None)
            # Save position state immediately — don't wait for periodic save
            with FaultTolerantContext(
                level=FaultLevel.DEGRADE,
                component="pos_state_save_mia_close",
            ):
                pm.save_state(config.position_state_path)
            _emit(
                "position_manager_mt5_not_found",
                ticket=pos.ticket,
                reason="position_closed_in_mt5",
                close_price=_mia_entry.get("detail", {}).get("close_price"),
                pnl=_mia_entry.get("pnl"),
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

    # ── DQAF-20260715-022: Magic-based strategy resolution fallback ──
    # When known_open_tickets lacks a strategy field (e.g. bootstrapped from
    # journal OPEN entries written before the bridge/journaller consistently
    # populated ``strategy``), resolve the strategy name from the position's
    # magic number stored in known_open_tickets.  Without this, modify_sltp
    # payloads carry magic=0/90401 → bridge writes
    # ``__UNATTRIBUTED_BRIDGE_DEFAULT__`` → 61.8% of today's journal entries
    # lose strategy attribution (FIX-20260715-017 only fixed open-path,
    # not the per-cycle trail dispatch path).
    if not _sname:
        _kot_entry = state.known_open_tickets.get(pos.ticket, {})
        _kot_magic = _kot_entry.get("magic", 0)
        if _kot_magic and int(_kot_magic) not in (0, 90401):
            try:
                from core.contracts.strategy_magic import MAGIC_TO_STRATEGY

                _sname = MAGIC_TO_STRATEGY.get(int(_kot_magic), "")
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                pass

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

    # ── V6 Layer B Shadow Evaluation (FIX-20260629-195) ──────────
    # Runs the 7-level Exit Priority Queue in parallel with existing
    # exit logic.  When config enabled + not shadow: preempts existing.
    # When config disabled: returns immediately (Delta-Zero Law).
    _v6_verdict = _evaluate_v6_exit_queue_shadow(
        config=config,
        state=state,
        pos=pos,
        pm=pm,
        mid_price=mid,
        current_atr=current_atr,
        strategy_name=_sname,
        current_bar=state.loop_iteration,
        mt5_worker=mt5_worker,
        symbol=config.symbol,
    )
    if _v6_verdict is not None and _v6_verdict.is_triggered:
        if getattr(state, "_v6_exit_shadow_mode", True):
            _emit(
                "v6_exit_priority_shadow",
                ticket=pos.ticket,
                v6_exit_code=_v6_verdict.exit_code,
                v6_priority=_v6_verdict.priority_level,
                v6_details=_v6_verdict.details,
                existing_stage=pos.lifecycle_stage,
            )

    # ── V6 Shadow Telemetry: persist evaluation results to JSONL ──
    # Every management phase cycle writes one line regardless of whether
    # any exit priority fired.  This gives the T24 analysis script a
    # complete record of: (a) what was evaluated, (b) why nothing fired,
    # (c) PnL/ATR/bar trajectory for threshold calibration.
    _v6_shadow_log_v6_telemetry(
        config=config,
        state=state,
        pos=pos,
        mid_price=mid,
        current_atr=current_atr,
        strategy_name=_sname,
        current_bar=state.loop_iteration,
        v6_verdict=_v6_verdict,
        ratchet_verdict=getattr(state, "_v6_last_ratchet_verdict", None),
    )

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
                _emit("health_action_skip", reason=_action.get("reason", ""))

                _emit("cycle_end", iteration=state.loop_iteration)
                return True  # skip cycle (FIX-20260620-064)
            elif _act_type == "reduce_new_position_sizes":
                _mult = _action.get("multiplier", 1.0)
                if _mult < (state._last_health_volume_mult or 1.0):
                    state._last_health_volume_mult = _mult
                _emit(
                    "health_action_reduce_size", multiplier=_mult, reason=_action.get("reason", "")
                )

    # ── 1c. Alert evaluation (FIX-20260529-040) ──
    _build_and_dispatch_alert_context(config, state, pos, pm, pnl_ledger)

    # ── DQAF-033 P2b: ExitWatchdog liveness probe ──
    # FIX-20260621-040: Periodic heartbeat confirms the management-phase code
    # path is alive.  If this log disappears from the event stream, the thread
    # or coroutine is hung (silent exception / event-loop stall / dead lock).
    _wd = getattr(state, "exit_watchdog", None)
    if pos.cycles_held % 10 == 0 or pos.cycles_held <= 3:
        _wd_status = "available" if _wd is not None else "MISSING"
        _emit(
            "watchdog_heartbeat",
            ticket=pos.ticket,
            cycles_held=pos.cycles_held,
            watchdog_status=_wd_status,
            side=getattr(pos, "side", "?"),
            entry_price=getattr(pos, "entry_price", 0),
            current_sl=getattr(pos, "current_sl", 0),
            unrealized_r=round(pm._compute_r_multiple(mid, ticket=pos.ticket), 3)
            if mid is not None
            else 0.0,
            status="management_phase_active",
        )

    # ── 2. Update regime detector ──
    regime_info: dict[str, Any] = {}
    if regime_detector is not None:
        with contextlib.suppress(RuntimeError, ValueError, KeyError, TypeError, OSError):
            regime_info = regime_detector.update(current_atr)

    # ── 3. Update position tracking ──
    # Pillar 1: Fetch current M5 bar for OHLC-calibrated extreme tracking.
    # M5 covers the full inter-cycle window; graceful degradation on failure.
    _m5_high, _m5_low, _m5_spread = None, None, 0
    if mt5_worker is not None:
        with FaultTolerantContext(
            level=FaultLevel.DEGRADE,
            component="MT5_IPC:copy_rates_from_pos:M5_OHLC_tracking",
        ):
            # ── DQAF-20260616-101/P1.1: timeout-wrapped MT5 call ──
            _m5_rates = mt5_call_with_timeout(
                mt5_worker.copy_rates_from_pos, config.symbol, 5, 0, 1, timeout=5.0
            )  # TIMEFRAME_M5
            if _m5_rates is _MT5_TIMEOUT_SENTINEL:
                _m5_rates = None  # timeout → skip OHLC tracking this cycle
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

    # ── DQAF-20260621-034 L3 Architecture Fix ──
    # live_intent_loop.py recovery (L1164-1166) unconditionally syncs
    # current_sl/current_tp from MT5 for EVERY restored position.  After
    # recovery, no code path sets current_sl back to ≤0.  Therefore a
    # position reaching management phase with current_sl ≤ 0 signals a
    # genuine recovery failure — NOT a condition to silently fix per-cycle.
    #
    # The old per-cycle sync_position_from_mt5() call was a dead safety
    # net: it would only fire if recovery had already failed, and it
    # masked the failure by silently patching the symptom every cycle.
    # Replaced with a CRITICAL alert — recovery failures must be VISIBLE.
    #
    # Edge case handled downstream: trail_dispatch force_init_snapshot
    # (FIX-037 blade #2) writes sl_uninitialized=true for SL≤0 positions,
    # so the position is NOT abandoned even in the anomalous case.
    if getattr(pos, "current_sl", 0) <= 0:
        _emit(
            "critical_sl_uninitialized",
            ticket=pos.ticket,
            current_sl=getattr(pos, "current_sl", 0),
            side=getattr(pos, "side", "?"),
            cycles_held=getattr(pos, "cycles_held", 0),
            entry_price=getattr(pos, "entry_price", 0),
            severity="CRITICAL",
            detail=(
                "Position entered management phase with uninitialized SL. "
                "live_intent_loop recovery should have synced SL from MT5 "
                "(L1164-1166). Check position_restored_from_state event "
                "for this ticket — MT5 may have reported sl=0 or recovery "
                "may have been skipped."
            ),
        )

    # ── 4-5.2: Trail SL, breakeven, trail TP ──
    # Strangler Fig #11: extracted to core/runtime/trail_dispatch.py
    _trail_result = compute_and_dispatch_trail(
        config=config,
        pos=pos,
        pm=pm,
        state=state,
        mid=mid,
        bid=bid,
        ask=ask,
        current_atr=current_atr,
        strategy_name=_sname,
        utc_iso_fn=_utc_iso,
        dispatch_modify_trail_fn=_modify_trail,
    )
    _final_sl = _trail_result["final_sl"]
    _final_tp = _trail_result["final_tp"]
    _reasons = _trail_result["reasons"]
    _be_triggered = _trail_result["be_triggered"]
    _be_dispatched = _trail_result["be_dispatched"]
    _sl_changed = _trail_result["sl_changed"]
    _tp_changed = _trail_result["tp_changed"]

    # ── DQAF-064 §2: Check previous cycle's trail dispatch for rejection ──
    if _reasons and _sl_changed:
        _prev_rejection = _check_trail_rejection(pos.ticket, config)
        if _prev_rejection:
            pos.trail_rejection_streak += 1
            pos.trail_last_rejection_code = _prev_rejection.get("retcode", 0)
            logger.warning(
                "Trail rejection streak=%d ticket=%d retcode=%d",
                pos.trail_rejection_streak,
                pos.ticket,
                pos.trail_last_rejection_code,
            )
        else:
            # Dispatch succeeded — reset streak
            if pos.trail_rejection_streak > 0:
                logger.info(
                    "Trail rejection resolved: ticket=%d after %d rejections",
                    pos.ticket,
                    pos.trail_rejection_streak,
                )
            pos.trail_rejection_streak = 0
            pos.trail_last_rejection_code = 0

        # ── Alert on 3+ consecutive rejections ──
        if pos.trail_rejection_streak >= 3:
            _send_trail_rejection_alert(
                config=config,
                state=state,
                ticket=pos.ticket,
                streak=pos.trail_rejection_streak,
                last_retcode=pos.trail_last_rejection_code,
                strategy_name=_sname,
            )
            # Reset after alert so the next cycle can retry
            pos.trail_rejection_streak = 0

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
                except (ImportError, TypeError) as _mg_exc:
                    # DQAF-076/BLE001-P0: STRATEGY_TO_MAGIC import
                    # (ImportError) or dict key TypeError on malformed
                    # strategy name.  Non-blocking: close uses default magic.
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
                            adapter_name=config.adapter_name,
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
                        _emit(
                            "partial_tp_watchdog_failed",
                            ticket=pos.ticket,
                            final_status=_ptp_result.final_status,
                            alerts=_ptp_result.alerts,
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
                        adapter_name=config.adapter_name,
                        extensions={"mt5_terminal_path": config.mt5_terminal_path},
                    )
                    _ptp_dispatched = True
            except (
                ImportError,
                RuntimeError,
                ValueError,
                ConnectionError,
                TimeoutError,
                OSError,
            ) as _ptp_exc:
                # DQAF-076/BLE001-P0: partial TP uses dispatch_live_order()
                # and exit_watchdog.execute_exit().  ImportError from
                # dynamic import, RuntimeError/ValueError from dispatch
                # guards, ConnectionError/TimeoutError/OSError from I/O.
                _emit("partial_tp_dispatch_error", ticket=pos.ticket, error=str(_ptp_exc))
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
                    _modify_trail(
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
                _emit(
                    "partial_tp_executed",
                    ticket=pos.ticket,
                    trigger="ofi" if _ofi_reason else "r_milestone",
                    r=round(pm._compute_r_multiple(mid, ticket=pos.ticket), 2),
                    closed_volume=ptp_close_vol,
                    remaining_volume=ptp_remain_vol,
                    sl_moved_to_be=improve,
                )

    # ── 6. R-milestone checks ──
    milestone = pm.check_r_milestones(mid, ticket=pos.ticket)
    if milestone:
        _emit(
            "r_milestone_hit",
            ticket=pos.ticket,
            milestone=milestone,
            r=round(pm._compute_r_multiple(mid, ticket=pos.ticket), 2),
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
                dispatch_ctx,
                pos,
                reason="grace_period_emergency",
                mid=mid,
                state=state,
                strategy_name=_sname,
                exit_confidence=_exit_confidence,
                exit_watchdog=state.exit_watchdog,
                mt5_worker=mt5_worker,
            )
            _emit(
                "grace_period_emergency_exit",
                ticket=pos.ticket,
                r=_gr_r,
                emergency_threshold=_emergency_r,
                dispatched=_dispatched,
            )

            if _dispatched:
                pm.clear_position(ticket=pos.ticket)
            return True

        _emit(
            "grace_period_skip",
            ticket=pos.ticket,
            recovery_cycle=pm._recovery_cycle,
            cycles_held=pos.cycles_held,
            r=_gr_r,
            skipped_layers=["brain_flip", "meta_exit", "time_decay"],
        )

        return False

    # ── 6.7 Pending Close Lock (FIX-20260613-052: resolved placeholder) ──
    # Prevents cross-cycle retry avalanche: when ExitWatchdog is already
    # trying to close this position, subsequent management cycles must NOT
    # spawn fresh watchdog batches.  The lock auto-expires after
    # ActivePositionManager.PENDING_CLOSE_MAX_CYCLES to allow retry.
    if pm.is_pending_close(pos.ticket, state.loop_iteration):
        _emit("pending_close_skipped", ticket=pos.ticket, loop_iteration=state.loop_iteration)

        return False

    # ── 7. Layer 2: Brain ensemble re-evaluation ──
    _ensemble_result = _evaluate_brain_ensemble(
        config=config,
        state=state,
        pm=pm,
        pos=pos,
        mid=mid if mid is not None else 0.0,
        current_atr=current_atr,
        strategy_name=_sname,
        exit_confidence=_exit_confidence,
        brains=brains,
        micro_feature_computer=micro_feature_computer,
        micro_feature_adapter=micro_feature_adapter,
        feature_service=feature_service,
        daily_feature_provider=daily_feature_provider,
        dispatch_ctx=dispatch_ctx,
        exit_watchdog=getattr(state, "exit_watchdog", None),
        mt5_worker=mt5_worker,
        flip_enabled=_flip_enabled,
        zscore_enabled=_zscore_enabled,
    )
    if _ensemble_result is True:
        return True
    current_consensus = _ensemble_result["current_consensus"]
    current_supporting = _ensemble_result["current_supporting"]
    meta_consensus = _ensemble_result["meta_consensus"]
    meta_supporting = _ensemble_result["meta_supporting"]

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
            # ── DQAF-033 P2a: MetaExit liveness probe ──
            # FIX-20260621-040: periodic heartbeat confirms the code path is alive
            # and reveals score vs threshold relationship in production.
            _meta_threshold = getattr(pm.meta_exit_engine, "threshold", "N/A")
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
                # handle exits.  TODO: re-enable when >=500 settled trades (any symbol)
                # with ExitFeatureSnapshot-level features are available for retraining.
                # Current status (2026-06-28): BTC 600 settled + 1075 snapshots ✅
                # — see scripts/check_symbol_liveness.py for liveness verification.
                # ExitFeatureSnapshot-level features are available for retraining.
                _emit(
                    "meta_exit_shadow_telemetry",
                    ticket=pos.ticket,
                    exit_urgency=round(evaluation.exit_urgency, 3),
                    threshold=_meta_threshold,
                    p_win=evaluation.p_win,
                    exit_reason=evaluation.exit_reason,
                    factor_breakdown=evaluation.factor_breakdown,
                    action="BLOCKED — telemetry only, close NOT dispatched",
                )
            elif pos.cycles_held % 20 == 0:
                # Periodic heartbeat: MetaExit alive, assessed, no trigger
                _emit(
                    "meta_exit_heartbeat",
                    ticket=pos.ticket,
                    cycles_held=pos.cycles_held,
                    threshold=_meta_threshold,
                    status="NO_TRIGGER — MetaExit engine alive, no exit signal",
                )

        except (ValueError, TypeError, AttributeError) as exc:
            # DQAF-076/BLE001-P0: MetaExit evaluation uses
            # pm.evaluate_meta_exit().  ValueError on bad inputs,
            # TypeError/AttributeError on malformed evaluation object.
            # Non-blocking: MetaExit is shadow-mode (telemetry only).
            _emit("meta_exit_error", error=str(exc))
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
                dispatch_ctx,
                pos,
                reason=hesitate_reason,
                mid=mid,
                state=state,
                strategy_name=_sname,
                exit_confidence=_exit_confidence,
                exit_watchdog=state.exit_watchdog,
                mt5_worker=mt5_worker,
            )
            _emit(
                "hesitation_exit_triggered",
                ticket=pos.ticket,
                cycles_held=pos.cycles_held,
                hesitation_cycles=pm.hesitation_cycles,
                dispatched=_dispatched,
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
                dispatch_ctx,
                pos,
                reason=exit_reason,
                mid=mid,
                state=state,
                strategy_name=_sname,
                exit_confidence=_exit_confidence,
                exit_watchdog=state.exit_watchdog,
                mt5_worker=mt5_worker,
            )
            _emit(
                "time_exit_triggered",
                ticket=pos.ticket,
                cycles_held=pos.cycles_held,
                r=round(pm._compute_r_multiple(mid, ticket=pos.ticket), 2),
                dispatched=_dispatched,
            )

            if _dispatched:
                pm.clear_position(ticket=pos.ticket)
            return True

    return False


# ── DQAF-064 §2: Trail rejection detection helpers ──────────────────────


def _check_trail_rejection(ticket: int, config: Any) -> dict | None:
    """Check if the most recent trail modify_sltp for ``ticket`` was rejected.

    Reads the journal tail (last ~16KB) and looks for the most recent
    modify_sltp entry for this position.  Returns a dict with ``retcode``
    if the last entry was rejected, or None if accepted / not found.
    """
    _journal_path = Path(getattr(config, "data_dir", "data")) / "live_trade_journal.jsonl"
    if not _journal_path.exists():
        return None

    try:
        # Read journal tail efficiently
        with open(_journal_path, "rb") as _fh:
            _fh.seek(0, 2)
            _size = _fh.tell()
            _read_size = min(_size, 16384)
            _fh.seek(max(0, _size - _read_size))
            _tail = _fh.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None

    _most_recent: dict | None = None
    for _line in reversed(_tail.strip().split("\n")):
        _line = _line.strip()
        if not _line:
            continue
        try:
            _entry = json.loads(_line)
        except json.JSONDecodeError:
            continue
        if _entry.get("position_ticket") == ticket and _entry.get("action") == "modify_sltp":
            _most_recent = _entry
            break

    if _most_recent is None:
        return None

    _ack = _most_recent.get("ack_status", "")
    if _ack == "rejected":
        _detail = _most_recent.get("detail", {})
        _retcode = _detail.get("retcode", 0) if isinstance(_detail, dict) else 0
        return {"retcode": _retcode, "ack_status": "rejected"}
    return None


def _send_trail_rejection_alert(
    *,
    config: Any,
    state: Any,
    ticket: int,
    streak: int,
    last_retcode: int,
    strategy_name: str = "",
) -> None:
    """Send DingTalk alert when trail modifications are repeatedly rejected.

    FIX-20260707-009: alert_hub attribute mismatch corrected.
    Previously looked up ``config.alert_hub``, but ``LiveCycleConfig``
    (a dataclass) has no ``alert_hub`` field — the hub lives on
    ``LiveCycleState``.  This caused every rejection alert to silently
    fall through to ``logger.warning()``, never reaching DingTalk.
    """
    _msg = (
        f"TRAIL_MODIFICATION_FAILED\n"
        f"Position: {ticket}\n"
        f"Strategy: {strategy_name}\n"
        f"Consecutive rejections: {streak}\n"
        f"Last MT5 retcode: {last_retcode}\n"
        f"Action: Trail suspended for this position, will retry next cycle."
    )
    try:
        _hub = getattr(state, "alert_hub", None)
        if _hub is not None and hasattr(_hub, "send_alert"):
            _hub.send_alert(
                severity="warning",
                title="Trail Modification Failed",
                message=_msg,
                tags=["trail", "mt5_rejection", f"rc{last_retcode}"],
            )
        else:
            logger.warning("TRAIL_REJECTION_ALERT (no hub): %s", _msg.replace("\n", " | "))
    except Exception:
        logger.exception("Failed to send trail rejection alert for ticket=%d", ticket)


# ═══════════════════════════════════════════════════════════════════════
# V6 Shared Trading Infrastructure — Shadow Integration (FIX-20260629-195)
# ═══════════════════════════════════════════════════════════════════════


def _compute_shadow_regime_info(
    mt5_worker: Any,
    symbol: str,
    pos: Any,
    mid_price: float,
    exit_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute M15/M30 regime proxies for V6 shadow StageGate + P3 RegimeCollapse.

    Fetches M15 and M30 OHLC bars from MT5, computes lightweight directional
    regime indicators.  All MT5 IPC is fault-tolerant — failures degrade to
    an empty dict (neutral regime, no P3/P4/P5 triggers).

    Returns dict with keys consumed by ExitPriorityQueue.evaluate():
      - m15_regime_prob:   float 0-1  — M15 directional alignment with position
      - m15_atr:           float      — M15 ATR(14) for at-risk checks
      - m30_regime:        float 0-1  — M30 directional alignment with position
      - m30_atr:           float      — M30 ATR(14) for exposure reduction
      - m15_confirm_regime_min: float — from stage_gate config (fallback 0.60)

    Design (v6_integration_blueprint §3 — StageGate):
      - Regime score = sigmoid(normalized EMA deviation × position sign)
        · 0.50 = neutral (price at M15/M30 EMA)
        · >0.60 = aligned  (M15/M30 trend supports position)
        · <0.30 = opposed  (M15/M30 trend contradicts — P3 may fire)
      - All computation is local (numpy) — no RegimeGate dependency.
      - Delta-Zero: when mt5_worker is None or MT5 calls fail, returns {} —
        P3 skips, StageGate stays neutral.
    """
    import numpy as np

    _regime: dict[str, Any] = {}
    if mt5_worker is None or not symbol:
        return _regime

    _stage_cfg = (exit_cfg or {}).get("stage_gate", {})
    _m15_min = float(_stage_cfg.get("m15_confirm_regime_min", 0.60))
    _regime["m15_confirm_regime_min"] = _m15_min

    _pos_side = getattr(pos, "side", "long")
    _pos_sign = 1.0 if _pos_side == "long" else -1.0

    # ── Helper: fetch TF bars + compute ATR + EMA ──────────────
    def _tf_regime(tf: int, label: str, count: int = 20) -> dict[str, float]:
        """Fetch *count* bars at *tf*, return {atr, ema_dev, close}."""
        result: dict[str, float] = {}
        try:
            rates = mt5_worker.copy_rates_from_pos(symbol, tf, 0, count, timeout=8.0)
        except (OSError, ValueError, RuntimeError, TypeError):
            return result
        if rates is None or len(rates) < 15:
            return result
        try:
            closes = np.array([r["close"] for r in rates], dtype=np.float64)
            highs = np.array([r["high"] for r in rates], dtype=np.float64)
            lows = np.array([r["low"] for r in rates], dtype=np.float64)
        except (KeyError, IndexError, TypeError):
            return result

        # ATR(14)
        period = 14
        prev_c = closes[-(period + 1) : -1]
        cur_h = highs[-period:]
        cur_l = lows[-period:]
        tr = np.maximum(
            cur_h - cur_l,
            np.maximum(np.abs(cur_h - prev_c), np.abs(cur_l - prev_c)),
        )
        atr_val = float(np.mean(tr))
        result[f"{label}_atr"] = round(atr_val, 6) if atr_val > 0 else 0.0

        # EMA(8) slope — simple trend direction
        ema_span = 8
        alpha = 2.0 / (ema_span + 1)
        ema = closes[0]
        for c in closes[1:]:
            ema = alpha * c + (1 - alpha) * ema
        # Normalized deviation of current close from EMA
        if atr_val > 0:
            ema_dev = (closes[-1] - ema) / max(atr_val, 0.0001)
        else:
            ema_dev = 0.0
        result[f"{label}_ema_dev"] = round(float(ema_dev), 6)
        result[f"{label}_close"] = round(float(closes[-1]), 6)
        return result

    # ── Fetch M15 + M30 ────────────────────────────────────────
    with FaultTolerantContext(
        level=FaultLevel.DEGRADE,
        component="V6_Shadow:M15_regime_fetch",
    ):
        _m15 = _tf_regime(MT5_TIMEFRAME_M15, "m15")
        if _m15:
            _regime["m15_atr"] = _m15.get("m15_atr", 0.0)
            _ema_dev = _m15.get("m15_ema_dev", 0.0)
            # Sigmoid: map [-inf, +inf] → [0, 1]
            # gain=6 gives ~0.27 at -0.2 ATR, ~0.73 at +0.2 ATR
            _regime["m15_regime_prob"] = round(
                float(1.0 / (1.0 + np.exp(-6.0 * _ema_dev * _pos_sign))), 4
            )

    with FaultTolerantContext(
        level=FaultLevel.DEGRADE,
        component="V6_Shadow:M30_regime_fetch",
    ):
        _m30 = _tf_regime(MT5_TIMEFRAME_M30, "m30")
        if _m30:
            _regime["m30_atr"] = _m30.get("m30_atr", 0.0)
            _ema_dev = _m30.get("m30_ema_dev", 0.0)
            _regime["m30_regime"] = round(
                float(1.0 / (1.0 + np.exp(-6.0 * _ema_dev * _pos_sign))), 4
            )

    return _regime


def _evaluate_v6_exit_queue_shadow(
    *,
    config: Any,
    state: Any,
    pos: Any,
    pm: Any,
    mid_price: float,
    current_atr: float,
    strategy_name: str,
    current_bar: int,
    mt5_worker: Any = None,
    symbol: str = "",
) -> Any | None:  # ExitVerdict | None
    """Shadow-evaluate the V6 7-level Exit Priority Queue.

    Delta-Zero Law: When config is absent or global.enabled=False, returns
    None immediately — zero runtime overhead beyond the function call.

    Lazy-initializes ExitPriorityQueue + RatchetRisk on state._v6_exit_queue
    and state._v6_ratchet_risk.  Config is read from configs/trading/exit_priority.yaml
    if available, otherwise uses inline defaults (all disabled).

    Returns ExitVerdict if the queue fires, None otherwise.
    """
    from pathlib import Path

    # ── Delta-Zero guard ──────────────────────────────────────
    _v6_cfg = getattr(state, "_v6_exit_config", None)
    if _v6_cfg is None:
        # Lazy-load config from YAML if available
        _cfg_path = Path("configs/trading/exit_priority.yaml")
        if _cfg_path.exists():
            try:
                import yaml

                with open(_cfg_path, encoding="utf-8") as f:
                    _v6_cfg = yaml.safe_load(f)
            except (OSError, ImportError, ValueError, KeyError, TypeError):
                _v6_cfg = {"global": {"enabled": False}}
        else:
            _v6_cfg = {"global": {"enabled": False}}
        state._v6_exit_config = _v6_cfg

    if not _v6_cfg.get("global", {}).get("enabled", False):
        return None

    # ── Lazy-init queue ───────────────────────────────────────
    if getattr(state, "_v6_exit_queue", None) is None:
        state._v6_exit_queue = ExitPriorityQueue(_v6_cfg)
        state._v6_ratchet_risk = RatchetRisk()
        _p6_cfg = _v6_cfg.get("exit_queue", {}).get("P6_ratchet_risk", {})
        _be_cfg = _p6_cfg.get("breakeven_defense", {})
        _dd_cfg = _p6_cfg.get("drawdown_lock", {})
        state._v6_ratchet_config = RatchetConfig(
            breakeven_enabled=_be_cfg.get("enabled", False),
            breakeven_atr_mult=float(_be_cfg.get("atr_mult", 1.2)),
            cost_buffer=float(_be_cfg.get("cost_buffer", 5.0)),
            drawdown_enabled=_dd_cfg.get("enabled", False),
            drawdown_activation_atr=float(_dd_cfg.get("activation_atr", 2.0)),
            drawdown_giveback_pct=float(_dd_cfg.get("giveback_pct", 35.0)),
        )

    queue: ExitPriorityQueue = state._v6_exit_queue
    ratchet: RatchetRisk = state._v6_ratchet_risk
    ratchet_cfg: RatchetConfig = state._v6_ratchet_config

    state._v6_exit_shadow_mode = _v6_cfg.get("global", {}).get("shadow_mode", True)

    # ── Compute OU params from position context ───────────────
    ou_params = {
        "z_score": getattr(pos, "entry_z_score", 0.0),
        "kf_failed_bars": 0,
        "kf_freeze_bars": 3,
        "kf_crossfade_bars": 3,
    }

    # ── Compute regime info (T23: M15/M30 data feed) ──────────
    # FIX-20260703-002-B: Replace broken regime_gate.classify() call
    # (zero-arg call raised TypeError, silently caught — _regime_info
    # was always {}).  Use lightweight MT5-based M15/M30 regime proxy
    # that computes directional alignment from EMA deviation × ATR.
    _regime_info = _compute_shadow_regime_info(
        mt5_worker=mt5_worker,
        symbol=symbol,
        pos=pos,
        mid_price=mid_price,
        exit_cfg=_v6_cfg,
    )
    state._v6_last_regime_info = _regime_info  # T23: for telemetry + analysis

    # ── Compute bar PnLs for circuit breaker ──────────────────
    bar_pnls = list(getattr(pos, "bar_pnls", []) or [])
    # Append current cycle PnL if available
    _current_pnl = getattr(pos, "current_pnl", None)
    if _current_pnl is None and mid_price > 0 and pos.entry_price > 0:
        if pos.side == "long":
            _current_pnl = (mid_price - pos.entry_price) * pos.volume * 100.0
        elif pos.side == "short":
            _current_pnl = (pos.entry_price - mid_price) * pos.volume * 100.0
    if _current_pnl is not None and (not bar_pnls or bar_pnls[-1] != _current_pnl):
        bar_pnls.append(_current_pnl)
    if not bar_pnls:
        return None

    # Temporarily attach current PnL to pos for the queue
    if not hasattr(pos, "current_pnl") or getattr(pos, "current_pnl", None) is None:
        pos.current_pnl = _current_pnl

    # ── Ratchet evaluation ────────────────────────────────────
    _point_value = 100.0  # XAUUSD default
    ratchet_verdict = ratchet.evaluate(
        net_pnl=float(_current_pnl or 0.0),
        atr=current_atr,
        point_value=_point_value,
        base_lot=pos.volume,
        breakeven_armed=getattr(pos, "ratchet_breakeven_armed", False),
        drawdown_armed=getattr(pos, "ratchet_drawdown_armed", False),
        peak_pnl=getattr(pos, "ratchet_peak_pnl", 0.0),
        config=ratchet_cfg,
    )

    # ── Exit Priority Queue evaluation ────────────────────────
    verdict = queue.evaluate(
        pos=pos,
        pm=pm,
        mid={"price": mid_price},
        current_atr=current_atr,
        regime_info=_regime_info,
        current_z_score=float(getattr(pos, "entry_z_score", 0.0)),
        prev_z_score=None,
        entry_z_score=float(getattr(pos, "entry_z_score", 0.0)),
        entry_half_life=float(getattr(pos, "entry_half_life", 0.0)),
        current_bar=current_bar,
        point_value=_point_value,
        ratchet_verdict=ratchet_verdict,
        ou_params=ou_params,
    )

    # ── Persist ratchet state updates ─────────────────────────
    if ratchet_verdict.details:
        if ratchet_verdict.details.get("_breakeven_armed"):
            pos.ratchet_breakeven_armed = True
        if ratchet_verdict.details.get("_drawdown_armed"):
            pos.ratchet_drawdown_armed = True
        _peak = ratchet_verdict.details.get("_peak_pnl", 0.0)
        if _peak > getattr(pos, "ratchet_peak_pnl", 0.0):
            pos.ratchet_peak_pnl = _peak
    # Store ratchet verdict for telemetry logging in caller
    state._v6_last_ratchet_verdict = ratchet_verdict
    return verdict


def _v6_shadow_log_v6_telemetry(
    *,
    config: Any,
    state: Any,
    pos: Any,
    mid_price: float,
    current_atr: float,
    strategy_name: str,
    current_bar: int,
    v6_verdict: Any | None,
    ratchet_verdict: Any | None,
) -> None:
    """Persist one V6 shadow evaluation record to a JSONL file.

    Writes every management-phase cycle regardless of whether any exit
    priority fired.  The T24 analysis script reads this file for
    calibration and verification.
    """
    import os as _os

    _base_dir = getattr(config, "base_dir", "data")
    _reports_dir = _os.path.join(_base_dir, "reports")
    _os.makedirs(_reports_dir, exist_ok=True)
    _path = _os.path.join(_reports_dir, "v6_shadow_exits.jsonl")

    _pnl = getattr(pos, "current_pnl", None)
    if _pnl is None and mid_price > 0 and getattr(pos, "entry_price", 0) > 0:
        if pos.side == "long":
            _pnl = (mid_price - pos.entry_price) * getattr(pos, "volume", 0.01) * 100.0
        elif pos.side == "short":
            _pnl = (pos.entry_price - mid_price) * getattr(pos, "volume", 0.01) * 100.0

    _triggered = v6_verdict is not None and getattr(v6_verdict, "is_triggered", False)
    _record: dict[str, Any] = {
        "event": "v6_shadow_telemetry",
        "time": _utc_iso(),
        "ticket": getattr(pos, "ticket", 0),
        "strategy": strategy_name,
        "side": getattr(pos, "side", ""),
        "entry_price": getattr(pos, "entry_price", 0.0),
        "volume": getattr(pos, "volume", 0.01),
        "mid_price": mid_price,
        "current_pnl": round(float(_pnl or 0.0), 4),
        "current_atr": round(current_atr, 4),
        "bar": current_bar,
        "cycles_held": getattr(pos, "cycles_held", 0),
        "lifecycle_stage": getattr(pos, "lifecycle_stage", "MANAGED"),
        "v6_triggered": _triggered,
    }

    if _triggered and v6_verdict is not None:
        _record["v6_exit_code"] = getattr(v6_verdict, "exit_code", "")
        _record["v6_priority"] = getattr(v6_verdict, "priority_level", "")
        _record["v6_details"] = getattr(v6_verdict, "details", {})
        _record["v6_reason"] = getattr(v6_verdict, "reason", "")

    if ratchet_verdict is not None:
        _record["ratchet_breakeven_armed"] = getattr(pos, "ratchet_breakeven_armed", False)
        _record["ratchet_drawdown_armed"] = getattr(pos, "ratchet_drawdown_armed", False)
        _record["ratchet_peak_pnl"] = getattr(pos, "ratchet_peak_pnl", 0.0)
        _rd = getattr(ratchet_verdict, "details", {}) or {}
        if _rd.get("_breakeven_fired"):
            _record["ratchet_breakeven_fired"] = True
        if _rd.get("_drawdown_fired"):
            _record["ratchet_drawdown_fired"] = True

    # ── T23: M15/M30 regime telemetry ─────────────────────────
    _ri = getattr(state, "_v6_last_regime_info", None) or {}
    if _ri.get("m15_regime_prob") is not None:
        _record["m15_regime_prob"] = _ri["m15_regime_prob"]
        _record["m15_atr"] = _ri.get("m15_atr", 0.0)
    if _ri.get("m30_regime") is not None:
        _record["m30_regime"] = _ri["m30_regime"]
        _record["m30_atr"] = _ri.get("m30_atr", 0.0)

    try:
        with open(_path, "a", encoding="utf-8") as _fh:
            _fh.write(json.dumps(_record, ensure_ascii=False) + "\n")
    except (OSError, ValueError, KeyError, TypeError):
        pass  # fail-safe: never crash management phase for telemetry
