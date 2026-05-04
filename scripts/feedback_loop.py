"""Feedback loop: ingest journal outcomes into BrainPerformanceTracker.

Closes the gap between dispatch-time optimism ("filled") and actual trade
outcomes (win/loss/breakeven) from the journal and P&L labels.

Attribution:
  - Single-brain mode (--brain-id): all trades attributed to that brain
  - Multi-brain mode: reads decision records from data/decisions/ to
    find supporting_brains and score them proportionally

Usage:
  # Single-brain: attribute all today's trades to one brain
  python scripts/feedback_loop.py --brain-id V9 --base-dir data

  # Multi-brain: use decision records for attribution
  python scripts/feedback_loop.py --multi-brain --base-dir data

  # Dry-run: show what would be updated
  python scripts/feedback_loop.py --brain-id V9 --base-dir data --dry-run
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "feedback_loop.v1"

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _today_key() -> str:
    return datetime.now(UTC).replace(tzinfo=None).date().isoformat()


def _read_journal(journal_path: Path, *, date_filter: str | None = None) -> list[dict[str, Any]]:
    """Parse live_trade_journal.jsonl entries."""
    entries: list[dict[str, Any]] = []
    if not journal_path.exists():
        return entries
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if date_filter and not str(rec.get("recorded_at", "")).startswith(date_filter):
            continue
        entries.append(rec)
    return entries


def _read_labels(labels_path: Path, *, date_filter: str | None = None) -> list[dict[str, Any]]:
    """Parse training labels JSONL."""
    return _read_journal(labels_path, date_filter=date_filter)


def _build_label_index(labels: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Index labels by position_ticket for O(1) lookup."""
    index: dict[int, dict[str, Any]] = {}
    for lbl in labels:
        ticket = lbl.get("position_ticket")
        if ticket is not None:
            index[int(ticket)] = lbl
    return index


def _outcome_from_label(label: dict[str, Any] | None, ack_status: str) -> dict[str, Any]:
    """Compute composite_score and execution_outcome from trade result.

    Priority: P&L label (win/loss/breakeven) > journal ack_status.
    """
    if label is not None:
        lbl = label.get("label", "unlabeled")
        pnl = label.get("pnl")
        if lbl == "win":
            return {
                "composite_score": min(0.95, 0.75 + abs(pnl or 0) * 0.01),
                "execution_outcome": "win",
            }
        if lbl == "loss":
            return {
                "composite_score": max(0.10, 0.35 - abs(pnl or 0) * 0.01),
                "execution_outcome": "loss",
            }
        if lbl == "breakeven":
            return {"composite_score": 0.50, "execution_outcome": "breakeven"}

    # Fallback: use journal ack_status
    if ack_status == "accepted":
        return {"composite_score": 0.55, "execution_outcome": "filled"}
    if ack_status == "rejected":
        return {"composite_score": 0.15, "execution_outcome": "rejected"}
    return {"composite_score": 0.30, "execution_outcome": str(ack_status)}


