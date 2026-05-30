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

# Default brain registrations when creating a fresh governance service.
# When empty, auto-discovers all brain_registry_entry.v1 configs from disk.
DEFAULT_BRAIN_REGISTRATIONS: dict[str, str] = {}


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
    """Load persisted governance state, or create a fresh one.

    When creating a new governance service, auto-discovers brain configs from
    ``configs/brains/`` and registers each as ``candidate``.  The hardcoded
    DEFAULT_BRAIN_REGISTRATIONS dict (above) can still be used to pin specific
    initial statuses, but the default empty dict triggers full auto-discovery.
    """
    gov_path = Path(base_dir) / "governance_state.json"
    try:
        from core.governance.governance_service import GovernanceService

        if gov_path.exists():
            return GovernanceService.load(gov_path)
        gov = GovernanceService()
        if DEFAULT_BRAIN_REGISTRATIONS:
            for brain_id, status in DEFAULT_BRAIN_REGISTRATIONS.items():
                gov.register_brain(brain_id, status)
        else:
            # Auto-discover from configs/brains/
            brains_dir = PROJECT_ROOT / "configs" / "brains"
            if brains_dir.is_dir():
                import json as _json

                for cfg_path in sorted(brains_dir.glob("*.json")):
                    if "normalization" in cfg_path.name.lower():
                        continue
                    try:
                        cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    if cfg.get("schema_version") != "brain_registry_entry.v1":
                        continue
                    bid = cfg.get("brain_id", "")
                    if bid:
                        cfg_status = cfg.get("status", "candidate")
                        initial = (
                            cfg_status if cfg_status in ("candidate", "shadow") else "candidate"
                        )
                        gov.register_brain(bid, initial)
        return gov
    except Exception:
        from core.governance.governance_service import GovernanceService

        gov = GovernanceService()
        if DEFAULT_BRAIN_REGISTRATIONS:
            for brain_id, status in DEFAULT_BRAIN_REGISTRATIONS.items():
                gov.register_brain(brain_id, status)
        return gov


def _load_or_create_pnl_store(base_dir: str) -> Any:
    """Load persisted PnL ledger, or create a fresh one.

    BrainPnLStore tracks per-brain counterfactual P&L with horizon-matched
    settlement.  Unlike BrainPerformanceTracker (which uses composite_score
    from consensus-round attribution), the PnL ledger records per-brain
    signals independently — no cross-brain contamination.
    """
    ledger_path = Path(base_dir) / "brain_pnl_ledger.json"
    try:
        from core.feedback.brain_pnl_ledger import BrainPnLStore

        if ledger_path.exists():
            return BrainPnLStore.load(ledger_path)
        return BrainPnLStore()
    except Exception:
        from core.feedback.brain_pnl_ledger import BrainPnLStore

        return BrainPnLStore()


