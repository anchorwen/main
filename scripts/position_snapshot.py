"""
Reliable position-snapshot utility.

Uses journal + active_position.json as the authoritative data sources,
NOT receipt matching (which misses broker-level SL/TP closes).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def load_journal(journal_path: str | Path) -> list[dict]:
    """Load all journal entries."""
    entries: list[dict[str, object]] = []
    p = Path(journal_path)
    if not p.exists():
        return entries
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:  # noqa: SIM105
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def snapshot(
    base_dir: str | Path = "data",
    *,
    date_prefix: str | None = None,
    verbose: bool = True,
) -> dict:
    """Return a reliable snapshot of current and today's positions.

    Args:
        base_dir: path to the data/ directory
        date_prefix: filter journal entries by this date (e.g. "2026-05-11")
        verbose: print summary to stdout

    Returns dict with keys:
        active_position: contents of active_position.json or None
        today_opens: count of today's open entries
        today_closes: count of today's close entries
        close_reasons: Counter of close reasons/labels
        unmatched_opens: list of open message_ids without matching close
    """
    base = Path(base_dir)
    journal_path = base / "live_trade_journal.jsonl"
    state_path = base / "state" / "active_position.json"

    # ── 1. Active position from state file (AUTHORITATIVE) ──
    active = None
    if state_path.exists():
        with open(state_path, encoding="utf-8") as f:
            active = json.load(f)

    # ── 2. Journal analysis ──
    entries = load_journal(journal_path)
    if date_prefix:
        entries = [e for e in entries if e.get("recorded_at", "").startswith(date_prefix)]

    opens: dict[str, dict] = {}  # message_id -> entry
    closes: dict[str, dict] = {}  # open_message_id -> close_entry
    reasons: Counter = Counter()
    modify_count = 0

    for e in entries:
        action = e.get("action", "")
        msg_id = e.get("message_id", "")
        if action == "open":
            opens[msg_id] = e
        elif action == "close":
            open_msg_id = e.get("open_message_id", "")
            if open_msg_id:
                closes[open_msg_id] = e
            # Track close reason
            label = e.get("label") or e.get("comment") or "unspecified"
            reasons[label] += 1
        elif action == "modify_sltp":
            modify_count += 1

    matched = sum(1 for oid in opens if oid in closes)
    unmatched = [oid for oid in opens if oid not in closes]

    result = {
        "active_position": active,
        "date_prefix": date_prefix or "all",
        "total_opens": len(opens),
        "total_closes": len(closes),
        "total_modify_sltp": modify_count,
        "matched_opens": matched,
        "unmatched_open_count": len(unmatched),
        "close_reasons": reasons,
    }

    # ── 3. Print summary ──
    if verbose:
        print("=" * 60)
        print("POSITION SNAPSHOT")
        print(f"  Source: journal ({journal_path}) + active_position.json")
        print(f"  Date filter: {date_prefix or 'ALL dates'}")
        print("=" * 60)

        if active:
            saved = active.get("saved_at_utc", "unknown")
            print(f"\n  [ACTIVE POSITION] ticket={active.get('ticket')}")
            print(f"    side={active.get('side')}, entry={active.get('entry_price')}")
            print(f"    SL={active.get('current_sl')}, TP={active.get('current_tp')}")
            print(f"    cycles_held={active.get('cycles_held')}, saved_at={saved}")
            print(f"    brains={len(active.get('supporting_brain_ids', []))}")
        else:
            print("\n  [ACTIVE POSITION] NONE — no position currently open")

        print(f"\n  Journal: {len(opens)} opens, {len(closes)} closes, {modify_count} modify_sltp")
        print(f"  Opens with matching close: {matched}/{len(opens)}")
        if unmatched:
            print(
                f"  Unmatched opens (close not in journal — likely broker SL/TP): {len(unmatched)}"
            )
            # Only show the first few
            for oid in list(unmatched)[:5]:
                o = opens[oid]
                print(
                    f"    - ticket={o.get('position_ticket')} side={o.get('side')} @ {o.get('recorded_at','?')}"
                )
            if len(unmatched) > 5:
                print(f"    ... and {len(unmatched) - 5} more")

        if reasons:
            print("\n  Close reasons:")
            for reason, count in reasons.most_common(10):
                print(f"    {reason}: {count}")

    return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Reliable position snapshot from journal + state")
    parser.add_argument("--base-dir", default="data", help="Path to data/ directory")
    parser.add_argument("--date", default=None, help="Date prefix filter (e.g. 2026-05-11)")
    parser.add_argument("--json", action="store_true", help="Output as JSON (no verbose)")
    parser.add_argument(
        "--check-active-only",
        action="store_true",
        help="Only print active position ticket or 'none'",
    )
    args = parser.parse_args(argv)

    if args.check_active_only:
        state_path = Path(args.base_dir) / "state" / "active_position.json"
        if state_path.exists():
            with open(state_path) as f:
                ap = json.load(f)
            print(ap.get("ticket", "unknown"))
        else:
            print("none")
        return 0

    result = snapshot(args.base_dir, date_prefix=args.date, verbose=not args.json)
    if args.json:
        # Convert Counter to dict for JSON
        result["close_reasons"] = dict(result["close_reasons"])
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