def _read_decision_records(
    decisions_dir: Path, *, date_filter: str | None = None
) -> list[dict[str, Any]]:
    """Read decision records from data/decisions/ for multi-brain attribution."""
    records: list[dict[str, Any]] = []
    date = date_filter or _today_key()
    pattern = decisions_dir / date / "XAUUSD.decisions.jsonl"
    if not pattern.exists():
        return records
    for line in pattern.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def ingest_journal_to_tracker(
    tracker: Any,
    base_dir: str = "data",
    *,
    brain_id: str | None = None,
    date_filter: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Read journal + labels, update tracker with real trade outcomes.

    Args:
        tracker: BrainPerformanceTracker to update.
        base_dir: Base data directory.
        brain_id: Single-brain attribution. If None, uses multi-brain mode.
        date_filter: UTC date key; defaults to today.
        dry_run: If True, return what would happen without applying.

    Returns:
        Report dict with updates applied.
    """
    date = date_filter or _today_key()
    base = Path(base_dir)
    journal_path = base / "live_trade_journal.jsonl"
    labels_path = base / "reports" / "live_labels.jsonl"
    decisions_dir = base / "decisions"

    journals = _read_journal(journal_path, date_filter=date)
    labels = _read_labels(labels_path, date_filter=date)
    label_index = _build_label_index(labels)

    # Accepted trades from journal
    accepted = [j for j in journals if j.get("ack_status") == "accepted"]

    updates: list[dict[str, Any]] = []
    tracked_brain_ids: set[str] = set()

    if brain_id is not None:
        # Single-brain: all trades attributed to this brain
        for entry in accepted:
            ticket = entry.get("position_ticket")
            label = label_index.get(int(ticket)) if ticket is not None else None
            outcome = _outcome_from_label(label, "accepted")
            outcome["brain_id"] = brain_id
            outcome["position_ticket"] = ticket
            outcome["symbol"] = entry.get("symbol", "")
            outcome["side"] = entry.get("side", "")
            outcome["recorded_at"] = entry.get("recorded_at", "")
            updates.append(outcome)
            tracked_brain_ids.add(brain_id)

        # Also handle rejected trades (negative signal)
        rejected = [j for j in journals if j.get("ack_status") == "rejected"]
        for entry in rejected:
            outcome = _outcome_from_label(None, "rejected")
            outcome["brain_id"] = brain_id
            outcome["position_ticket"] = entry.get("position_ticket")
            outcome["symbol"] = entry.get("symbol", "")
            outcome["side"] = entry.get("side", "")
            outcome["recorded_at"] = entry.get("recorded_at", "")
            updates.append(outcome)
            tracked_brain_ids.add(brain_id)
    else:
        # Multi-brain: use decision records for attribution
        decisions = _read_decision_records(decisions_dir, date_filter=date)
        for entry in accepted:
            # Match journal entry to decision record by symbol+side proximity
            supporting = []
            for dec in decisions:
                dec_side = (dec.get("labels") or {}).get("decision_side", "")
                journal_side = str(entry.get("side", "")).upper()
                if dec_side == journal_side:
                    supporting = (dec.get("attribution") or {}).get("supporting_brains", [])
                    break

            ticket = entry.get("position_ticket")
            label = label_index.get(int(ticket)) if ticket is not None else None

            if supporting:
                for bid in supporting:
                    outcome = _outcome_from_label(label, "accepted")
                    outcome["brain_id"] = bid
                    outcome["position_ticket"] = ticket
                    outcome["symbol"] = entry.get("symbol", "")
                    outcome["side"] = entry.get("side", "")
                    outcome["recorded_at"] = entry.get("recorded_at", "")
                    updates.append(outcome)
                    tracked_brain_ids.add(bid)

    if not dry_run and updates:
        for u in updates:
            tracker.record_outcome(
                u["brain_id"],
                {
                    "composite_score": u["composite_score"],
                    "execution_outcome": u["execution_outcome"],
                    "dimensions": {
                        "position_ticket": u.get("position_ticket"),
                        "symbol": u.get("symbol", ""),
                        "side": u.get("side", ""),
                        "recorded_at": u.get("recorded_at", ""),
                    },
                },
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "date_filter": date,
        "mode": "single_brain" if brain_id else "multi_brain",
        "brain_id": brain_id,
        "journal_entries": len(journals),
        "accepted_trades": len(accepted),
        "labels_available": len(labels),
        "updates_applied": len(updates) if not dry_run else 0,
        "updates_would_apply": len(updates) if dry_run else 0,
        "brain_ids_updated": sorted(tracked_brain_ids),
        "updates": updates if dry_run else [],
    }


# ── CLI ──


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="feedback_loop")
    p.add_argument("--base-dir", default="data", help="Base data directory")
    p.add_argument("--brain-id", default=None, help="Single-brain attribution")
    p.add_argument(
        "--multi-brain", action="store_true", help="Use decision records for attribution"
    )
    p.add_argument("--date", default=None, help="UTC date key; default=today")
    p.add_argument("--dry-run", action="store_true", help="Show what would be applied")
    p.add_argument("--output", type=Path, default=None, help="Write report JSON to file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from core.feedback.brain_performance_tracker import BrainPerformanceTracker

    # Load existing tracker
    tracker_path = Path(args.base_dir) / "brain_performance.json"
    if tracker_path.exists():
        tracker = BrainPerformanceTracker.load(tracker_path)
    else:
        tracker = BrainPerformanceTracker(window_size=100)

    brain_id = args.brain_id if not args.multi_brain else None
    report = ingest_journal_to_tracker(
        tracker,
        base_dir=args.base_dir,
        brain_id=brain_id,
        date_filter=args.date,
        dry_run=args.dry_run,
    )

    # Save updated tracker
    if not args.dry_run and report["updates_applied"] > 0:
        tracker.save(tracker_path)

    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    return 0 if report["updates_applied"] >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