def _step_label_builder(
    base_dir: str, *, dry_run: bool = False, contract_path: Path | None = None
) -> dict[str, Any]:
    """Generate training labels from live + paper trade journals.

    Calls label_builder.build_trade_records() to produce live_labels.jsonl,
    which downstream steps (feedback_loop, retraining_check, leaderboard) depend on.
    Runs BEFORE feedback_loop so tracker sees fresh labels.
    """
    try:
        from scripts.training.label_builder import build_trade_records

        base = Path(base_dir)
        out_dir = base / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "live_labels.jsonl"

        # Load optional label contract for barrier-based classification
        contract = None
        if contract_path is not None and contract_path.exists():
            from core.contracts.training.label_contract import LabelContract

            contract = LabelContract.from_file(contract_path)

        # Process live journal
        live_records: list[dict[str, Any]] = []
        live_journal = base / "live_trade_journal.jsonl"
        if live_journal.exists():
            live_records = build_trade_records(live_journal, contract=contract)

        # Process paper journal
        paper_records: list[dict[str, Any]] = []
        paper_journal = base / "paper_trade_journal.jsonl"
        if paper_journal.exists():
            paper_records = build_trade_records(paper_journal, contract=contract)

        all_records = live_records + paper_records

        if not dry_run:
            lines = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in all_records)
            out_path.write_text(lines + "\n", encoding="utf-8")

        closed = sum(1 for r in all_records if r.get("is_closed"))
        open_trades = len(all_records) - closed
        wins = sum(1 for r in all_records if r["label"] in ("win", "tp_hit_first"))
        losses = sum(1 for r in all_records if r["label"] in ("loss", "sl_hit_first"))

        return {
            "step": "label_builder",
            "status": "ok",
            "dry_run": dry_run,
            "total_labels": len(all_records),
            "live_labels": len(live_records),
            "paper_labels": len(paper_records),
            "closed_trades": closed,
            "open_trades": open_trades,
            "wins": wins,
            "losses": losses,
            "output": str(out_path) if not dry_run else None,
        }
    except Exception as exc:
        return {"step": "label_builder", "status": "error", "error": str(exc)[:500]}


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
    """Online_MLP_V1 retired 2026-05-25 (pnl:critical). This step is permanently skipped."""
    return {"step": "online_feedback", "status": "skipped", "reason": "brain_retired"}


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
    base_dir: str,
    *,
    dry_run: bool = False,
    tracker: Any = None,
    governance: Any = None,
    pnl_store: Any = None,
) -> dict[str, Any]:
    """Run governance cycle and cross-validate against leaderboard data.

    Cross-checks tracker-based recommendations against the leaderboard's
    trade-linked win_rates to detect inconsistent governance signals.
    """
    try:
        from scripts.training.governance_scheduler import run_governance_cycle

        if tracker is None:
            tracker = _load_or_create_tracker(base_dir)
        if governance is None:
            governance = _load_or_create_governance(base_dir)
        if pnl_store is None:
            pnl_store = _load_or_create_pnl_store(base_dir)
        report = run_governance_cycle(tracker, governance, dry_run=dry_run, pnl_store=pnl_store)

        # ── Cross-validate against leaderboard ──
        cross_check = _cross_check_governance_with_leaderboard(base_dir, report)

        return {
            "step": "governance",
            "status": "ok",
            "brains_assessed": report.get("brains_assessed", 0),
            "actions_applied": len(report.get("actions_applied", [])),
            "actions_flagged": len(report.get("actions_flagged", [])),
            "details": report.get("actions_applied", []),
            "flagged": report.get("actions_flagged", []),
            "leaderboard_cross_check": cross_check,
        }
    except Exception as exc:
        return {"step": "governance", "status": "error", "error": str(exc)[:500]}


