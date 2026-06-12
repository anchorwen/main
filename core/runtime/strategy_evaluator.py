"""Multi-strategy evaluation — independent strategy evaluation + risk + execution queue.

Extracted from live_cycle.py per the Strangler Fig pattern (#7).
Runs each strategy line independently, applies regime gates, cooldown,
family spacing, portfolio risk checks, and √N correlation discount.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from core.execution.execution_queue import ExecutionQueue
from core.execution.portfolio_risk import PortfolioRiskController, RiskVerdict
from core.execution.regime_gate import RegimeGate


def _utc_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def evaluate_strategy_lines(
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
    # ── FIX-20260607-007: trend maturity signals ──
    hurst: float | None = None,
    kalman_velocity_bps: float | None = None,
    # ── FIX-20260606-131: reentry guard front-placement (P2.6) ──
    reentry_states: dict[str, Any] | None = None,
    reentry_sl_cooldown: float | None = None,
    reentry_sl_penalty: float | None = None,
    reentry_bleed_cooldown: float | None = None,
    reentry_bleed_penalty: float | None = None,
    # ── FIX-20260606-138: bootstrap degraded flag (Fail-Closed) ──
    bootstrap_degraded: bool = False,
    btc_augment: Any = None,  # FIX-20260607-XXX: pre-computed 37-dim BTC vector
    # ── FIX-20260609-011: governance degradation gate ──
    governance_state: dict[str, Any] | None = None,
    # ── FIX-20260611-022: data-health degradation constraints ──
    degradation_constraints: Any | None = None,
) -> dict[str, Any]:
    """Run independent strategy evaluations + portfolio risk + execution queue.

    Returns a summary dict for logging.

    When *bootstrap_degraded* is True (restart state restoration failed),
    ALL trades are blocked — the system defaults to Fail-Closed rather than
    silently allowing trades through empty guard state.
    """
    # ── FIX-20260606-138: Fail-Closed on bootstrap degradation ──
    if bootstrap_degraded:
        import json as _json_fc

        _blocked_summary: dict[str, Any] = {
            "event": "gate_chain_blocked",
            "reason": "bootstrap_degraded_fail_closed",
            "time": _utc_iso(),
            "message": (
                "Restart state bootstrap failed — reentry guard, cooldown, "
                "and budget state could not be verified.  All trades are "
                "blocked until manual intervention confirms state integrity."
            ),
            "action_required": (
                "Check journal file integrity and restart the system. "
                "If journal is intact, review bootstrap logs for errors."
            ),
            "strategies_blocked": sorted(strategy_lines.keys()),
        }
        print(_json_fc.dumps(_blocked_summary, ensure_ascii=False), flush=True)
        return {
            "decisions_map": {},
            "trade_decisions": 0,
            "strategy_results": [
                {
                    "strategy": sname,
                    "should_trade": False,
                    "direction": "neutral",
                    "confidence": 0.0,
                    "reason": "bootstrap_degraded_fail_closed",
                }
                for sname in strategy_lines
            ],
        }

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

        # ── M15 bar-boundary gating ──
        _tf = getattr(getattr(strategy, "config", None), "timeframe", "M5")
        if _tf == "M15" and mtf_price_service is not None:
            _utc_minute = datetime.now(UTC).minute
            if not mtf_price_service.is_m15_boundary(_utc_minute):
                continue
        _effective_mid = mid_price

        # ── Cut 1: Absolute Refractory Period (cooldown check) ──
        if cooldown_registry is not None:
            _cd_allowed, _cd_reason = cooldown_registry.check_cooldown(
                sname,
                "long",
            )
            if not _cd_allowed:
                pass

        # ── Cut 2: Family entry spacing check (pre-evaluate) ──
        if family_entry_tracker is not None:
            from core.execution.pre_trade_guards import strategy_to_family

            _fam = strategy_to_family(sname)
            if _fam != sname:
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
            hurst=hurst,  # FIX-20260607-007
            kalman_velocity_bps=kalman_velocity_bps,  # FIX-20260607-007
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
            btc_augment=btc_augment,  # FIX-20260607-XXX
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
            if _fam != sname:
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
                else:
                    # ── FIX-20260609-002: intra-cycle optimistic lock ──
                    # Record the entry immediately so that subsequent
                    # family members evaluated in the SAME cycle see the
                    # spacing gap and are blocked.  Without this, all
                    # family members pass spacing check simultaneously
                    # (no entries recorded yet) → cluster entry.
                    # DQAF-20260609-002 diagnosed (3 swing same-second).
                    import time as _time

                    family_entry_tracker.record_entry(
                        _fam,
                        decision.direction,
                        _time.time(),
                    )

        # ── Cut 3: Reentry quality guard (FIX-20260606-131, P2.6 front-placement) ──
        if decision.should_trade and reentry_states is not None:
            from core.execution.reentry_guard import ensure_reentry_state

            _rs = ensure_reentry_state(reentry_states, sname)
            _allowed, _rr_reason, _cons_count_f = _rs.check_and_record_entry(
                direction=decision.direction,
                confidence=decision.confidence,
                mid=mid_price or 0.0,
                entry_half_life=getattr(decision, "entry_half_life", 0.0),
                timeframe_minutes=5.0,
                sl_cooldown=reentry_sl_cooldown,
                sl_penalty=reentry_sl_penalty,
                bleed_cooldown=reentry_bleed_cooldown,
                bleed_penalty=reentry_bleed_penalty,
            )
            if not _allowed:
                decision.should_trade = False
                decision.reason = _rr_reason
                print(
                    json.dumps(
                        {
                            "event": "reentry_blocked",
                            "time": _utc_iso(),
                            "strategy": sname,
                            "direction": decision.direction,
                            "confidence": round(decision.confidence, 4),
                            "reason": _rr_reason,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        # ── Cut 4: Governance degradation gate (FIX-20260609-011) ──────────
        # When NO brain in this strategy has achieved "live" status, the
        # strategy is trading with unproven (candidate) or degraded
        # (probation/frozen) models.  Degrade to minimum exploration volume
        # and require higher confidence to prevent "cadet brains driving
        # heavy mechs" (observed: 4 candidate brains, 0.1 lot, -$30/day).
        if decision.should_trade and governance_state is not None:
            # ── DQAF-20260612-002 / FIX-20260612-006: SSOT fix ──
            # Bypass legacy strategy.brains nested-dict lookup (fragile: depends
            # on "brain_id" key convention and is vulnerable to registry→governance
            # status skew).  Use the flat list[str] from the decision object —
            # these are the brain IDs that actually voted in this cycle, freshly
            # resolved by strategy.evaluate().
            _voted_brain_ids = getattr(decision, "brain_ids", [])
            _live_count = sum(
                1
                for bid in _voted_brain_ids
                if governance_state.get(bid, {}).get("status") == "live"
            )
            if _live_count == 0:
                _degraded_confidence_floor = 0.50
                _degraded_max_volume = 0.01
                if decision.confidence < _degraded_confidence_floor:
                    decision.should_trade = False
                    decision.reason = "no_live_brains_and_low_confidence"
                    print(
                        json.dumps(
                            {
                                "event": "governance_degraded_blocked",
                                "time": _utc_iso(),
                                "strategy": sname,
                                "direction": decision.direction,
                                "confidence": round(decision.confidence, 4),
                                "live_brains": 0,
                                "reason": decision.reason,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                else:
                    decision.volume = min(decision.volume, _degraded_max_volume)
                    decision.reason = (decision.reason or "") + " [degraded: no_live_brains]"
                    print(
                        json.dumps(
                            {
                                "event": "governance_degraded_volume",
                                "time": _utc_iso(),
                                "strategy": sname,
                                "direction": decision.direction,
                                "confidence": round(decision.confidence, 4),
                                "volume": decision.volume,
                                "live_brains": 0,
                                "reason": decision.reason,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

        # ── Cut 5: Fail-Closed SL/TP assertion (FIX-20260611-020) ───────
        # Reject ANY trade decision that lacks valid SL/TP, regardless of
        # confidence or brain votes.  SL=0 means "unlimited risk" which is
        # never acceptable for automated trading.  Shadow-mode decisions
        # are exempt (virtual tracking, no real order).
        #
        # Historical: DQAF-20260607-005 (FIX-140/141/142) established
        # Fail-Closed dispatch after UnboundLocalError caused orphan
        # positions.  This extends the pattern to the SL/TP dimension:
        # FIX-20260611-017 fixed premature-breakeven from uninitialized
        # lowest_low=0.0, but the symmetric risk (SL/TP uninitialized = 0)
        # was left unprotected until now.
        if (
            decision.should_trade
            and gate_mode != "shadow"
            and (decision.sl <= 0 or decision.tp <= 0)
        ):
            decision.should_trade = False
            decision.reason = f"fail_closed_sltp_missing(sl={decision.sl:.1f}_tp={decision.tp:.1f})"
            print(
                json.dumps(
                    {
                        "event": "fail_closed_sltp_rejected",
                        "time": _utc_iso(),
                        "strategy": sname,
                        "direction": decision.direction,
                        "sl": decision.sl,
                        "tp": decision.tp,
                        "hard_sl": decision.hard_sl,
                        "confidence": round(decision.confidence, 4),
                        "live_brains": _live_count if governance_state is not None else -1,
                        "reason": decision.reason,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        # ── Cut 6: Data-health degradation → progressive risk reduction ──
        # FIX-20260611-022: Computed upstream from DataHealthService output.
        # NORMAL(100%) → YELLOW(40%) → ORANGE(15%,no new) → RED(0%,close-only).
        if (
            decision.should_trade
            and gate_mode != "shadow"
            and degradation_constraints is not None
        ):
            try:
                from core.observability.degradation import apply_degradation_to_decision

                _dv, _dt, _dr = apply_degradation_to_decision(
                    degradation_constraints,
                    decision.volume,
                    decision.should_trade,
                )
                if _dr:
                    decision.volume = _dv
                    decision.should_trade = _dt
                    decision.reason = (decision.reason or "") + _dr
                    if not _dt:
                        print(
                            json.dumps(
                                {
                                    "event": "degradation_blocked",
                                    "time": _utc_iso(),
                                    "strategy": sname,
                                    "direction": decision.direction,
                                    "reason": decision.reason,
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
            except Exception:  # noqa: BLE001
                pass

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
            except Exception:  # noqa: BLE001
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

        # Update current_positions snapshot
        current_positions[sname] = {
            "strategy": sname,
            "direction": decision.direction,
            "volume": risk_result.adjusted_volume
            if risk_result.adjusted_volume > 0
            else decision.volume,
            "ticket": 0,
            "entry_cycle": cycle_count,
            "brain_ids": getattr(decision, "brain_ids", []),
        }

    # ── Tier 3: √N correlation discount ──
    from core.execution.correlation_sizer import apply_sqrt_n_discount

    _, sqrt_n_clusters = apply_sqrt_n_discount(decisions)

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
        for sname in list(current_positions.keys()):
            if sname in dropped_names:
                del current_positions[sname]
        for sr in strategy_results:
            if sr.get("strategy", "") in dropped_names:
                for d in decisions:
                    if d.strategy_name == sr["strategy"]:
                        sr["should_trade"] = False
                        sr["reason"] = d.reason
                        sr["volume"] = 0.0
                        break

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

    return {
        "strategy_results": strategy_results,
        "trade_decisions": len(decisions),
        "queued": execution_queue.queue_size,
        "active_strategies": list(strategy_lines.keys()),
        "decisions_map": {d.strategy_name: d for d in decisions},
    }
