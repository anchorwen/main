#!/usr/bin/env python
"""DQAF-20260622-059 / P0: Generate an augmented journal view with strategy backfill.

Reads the immutable ``live_trade_journal.jsonl`` and produces an augmented copy
(``live_trade_journal.augmented.jsonl``) where entries with ``strategy=""`` are
backfilled from their ``magic`` field using the authoritative MAGIC_TO_STRATEGY
mapping.

**Immutability guarantee**: The original journal is NEVER modified.  The
augmented view is a separate file.  Downstream consumers should prefer the
augmented view when it exists.

**Backfill metadata**: Each augmented entry gains an ``_augment`` field:
    {
        "_augment": {
            "source": "DQAF-20260622-059",
            "original_strategy": "",
            "resolved_strategy": "btc_swing_h1",
            "resolution_method": "magic_lookup"
        }
    }

Entries that already had a non-empty strategy are copied verbatim (no
``_augment`` field).

Usage:
    python scripts/augment_journal_strategy.py --data-dir data_btc
    python scripts/augment_journal_strategy.py --data-dir data      # XAU
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path


def _utc_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def load_journal(journal_path: Path) -> list[dict]:
    """Load all entries from a JSONL journal, skipping blank lines."""
    entries: list[dict] = []
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entries.append(json.loads(stripped))
            except json.JSONDecodeError:
                print(f"WARNING: skipping unparseable line in {journal_path}", file=sys.stderr)
    return entries


def augment_entries(
    entries: list[dict],
    magic_to_strategy: dict[int, str],
) -> tuple[list[dict], int, int]:
    """Backfill strategy for entries where strategy is empty.

    Returns (augmented_entries, backfilled_count, still_empty_count).
    """
    augmented: list[dict] = []
    backfilled = 0
    still_empty = 0

    for entry in entries:
        strategy = entry.get("strategy", "")
        if strategy != "":
            # Already has strategy — copy verbatim
            augmented.append(entry)
            continue

        magic = entry.get("magic")
        resolved = ""
        resolution_method = "none"

        if magic is not None and isinstance(magic, int):
            resolved = magic_to_strategy.get(magic, "")
            if resolved and not resolved.startswith("__"):
                resolution_method = "magic_lookup"
            elif resolved.startswith("__"):
                # Sentinel value — do NOT treat as valid attribution
                resolution_method = "sentinel_skipped"
                resolved = ""

        if resolved:
            entry_copy = dict(entry)
            entry_copy["strategy"] = resolved
            entry_copy["_augment"] = {
                "source": "DQAF-20260622-059",
                "original_strategy": "",
                "resolved_strategy": resolved,
                "resolution_method": resolution_method,
                "augmented_at": _utc_iso(),
            }
            augmented.append(entry_copy)
            backfilled += 1
        else:
            # Cannot resolve — keep as-is (no _augment field)
            augmented.append(entry)
            still_empty += 1

    return augmented, backfilled, still_empty


def main() -> int:
    parser = argparse.ArgumentParser(
        description="DQAF-059: Generate augmented journal with strategy backfill"
    )
    parser.add_argument(
        "--data-dir",
        default="data_btc",
        help="Data directory containing live_trade_journal.jsonl (default: data_btc)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing the augmented file",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    journal_path = data_dir / "live_trade_journal.jsonl"
    augmented_path = data_dir / "live_trade_journal.augmented.jsonl"

    if not journal_path.exists():
        print(f"ERROR: journal not found: {journal_path}", file=sys.stderr)
        return 1

    # Load original journal (IMMUTABLE — read only)
    entries = load_journal(journal_path)
    print(f"Loaded {len(entries)} entries from {journal_path}")

    # Import magic mapping (auto-initialised from hardcoded fallback)
    from core.contracts.strategy_magic import MAGIC_TO_STRATEGY

    print(f"Magic→Strategy mapping has {len(MAGIC_TO_STRATEGY)} entries")

    # Augment
    augmented, backfilled, still_empty = augment_entries(entries, MAGIC_TO_STRATEGY)

    # Report
    empty_before = sum(1 for e in entries if e.get("strategy", "") == "")
    print(f"\n  Entries with empty strategy (before): {empty_before}")
    print(f"  Backfilled:                        {backfilled}")
    print(f"  Still empty (unknown magic):       {still_empty}")
    print(f"  Strategy coverage improvement:      {empty_before - still_empty} entries resolved")

    if still_empty > 0:
        # Show breakdown of unresolvable entries
        from collections import Counter

        unresolvable_magics = Counter(
            e.get("magic") for e in augmented if e.get("strategy", "") == ""
        )
        print("\n  Unresolvable magic numbers:")
        for magic, count in unresolvable_magics.most_common():
            print(f"    magic={magic}: {count} entries")

    if args.dry_run:
        print(f"\n[DRY-RUN] Would write {len(augmented)} entries to {augmented_path}")
        print("[DRY-RUN] No files modified.")
        return 0

    # Write augmented view
    augmented_path.parent.mkdir(parents=True, exist_ok=True)
    with open(augmented_path, "w", encoding="utf-8") as f:
        for entry in augmented:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"\n[DONE] Augmented journal written to {augmented_path}")
    print(f"       Original journal ({journal_path}) is UNMODIFIED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
