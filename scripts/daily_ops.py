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
    "Online_SGD_V1": "candidate",
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
            symbol="XAUUSDc",
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


def _step_online_feedback(base_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Feed closed trade outcomes to OnlineLearnerAdapter via partial_fit."""
    try:
        from core.brains.adapters.online_learner_adapter import OnlineLearnerAdapter
        from core.feedback.online_feedback_hook import OnlineFeedbackHook

        base = Path(base_dir)
        config_path = PROJECT_ROOT / "configs" / "brains" / "online_learner_v1.json"
        if not config_path.exists():
            return {"step": "online_feedback", "status": "skipped", "reason": "no_config"}

        brain_entry = json.loads(config_path.read_text(encoding="utf-8"))
        artifact = brain_entry.get("artifact_path", "")
        if artifact and not Path(artifact).is_absolute():
            brain_entry["artifact_path"] = str((PROJECT_ROOT / artifact).resolve())

        adapter = OnlineLearnerAdapter(brain_entry)
        adapter.load()
        updates_before = adapter._total_updates

        if dry_run:
            journal_path = base / "live_trade_journal.jsonl"
            paper_path = base / "paper_trade_journal.jsonl"
            closed_count = 0
            for jp in (journal_path, paper_path):
                if jp.exists():
                    for line in jp.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        try:
                            if str(json.loads(line).get("ack_status", "")) == "closed":
                                closed_count += 1
                        except json.JSONDecodeError:
                            continue
            return {
                "step": "online_feedback",
                "status": "ok",
                "dry_run": True,
                "updates_before": updates_before,
                "closed_trades_in_journals": closed_count,
            }

        total_updated = 0
        total_skipped = 0
        total_errors = 0

        # 1. Process live trade journal
        live_journal = base / "live_trade_journal.jsonl"
        if live_journal.exists():
            hook = OnlineFeedbackHook(
                adapter=adapter,
                journal_path=str(live_journal),
                feature_store_dir=str(base / "feature_store" / "records"),
            )
            result = hook.process_new_trades(save_weights=False)
            total_updated += result.get("updated", 0)
            total_skipped += result.get("skipped", 0)
            total_errors += result.get("errors", 0)

        # 2. Process paper trade journal
        paper_journal = base / "paper_trade_journal.jsonl"
        if paper_journal.exists():
            paper_hook = OnlineFeedbackHook(
                adapter=adapter,
                journal_path=str(paper_journal),
                feature_store_dir=str(base / "feature_store" / "records"),
                last_processed_path=str(base / "paper_feedback_state.json"),
            )
            result = paper_hook.process_new_trades(save_weights=False)
            total_updated += result.get("updated", 0)
            total_skipped += result.get("skipped", 0)
            total_errors += result.get("errors", 0)

        if total_updated > 0:
            adapter.save_weights()

        updates_after = adapter._total_updates
        return {
            "step": "online_feedback",
            "status": "ok",
            "updates_before": updates_before,
            "updates_after": updates_after,
            "updated": total_updated,
            "skipped": total_skipped,
            "errors": total_errors,
        }
    except Exception as exc:
        return {"step": "online_feedback", "status": "error", "error": str(exc)[:500]}


def _step_paper_trade_simulation(base_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Run paper trade simulator to generate labeled outcomes from shadow decisions."""
    try:
        from scripts.paper_trade_simulator import run_simulator

        result = run_simulator(
            since=None,
            dry_run=dry_run,
            output_path=Path(base_dir) / "paper_trade_journal.jsonl",
        )
        return {
            "step": "paper_trade_simulation",
            "status": result.get("status", "ok"),
            "trades": result.get("trades", 0),
            "total_pnl": result.get("total_pnl", 0),
            "win_rate": result.get("win_rate", 0),
            "dry_run": dry_run,
        }
    except Exception as exc:
        return {"step": "paper_trade_simulation", "status": "error", "error": str(exc)[:500]}


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


def _step_alpha_lifecycle(base_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Run alpha lifecycle evaluation: promotion gate on all registered alphas."""
    try:
        from core.alpha.lifecycle_service import AlphaLifecycleService
        from core.alpha.performance_store import AlphaPerformanceStore
        from core.alpha.promotion_gate import AlphaPromotionGate, AlphaPromotionPolicy
        from core.alpha.registry import AlphaRegistry

        registry_path = Path(base_dir) / "alpha_registry.json"
        perf_path = Path(base_dir) / "alpha_performance.json"

        if registry_path.exists():
            registry = AlphaRegistry.load(registry_path)
        else:
            registry = AlphaRegistry()

        perf_store = (
            AlphaPerformanceStore.load(perf_path) if perf_path.exists() else AlphaPerformanceStore()
        )
        lifecycle = AlphaLifecycleService(registry)
        gate = AlphaPromotionGate(perf_store, policy=AlphaPromotionPolicy())

        decisions: list[dict[str, Any]] = []
        for record in registry.list_records():
            decision = gate.evaluate(record)
            decisions.append(decision.to_dict())
            if decision.approved and decision.target_state and not dry_run:
                try:
                    lifecycle.transition(record.alpha_id, decision.target_state, decision.action)
                except ValueError:
                    pass

        if not dry_run:
            registry.save(registry_path)
            perf_store.save(perf_path)

        applied = [d for d in decisions if d.get("approved")]
        return {
            "step": "alpha_lifecycle",
            "status": "ok",
            "alphas_assessed": len(decisions),
            "actions_applied": len(applied) if not dry_run else 0,
            "actions_flagged": len(applied) if dry_run else len(applied),
            "details": applied,
        }
    except Exception as exc:
        return {"step": "alpha_lifecycle", "status": "error", "error": str(exc)[:500]}


def _step_feature_store_maintenance(
    base_dir: str, *, dry_run: bool = False, retention_days: int = 90
) -> dict[str, Any]:
    """Run feature store compaction and stats collection."""
    try:
        from scripts.feature_store_maintenance import run_full_maintenance

        store_dir = Path(base_dir) / "feature_store"
        fs_dir = str(store_dir) if store_dir.exists() else None
        report = run_full_maintenance(
            base_dir=base_dir,
            feature_store_dir=fs_dir,
            retention_days=retention_days,
            skip_update=True,  # daily ops focuses on compaction + stats
            dry_run=dry_run,
        )
        steps = report.get("steps", [])
        compaction = next(
            (s for s in steps if s.get("step") == "compaction"),
            {"records_before": 0, "records_after": 0, "duplicates_removed": 0},
        )
        stats = next(
            (s for s in steps if s.get("step") == "stats"),
            {"total_records": 0, "total_file_size_mb": 0},
        )
        return {
            "step": "feature_store_maintenance",
            "status": "ok",
            "dry_run": dry_run,
            "compaction": compaction,
            "stats": stats,
        }
    except Exception as exc:
        return {"step": "feature_store_maintenance", "status": "error", "error": str(exc)[:500]}


def _step_daily_recap(base_dir: str, *, mt5_terminal_path: str | None = None) -> dict[str, Any]:
    """Run daily recap and return summary."""
    try:
        from scripts.live_daily_recap import build_report as build_recap

        report = build_recap(
            base_dir=Path(base_dir),
            symbol="XAUUSDc",
            mt5_terminal_path=mt5_terminal_path,
        )
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
    skip_alpha: bool = False,
    skip_online_feedback: bool = False,
    skip_paper_simulation: bool = False,
    skip_fs_maintenance: bool = False,
    dry_run: bool = False,
    mt5_terminal_path: str | None = None,
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
        skip_online_feedback: Skip online learner partial_fit from closed trades.
        skip_paper_simulation: Skip paper trade simulation from shadow decisions.
        skip_fs_maintenance: Skip feature store maintenance (compaction + stats).
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

    # Paper trade simulation: generate labeled trade outcomes from shadow decisions
    if not skip_paper_simulation:
        steps.append(_step_paper_trade_simulation(base_dir, dry_run=dry_run))

    # Online feedback: feed closed trades to online SGD learner via partial_fit
    # Runs after paper_trade_simulation and feedback_loop so all data is available
    if not skip_online_feedback:
        steps.append(_step_online_feedback(base_dir, dry_run=dry_run))

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

    # Persist tracker and governance state after modifications
    if not dry_run:
        if shared_tracker is not None:
            try:
                tracker_path = Path(base_dir) / "brain_performance.json"
                shared_tracker.save(tracker_path)
            except Exception:
                pass
        if shared_governance is not None:
            try:
                gov_path = Path(base_dir) / "governance_state.json"
                shared_governance.save(gov_path)
            except Exception:
                pass

    if not skip_retraining:
        steps.append(_step_retraining_check(base_dir))

    if not skip_recap:
        steps.append(_step_daily_recap(base_dir, mt5_terminal_path=mt5_terminal_path))

    if not skip_alpha:
        steps.append(_step_alpha_lifecycle(base_dir, dry_run=dry_run))

    if not skip_fs_maintenance:
        steps.append(_step_feature_store_maintenance(base_dir, dry_run=dry_run))

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
    p.add_argument("--skip-alpha", action="store_true", help="Skip alpha lifecycle evaluation")
    p.add_argument(
        "--skip-online-feedback", action="store_true", help="Skip online learner feedback"
    )
    p.add_argument(
        "--skip-paper-simulation", action="store_true", help="Skip paper trade simulation"
    )
    p.add_argument(
        "--skip-fs-maintenance", action="store_true", help="Skip feature store maintenance"
    )
    p.add_argument("--output", type=Path, default=None, help="Write combined report JSON to file")
    p.add_argument(
        "--mt5-terminal-path", default=None, help="MT5 terminal64.exe path for P&L snapshot"
    )
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
        skip_alpha=args.skip_alpha,
        skip_online_feedback=args.skip_online_feedback,
        skip_paper_simulation=args.skip_paper_simulation,
        skip_fs_maintenance=args.skip_fs_maintenance,
        dry_run=args.dry_run,
        mt5_terminal_path=args.mt5_terminal_path,
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
