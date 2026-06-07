"""Compute per-brain performance from P&L ledger and run promotion evaluator.

Bridges the counterfactual P&L ledger (brain_pnl_ledger.json) into the
BrainPromotionEvaluator, then applies approved transitions to governance state.

Usage:
  python scripts/training/run_promotion.py              # dry-run: show decisions
  python scripts/training/run_promotion.py --apply      # apply approved transitions
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.brains.services.brain_promotion import BrainPromotionEvaluator


def compute_performance_from_ledger(
    pnl_ledger: dict,
    window: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Compute BrainPromotionEvaluator-compatible metrics from settled P&L records.

    .. deprecated:: 2026-05-29 (FIX-20260529-035)
        Use ``BrainPnLStore.get_all_metrics()`` instead — the SSOT for per-brain
        P&L statistics.  This function reads raw JSON and computes a DIFFERENT
        set of metrics than the canonical BrainPnLMetrics, violating the SSOT
        principle.  Kept only for backward compat in manual ``run_promotion.py``
        CLI usage; must not be called from automated governance pipelines.

    Args:
        pnl_ledger: Loaded brain_pnl_ledger.json dict.
        window: If set, only use last N signals per brain.

    Returns:
        Dict of brain_id → {win_rate, profit_factor, signal_count,
                            consecutive_losses, recent_win_rate}
    """
    import warnings

    warnings.warn(
        "compute_performance_from_ledger is deprecated — use BrainPnLStore.get_all_metrics()",
        DeprecationWarning,
        stacklevel=2,
    )
    settled = pnl_ledger.get("settled", {})
    perf: dict[str, dict[str, Any]] = {}

    for brain_id, records in settled.items():
        if not records:
            continue

        # Apply window if specified
        recs = records[-window:] if window else records

        n = len(recs)
        wins = sum(1 for r in recs if r.get("is_win"))
        n - wins

        win_rate = wins / n if n > 0 else 0.0

        # Profit factor: gross profit / gross loss (absolute values)
        gross_profit = sum(r.get("pnl_per_unit", 0) for r in recs if r.get("pnl_per_unit", 0) > 0)
        gross_loss = abs(
            sum(r.get("pnl_per_unit", 0) for r in recs if r.get("pnl_per_unit", 0) < 0)
        )
        profit_factor = (
            gross_profit / gross_loss if gross_loss > 0 else (99.0 if gross_profit > 0 else 1.0)
        )

        # Consecutive losses (trailing)
        consecutive_losses = 0
        for r in reversed(recs):
            if not r.get("is_win"):
                consecutive_losses += 1
            else:
                break

        # Recent win rate: last 20 or all if fewer
        recent_n = min(20, n)
        recent_recs = recs[-recent_n:]
        recent_wins = sum(1 for r in recent_recs if r.get("is_win"))
        recent_wr = recent_wins / recent_n if recent_n > 0 else win_rate

        perf[brain_id] = {
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 4),
            "signal_count": n,
            "consecutive_losses": consecutive_losses,
            "recent_win_rate": round(recent_wr, 4),
        }

    return perf


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def ensure_governance_registration(
    gov_state: dict,
    brain_ids: list[str],
) -> int:
    """Register brains that exist in P&L but not in governance state. Returns count added."""
    from datetime import UTC, datetime

    brain_states = gov_state.setdefault("brain_states", {})
    transition_log = gov_state.setdefault("transition_log", [])
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    added = 0

    for bid in brain_ids:
        if bid not in brain_states:
            brain_states[bid] = {
                "brain_id": bid,
                "status": "candidate",
                "registered_at": now,
                "last_transition_at": now,
                "transition_count": 0,
                "freeze_count": 0,
            }
            transition_log.append(
                {
                    "brain_id": bid,
                    "from_status": "unknown",
                    "to_status": "candidate",
                    "reason": "auto:registered_from_ledger",
                    "timestamp": now,
                }
            )
            added += 1

    return added


