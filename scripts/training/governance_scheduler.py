"""Governance auto-scheduler: periodically apply brain lifecycle actions.

Uses BrainPnLStore metrics (Sharpe, win rate, profit factor, max drawdown)
as the primary signal for governance decisions. Falls back to the older
BrainPerformanceTracker (composite scores) only when PnL data is unavailable.

FIX-20260621-043: Journal metrics type normalization — compute_journal_brain_metrics()
returns dicts, but downstream code expects BrainPnLMetrics dataclass instances.
Added _dict_to_pnl_metrics() converter and post-augmentation type assertion.

Usage:
  # One-shot check + apply (PnL-first)
  python scripts/training/governance_scheduler.py --base-dir data

  # Dry-run: check what would happen without applying
  python scripts/training/governance_scheduler.py --base-dir data --dry-run

  # Run from daily recap or cron
  python scripts/training/governance_scheduler.py --base-dir data --output data/reports/governance_actions.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from core.feedback.brain_performance_tracker import BrainPerformanceTracker
from core.feedback.brain_pnl_ledger import BrainPnLMetrics
from core.feedback.brain_pnl_ledger import BrainPnLStore
from core.governance.governance_service import GovernanceService

SCHEMA_VERSION = "governance_scheduler.v2"

# ── Fallback constants for legacy tracker path ──
AUTO_APPLY = {"demote_to_probation", "freeze", "limit_exposure"}
REQUIRE_CONFIRMATION = {"eligible_for_promotion", "retire", "restrict", "archive"}

# ── PnL-based governance thresholds ──
MIN_TRADES_FOR_LIVE = 50  # ↑ 30→50: need at least 50 settled signals to promote to live
MIN_TRADES_FOR_RETIRE = 50  # need at least 50 before retirement is allowed
MIN_TRADES_FOR_FREEZE = 50  # need at least 50 shadow trades before auto-freeze
MIN_TRADES_FOR_DEMOTE = 100  # minimum trades before auto-demoting a live brain
SHARPE_RETIRE_THRESHOLD = (
    -2.0
)  # Sharpe below this + 50+ trades → retire (aligned with BrainQualityEngine)
SHARPE_FREEZE_THRESHOLD = -1.5  # Shadow brain: Sharpe below this + 50+ trades → freeze
SHARPE_DEMOTE_THRESHOLD = -5.0  # Live brain: Sharpe below this + 100+ trades → probation
SHARPE_PROBATION_THRESHOLD = 0.0  # Sharpe below this → probation
WR_PROBATION_THRESHOLD = 0.45  # win rate below this → probation
SHARPE_HIGH_ALPHA = 1.5  # Sharpe above this + WR/PF checks → high_alpha
WR_HIGH_ALPHA = 0.55  # win rate threshold for high_alpha
PF_HIGH_ALPHA = 1.5  # profit factor threshold for high_alpha

# ── FIX-20260627-152: RR-adjusted channel for low-WR high-RR strategies ──
# ── FIX-20260629-173: Structural Sharpe concession for trend-following ──
# Swing/directional strategies with WR < 45% but positive expectancy via
# high reward:risk ratio (>2:1) are profitable engines blocked by the
# one-size-fits-all WR threshold.  This channel exempts them.
#
# **Structural Concession Rationale (投委会 2026-06-29):**
# Trend-following strategies (like V4) suffer frequent small losses during
# chop/consolidation — depressing both WR and Sharpe.  But they compensate
# with large wins when a trend materializes.  A backtest Sharpe of 1.08
# decaying to live Sharpe of 0.545 is NOT evidence of strategy failure —
# it is the natural "live friction" (slippage, spread, non-ideal fills)
# compressing a high-RR strategy's risk-adjusted metric toward mediocrity.
#
# SHARPE_RR_ADJUSTED_MIN = 0.4 is NOT a tolerance for bad strategies.
# It is a structural concession that acknowledges:
#   (a) High R:R trend-followers inherently have lower Sharpe than mean-
#       reverting strategies of equivalent profitability.
#   (b) Live Sharpe is always lower than backtest Sharpe (overfitting +
#       execution friction).
#   (c) Profit factor (PF ≥ 1.1) + positive Sharpe (> 0) + substantial
#       trade count (≥ 50) = "proven profitable, not lucky."
#
# V4 live (2026-06-28): WR=35.5%, PF=1.147, SR=0.545, 298 trades, +42.4R
# V4 backtest:           WR=39.1%, PF=1.36,  SR=1.08,  implied RR=2.12:1
PF_RR_ADJUSTED_MIN = (
    1.1  # profit factor threshold for RR-adjusted live status (↓1.3→1.1: DQAF-063 V4 relief)
)
SHARPE_RR_ADJUSTED_MIN = 0.4  # Sharpe threshold for RR-adjusted live status (↓0.8→0.4: FIX-20260629-173 live-friction concession)


from core.training.utils import utc_now_iso as _utc_now_iso  # noqa: F401


def _resolve_brains_dir(base_dir: str) -> str:
    """Map data directory to the authoritative brains config directory.

    DQAF-063: Ghost registration guard needs the config SSOT list
    to block PnL-ledger-only brain IDs that have no config on disk.
    """
    if base_dir in ("data_btc", "data_btc/"):
        return "configs/brains_btc"
    return "configs/brains"


def _compute_pnl_based_status(
    metrics: BrainPnLMetrics,
    quality_engine: Any = None,
    current_status: str = "candidate",
) -> tuple[str, str]:
    """Determine target governance status and health signal from PnL metrics.

    When *quality_engine* (BrainQualityEngine) is provided, delegates to the
    single source of truth.  Otherwise uses the legacy threshold chain (kept
    for backward compat).

    Returns (status, health_signal).
    """
    if quality_engine is not None:
        verdict = quality_engine.assess(
            metrics.brain_id,
            metrics,
            governance_status=getattr(metrics, "governance_status", ""),
        )
        # Map quality_tier → governance status
        tier_to_status = {
            "exceptional": "live",
            "healthy": "live",
            "stable": "live",
            "warning": "probation",
            "degraded": "probation",
            "marginal": "frozen",
            "critical": "retired",
            "insufficient_data": "insufficient_data",
        }
        return tier_to_status.get(verdict.quality_tier, "candidate"), verdict.quality_tier

    n = metrics.sample_count
    sharpe = metrics.sharpe_ratio
    wr = metrics.win_rate
    # DQAF-060: pf may be float('inf') when gross_loss=0 (all wins).
    # inf >= threshold checks are True — all-win brains correctly pass gates.
    pf = metrics.profit_factor

    # Insufficient data → observe only, don't change governance status
    if n < MIN_TRADES_FOR_LIVE:
        return "insufficient_data", "insufficient_data"

    # Auto-freeze: shadow/candidate brains with catastrophic negative Sharpe
    if (
        current_status in ("shadow", "candidate")
        and n >= MIN_TRADES_FOR_FREEZE
        and sharpe < SHARPE_FREEZE_THRESHOLD
    ):
        return "frozen", "critical"

    # Auto-demote: live/probation brains with sustained negative Sharpe
    if (
        current_status in ("live", "probation")
        and n >= MIN_TRADES_FOR_DEMOTE
        and sharpe < SHARPE_DEMOTE_THRESHOLD
    ):
        return "probation", "warning"

    # Retirement: catastrophically bad (requires even worse Sharpe than freeze)
    if n >= MIN_TRADES_FOR_RETIRE and sharpe < SHARPE_RETIRE_THRESHOLD:
        return "retired", "critical"

    # ── FIX-20260627-152 / FIX-20260629-173: RR-adjusted channel ──
    # Profitable low-WR high-RR strategies (e.g. trend-following swing:
    # avg_win/avg_loss > 2:1) are blocked by the one-size-fits-all
    # WR >= 45% threshold below.  This channel exempts strategies with
    # positive risk-adjusted returns and healthy profit factor, even if
    # WR < 45%.  SHARPE_RR_ADJUSTED_MIN=0.4 is a structural concession
    # for live-friction Sharpe decay (see module-level docstring).
    # V4 backtest: WR=39.1%, PF=1.36, SR=1.08, RR=2.12:1
    # V4 live:     WR=35.5%, PF=1.147, SR=0.545, 298 trades, +42.4R
    if n >= MIN_TRADES_FOR_LIVE and pf >= PF_RR_ADJUSTED_MIN and sharpe >= SHARPE_RR_ADJUSTED_MIN:
        health = "healthy" if sharpe >= 1.0 and pf >= 1.5 else "stable"
        return "live", health

    # Probation: negative expectancy
    if sharpe < SHARPE_PROBATION_THRESHOLD or wr < WR_PROBATION_THRESHOLD:
        return "probation", "warning" if sharpe < 0 else "degraded"

    # High alpha: exceptional performance
    if sharpe >= SHARPE_HIGH_ALPHA and wr >= WR_HIGH_ALPHA and pf >= PF_HIGH_ALPHA:
        return "live", "high_alpha"

    # Live: solid, positive expectancy
    if sharpe > 0 and wr >= WR_PROBATION_THRESHOLD:
        health = "healthy" if sharpe >= 1.0 and wr >= 0.55 else "stable"
        return "live", health

    return "probation", "warning"


def _enforce_3d_override_expiry(
    governance: GovernanceService,
    pnl_store: BrainPnLStore | None = None,
) -> list[dict[str, Any]]:
    """Check 3D override expiry for all brains with manual activation overrides.

    FIX-20260620-013: 3D Expiry Contract enforcement.
    ANY dimension triggered → force rollback to candidate.

    Dimensions:
      1. override_expires_after_trades: trade count threshold (needs PnL ledger)
      2. override_expires_at: absolute time cap (no data dependency)
      3. override_max_probation_dd: cumulative PnL drawdown floor (needs PnL ledger)

    Returns list of rollback records for logging.
    """
    rollbacks: list[dict[str, Any]] = []
    all_states = governance.get_all_states()

    for brain_id, state in all_states.items():
        # Sentinel: only process brains with a 3D override contract
        if state.get("override_fix_id") is None:
            continue

        triggered: str | None = None

        # ── Dimension 2: Time expiry (checked first — no data dependency) ──
        expires_at = state.get("override_expires_at")
        if expires_at:
            now = _utc_now_iso()
            if now >= expires_at:
                triggered = f"3D:time_expired({now}>={expires_at})"

        # ── Dimension 3: Drawdown circuit breaker (needs PnL ledger) ──
        if not triggered and pnl_store is not None:
            max_dd = state.get("override_max_probation_dd")
            if max_dd is not None:
                metrics = pnl_store.get_metrics(brain_id)
                if metrics is not None and metrics.sample_count > 0:
                    if metrics.cumulative_pnl < max_dd:
                        triggered = (
                            f"3D:drawdown_breach(cum_pnl={metrics.cumulative_pnl:.2f}" f"<{max_dd})"
                        )

        # ── Dimension 1: Trade count expiry (needs PnL ledger) ──
        if not triggered and pnl_store is not None:
            expires_trades = state.get("override_expires_after_trades")
            if expires_trades is not None:
                metrics = pnl_store.get_metrics(brain_id)
                if metrics is not None and metrics.sample_count >= expires_trades:
                    triggered = f"3D:trades_reached({metrics.sample_count}>={expires_trades})"

        if triggered:
            result = governance.transition(brain_id, "candidate", reason=triggered)
            rollbacks.append(
                {
                    "brain_id": brain_id,
                    "trigger": triggered,
                    "result": result,
                }
            )
            print(
                f"[3D_ENFORCE] {brain_id}: {triggered} → rolled back to candidate",
                flush=True,
            )

    return rollbacks


def _dict_to_pnl_metrics(brain_id: str, d: dict[str, Any]) -> BrainPnLMetrics:
    """Convert journal metrics dict to BrainPnLMetrics dataclass.

    FIX-20260621-043: compute_journal_brain_metrics() returns plain dicts,
    but run_governance_cycle() expects BrainPnLMetrics instances with
    attribute access. This converter bridges the type gap.

    Journal fields mapped:
      sample_count, cumulative_pnl, win_rate, sharpe_ratio,
      profit_factor, max_drawdown, long_win_rate, short_win_rate,
      long_count, short_count
    """
    return BrainPnLMetrics(
        brain_id=brain_id,
        sample_count=d.get("sample_count", 0),
        cumulative_pnl=d.get("cumulative_pnl", d.get("pnl_r", 0.0)),
        win_rate=d.get("win_rate", 0.0),
        sharpe_ratio=d.get("sharpe_ratio", 0.0),
        profit_factor=d.get("profit_factor", 0.0),
        max_drawdown=d.get("max_drawdown", 0.0),
        long_win_rate=d.get("long_win_rate", 0.0),
        short_win_rate=d.get("short_win_rate", 0.0),
        long_count=d.get("long_count", 0),
        short_count=d.get("short_count", 0),
    )


def _promote_shadow_brains(
    governance: GovernanceService,
    base_dir: str,
    *,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Scan shadow/candidate brains for Rule 85 promotion eligibility.

    Rule 85 (auto_promote_shadow_to_probation) thresholds:
    - ≥50 shadow signals (non-neutral directional votes)
    - ≥5 long AND ≥5 short signals (bidirectional competence)
    - average confidence ≥0.50

    This bridges the local deployment gap: daily_ops → run_governance_cycle()
    was PnL-first only, so candidate brains accumulated shadow signals but
    were never evaluated for promotion.  The cloud scheduler_service.py path
    already calls GovernanceRuleEngine.with_default_rules() which includes
    Rule 85 — this adds the equivalent for the local daily_ops path.
    """
    from core.governance.shadow_tracker import ShadowTracker

    applied: list[dict[str, Any]] = []
    tracker = ShadowTracker(base_dir=base_dir, shadow_target=50)

    all_states = governance.get_all_states()
    shadow_candidate_ids = [
        bid for bid, bs in all_states.items() if bs.get("status") in ("shadow", "candidate")
    ]

    if not shadow_candidate_ids:
        return applied

    metrics_map = tracker.all_candidate_metrics(shadow_candidate_ids)

    for bid, m in metrics_map.items():
        # Rule 85 thresholds
        if m.shadow_signal_count < 50:
            continue
        # ── DQAF-20260630-202 / FIX-20260701-204: Macro-Regime Diversity Exemption ──
        # H4/D1 timeframes produce directionally-monopolistic signals by design.
        # A genuine H4 trend follower in a multi-week downtrend SHOULD output
        # 100% SHORT.  Requiring long≥5 AND short≥5 would structurally exclude
        # the best macro-trend brains.  This mirrors the exemption in
        # GovernanceRuleEngine._shadow_to_probation_condition() (governance_rule_engine.py).
        is_macro = False
        try:
            from core.brains.brain_registry import BrainRegistry

            registry = BrainRegistry.instance()
            brain_entry = registry.get(bid)
            if brain_entry is not None:
                cg = (brain_entry.contract_group or "").lower()
                tf = (brain_entry.raw.get("timeframe", "") or "").upper()
                is_macro = "h4" in cg or "d1" in cg or tf in ("H4", "D1")
        except (RuntimeError, ValueError, KeyError, TypeError, OSError, AttributeError):
            pass  # fail-open: fall through to legacy diversity check

        if not is_macro and (m.long_count < 5 or m.short_count < 5):
            continue
        if m.avg_confidence < 0.50:
            continue

        reason = (
            f"auto_promote_shadow_to_probation: "
            f"{m.shadow_signal_count} signals, "
            f"long/short={m.long_count}/{m.short_count}, "
            f"avg_conf={m.avg_confidence:.3f}"
            f"{', macro_exempt=True' if is_macro else ''}"
        )

        entry: dict[str, Any] = {
            "brain_id": bid,
            "from_status": all_states[bid].get("status"),
            "to_status": "probation",
            "shadow_signal_count": m.shadow_signal_count,
            "long_count": m.long_count,
            "short_count": m.short_count,
            "avg_confidence": round(m.avg_confidence, 4),
        }

        if dry_run:
            entry["result"] = {
                "action": "would_transition",
                "brain_id": bid,
                "from": entry["from_status"],
                "to": "probation",
            }
            applied.append(entry)
        else:
            result = governance.transition(bid, "probation", reason=reason)
            entry["result"] = result
            if result.get("action") in ("transitioned",):
                applied.append(entry)
                logger.info("SHADOW→PROBATION: %s — %s", bid, reason)

    return applied


