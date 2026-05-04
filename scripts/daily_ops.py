"""Daily operations orchestrator: run all governance and monitoring in sequence.

Ties together shadow ensemble, governance scheduler, champion/challenger,
retraining trigger, and daily recap into a single daily pipeline.

Usage:
  # Full pipeline (all steps)
  python scripts/daily_ops.py

  # Dry-run: assess everything without applying transitions
  python scripts/daily_ops.py --dry-run

  # Skip specific steps
  python scripts/daily_ops.py --skip-shadow --skip-retraining

  # Write combined report
  python scripts/daily_ops.py --output data/reports/daily_ops.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "daily_ops.v1"

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent

DEFAULT_TRACKER_PATH = "data/brain_performance.json"
DEFAULT_GOVERNANCE_PATH = "data/governance_state.json"

# Default brain registrations when creating a fresh governance service
DEFAULT_BRAIN_REGISTRATIONS = {
    "V9_Institutional_01": "candidate",
    "XGBoost_V4.5_Microstructure": "candidate",
    "OU_Params_V6_Sniper": "candidate",
}


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _load_or_create_tracker(base_dir: str) -> Any:
    """Load persisted tracker state, or create a fresh one."""
    tracker_path = Path(base_dir) / "brain_performance.json"
    try:
        from core.feedback.brain_performance_tracker import BrainPerformanceTracker

        if tracker_path.exists():
            return BrainPerformanceTracker.load(tracker_path)
        return BrainPerformanceTracker(window_size=100)
    except Exception:
        from core.feedback.brain_performance_tracker import BrainPerformanceTracker

        return BrainPerformanceTracker(window_size=100)


def _load_or_create_governance(base_dir: str) -> Any:
    """Load persisted governance state, or create a fresh one with defaults."""
    gov_path = Path(base_dir) / "governance_state.json"
    try:
        from core.governance.governance_service import GovernanceService

        if gov_path.exists():
            return GovernanceService.load(gov_path)
        gov = GovernanceService()
        for brain_id, status in DEFAULT_BRAIN_REGISTRATIONS.items():
            gov.register_brain(brain_id, status)
        return gov
    except Exception:
        from core.governance.governance_service import GovernanceService

        gov = GovernanceService()
        for brain_id, status in DEFAULT_BRAIN_REGISTRATIONS.items():
            gov.register_brain(brain_id, status)
        return gov


def _step_shadow_ensemble(base_dir: str) -> dict[str, Any]:
    """Run shadow ensemble and return summary."""
    try:
        from scripts.live_shadow_ensemble import build_report

        report = build_report(
            brains_dir=PROJECT_ROOT / "configs" / "brains",
            feature_store_dir=Path(base_dir) / "feature_store",
            parallel=True,
            symbol="XAUUSD",
        )
        return {
            "step": "shadow_ensemble",
            "status": "ok" if "error" not in report else "error",
            "brains": report.get("total_brains", 0),
            "consensus": report.get("comparison", {}).get("consensus", "unknown"),
            "agreement": report.get("comparison", {}).get("agreement_score", 0.0),
        }
    except Exception as exc:
        return {"step": "shadow_ensemble", "status": "error", "error": str(exc)[:500]}


def _step_feedback_loop(
    base_dir: str, *, dry_run: bool = False, tracker: Any = None
) -> dict[str, Any]:
    """Run feedback loop to update tracker with real trade outcomes from journal."""
    try:
        from scripts.feedback_loop import ingest_journal_to_tracker

        if tracker is None:
            tracker = _load_or_create_tracker(base_dir)
        report = ingest_journal_to_tracker(tracker, base_dir=base_dir, dry_run=dry_run)
        return {
            "step": "feedback_loop",
            "status": "ok",
            "mode": report.get("mode", "multi_brain"),
            "journal_entries": report.get("journal_entries", 0),
            "accepted_trades": report.get("accepted_trades", 0),
            "updates_applied": report.get("updates_applied", 0),
            "brains_updated": report.get("brain_ids_updated", []),
        }
    except Exception as exc:
        return {"step": "feedback_loop", "status": "error", "error": str(exc)[:500]}


def _step_governance(
    base_dir: str, *, dry_run: bool = False, tracker: Any = None, governance: Any = None
) -> dict[str, Any]:
    """Run governance cycle and return summary."""
    try:
        from scripts.training.governance_scheduler import run_governance_cycle

        if tracker is None:
            tracker = _load_or_create_tracker(base_dir)
        if governance is None:
            governance = _load_or_create_governance(base_dir)
        report = run_governance_cycle(tracker, governance, dry_run=dry_run)
        return {
            "step": "governance",
            "status": "ok",
            "brains_assessed": report.get("brains_assessed", 0),
            "actions_applied": len(report.get("actions_applied", [])),
            "actions_flagged": len(report.get("actions_flagged", [])),
            "details": report.get("actions_applied", []),
            "flagged": report.get("actions_flagged", []),
        }
    except Exception as exc:
        return {"step": "governance", "status": "error", "error": str(exc)[:500]}


def _step_champion_challenger(
    base_dir: str, *, dry_run: bool = False, tracker: Any = None, governance: Any = None
) -> dict[str, Any]:
    """Run champion/challenger promotion cycle and return summary."""
    try:
        from scripts.training.champion_challenger import run_promotion_cycle

        if tracker is None:
            tracker = _load_or_create_tracker(base_dir)
        if governance is None:
            governance = _load_or_create_governance(base_dir)
        report = run_promotion_cycle(tracker, governance, dry_run=dry_run)
        return {
            "step": "champion_challenger",
            "status": "ok",
            "brains_assessed": report.get("brains_assessed", 0),
            "comparisons": len(report.get("comparisons", [])),
            "promotions": len(report.get("promotions", [])),
            "eligible": sum(1 for c in report.get("comparisons", []) if c.get("eligible")),
            "details": report.get("promotions", []),
        }
    except Exception as exc:
        return {"step": "champion_challenger", "status": "error", "error": str(exc)[:500]}


def _step_retraining_check(base_dir: str) -> dict[str, Any]:
    """Run retraining trigger degradation check and return summary."""
    try:
        from scripts.training.brain_leaderboard import build_report as build_lb
        from scripts.training.retraining_trigger import detect_degradation

        # Build leaderboard from current decisions and labels
        decisions_dir = Path(base_dir) / "decisions"
        labels_path = Path(base_dir) / "reports" / "live_labels.jsonl"
        leaderboard = build_lb(
            decisions_dir, labels_path=labels_path if labels_path.exists() else None
        )
        result = detect_degradation(leaderboard)
        return {
            "step": "retraining_check",
            "status": "ok" if "error" not in result else "error",
            "degraded_brains": result.get("degraded_brains", 0),
            "healthy_brains": result.get("healthy_brains", 0),
            "details": result.get("assessments", []),
        }
    except Exception as exc:
        return {"step": "retraining_check", "status": "error", "error": str(exc)[:500]}


def _step_daily_recap(base_dir: str) -> dict[str, Any]:
    """Run daily recap and return summary."""
    try:
        from scripts.live_daily_recap import build_report as build_recap

        report = build_recap(base_dir=Path(base_dir), symbol="XAUUSD")
        run_state = report.get("run_state", "unknown")
        return {
            "step": "daily_recap",
            "status": "ok",
            "run_state": run_state,
            "date_key": report.get("date_key_utc", ""),
            "sections": list(report.keys()),
        }
    except Exception as exc:
        return {"step": "daily_recap", "status": "error", "error": str(exc)[:500]}


def run_daily_ops(
    base_dir: str = "data",
    *,
    skip_shadow: bool = False,
    skip_feedback: bool = False,
    skip_governance: bool = False,
    skip_champion: bool = False,
    skip_retraining: bool = False,
    skip_recap: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the full daily operations pipeline.

    Args:
        base_dir: Base data directory.
        skip_shadow: Skip shadow ensemble step.
        skip_feedback: Skip feedback loop step.
        skip_governance: Skip governance cycle.
        skip_champion: Skip champion/challenger promotion.
        skip_retraining: Skip retraining degradation check.
        skip_recap: Skip daily recap.
        dry_run: Assess but don't apply transitions.

    Returns:
        Combined report dict with per-step results.
    """
    steps: list[dict[str, Any]] = []

    # Shared tracker + governance: load persisted state so governance and champion
    # see data accumulated by live_intent_loop, and brain registrations survive restarts.
    shared_tracker: Any = None
    shared_governance: Any = None
    if not skip_feedback or not skip_governance or not skip_champion:
        shared_tracker = _load_or_create_tracker(base_dir)
        shared_governance = _load_or_create_governance(base_dir)
        brain_count = len(shared_tracker.get_all_summaries())
        gov_brain_count = len(shared_governance.get_all_states())
        if brain_count > 0 or gov_brain_count > 0:
            steps.append(
                {
                    "step": "state_loaded",
                    "status": "ok",
                    "brains_tracked": brain_count,
                    "brains_registered": gov_brain_count,
                }
            )

    if not skip_shadow:
        steps.append(_step_shadow_ensemble(base_dir))

    # Feedback loop: resolve pending dispatch outcomes → real P&L scores
    # Runs before governance/champion so they see the latest data
    if not skip_feedback:
        steps.append(_step_feedback_loop(base_dir, dry_run=dry_run, tracker=shared_tracker))

    if not skip_governance:
        steps.append(
            _step_governance(
                base_dir, dry_run=dry_run, tracker=shared_tracker, governance=shared_governance
            )
        )

    if not skip_champion:
        steps.append(
            _step_champion_challenger(
                base_dir, dry_run=dry_run, tracker=shared_tracker, governance=shared_governance
            )
        )

    # Persist governance state after modifications
    if shared_governance is not None and not dry_run:
        try:
            gov_path = Path(base_dir) / "governance_state.json"
            shared_governance.save(gov_path)
        except Exception:
            pass

    if not skip_retraining:
        steps.append(_step_retraining_check(base_dir))

    if not skip_recap:
        steps.append(_step_daily_recap(base_dir))

    errors = [s for s in steps if s.get("status") == "error"]
    actions = sum(s.get("actions_applied", 0) + s.get("promotions", 0) for s in steps)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "base_dir": base_dir,
        "dry_run": dry_run,
        "total_steps": len(steps),
        "errors": len(errors),
        "actions_total": actions,
        "steps": steps,
    }