def _cross_check_governance_with_leaderboard(
    base_dir: str, gov_report: dict[str, Any]
) -> list[dict[str, Any]]:
    """Cross-validate governance actions against leaderboard win_rates.

    Returns a list of conflict warnings where tracker composite_score and
    leaderboard trade-linked win_rate tell contradictory stories.
    """
    lb_path = Path(base_dir) / "reports" / "leaderboard.json"
    if not lb_path.exists():
        return []

    try:
        leaderboard = json.loads(lb_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    lb_entries = leaderboard.get("leaderboard", [])
    if not lb_entries:
        return []

    # Index leaderboard by brain_id
    lb_index: dict[str, dict[str, Any]] = {}
    for e in lb_entries:
        lb_index[e.get("brain_id", "")] = e

    conflicts: list[dict[str, Any]] = []

    actions = gov_report.get("actions_applied", []) + gov_report.get("actions_flagged", [])
    for action in actions:
        brain_id = action.get("brain_id", "")
        lb_entry = lb_index.get(brain_id)
        if lb_entry is None:
            continue

        trade_perf = lb_entry.get("trade_performance") or {}
        lb_win_rate = trade_perf.get("win_rate")
        lb_linked = trade_perf.get("linked_trades", 0)
        tracker_composite = action.get("composite_mean", 0.0)

        # Conflict: tracker says healthy (composite > 0.5) but leaderboard
        # shows low win_rate (<0.35) with enough linked trades
        if (
            lb_win_rate is not None
            and lb_linked >= 5
            and lb_win_rate < 0.35
            and tracker_composite > 0.50
        ):
            conflicts.append(
                {
                    "brain_id": brain_id,
                    "type": "tracker_leaderboard_divergence",
                    "tracker_composite": tracker_composite,
                    "leaderboard_win_rate": lb_win_rate,
                    "leaderboard_linked_trades": lb_linked,
                    "detail": "Tracker shows healthy composite but leaderboard win_rate is low",
                }
            )

        # Conflict: tracker recommends demotion but leaderboard shows
        # strong win_rate with sufficient data
        recommendation = action.get("recommendation", "")
        if (
            recommendation in ("freeze", "demote_to_probation")
            and lb_win_rate is not None
            and lb_linked >= 5
            and lb_win_rate > 0.50
        ):
            conflicts.append(
                {
                    "brain_id": brain_id,
                    "type": "recommendation_leaderboard_divergence",
                    "recommendation": recommendation,
                    "leaderboard_win_rate": lb_win_rate,
                    "leaderboard_linked_trades": lb_linked,
                    "detail": f"Governance recommends {recommendation} but leaderboard shows strong win_rate",
                }
            )

    return conflicts


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


def _step_retraining_check(
    base_dir: str, *, dry_run: bool = False, auto_execute: bool = False
) -> dict[str, Any]:
    """Run retraining trigger degradation check and optionally auto-execute.

    Auto-execute safety gates:
      - Only when >=2 brains are degraded with critical severity
      - Only when the same brain was degraded in the previous report
        (2-day persistence gate), OR >=3 critical signals (strong first-time signal)
      - Logs all execution events to data/retraining_log.jsonl
    """
    try:
        from scripts.training.brain_leaderboard import build_report as build_lb
        from scripts.training.retraining_trigger import detect_degradation, execute_retraining

        base = Path(base_dir)
        decisions_dir = base / "decisions"
        labels_path = base / "reports" / "live_labels.jsonl"

        # Load previous leaderboard for trend comparison
        baseline = None
        prev_lb_path = base / "reports" / "leaderboard_prev.json"
        if prev_lb_path.exists():
            baseline = json.loads(prev_lb_path.read_text(encoding="utf-8"))

        leaderboard = build_lb(
            decisions_dir, labels_path=labels_path if labels_path.exists() else None
        )
        result = detect_degradation(leaderboard, baseline)

        # Persist leaderboard for next run's comparison
        reports_dir = base / "reports"
        if not dry_run:
            reports_dir.mkdir(parents=True, exist_ok=True)
            (reports_dir / "leaderboard.json").write_text(
                json.dumps(leaderboard, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            (reports_dir / "leaderboard_prev.json").write_text(
                json.dumps(leaderboard, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )

        # ── Auto-execute retraining (with safety gates) ──
        execution_result = None
        if auto_execute and not dry_run:
            critical_signals = [s for s in result.get("signals", []) if s["urgency"] == "critical"]
            if len(critical_signals) >= 2:
                prev_critical_ids = _load_prev_critical_ids(base)
                current_critical_ids = {s["brain_id"] for s in critical_signals}
                persistent = current_critical_ids & prev_critical_ids

                should_execute = len(persistent) >= 1 or len(critical_signals) >= 3
                if should_execute:
                    execution_result = execute_retraining(
                        critical_signals,
                        feature_store_dir=base / "feature_store",
                        output_dir=base / "training",
                        labels_path=labels_path if labels_path.exists() else None,
                        dry_run=False,
                    )
                    _log_retraining_event(base, critical_signals, execution_result, persistent)
            # Always save current signal for next day's persistence check
            _save_prev_signal(base, result)

        return {
            "step": "retraining_check",
            "status": "ok" if "error" not in result else "error",
            "degraded_count": result.get("degraded_count", 0),
            "healthy_brains": result.get("total_brains_assessed", 0)
            - result.get("degraded_count", 0),
            "overall_urgency": result.get("overall_urgency", "ok"),
            "details": result.get("signals", []),
            "auto_execution": execution_result,
        }
    except Exception as exc:
        return {"step": "retraining_check", "status": "error", "error": str(exc)[:500]}


def _load_prev_critical_ids(base: Path) -> set[str]:
    """Load brain_ids that were critical in yesterday's retraining signal."""
    sig_path = base / "reports" / "retraining_signal_prev.json"
    if not sig_path.exists():
        return set()
    try:
        prev = json.loads(sig_path.read_text(encoding="utf-8"))
        return {s["brain_id"] for s in prev.get("signals", []) if s["urgency"] == "critical"}
    except (json.JSONDecodeError, OSError, KeyError):
        return set()


def _save_prev_signal(base: Path, result: dict[str, Any]) -> None:
    """Save current retraining signal for next day's persistence check."""
    sig_path = base / "reports" / "retraining_signal_prev.json"
    sig_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _log_retraining_event(
    base: Path,
    critical_signals: list[dict[str, Any]],
    exec_result: dict[str, Any],
    persistent_ids: set[str],
) -> None:
    """Append a retraining execution event to the retraining log."""
    log_path = base / "retraining_log.jsonl"
    event = {
        "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
        "critical_brains": [s["brain_id"] for s in critical_signals],
        "persistent_brains": sorted(persistent_ids),
        "execution": exec_result,
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except OSError:
        print(
            json.dumps(
                {"event": "retraining_log_write_error", "path": str(log_path)},
                ensure_ascii=False,
            ),
            flush=True,
        )


def _step_param_optimization(
    base_dir: str, retraining_result: dict[str, Any], *, dry_run: bool = False
) -> dict[str, Any]:
    """Generate parameter optimization suggestions for degraded brains.

    Calls param_optimizer.suggest_parameters() with degraded brain_ids.
    Writes suggestions to data/reports/param_suggestions.json for manual review.
    """
    try:
        from core.feedback.param_optimizer import suggest_parameters

        details = retraining_result.get("details", [])
        degraded_ids = [
            s["brain_id"] for s in details if isinstance(s, dict) and s.get("urgency") == "critical"
        ]
        if not degraded_ids:
            return {
                "step": "param_optimization",
                "status": "skipped",
                "reason": "no_critical_brains",
            }

        if not dry_run:
            report = suggest_parameters(degraded_ids, base_dir=base_dir)
            return {
                "step": "param_optimization",
                "status": "ok",
                "degraded_brains": degraded_ids,
                "searchable_count": report.get("searchable_count", 0),
                "no_search_count": report.get("no_search_count", 0),
                "output": f"{base_dir}/reports/param_suggestions.json",
            }

        return {
            "step": "param_optimization",
            "status": "ok",
            "dry_run": True,
            "degraded_brains": degraded_ids,
            "would_generate": len(degraded_ids),
        }
    except Exception as exc:
        return {"step": "param_optimization", "status": "error", "error": str(exc)[:500]}


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
                except ValueError as exc:
                    print(
                        json.dumps(
                            {
                                "event": "alpha_transition_error",
                                "alpha_id": record.alpha_id,
                                "target_state": decision.target_state,
                                "error": str(exc),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

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


def _step_alpha_allocation(base_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Run AlphaPortfolioAllocator: produce capital allocation recommendations."""
    try:
        from core.alpha.performance_store import AlphaPerformanceStore
        from core.alpha.portfolio_allocator import (
            AlphaAllocationPolicy,
            AlphaPortfolioAllocator,
        )
        from core.alpha.registry import AlphaRegistry

        registry_path = Path(base_dir) / "alpha_registry.json"
        perf_path = Path(base_dir) / "alpha_performance.json"

        registry = AlphaRegistry.load(registry_path) if registry_path.exists() else AlphaRegistry()
        perf_store = (
            AlphaPerformanceStore.load(perf_path) if perf_path.exists() else AlphaPerformanceStore()
        )

        allocator = AlphaPortfolioAllocator(registry, perf_store, policy=AlphaAllocationPolicy())
        allocation = allocator.allocate()

        # Persist allocation report
        out_dir = Path(base_dir) / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "alpha_allocation.json"
        if not dry_run:
            out_path.write_text(
                json.dumps(allocation, indent=2, ensure_ascii=False), encoding="utf-8"
            )

        return {
            "step": "alpha_allocation",
            "status": "ok",
            "alpha_count": allocation.get("alpha_count", 0),
            "allocatable_count": allocation.get("allocatable_count", 0),
            "output": str(out_path) if not dry_run else None,
        }
    except Exception as exc:
        return {"step": "alpha_allocation", "status": "error", "error": str(exc)[:500]}


def _step_feature_store_maintenance(
    base_dir: str,
    *,
    dry_run: bool = False,
    retention_days: int = 90,
    skip_update: bool = False,
    mt5_terminal_path: str | None = None,
) -> dict[str, Any]:
    """Run feature store compaction, incremental update, and stats collection."""
    try:
        from scripts.feature_store_maintenance import run_full_maintenance

        store_dir = Path(base_dir) / "feature_store"
        fs_dir = str(store_dir) if store_dir.exists() else None
        report = run_full_maintenance(
            base_dir=base_dir,
            feature_store_dir=fs_dir,
            retention_days=retention_days,
            skip_update=skip_update,
            dry_run=dry_run,
            mt5_terminal_path=mt5_terminal_path,
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


def _resolve_base_dir(base_dir: str | Path) -> str:
    """Resolve relative base_dir against PROJECT_ROOT to guard against CWD drift."""
    p = Path(base_dir)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return str(p)


def run_daily_ops(
    base_dir: str = "data",
    *,
    skip_shadow: bool = False,
    skip_label_builder: bool = False,
    skip_feedback: bool = False,
    skip_governance: bool = False,
    skip_champion: bool = False,
    skip_retraining: bool = False,
    skip_recap: bool = False,
    skip_alpha: bool = False,
    skip_alpha_allocation: bool = False,
    skip_online_feedback: bool = False,
    skip_paper_simulation: bool = False,
    skip_fs_maintenance: bool = False,
    dry_run: bool = False,
    mt5_terminal_path: str | None = None,
) -> dict[str, Any]:
    """Run the full daily operations pipeline.

    base_dir is resolved against PROJECT_ROOT when relative, so the pipeline
    works regardless of the process CWD.

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
    base_dir = _resolve_base_dir(base_dir)
    steps: list[dict[str, Any]] = []

    # Shared tracker + governance + pnl_store: load persisted state so governance
    # and champion see data accumulated by live_intent_loop, and brain registrations
    # survive restarts.  PnL store provides per-brain counterfactual P&L with
    # horizon-matched settlement — the preferred data source for governance decisions
    # (no cross-brain contamination, unlike tracker composite_scores).
    shared_tracker: Any = None
    shared_governance: Any = None
    shared_pnl_store: Any = None
    if not skip_feedback or not skip_governance or not skip_champion:
        shared_tracker = _load_or_create_tracker(base_dir)
        shared_governance = _load_or_create_governance(base_dir)
        shared_pnl_store = _load_or_create_pnl_store(base_dir)
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

    # ── SSOT reconciliation + PnL ledger retention ──
    # FIX-081: Runs first so all downstream steps see consistent config and clean data.
    try:
        from scripts.brain import cmd_reconcile

        _rec_steps: list[dict[str, Any]] = []
        _rec_steps.append({"step": "reconcile", "action": "ssot_alignment"})
        cmd_reconcile(auto_fix=True, cleanup_ledger=True)
        _rec_steps.append({"step": "reconcile", "action": "ssot_alignment", "status": "ok"})

        # PnL ledger retention: prune entries older than 90 days
        _ledger_path = Path(base_dir) / "brain_pnl_ledger.json"
        if _ledger_path.exists():
            from core.feedback.brain_pnl_ledger import BrainPnLStore

            _store = BrainPnLStore.load(str(_ledger_path))
            _pruned = _store.retention_prune(retention_days=90)
            if _pruned:
                _store.save(str(_ledger_path))
                _total_pruned = sum(_pruned.values())
                _rec_steps.append(
                    {
                        "step": "ledger_retention",
                        "status": "ok",
                        "retention_days": 90,
                        "brains_pruned": len(_pruned),
                        "entries_pruned": _total_pruned,
                    }
                )
            else:
                _rec_steps.append({"step": "ledger_retention", "status": "ok", "entries_pruned": 0})
        steps.extend(_rec_steps)
    except Exception as _exc:
        steps.append({"step": "reconcile", "status": "error", "error": str(_exc)})

    if not skip_shadow:
        steps.append(_step_shadow_ensemble(base_dir))

    # Label builder: generate fresh training labels from journals.
    # Runs BEFORE feedback_loop so tracker sees the latest labels.
    if not skip_label_builder:
        steps.append(_step_label_builder(base_dir, dry_run=dry_run))

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
                base_dir,
                dry_run=dry_run,
                tracker=shared_tracker,
                governance=shared_governance,
                pnl_store=shared_pnl_store,
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
        steps.append(_step_retraining_check(base_dir, dry_run=dry_run, auto_execute=not dry_run))

    if not skip_recap:
        steps.append(_step_daily_recap(base_dir, mt5_terminal_path=mt5_terminal_path))

    if not skip_alpha:
        steps.append(_step_alpha_lifecycle(base_dir, dry_run=dry_run))
    if not skip_alpha_allocation:
        steps.append(_step_alpha_allocation(base_dir, dry_run=dry_run))

    if not skip_fs_maintenance:
        steps.append(
            _step_feature_store_maintenance(
                base_dir, dry_run=dry_run, mt5_terminal_path=mt5_terminal_path
            )
        )

    # Parameter optimization suggestions for degraded brains
    # Runs after retraining_check so we know which brains are degraded
    if not skip_retraining:
        retraining_results = [s for s in steps if s.get("step") == "retraining_check"]
        retraining_result = retraining_results[-1] if retraining_results else None
        if retraining_result and retraining_result.get("degraded_count", 0) > 0:
            steps.append(_step_param_optimization(base_dir, retraining_result, dry_run=dry_run))

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
    p.add_argument("--skip-label-builder", action="store_true", help="Skip label builder")
    p.add_argument("--skip-feedback", action="store_true", help="Skip feedback loop")
    p.add_argument("--skip-governance", action="store_true", help="Skip governance cycle")
    p.add_argument("--skip-champion", action="store_true", help="Skip champion/challenger")
    p.add_argument("--skip-retraining", action="store_true", help="Skip retraining check")
    p.add_argument("--skip-recap", action="store_true", help="Skip daily recap")
    p.add_argument("--skip-alpha", action="store_true", help="Skip alpha lifecycle evaluation")
    p.add_argument(
        "--skip-alpha-allocation", action="store_true", help="Skip alpha portfolio allocation"
    )
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
        skip_label_builder=args.skip_label_builder,
        skip_feedback=args.skip_feedback,
        skip_governance=args.skip_governance,
        skip_champion=args.skip_champion,
        skip_retraining=args.skip_retraining,
        skip_recap=args.skip_recap,
        skip_alpha=args.skip_alpha,
        skip_alpha_allocation=args.skip_alpha_allocation,
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


try:
    from core.deployment.scheduled_task_registry import register

    register("daily_ops", run_daily_ops)
except ImportError:
    pass

if __name__ == "__main__":
    raise SystemExit(main())
