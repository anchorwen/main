#!/usr/bin/env python
"""Orphan entry tombstone — isolate contaminated journal entries.

Scans live_trade_journal.jsonl for orphan entries (null PnL, null ticket,
auto_orphan labels) and moves them to a separate archive file, keeping
the main journal clean for statistical analysis.

DQAF-20260616-005/P1: 5.8% contamination in BTC journal distorts
win rate, profit factor, and future Meta-Labeler training baselines.

Usage:
  # Dry-run: show what would be moved
  python scripts/tombstone_orphans.py --data-dir data_btc --dry-run

  # Execute: move orphans to archive
  python scripts/tombstone_orphans.py --data-dir data_btc

  # Clean both data dirs
  python scripts/tombstone_orphans.py --data-dir data
  python scripts/tombstone_orphans.py --data-dir data_btc

Safety:
  - Original journal is backed up to .bak before modification
  - Orphans are written to .orphans.jsonl with metadata
  - Dry-run mode shows exactly what would change
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path


def is_orphan(entry: dict) -> tuple[bool, str]:
    """Check if a journal entry is an orphan. Returns (is_orphan, reason)."""
    if entry.get("action") != "close":
        return False, ""
    pnl = entry.get("pnl")
    ticket = entry.get("position_ticket")
    label = str(entry.get("label", ""))

    if pnl is None:
        return True, "null_pnl"
    if ticket is None:
        return True, "null_ticket"
    if "auto_orphan" in label:
        return True, f"orphan_label:{label[:40]}"
    return False, ""


def main() -> int:
    p = argparse.ArgumentParser(prog="tombstone_orphans")
    p.add_argument("--data-dir", type=str, default="data_btc", help="Data directory")
    p.add_argument("--dry-run", action="store_true", help="Preview only, no changes")
    args = p.parse_args()

    journal_path = Path(args.data_dir) / "live_trade_journal.jsonl"
    if not journal_path.exists():
        print(f"[ERROR] Journal not found: {journal_path}")
        return 1

    # Read all entries
    entries: list[dict] = []
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"_raw": line, "_parse_error": True})

    # Classify
    orphans: list[dict] = []
    clean: list[dict] = []
    orphan_reasons: dict[str, int] = {}
    for e in entries:
        ok, reason = is_orphan(e)
        if ok and not e.get("_parse_error"):
            orphans.append(e)
            orphan_reasons[reason] = orphan_reasons.get(reason, 0) + 1
        else:
            clean.append(e)

    total = len(entries)
    n_orphans = len(orphans)
    pct = n_orphans / max(total, 1) * 100

    print(f"Journal: {journal_path}")
    print(f"  Total entries: {total}")
    print(f"  Orphan entries: {n_orphans} ({pct:.1f}%)")
    print(f"  Clean entries: {len(clean)}")
    print(f"  Reasons: {orphan_reasons}")

    if n_orphans == 0:
        print("\n[OK] No orphans found — journal is clean.")
        return 0

    if args.dry_run:
        print("\n[DRY-RUN] No changes made. Run without --dry-run to execute.")
        # Show sample orphans
        print("\nSample orphans (first 3):")
        for e in orphans[:3]:
            ts = e.get("recorded_at", "?")[:19]
            mag = e.get("magic", "?")
            lbl = e.get("label", "?")
            tkt = e.get("position_ticket", "?")
            print(f"  {ts} magic={mag} label={lbl} ticket={tkt}")
        return 0

    # Execute: backup, write clean journal, write orphan archive
    bak_path = journal_path.with_suffix(".jsonl.bak")
    orphan_path = journal_path.with_name(journal_path.stem + ".orphans.jsonl")

    now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat()

    # 1. Backup
    shutil.copy2(journal_path, bak_path)
    print(f"\n[1/3] Backup: {bak_path}")

    # 2. Write orphan archive
    with open(orphan_path, "w", encoding="utf-8") as f:
        f.write(
            json.dumps({
                "_archive_meta": {
                    "created_at": now_iso,
                    "source": str(journal_path),
                    "orphan_count": n_orphans,
                    "reasons": orphan_reasons,
                    "note": "Orphan entries isolated for data quality. "
                            "These entries have null PnL, null ticket, or "
                            "auto_orphan labels and should not be used for "
                            "statistical analysis.",
                }
            }) + "\n"
        )
        for e in orphans:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"[2/3] Orphans archived: {orphan_path} ({n_orphans} entries)")

    # 3. Write clean journal
    with open(journal_path, "w", encoding="utf-8") as f:
        for e in clean:
            if e.get("_parse_error"):
                f.write(e.get("_raw", "") + "\n")
            else:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"[3/3] Clean journal written: {journal_path} ({len(clean)} entries)")

    print(f"\n[DONE] Removed {n_orphans} orphan entries ({pct:.1f}%).")
    print(f"       Backup: {bak_path}")
    print(f"       Archive: {orphan_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
