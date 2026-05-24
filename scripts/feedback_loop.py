"""Feedback loop: ingest journal outcomes into BrainPerformanceTracker.

Closes the gap between dispatch-time optimism ("pending") and actual trade
outcomes (win/loss/breakeven) from the journal and P&L labels.

Attribution:
  - Single-brain mode (--brain-id): all trades attributed to that brain
  - Multi-brain mode: reads decision records from data/decisions/ to
    find brain attribution by time proximity (nearest decision record)

Usage:
  python scripts/feedback_loop.py --multi-brain --base-dir data
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


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO timestamp string, handling Z suffix."""
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


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
    """Compute composite_score and execution_outcome from trade result."""
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
    if ack_status == "accepted":
        return {"composite_score": 0.55, "execution_outcome": "filled"}
    if ack_status == "rejected":
        return {"composite_score": 0.15, "execution_outcome": "rejected"}
    return {"composite_score": 0.30, "execution_outcome": str(ack_status)}


def _read_decision_records(
    decisions_dir: Path, *, date_filter: str | None = None, symbol: str = "XAUUSDc"
) -> list[dict[str, Any]]:
    """Read decision records from data/decisions/ for multi-brain attribution."""
    records: list[dict[str, Any]] = []
    date = date_filter or _today_key()
    pattern = decisions_dir / date / f"{symbol}.decisions.jsonl"
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


def _find_brains_by_time(
    decisions: list[dict[str, Any]],
    target_time: str,
) -> tuple[list[str], list[str]]:
    """Find supporting/opposing brains from the decision record nearest to target_time.

    Returns (supporting_brains, opposing_brains).
    If no decisions found, returns empty lists.
    """
    target_dt = _parse_iso(target_time)
    if target_dt is None or not decisions:
        return [], []

    best: dict[str, Any] | None = None
    best_delta = float("inf")
    for dec in decisions:
        et = _parse_iso(dec.get("event_time", ""))
        if et is None:
            continue
        delta = abs((et - target_dt).total_seconds())
        if delta < best_delta:
            best_delta = delta
            best = dec

    if best is None:
        return [], []

    attr = best.get("attribution") or {}
    return attr.get("supporting_brains", []), attr.get("opposing_brains", [])


