#!/usr/bin/env python
"""Deduplicate journal closes — keep real close, discard synthetic duplicate.

Finds tickets with multiple close entries and keeps the one with real PnL data
(non-null PnL, valid timestamp), discarding synthetic duplicates (null PnL,
empty timestamp) created by journal_cleanup.

DQAF-20260616-005: 46 XAU + 15 BTC duplicate closes detected.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(prog="dedup_journal")
    p.add_argument("--data-dir", type=str, default="data", help="Data directory")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    jp = Path(args.data_dir) / "live_trade_journal.jsonl"
    if not jp.exists():
        print(f"[ERROR] Not found: {jp}")
        return 1

    entries: list[dict] = []
    with open(jp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"_raw": line})

    # Find duplicate closes per ticket
    from collections import defaultdict
    close_groups: dict[int, list[int]] = defaultdict(list)
    for i, e in enumerate(entries):
        if e.get("action") != "close":
            continue
        tkt = e.get("position_ticket")
        if tkt:
            close_groups[int(tkt)].append(i)

    # Resolve: keep the entry with non-null PnL and valid timestamp
    removed = 0
    keep_mask = [True] * len(entries)
    for tkt, indices in close_groups.items():
        if len(indices) <= 1:
            continue
        # Score each entry: prefer non-null PnL + has timestamp
        scored = []
        for idx in indices:
            e = entries[idx]
            score = 0
            if e.get("pnl") is not None:
                score += 10
            if e.get("recorded_at") and len(str(e.get("recorded_at", ""))) > 10:
                score += 5
            if e.get("_raw"):
                score -= 100
            scored.append((idx, score))
        scored.sort(key=lambda x: -x[1])
        # Keep the highest-scored, remove others
        for idx, _ in scored[1:]:
            keep_mask[idx] = False
            removed += 1

    if removed == 0:
        print("[OK] No duplicate closes found.")
        return 0

    print(f"Found {removed} duplicate close entries across {sum(1 for v in close_groups.values() if len(v)>1)} tickets.")

    if args.dry_run:
        print("[DRY-RUN] No changes made.")
        return 0

    # Backup and rewrite
    bak = jp.with_suffix(".jsonl.bak2")
    shutil.copy2(jp, bak)
    print(f"Backup: {bak}")

    clean = [e for i, e in enumerate(entries) if keep_mask[i]]
    with open(jp, "w", encoding="utf-8") as f:
        for e in clean:
            if e.get("_raw"):
                f.write(e["_raw"] + "\n")
            else:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"Clean journal: {jp} ({len(clean)} entries, {removed} removed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