def _apply_daily_ops_transition(
    governance: GovernanceService,
    brain_id: str,
    current_status: str,
    target_status: str,
    health: str,
    metrics: BrainPnLMetrics,
    base_dir: str,
    engine: Any | None = None,
) -> dict[str, Any]:
    """Route a daily_ops PnL-based transition through the rule engine sole writer.

    FIX-20260801-011 (P3): The direct ``governance.transition()`` here was the
    hidden third-rail writer — it bypassed both the rule engine (Iron Law #14
    sole executor) and the observation hold (FIX-20260801-012).  Build a
    BrainPromotionDecision and delegate to ``execute_transitions()`` so
    daily_ops demotions respect the SAME hold guard + sole-writer semantics as
    the brain_performance SSOT path (governance_evaluator.py).  The call-site
    last-live guard (DQAF-063) is preserved upstream of this helper.

    Args:
        governance: The GovernanceService owning the lifecycle state.
        brain_id / current_status / target_status / health / metrics: the
            PnL-first evaluation result for this brain.
        base_dir: Data dir (used to resolve the brains config dir for holds).
        engine: Pre-built GovernanceRuleEngine with holds attached (built once
            per cycle by the caller).  When None, a fresh engine is built
            (fail-open: holds may be stale).

    Returns:
        A result dict with an ``action`` key in
        {"transitioned", "registered", "hold_throttle", "rejected",
         "no_change"} plus the engine's change detail.
    """
    from core.brains.services.brain_promotion import BrainPromotionDecision

    decision = BrainPromotionDecision(
        brain_id=brain_id,
        current_status=current_status,
        action="daily_ops_pnl",
        target_status=target_status,
        approved=True,
        reasons=[f"pnl:{health}"],
        metrics_snapshot={
            "win_rate": metrics.win_rate,
            "profit_factor": metrics.profit_factor,
            "signal_count": metrics.sample_count,
        },
    )
    if engine is None:
        from core.deployment.governance_evaluator import (
            load_observation_holds,
            resolve_brains_dir,
        )
        from core.governance.governance_rule_engine import GovernanceRuleEngine

        engine = GovernanceRuleEngine.with_default_rules(governance)
        try:  # BLE001:FOG — hold loading is a non-critical policy guard
            engine.set_observation_holds(load_observation_holds(resolve_brains_dir(base_dir)))
        except (RuntimeError, ValueError, KeyError, TypeError, OSError, AttributeError):
            pass

    changes = engine.execute_transitions([decision])
    first = changes[0] if changes and changes[0] != "no_changes" else ""
    if "BLOCKED" in first:
        return {
            "action": "hold_throttle",
            "brain_id": brain_id,
            "reason": "observation_period_active",
            "detail": first,
        }
    if "not registered" in first:
        return {"action": "registered", "brain_id": brain_id, "detail": first}
    if "→" in first:
        return {
            "action": "transitioned",
            "brain_id": brain_id,
            "from": current_status,
            "to": target_status,
            "detail": first,
        }
    return {"action": "no_change", "brain_id": brain_id, "detail": first or "no_transition"}


