"""One-time batch brain reactivation: re-score all retired brains with the
fixed BrainQualityEngine and restore those that qualify.

Usage:
  python scripts/training/reactivate_brains.py --base-dir data
  python scripts/training/reactivate_brains.py --base-dir data --dry-run
  python scripts/training/reactivate_brains.py --base-dir data --min-score-probation 10 --min-score-live 50
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.feedback.brain_pnl_ledger import BrainPnLMetrics, BrainPnLStore
from core.feedback.brain_quality_engine import BrainQualityEngine


from core.training.utils import utc_now_iso as _utc_now_iso  # noqa: F401


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reactivate_brains")
    p.add_argument("--base-dir", default="data", help="Base data directory")
    p.add_argument("--dry-run", action="store_true", help="Report without applying changes")
    p.add_argument(
        "--min-score-probation",
        type=float,
        default=10.0,
        help="Minimum score to reactivate to probation (default 10)",
    )
    p.add_argument(
        "--min-score-live",
        type=float,
        default=50.0,
        help="Minimum score to reactivate to live (default 50)",
    )
    p.add_argument(
        "--output", type=Path, default=None, help="Write reactivation report JSON to file"
    )
    return p


def compute_metrics_from_records(store: BrainPnLStore, brain_id: str) -> BrainPnLMetrics:
    """Compute BrainPnLMetrics for a brain from its settled trade records."""
    return store.get_metrics(brain_id)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = Path(args.base_dir)

    # ── Load PnL ledger ───────────────────────────────────────────────
    ledger_path = base / "brain_pnl_ledger.json"
    if not ledger_path.exists():
        print(f"[reactivate] ERROR: PnL ledger not found at {ledger_path}")
        return 1

    with open(ledger_path, encoding="utf-8") as fh:
        ledger_data = json.load(fh)

    # Build a BrainPnLStore with a large window to capture all history
    pnl_store = BrainPnLStore(
        window_size=ledger_data.get("window_size", 14400),
    )
    pnl_store._settled = ledger_data.get("settled", {})

    # Also load pending if available
    pending = ledger_data.get("pending", {})
    if pending:
        pnl_store._pending = pending

    print(
        f"[reactivate] Loaded PnL ledger: {len(pnl_store._settled)} brains, "
        f"{sum(len(v) for v in pnl_store._settled.values())} settled trades"
    )

    # ── Load governance state ─────────────────────────────────────────
    gov_path = base / "governance_state.json"
    if not gov_path.exists():
        print(f"[reactivate] ERROR: governance state not found at {gov_path}")
        return 1

    try:
        from core.governance.governance_service import GovernanceService
        gov_svc = GovernanceService.load(str(gov_path))
        brain_states = gov_svc.get_all_states()
        transition_log = gov_svc.get_transition_log()
    except Exception:  # noqa: BLE001
        print(f"[reactivate] ERROR: failed to load governance state from {gov_path}")
        return 1

    # ── Assess all brains ─────────────────────────────────────────────
    engine = BrainQualityEngine()
    report: dict[str, Any] = {
        "schema_version": "reactivate_brains.v1",
        "generated_at": _utc_now_iso(),
        "dry_run": args.dry_run,
        "assessments": [],
        "reactivations": [],
        "no_change": [],
    }

    changed = False

    for brain_id, state in sorted(brain_states.items()):
        current_status = state.get("status", "candidate")

        # Compute metrics
        if brain_id not in pnl_store._settled:
            report["assessments"].append(
                {
                    "brain_id": brain_id,
                    "current_status": current_status,
                    "result": "no_pnl_data",
                }
            )
            continue

        metrics = compute_metrics_from_records(pnl_store, brain_id)
        # Bypass governance_status so retired brains get a real score.
        # Otherwise the engine's retired→score=0 hard override creates a
        # deadlock: retired brains can never be re-evaluated positively.
        verdict = engine.assess(brain_id, metrics, governance_status="")

        assessment = {
            "brain_id": brain_id,
            "current_status": current_status,
            "quality_tier": verdict.quality_tier,
            "score": round(verdict.score, 2),
            "sample_count": verdict.sample_count,
            "sharpe": round(verdict.sharpe, 4),
            "win_rate": round(verdict.win_rate, 4),
            "profit_factor": round(verdict.profit_factor, 4),
            "cumulative_pnl": round(verdict.cumulative_pnl, 4),
            "max_drawdown": round(verdict.max_drawdown, 4),
        }
        report["assessments"].append(assessment)

        # Only consider retired brains for reactivation
        if current_status != "retired":
            report["no_change"].append(assessment)
            continue

        # Determine new status
        score = verdict.score
        if score >= args.min_score_live:
            new_status = "live"
        elif score >= args.min_score_probation:
            new_status = "probation"
        else:
            # Score too low, keep retired
            assessment["result"] = "keep_retired"
            report["no_change"].append(assessment)
            continue

        # Reactivation
        assessment["new_status"] = new_status
        assessment["result"] = f"reactivate_{new_status}"

        if args.dry_run:
            report["reactivations"].append(assessment)
        else:
            # Direct state manipulation: bypass VALID_TRANSITIONS since
            # retired→{live,probation} is only allowed in this one-time
            # recovery from a broken scoring system.
            old_status = state["status"]
            state["status"] = new_status
            state["last_transition_at"] = datetime.now(UTC).replace(tzinfo=None).isoformat()
            state["transition_count"] = state.get("transition_count", 0) + 1

            # Record in transition log
            entry = {
                "brain_id": brain_id,
                "from_status": old_status,
                "to_status": new_status,
                "reason": f"reactivation: quality_tier={verdict.quality_tier} score={verdict.score:.1f}",
                "timestamp": _utc_now_iso(),
            }
            transition_log.append(entry)
            report["reactivations"].append(assessment)
            changed = True

    # ── Save ──────────────────────────────────────────────────────────
    if not args.dry_run and changed:
        backup_path = gov_path.with_suffix(".json.bak")
        gov_path.rename(backup_path)
        print(f"[reactivate] Backup saved to {backup_path}")

        # FIX-20260604-088: locked, atomic write via GovernanceService
        from core.governance.governance_service import GovernanceService
        _svc = GovernanceService()
        _svc._brain_states = brain_states
        _svc._transition_log = transition_log
        _svc.save(str(gov_path), lock_timeout=30.0)
        print(f"[reactivate] Governance state saved to {gov_path}")

    # ── Summary ───────────────────────────────────────────────────────
    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Reactivation Summary:")
    print(f"  Brains assessed: {len(report['assessments'])}")
    print(f"  Reactivations:   {len(report['reactivations'])}")
    for r in report["reactivations"]:
        print(
            f"    {r['brain_id']}: retired → {r['new_status']} "
            f"(score={r['score']:.1f}, tier={r['quality_tier']}, "
            f"sharpe={r['sharpe']}, wr={r['win_rate']}, n={r['sample_count']})"
        )
    kept = [
        a
        for a in report["assessments"]
        if a["current_status"] == "retired" and a.get("result") == "keep_retired"
    ]
    if kept:
        print(f"  Staying retired: {len(kept)}")
        for a in kept:
            print(f"    {a['brain_id']}: score={a['score']:.1f} ({a['quality_tier']})")

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        print(f"\n[reactivate] Report written to {out}")

    return 0 if not report["reactivations"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