# ── CLI ──


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="daily_ops")
    p.add_argument("--base-dir", default="data", help="Base data directory")
    p.add_argument("--dry-run", action="store_true", help="Assess without applying transitions")
    p.add_argument("--skip-shadow", action="store_true", help="Skip shadow ensemble")
    p.add_argument("--skip-feedback", action="store_true", help="Skip feedback loop")
    p.add_argument("--skip-governance", action="store_true", help="Skip governance cycle")
    p.add_argument("--skip-champion", action="store_true", help="Skip champion/challenger")
    p.add_argument("--skip-retraining", action="store_true", help="Skip retraining check")
    p.add_argument("--skip-recap", action="store_true", help="Skip daily recap")
    p.add_argument("--output", type=Path, default=None, help="Write combined report JSON to file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Ensure project root on path
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    report = run_daily_ops(
        base_dir=args.base_dir,
        skip_shadow=args.skip_shadow,
        skip_feedback=args.skip_feedback,
        skip_governance=args.skip_governance,
        skip_champion=args.skip_champion,
        skip_retraining=args.skip_retraining,
        skip_recap=args.skip_recap,
        dry_run=args.dry_run,
    )

    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    # Non-zero if any errors or actions applied (signals ops attention)
    if report["errors"] > 0:
        return 2
    if report["actions_total"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