def run_governance_cycle(
    tracker: BrainPerformanceTracker,
    governance: GovernanceService,
    *,
    dry_run: bool = False,
    pnl_store: BrainPnLStore | None = None,
    quality_engine: Any = None,
    base_dir: str = "data_btc",
) -> dict[str, Any]:
    """Read PnL metrics (primary) or tracker summaries (fallback) and apply governance.

    Args:
        tracker: BrainPerformanceTracker instance (fallback).
        governance: GovernanceService with registered brains.
        dry_run: If True, assess but don't apply transitions.
        pnl_store: Optional BrainPnLStore with per-brain PnL metrics (preferred).
        quality_engine: Optional BrainQualityEngine — single source of truth for
                        quality assessment. When provided, overrides legacy
                        _compute_pnl_based_status().
        base_dir: Data directory for live trade journal (default: data_btc).

    Returns:
        Report dict with actions applied and flagged.
    """
    applied: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []

    # ── Safety valve: max 1 retirement per cycle ──
    max_retirements = 1
    retirement_count = 0

    # ── FIX-20260620-013: 3D override expiry enforcement (Phase 1 mandatory) ──
    three_d_rollbacks = _enforce_3d_override_expiry(governance, pnl_store=pnl_store)
    if three_d_rollbacks:
        for rb in three_d_rollbacks:
            applied.append(
                {
                    "brain_id": rb["brain_id"],
                    "action": "3d_override_rollback",
                    "trigger": rb["trigger"],
                    "result": rb["result"],
                }
            )

    # ── PnL-first path ──
    if pnl_store is not None:
        all_metrics = pnl_store.get_all_metrics()
        # FIX-20260621-032: Augment PnL store metrics with live journal data.
        # Shadow-only brains (no live trades) keep PnL store metrics.
        # Live-trading brains get journal-based pnl_r injected.
        #
        # FIX-20260621-043: compute_journal_brain_metrics() returns plain dicts,
        # but downstream code (set_performance_metrics, _compute_pnl_based_status)
        # expects BrainPnLMetrics dataclass instances with attribute access.
        # Convert dicts → BrainPnLMetrics to prevent AttributeError.
        #
        # DQAF-060: Track which brains received journal-augmented metrics
        # so _data_source reflects honest lineage (live_journal vs pnl_store).
        _journal_augmented_bids: set[str] = set()
        try:
            from core.feedback.live_journal_metrics import compute_journal_brain_metrics

            _journal_metrics = compute_journal_brain_metrics(base_dir)
            for _bid, _jm in _journal_metrics.items():
                if _jm.get("sample_count", 0) > 0:
                    # Convert journal dict → BrainPnLMetrics for type safety
                    all_metrics[_bid] = _dict_to_pnl_metrics(_bid, _jm)
                    _journal_augmented_bids.add(_bid)  # DQAF-060: track journal lineage
                elif _bid not in all_metrics:
                    # Brain in journal but with 0 trades — register as
                    # BrainPnLMetrics with zeroed metrics (no backtest leak)
                    all_metrics[_bid] = _dict_to_pnl_metrics(_bid, _jm)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass

        # FIX-20260621-043: Post-augmentation type assertion.
        # Verify no raw dicts leaked into all_metrics after journal augmentation.
        # A dict in all_metrics will cause AttributeError downstream
        # (metrics.win_rate on dict → crash → silent governance failure).
        _dict_leaks = [k for k, v in all_metrics.items() if isinstance(v, dict)]
        if _dict_leaks:
            import logging as _inj_log

            _inj_log.getLogger(__name__).error(
                "FIX-043 TYPE_LEAK: dict metrics found in all_metrics after "
                "journal augmentation: %s — converting to BrainPnLMetrics",
                _dict_leaks,
            )
            for _bid in _dict_leaks:
                _jm = all_metrics[_bid]
                if isinstance(_jm, dict):
                    all_metrics[_bid] = _dict_to_pnl_metrics(_bid, _jm)
        if all_metrics:
            # ── DQAF-063 P0: Ghost registration guard ──
            # PnL ledger retains archived brain metrics indefinitely (no GC).
            # Without this guard, every governance cycle re-registers 12+
            # archived brains as "candidate", bloating governance_state.json
            # and triggering phantom governance actions.
            # Only brain IDs that exist as config files on disk are eligible.
            _brains_dir = _resolve_brains_dir(base_dir)
            _valid_bids: set[str] = set()

            # ── FIX-20260801-011 (P3): rule engine for routing daily_ops
            #    transitions through the sole writer (observation holds
            #    attached once per cycle).  Demotions during an active hold
            #    are refused by the Executor — same semantics as the SSOT
            #    evaluator path.  Fail-open: engine=None → direct route.
            try:
                from core.deployment.governance_evaluator import load_observation_holds
                from core.governance.governance_rule_engine import GovernanceRuleEngine

                _daily_ops_engine = GovernanceRuleEngine.with_default_rules(governance)
                _daily_ops_engine.set_observation_holds(load_observation_holds(_brains_dir))
            except (RuntimeError, ValueError, KeyError, TypeError, OSError, AttributeError):
                _daily_ops_engine = None  # BLE001:FOG — hold guard unavailable
            for _cfg_path in Path(_brains_dir).glob("*.json"):
                try:
                    _cfg = json.loads(_cfg_path.read_text(encoding="utf-8"))
                    _bid = _cfg.get("brain_id", _cfg_path.stem)
                    if _bid:
                        _valid_bids.add(_bid)
                except (json.JSONDecodeError, OSError, KeyError):
                    pass

            for brain_id, metrics in sorted(all_metrics.items()):
                # Skip already-retired brains — no further governance actions apply
                current_state = governance.get_brain_state(brain_id)
                # ── DQAF-061: Auto-register brains from PnLStore/metrics sources
                # that haven't been registered in governance yet.
                # Without this, set_performance_metrics() is a silent no-op
                # for 31/49 XAU brains — governance never sees their data.
                # Safety valve: always register as "candidate" regardless of
                # PnL performance — governance lifecycle transitions are gated
                # by _compute_pnl_based_status() below.
                #
                # DQAF-063: Ghost filter — only register if brain exists in
                # config SSOT. Archived brains with PnL history but no config
                # are silently skipped.
                if current_state is None:
                    if brain_id not in _valid_bids:
                        logger.warning(
                            "GHOST REGISTRATION BLOCKED: %s has PnL data but no config on disk — skipping",
                            brain_id,
                        )
                        continue
                    governance.register_brain(brain_id, "candidate")
                    current_state = governance.get_brain_state(brain_id)
                current_status = current_state["status"] if current_state else "candidate"
                if current_status == "retired":
                    continue

                # ── FIX-20260611-020: Record contamination confirmed fixed ──
                # FIX-20260613-080 resolved the signal cloning bug that caused
                # shared performance records.  PnP ledger metrics are now live.
                # FIX-20260614-B0: Manual mode removed — metrics injection
                # re-enabled.  Auto-transition safety valves remain:
                #   1. max 1 retirement per cycle
                #   2. insufficient_data (< 20 trades) skips
                #   3. dry_run=True prevents actual transitions
                _GOVERNANCE_MANUAL_MODE = False
                if _GOVERNANCE_MANUAL_MODE:
                    print(
                        f"[GOV_MANUAL] Training: would inject brain={brain_id} "
                        f"wr={metrics.win_rate:.3f} pf={metrics.profit_factor:.2f} "
                        f"trades={metrics.sample_count} pnl={metrics.cumulative_pnl:.2f} "
                        f"— SKIPPED (manual mode)",
                        flush=True,
                    )
                    continue

                # P0.1: Inject performance_metrics into governance state
                # DQAF-060: Honest lineage — tag data source based on actual origin.
                # Journal-augmented = "live_journal" (real MT5 execution data).
                # PnL store only = "pnl_store" (event stream replay or JSON ledger).
                _data_source = (
                    "live_journal" if brain_id in _journal_augmented_bids else "pnl_store"
                )
                # DQAF-061: If metrics have 0 trades from both sources,
                # mark as "no_data" to prevent downstream false confidence.
                if metrics.sample_count == 0:
                    _data_source = "no_data"
                governance.set_performance_metrics(
                    brain_id,
                    {
                        "win_rate": metrics.win_rate,
                        "profit_factor": (
                            metrics.profit_factor
                            if not math.isinf(metrics.profit_factor)
                            else None  # DQAF-060: None = "no losses" (JSON-safe)
                        ),
                        "sharpe_ratio": metrics.sharpe_ratio,
                        "total_trades": metrics.sample_count,
                        "pnl_r": round(metrics.cumulative_pnl, 2),
                        "_data_source": _data_source,
                    },
                )

                target_status, health = _compute_pnl_based_status(
                    metrics, quality_engine=quality_engine, current_status=current_status
                )

                entry = {
                    "brain_id": brain_id,
                    "target_status": target_status,
                    "health_signal": health,
                    "sharpe": round(metrics.sharpe_ratio, 3),
                    "win_rate": round(metrics.win_rate, 4),
                    "profit_factor": round(metrics.profit_factor, 3),
                    "cumulative_pnl": round(metrics.cumulative_pnl, 4),
                    "max_drawdown": round(metrics.max_drawdown, 4),
                    "sample_count": metrics.sample_count,
                }

                if target_status == "insufficient_data":
                    entry["result"] = {
                        "action": "skip",
                        "brain_id": brain_id,
                        "reason": "insufficient_data",
                    }
                    flagged.append(entry)
                    continue

                # Safety valve: throttle retirements to 1 per cycle
                if target_status == "retired" and retirement_count >= max_retirements:
                    entry["result"] = {
                        "action": "throttled",
                        "brain_id": brain_id,
                        "reason": f"retirement_limit_reached ({max_retirements}/cycle)",
                    }
                    flagged.append(entry)
                    continue

                if current_status == target_status:
                    entry["result"] = {
                        "action": "no_change",
                        "brain_id": brain_id,
                        "status": current_status,
                    }
                    continue

                if dry_run:
                    entry["result"] = {
                        "action": "would_transition",
                        "brain_id": brain_id,
                        "from": current_status,
                        "to": target_status,
                    }
                    flagged.append(entry)
                else:
                    # ── DQAF-063 P0: Last-live guard (contract patch) ──
                    # FIX-20260628-162 added this guard to governance_rule_engine.py
                    # but the actual demotion path (governance_scheduler.py →
                    # GovernanceService.transition()) bypasses the rule engine
                    # entirely.  This is the binding patch — intercepts the
                    # demotion AT THE CALL SITE before the transition fires.
                    # Demoting the sole live brain triggers DQAF-059 (0 live →
                    # fail-closed p_win=0.40 → all trading blocked).
                    if current_status == "live" and target_status in (
                        "probation",
                        "frozen",
                        "retired",
                    ):
                        _live_brains = [
                            _bid
                            for _bid, _bs in governance.get_all_states().items()
                            if _bs.get("status") == "live"
                        ]
                        if len(_live_brains) <= 1:
                            logger.warning(
                                "LAST-LIVE GUARD TRIGGERED: Refusing to demote %s "
                                "(last live brain) from %s to %s",
                                brain_id,
                                current_status,
                                target_status,
                            )
                            entry["result"] = {
                                "action": "rejected",
                                "brain_id": brain_id,
                                "reason": "last_live_guard",
                                "detail": (
                                    f"Refusing to demote sole live brain "
                                    f"from {current_status} to {target_status}"
                                ),
                            }
                            flagged.append(entry)
                            continue

                    # ── FIX-20260801-011 (P3): route through the sole writer ──
                    # Direct GovernanceService.transition() was the hidden
                    # third-rail writer (bypassed rule engine + observation
                    # hold).  Delegate to execute_transitions() so daily_ops
                    # respects the same hold guard as the SSOT evaluator.
                    # Holds are attached once per cycle via _daily_ops_engine.
                    result = _apply_daily_ops_transition(
                        governance,
                        brain_id,
                        current_status,
                        target_status,
                        health,
                        metrics,
                        base_dir,
                        engine=_daily_ops_engine,
                    )
                    entry["result"] = result
                    if result.get("action") in ("transitioned", "registered"):
                        applied.append(entry)
                        if target_status == "retired":
                            retirement_count += 1
                    else:
                        flagged.append(entry)

            # ── ShadowTracker integration (local path gap bridge) ──
            # Scan shadow/candidate brains that have no PnL data for
            # Rule 85 promotion eligibility.  This closes the gap
            # between the local daily_ops PnL-first path and the cloud
            # scheduler_service rule-engine path (which calls
            # GovernanceRuleEngine.with_default_rules() → Rule 85).
            _shadow_results = _promote_shadow_brains(governance, base_dir, dry_run=dry_run)
            if _shadow_results:
                applied.extend(_shadow_results)

            return {
                "schema_version": SCHEMA_VERSION,
                "generated_at": _utc_now_iso(),
                "data_source": "BrainPnLStore",
                "brains_assessed": len(all_metrics),
                "actions_applied": applied,
                "actions_flagged": flagged,
            }

    # ── Fallback: tracker-based path (legacy) ──
    summaries = tracker.get_all_summaries()
    if not summaries:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_now_iso(),
            "data_source": "BrainPerformanceTracker",
            "brains_assessed": 0,
            "actions_applied": [],
            "actions_flagged": [],
        }

    for summary in summaries:
        brain_id = summary["brain_id"]
        recommendation = summary.get("recommendation", "observe")
        health = summary.get("health_signal", "unknown")
        composite = summary.get("composite_mean", 0.0)

        if recommendation in ("maintain", "observe"):
            continue

        entry = {
            "brain_id": brain_id,
            "recommendation": recommendation,
            "health_signal": health,
            "composite_mean": composite,
            "sample_count": summary.get("sample_count", 0),
        }

        if recommendation in AUTO_APPLY:
            if not dry_run:
                result = governance.apply_recommendation(
                    brain_id,
                    recommendation,
                    reason=f"auto:{health}",
                )
                entry["result"] = result
            else:
                entry["result"] = {"action": "would_apply", "brain_id": brain_id}
            applied.append(entry)
        elif recommendation in REQUIRE_CONFIRMATION:
            entry["note"] = "requires_manual_confirmation"
            flagged.append(entry)

    # ── ShadowTracker integration (fallback path) ──
    _shadow_results = _promote_shadow_brains(governance, base_dir, dry_run=dry_run)
    if _shadow_results:
        applied.extend(_shadow_results)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "data_source": "BrainPerformanceTracker",
        "brains_assessed": len(summaries),
        "actions_applied": applied,
        "actions_flagged": flagged,
    }


