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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.feedback.brain_performance_tracker import BrainPerformanceTracker
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


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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


def run_governance_cycle(
    tracker: BrainPerformanceTracker,
    governance: GovernanceService,
    *,
    dry_run: bool = False,
    pnl_store: BrainPnLStore | None = None,
    quality_engine: Any = None,
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

    Returns:
        Report dict with actions applied and flagged.
    """
    applied: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []

    # ── Safety valve: max 1 retirement per cycle ──
    max_retirements = 1
    retirement_count = 0

    # ── PnL-first path ──
    if pnl_store is not None:
        all_metrics = pnl_store.get_all_metrics()
        if all_metrics:
            for brain_id, metrics in sorted(all_metrics.items()):
                # Skip already-retired brains — no further governance actions apply
                current_state = governance.get_brain_state(brain_id)
                current_status = current_state["status"] if current_state else "candidate"
                if current_status == "retired":
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