def apply_decisions(gov_state: dict, decisions: list) -> int:
    """Apply approved promotion decisions to governance state. Returns count applied."""
    from datetime import UTC, datetime

    brain_states = gov_state.setdefault("brain_states", {})
    transition_log = gov_state.setdefault("transition_log", [])
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    applied = 0

    for d in decisions:
        if not d.approved or d.action == "hold":
            continue
        if d.brain_id not in brain_states:
            continue

        entry = brain_states[d.brain_id]
        old_status = entry["status"]
        entry["status"] = d.target_status
        entry["last_transition_at"] = now
        entry["transition_count"] = entry.get("transition_count", 0) + 1
        transition_log.append(
            {
                "brain_id": d.brain_id,
                "from_status": old_status,
                "to_status": d.target_status,
                "reason": f"auto:promotion:{';'.join(d.reasons)}"
                if d.reasons
                else f"auto:promotion:{d.action}",
                "timestamp": now,
            }
        )
        applied += 1

    return applied


def main():
    p = argparse.ArgumentParser(description="Run brain promotion from P&L ledger")
    p.add_argument("--apply", action="store_true", help="Apply approved transitions")
    p.add_argument(
        "--pnl-path",
        default="data/brain_pnl_ledger.json",
        help="Path to P&L ledger (default: data/brain_pnl_ledger.json)",
    )
    p.add_argument(
        "--gov-path",
        default="data/governance_state.json",
        help="Path to governance state (default: data/governance_state.json)",
    )
    p.add_argument(
        "--window",
        type=int,
        default=None,
        help="Only use last N signals per brain for evaluation",
    )
    args = p.parse_args()

    root = PROJECT_ROOT
    pnl_path = root / args.pnl_path
    gov_path = root / args.gov_path

    if not pnl_path.exists():
        print(f"[ERROR] P&L ledger not found: {pnl_path}")
        return 1

    # Load data
    pnl_ledger = load_json(pnl_path)
    try:
        from core.governance.governance_service import GovernanceService
        gov_svc = GovernanceService.load(str(gov_path))
        gov_state: dict[str, Any] = {
            "brain_states": gov_svc.get_all_states(),
            "transition_log": gov_svc.get_transition_log(),
        }
    except Exception:  # noqa: BLE001
        print(f"[run_promotion] ERROR: failed to load governance state from {gov_path}")
        return 1

    # Ensure all brains from P&L are in governance state
    settled_brain_ids = list(pnl_ledger.get("settled", {}).keys())
    added = ensure_governance_registration(gov_state, settled_brain_ids)
    if added:
        print(f"Registered {added} new brains in governance state")

    # Compute performance metrics from settled P&L
    perf = compute_performance_from_ledger(pnl_ledger, window=args.window)

    # Run evaluator
    evaluator = BrainPromotionEvaluator()
    brain_states = gov_state.get("brain_states", {})
    decisions = evaluator.evaluate_all(brain_states, perf)

    # Print results
    print(
        f"\n{'Brain':40s} {'Status':12s} {'Action':10s} {'Target':12s} {'Sigs':>5s} {'WR':>7s} {'PF':>7s} {'Reason'}"
    )
    print("-" * 130)
    for d in decisions:
        m = d.metrics_snapshot
        reason = d.reasons[0] if d.reasons else "-"
        print(
            f"{d.brain_id:40s} {d.current_status:12s} {d.action:10s} "
            f"{d.target_status or '-':12s} {m['signal_count']:5d} "
            f"{m['win_rate']:6.1%} {m['profit_factor']:7.2f} "
            f"{reason[:60]}"
        )

    approved = [d for d in decisions if d.approved and d.action != "hold"]
    if approved:
        print(f"\n{len(approved)} decision(s) approved:")
        for d in approved:
            print(
                f"  {d.brain_id}: {d.current_status} → {d.target_status} "
                f"({', '.join(d.reasons)})"
            )
    else:
        print("\nNo promotions/retirements approved.")

    if args.apply:
        n = apply_decisions(gov_state, decisions)
        # FIX-20260604-088: locked, atomic write via GovernanceService
        from core.governance.governance_service import GovernanceService
        _svc = GovernanceService()
        _svc._brain_states = gov_state.get("brain_states", {})
        _svc._transition_log = gov_state.get("transition_log", [])
        _svc.save(str(gov_path), lock_timeout=30.0)
        print(f"\nApplied {n} transition(s). Governance state saved to {gov_path}")
    else:
        print("\n[Dry run — use --apply to write changes]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
