"""Brain promotion runner — evaluate and apply promotion/retirement decisions.

Reads brain performance data from live trading, evaluates each brain against
promotion/retirement thresholds, and updates governance_state.json.

This is a more detailed evaluation than governance_scheduler.py, using
win_rate, profit_factor, and consecutive_losses instead of just composite_score.

Usage:
  # Evaluate and apply (default)
  python scripts/training/brain_promotion_runner.py

  # Dry run (show decisions without applying)
  python scripts/training/brain_promotion_runner.py --dry-run

  # Custom paths
  python scripts/training/brain_promotion_runner.py \\
    --base-dir data \\
    --performance data/brain_performance.json \\
    --governance data/governance_state.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def load_performance(path: Path) -> dict[str, dict[str, Any]]:
    """Load brain performance data into {brain_id: metrics} dict."""
    if not path.exists():
        return {}

    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") == "brain_performance_tracker.v1":
        records = raw.get("records", {})
        result: dict[str, dict[str, Any]] = {}
        for brain_id, brain_records in records.items():
            if not brain_records:
                continue
            result[brain_id] = _compute_metrics_from_tracker(brain_records)
        return result
    return {}


def _compute_metrics_from_tracker(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute standard metrics from BrainPerformanceTracker records."""
    if not records:
        return {}

    outcomes = [r.get("execution_outcome", "") for r in records]
    wins = sum(1 for o in outcomes if "win" in o.lower() or "tp" in o.lower())
    losses = sum(1 for o in outcomes if "loss" in o.lower() or "sl" in o.lower())
    total_closed = wins + losses

    win_rate = wins / total_closed if total_closed > 0 else 0.0

    recent = outcomes[-20:]
    recent_wins = sum(1 for o in recent if "win" in o.lower() or "tp" in o.lower())
    recent_closed = sum(1 for o in recent if o not in ("pending", "skipped", ""))
    recent_wr = recent_wins / recent_closed if recent_closed > 0 else win_rate

    cons_losses = 0
    max_cons = 0
    for o in outcomes:
        if "loss" in o.lower() or "sl" in o.lower():
            cons_losses += 1
            max_cons = max(max_cons, cons_losses)
        elif "win" in o.lower() or "tp" in o.lower():
            cons_losses = 0

    scores = [r.get("composite_score", 0.5) for r in records]
    avg_score = sum(scores) / len(scores) if scores else 0.5
    if avg_score >= 0.99:
        profit_factor = 10.0  # cap near-singularity
    elif avg_score < 1.0:
        profit_factor = min(avg_score / (1.0 - avg_score), 10.0)
    else:
        profit_factor = min(avg_score * 2.0, 10.0)

    dispatched = sum(1 for o in outcomes if o not in ("", "skipped"))

    return {
        "signal_count": dispatched,
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4),
        "consecutive_losses": max_cons,
        "recent_win_rate": round(recent_wr, 4),
        "total_outcomes": len(outcomes),
    }


def run_evaluation(
    performance_path: Path,
    governance_path: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run full evaluation cycle and return a report dict."""
    from core.brains.services.brain_promotion import (
        BrainPromotionEvaluator,
        apply_promotion_decisions,
    )

    performance = load_performance(performance_path)
    try:
        from core.governance.governance_service import GovernanceService

        gov_svc = GovernanceService.load(str(governance_path))
        gov: dict[str, Any] = {
            "brain_states": gov_svc.get_all_states(),
            "transition_log": gov_svc.get_transition_log(),
        }
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        gov = {}
    brain_states = gov.get("brain_states", {})

    evaluator = BrainPromotionEvaluator()
    decisions = evaluator.evaluate_all(brain_states, performance)

    changes = apply_promotion_decisions(governance_path, decisions, dry_run=dry_run)

    return {
        "evaluated_at": _utc_now_iso(),
        "brains_evaluated": len(decisions),
        "brains_with_performance": len(performance),
        "decisions": [
            {
                "brain_id": d.brain_id,
                "current_status": d.current_status,
                "action": d.action,
                "target_status": d.target_status,
                "approved": d.approved,
                "reasons": d.reasons,
                "metrics": d.metrics_snapshot,
            }
            for d in decisions
        ],
        "changes_applied": changes if not dry_run else [f"[DRY RUN] {c}" for c in changes],
        "dry_run": dry_run,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="brain_promotion_runner",
        description="Brain promotion evaluation from live performance data",
    )
    p.add_argument("--base-dir", default="data", help="Base data directory")
    p.add_argument(
        "--performance",
        default=None,
        help="Path to brain_performance.json (default: <base-dir>/brain_performance.json)",
    )
    p.add_argument(
        "--governance",
        default=None,
        help="Path to governance_state.json (default: <base-dir>/governance_state.json)",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Evaluate without modifying governance state"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    base = Path(args.base_dir)
    performance_path = (
        Path(args.performance) if args.performance else base / "brain_performance.json"
    )
    governance_path = Path(args.governance) if args.governance else base / "governance_state.json"

    if not governance_path.exists():
        print(json.dumps({"error": "governance_state_not_found", "path": str(governance_path)}))
        return 2

    report = run_evaluation(performance_path, governance_path, dry_run=args.dry_run)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
