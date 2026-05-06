"""Online feedback hook CLI — feed closed trades to OnlineLearnerAdapter.partial_fit().

Bridges the gap between trade journal outcomes and the online SGD learner.
For each closed trade with a matchable feature vector, calls adapter.partial_fit()
to incrementally update model weights from real-world trade outcomes.

Usage:
  python scripts/online_feedback_hook.py                    # process new trades
  python scripts/online_feedback_hook.py --dry-run          # show what would happen
  python scripts/online_feedback_hook.py --base-dir data    # custom data directory
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="online_feedback_hook")
    p.add_argument("--base-dir", default="data", help="Base data directory")
    p.add_argument(
        "--config",
        default="configs/brains/online_learner_v1.json",
        help="Brain config JSON path",
    )
    p.add_argument("--dry-run", action="store_true", help="Show what would be applied")
    p.add_argument(
        "--journal", default=None, help="Journal path override (default: live_trade_journal.jsonl)"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    config_path = (PROJECT_ROOT / args.config).resolve()
    base_dir_abs = (PROJECT_ROOT / args.base_dir).resolve()

    if not config_path.exists():
        print(f"[online_feedback_hook] ERROR: config not found: {config_path}", file=sys.stderr)
        return 2

    # Load brain config
    brain_entry = json.loads(config_path.read_text(encoding="utf-8"))
    brain_id = brain_entry.get("brain_id", "Online_SGD_V1")

    # Resolve artifact path relative to project root
    artifact_path = brain_entry.get("artifact_path", "")
    if artifact_path and not Path(artifact_path).is_absolute():
        brain_entry["artifact_path"] = str((PROJECT_ROOT / artifact_path).resolve())

    # Build the adapter
    from core.brains.adapters.online_learner_adapter import OnlineLearnerAdapter

    adapter = OnlineLearnerAdapter(brain_entry)
    adapter.load()

    from core.feedback.online_feedback_hook import OnlineFeedbackHook

    # Resolve journal path
    if args.journal:
        journal_path = (PROJECT_ROOT / args.journal).resolve()
    else:
        journal_path = base_dir_abs / "live_trade_journal.jsonl"

    if args.dry_run:
        closed_count = 0
        if journal_path.exists():
            for line in journal_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(entry.get("ack_status", "")) == "closed":
                    closed_count += 1

        report = {
            "brain_id": brain_id,
            "current_updates": adapter._total_updates,
            "closed_trades_in_journal": closed_count,
            "journal_path": str(journal_path),
            "would_update": closed_count > 0,
            "dry_run": True,
        }
    else:
        # Use paper-specific state file for paper journal to avoid conflict with live state
        state_path = str(base_dir_abs / "online_feedback_state.json")
        if args.journal and "paper" in args.journal.lower():
            state_path = str(base_dir_abs / "paper_feedback_state.json")

        hook = OnlineFeedbackHook(
            adapter=adapter,
            journal_path=str(journal_path),
            feature_store_dir=f"{base_dir_abs}/feature_store/records",
            last_processed_path=state_path,
        )
        result = hook.process_new_trades(save_weights=True)
        report = {
            "brain_id": brain_id,
            "current_updates": adapter._total_updates,
            "journal_path": str(journal_path),
            "state_path": state_path,
            "feedback_result": result,
        }

    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