# ── CLI ──


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="governance_scheduler")
    p.add_argument(
        "--base-dir",
        default="data",
        help="Base data directory for persistence (default: data)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Assess recommendations without applying transitions",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write governance action report JSON to file",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    base = Path(args.base_dir)

    # Load BrainPnLStore (primary data source)
    pnl_path = base / "brain_pnl_ledger.json"
    pnl_store: BrainPnLStore | None = None
    if pnl_path.exists():
        pnl_store = BrainPnLStore.load(pnl_path)
        print(
            f"[governance] loaded PnL ledger: {pnl_store.total_settled} settled across {len(pnl_store.brain_ids)} brains"
        )
    else:
        print(f"[governance] WARNING: no PnL ledger at {pnl_path}, falling back to tracker")

    # Load or create GovernanceService
    gov_path = base / "governance_state.json"
    if gov_path.exists():
        governance = GovernanceService.load(gov_path)
    else:
        governance = GovernanceService()

    tracker = BrainPerformanceTracker(window_size=100)

    report = run_governance_cycle(tracker, governance, dry_run=args.dry_run, pnl_store=pnl_store)

    # Persist governance state if actions were actually applied (not dry-run)
    if not args.dry_run and (report["actions_applied"] or report["actions_flagged"]):
        governance.save(gov_path)
        print(f"[governance] state saved to {gov_path}")

    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    # Non-zero exit if any auto-actions were applied (signals ops attention)
    if report["actions_applied"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