def ingest_journal_to_tracker(
    tracker: Any,
    base_dir: str = "data",
    *,
    brain_id: str | None = None,
    date_filter: str | None = None,
    dry_run: bool = False,
    symbol: str = "XAUUSDc",
) -> dict[str, Any]:
    """Read journal + labels, update tracker with real trade outcomes."""
    date = date_filter or _today_key()
    base = Path(base_dir)
    journal_path = base / "live_trade_journal.jsonl"
    labels_path = base / "reports" / "live_labels.jsonl"
    decisions_dir = base / "decisions"

    # Read ALL journal entries for close-matching (cross-date opens/closes)
    all_journals = _read_journal(journal_path, date_filter=None)
    journals = _read_journal(journal_path, date_filter=date)
    labels = _read_labels(labels_path, date_filter=date)
    label_index = _build_label_index(labels)

    # Build open-entry index from ALL journal entries (not just today)
    open_by_ticket: dict[int, dict[str, Any]] = {}
    for j in all_journals:
        if j.get("action") == "open" and j.get("ack_status") == "accepted":
            ticket = j.get("position_ticket")
            if ticket is not None and isinstance(ticket, int) and ticket > 0:
                open_by_ticket[ticket] = j

    # ── Process close entries ──
    close_updates: list[dict[str, Any]] = []
    for j in all_journals:
        if j.get("action") != "close":
            continue
        ticket = j.get("position_ticket")
        # Coerce string tickets for cross-source compatibility
        try:
            ticket_int = int(ticket) if ticket is not None else None
        except (TypeError, ValueError):
            ticket_int = None
        open_entry = open_by_ticket.get(ticket_int) if ticket_int else None
        if open_entry is None:
            continue
        detail = j.get("detail", {}) if isinstance(j.get("detail"), dict) else {}
        pnl = detail.get("pnl")
        close_price = detail.get("close_price")

        if pnl is None and close_price is not None:
            open_detail = (
                open_entry.get("detail", {}) if isinstance(open_entry.get("detail"), dict) else {}
            )
            open_req = open_detail.get("request", {}) if isinstance(open_detail, dict) else {}
            entry_price = open_req.get("price")
            volume = open_entry.get("volume") or open_entry.get("effective_volume_hint", 0.01)
            side = str(open_entry.get("side", ""))
            if entry_price is not None and volume:
                if side == "long":
                    pnl = round((close_price - entry_price) * volume, 2)
                elif side == "short":
                    pnl = round((entry_price - close_price) * volume, 2)

        if pnl is not None:
            outcome = "win" if pnl > 0 else ("loss" if pnl < 0 else "breakeven")
            score = (
                min(0.95, 0.55 + abs(pnl) * 0.01)
                if pnl > 0
                else (max(0.10, 0.45 - abs(pnl) * 0.01) if pnl < 0 else 0.50)
            )
            close_updates.append(
                {
                    "position_ticket": ticket,
                    "pnl": pnl,
                    "composite_score": round(score, 4),
                    "execution_outcome": outcome,
                    "symbol": open_entry.get("symbol", ""),
                    "side": open_entry.get("side", ""),
                    "recorded_at": j.get("recorded_at", ""),
                    "close_message_id": j.get("message_id"),
                    "open_recorded_at": open_entry.get("recorded_at", ""),
                }
            )

    # ── Resolve close_updates to brain tracker entries ──
    updates: list[dict[str, Any]] = []
    tracked_brain_ids: set[str] = set()

    # Deduplicate close_updates: keep only the latest per position_ticket
    if close_updates:
        latest_cu: dict[int, dict[str, Any]] = {}
        for cu in close_updates:
            ticket = cu["position_ticket"]
            if ticket not in latest_cu or cu.get("recorded_at", "") > latest_cu[ticket].get(
                "recorded_at", ""
            ):
                latest_cu[ticket] = cu
        close_updates = list(latest_cu.values())

    if close_updates:
        decisions = (
            _read_decision_records(decisions_dir, date_filter=date, symbol=symbol)
            if brain_id is None
            else []
        )
        for cu in close_updates:
            if brain_id is not None:
                brain_ids_for_trade = {brain_id}
            else:
                supporting, opposing = _find_brains_by_time(
                    decisions, cu.get("open_recorded_at", "")
                )
                all_brains = supporting + opposing
                brain_ids_for_trade = (
                    set(all_brains) if all_brains else {"V9_Institutional_01", "Online_SGD_V1"}
                )

            for bid in brain_ids_for_trade:
                score = cu["composite_score"]
                outcome = cu["execution_outcome"]
                # Reduce score for opposing brains
                if brain_id is None:
                    sup, opp = _find_brains_by_time(decisions, cu.get("open_recorded_at", ""))
                    if bid in opp:
                        score = round(max(0.10, score - 0.20), 4)
                        outcome = "loss" if outcome == "win" else outcome

                tracked_brain_ids.add(bid)
                updates.append(
                    {
                        "brain_id": bid,
                        "composite_score": score,
                        "execution_outcome": outcome,
                        "position_ticket": cu["position_ticket"],
                        "pnl": cu["pnl"],
                    }
                )

    # ── Accepted trades (label-based resolution) ──
    accepted = [j for j in journals if j.get("ack_status") == "accepted"]

    if brain_id is not None:
        for entry in accepted:
            ticket = entry.get("position_ticket")
            label = label_index.get(int(ticket)) if ticket is not None else None
            resolved = _outcome_from_label(label, "accepted")
            resolved["brain_id"] = brain_id
            resolved["position_ticket"] = ticket
            resolved["symbol"] = entry.get("symbol", "")
            resolved["side"] = entry.get("side", "")
            resolved["recorded_at"] = entry.get("recorded_at", "")
            updates.append(resolved)
            tracked_brain_ids.add(brain_id)

        rejected = [j for j in journals if j.get("ack_status") == "rejected"]
        for entry in rejected:
            resolved = _outcome_from_label(None, "rejected")
            resolved["brain_id"] = brain_id
            resolved["position_ticket"] = entry.get("position_ticket")
            resolved["symbol"] = entry.get("symbol", "")
            resolved["side"] = entry.get("side", "")
            resolved["recorded_at"] = entry.get("recorded_at", "")
            updates.append(resolved)
            tracked_brain_ids.add(brain_id)

    if not dry_run and updates:
        # Deduplicate: skip (brain_id, position_ticket) already in tracker or in batch
        seen: set[tuple[str, int | None]] = set()
        # Pre-seed with existing tracker entries to avoid re-appending on re-runs
        for bid, entries in tracker._records.items():
            for e in entries:
                existing_ticket = e.get("dimensions", {}).get("position_ticket")
                if existing_ticket is not None:
                    seen.add((bid, existing_ticket))
        written = 0
        for u in updates:
            key = (u["brain_id"], u.get("position_ticket"))
            if key in seen:
                continue
            seen.add(key)
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
            written += 1

    if not dry_run and updates:
        n_updates = written
    else:
        n_updates = 0
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "date_filter": date,
        "mode": "single_brain" if brain_id else "multi_brain",
        "brain_id": brain_id,
        "journal_entries": len(journals),
        "all_journal_entries": len(all_journals),
        "accepted_trades": len(accepted),
        "labels_available": len(labels),
        "close_updates_found": len(close_updates),
        "updates_applied": n_updates,
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

    if not args.dry_run and report["updates_applied"] > 0:
        tracker.save(tracker_path)

    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    return 0 if report.get("updates_applied", 0) >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
