"""Governance auto-scheduler: periodically apply brain lifecycle actions.

Reads BrainPerformanceTracker summaries and auto-applies governance
recommendations. Risk-reducing actions (freeze, demote, limit_exposure)
are applied automatically. Promotions are flagged for manual review.

Usage:
  # One-shot check + apply
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
from core.governance.governance_service import GovernanceService

SCHEMA_VERSION = "governance_scheduler.v1"

# Recommendations that are auto-applied (risk-reducing)
AUTO_APPLY = {"freeze", "demote_to_probation", "limit_exposure"}

# Recommendations that require manual confirmation (risk-increasing)
REQUIRE_CONFIRMATION = {"eligible_for_promotion"}


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def run_governance_cycle(
    tracker: BrainPerformanceTracker,
    governance: GovernanceService,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Read tracker summaries and apply governance recommendations.

    Args:
        tracker: Populated BrainPerformanceTracker instance.
        governance: GovernanceService instance with registered brains.
        dry_run: If True, assess but don't apply transitions.

    Returns:
        Report dict with actions applied and flagged.
    """
    summaries = tracker.get_all_summaries()
    if not summaries:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_now_iso(),
            "brains_assessed": 0,
            "actions_applied": [],
            "actions_flagged": [],
        }

    applied: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []

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

    tracker = BrainPerformanceTracker(window_size=100)
    governance = GovernanceService()

    report = run_governance_cycle(tracker, governance, dry_run=args.dry_run)

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
