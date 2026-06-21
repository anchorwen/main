"""Governance auto-scheduler: periodically apply brain lifecycle actions.

Uses BrainPnLStore metrics (Sharpe, win rate, profit factor, max drawdown)
as the primary signal for governance decisions. Falls back to the older
BrainPerformanceTracker (composite scores) only when PnL data is unavailable.

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
from pathlib import Path
from typing import Any

from core.feedback.brain_performance_tracker import BrainPerformanceTracker
from core.runtime.fault_handler import fail_open_guard
from core.feedback.brain_pnl_ledger import BrainPnLMetrics, BrainPnLStore
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


from core.training.utils import utc_now_iso as _utc_now_iso  # noqa: F401


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
                            f"3D:drawdown_breach(cum_pnl={metrics.cumulative_pnl:.2f}"
                            f"<{max_dd})"
                        )

        # ── Dimension 1: Trade count expiry (needs PnL ledger) ──
        if not triggered and pnl_store is not None:
            expires_trades = state.get("override_expires_after_trades")
            if expires_trades is not None:
                metrics = pnl_store.get_metrics(brain_id)
                if metrics is not None and metrics.sample_count >= expires_trades:
                    triggered = (
                        f"3D:trades_reached({metrics.sample_count}>={expires_trades})"
                    )

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
        try:
            from core.feedback.live_journal_metrics import compute_journal_brain_metrics

            _journal_metrics = compute_journal_brain_metrics(base_dir)
            for _bid, _jm in _journal_metrics.items():
                if _bid in all_metrics:
                    # Replace PnL store metrics with journal-based for
                    # brains that have actual live trades
                    if _jm.get("sample_count", 0) > 0:
                        all_metrics[_bid] = _jm
                else:
                    all_metrics[_bid] = _jm
        except Exception:  # BLE001:FOG
            with fail_open_guard("governance_scheduler:journal_metrics"):
                pass
        if all_metrics:
            for brain_id, metrics in sorted(all_metrics.items()):
                # Skip already-retired brains — no further governance actions apply
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
                governance.set_performance_metrics(
                    brain_id,
                    {
                        "win_rate": metrics.win_rate,
                        "profit_factor": metrics.profit_factor,
                        "sharpe_ratio": metrics.sharpe_ratio,
                        "total_trades": metrics.sample_count,
                        "pnl_r": round(metrics.cumulative_pnl, 2),
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
                    result = governance.transition(brain_id, target_status, reason=f"pnl:{health}")
                    entry["result"] = result
                    if result.get("action") in ("transitioned", "registered"):
                        applied.append(entry)
                        if target_status == "retired":
                            retirement_count += 1
                    else:
                        flagged.append(entry)

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
