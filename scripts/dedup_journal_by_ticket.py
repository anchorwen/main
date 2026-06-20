"""One-shot journal dedup: keep only the LAST close per position_ticket.

FIX-20260612-024: 76x retry storm on ticket=3807506009 left 76 close entries
with different message_ids.  Message-id dedup (repair_journal) cannot catch this
pattern — each retry got a fresh message_id.  This script keeps only the last
close per ticket, preserving the retry chain's final outcome.

Safety:
  - Dry-run mode (default) prints what would be removed
  - Atomic write (tmp + os.replace) — crash-safe
  - FileLock serialisation with bridge worker
  - Only deduplicates close entries with >= 3 duplicates
  - Keeps the LAST close by recorded_at (the one that matters for PnL)

Iron Law #11 compliance: stdout is the sole evidence source.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from core.runtime.fault_handler import fail_open_guard


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dedup journal close entries by position_ticket")
    p.add_argument("--data-dir", default="data_btc", help="Data directory (default: data_btc)")
    p.add_argument("--max-dupes", type=int, default=3,
                   help="Only dedup tickets with >= N close entries (default: 3)")
    p.add_argument("--dry-run", action="store_true", default=True,
                   help="Dry run (default: True)")
    p.add_argument("--execute", action="store_true",
                   help="Actually execute the dedup")
    return p.parse_args()


def _load_journal(path: Path) -> list[dict]:
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"_raw": line, "_corrupt": True})
    return entries


def main() -> int:
    args = _parse_args()
    journal_path = Path(args.data_dir) / "live_trade_journal.jsonl"

    if not journal_path.exists():
        print(f"ERROR: {journal_path} not found")
        return 1

    entries = _load_journal(journal_path)
    print(f"Total entries: {len(entries)}")

    # ── Build close index by position_ticket ──
    close_index: dict[int, list[tuple[int, str, dict]]] = {}
    for idx, entry in enumerate(entries):
        if entry.get("action") != "close":
            continue
        ticket = entry.get("position_ticket")
        if not ticket or ticket == 0:
            continue
        recorded = entry.get("recorded_at", "")
        close_index.setdefault(ticket, []).append((idx, recorded, entry))

    # ── Find tickets with excessive closes ──
    to_remove: list[int] = []  # indices to remove
    stats: dict[int, dict] = {}

    for ticket, closes in close_index.items():
        if len(closes) < args.max_dupes:
            continue
        # Sort by recorded_at, keep only the last one
        sorted_closes = sorted(closes, key=lambda x: x[1])
        # Keep the last one, mark all earlier for removal
        for idx, recorded, entry in sorted_closes[:-1]:
            to_remove.append(idx)
            if ticket not in stats:
                stats[ticket] = {"total": len(closes), "removed": 0,
                                 "kept_recorded": sorted_closes[-1][1],
                                 "pnl_values": set()}
            stats[ticket]["removed"] += 1
            pnl = entry.get("pnl")
            if pnl is not None:
                stats[ticket]["pnl_values"].add(round(pnl, 2))

    if not stats:
        print("\nNo tickets with excessive close entries found.")
        return 0

    print(f"\n=== Tickets with >= {args.max_dupes} close entries ===")
    for ticket, s in sorted(stats.items(), key=lambda x: -x[1]["total"]):
        print(f"  ticket={ticket}: {s['total']} closes → remove {s['removed']}, "
              f"keep last at {s['kept_recorded']}, "
              f"PnL values seen: {sorted(s['pnl_values'])}")

    total_removed = len(to_remove)
    print(f"\nTotal entries to remove: {total_removed}")
    print(f"Resulting journal: {len(entries) - total_removed} entries")

    if args.execute:
        # ── Acquire lock ──
        lock_dir = Path(args.data_dir) / ".locks"
        try:
            from core.infrastructure.distributed_lock import FileLock
            lock = FileLock("live_trade_journal", lock_dir=str(lock_dir), ttl_seconds=10)
            acquired = lock.acquire(blocking=True, timeout_seconds=5)
        except Exception:  # BLE001:FOG
            with fail_open_guard("dedup_journal_by_ticket:main"):
                acquired = None
        try:
            # ── Remove marked entries ──
            remove_set = set(to_remove)
            kept = [e for i, e in enumerate(entries) if i not in remove_set]

            # ── Atomic write ──
            tmp_path = journal_path.with_suffix(".jsonl.dedup_tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in kept:
                    f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            os.replace(tmp_path, journal_path)

            print(f"\n[DONE] Removed {total_removed} duplicate close entries.")
            print(f"Journal: {len(entries)} → {len(kept)} entries")

            # Verify
            verify = _load_journal(journal_path)
            close_counts = Counter(
                e.get("position_ticket") for e in verify
                if e.get("action") == "close" and e.get("position_ticket")
            )
            multi = {k: v for k, v in close_counts.items() if v >= args.max_dupes}
            if multi:
                print(f"WARNING: Still have {len(multi)} tickets with >= {args.max_dupes} closes")
            else:
                print(f"VERIFIED: All tickets now have < {args.max_dupes} close entries")
        finally:
            if acquired:
                lock.release()
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
    else:
        print("\n[Dry run — use --execute to apply]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
