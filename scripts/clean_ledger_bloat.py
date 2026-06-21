"""One-shot cleanup: deduplicate SignalSettled events in ledger_events.jsonl.

DQAF-20260621-030: ledger_events.jsonl has bloated to 25MB / 72k lines because
the startup reconciliation path (_reconcile_closed_positions) generates
SignalSettled events for ALL historically-closed positions on every system
restart, without an idempotency check.

Idempotency key: (position_ticket, brain_id) for SignalSettled events.
Non-SignalSettled events (SignalRecorded, migration, etc.) are preserved as-is.

Usage:
  python scripts/clean_ledger_bloat.py
  python scripts/clean_ledger_bloat.py --base-dir data_btc --backup
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


def deduplicate(
    input_path: Path,
    output_path: Path,
    *,
    backup: bool = False,
) -> dict[str, int]:
    """Deduplicate ledger_events.jsonl. Returns {event_type: count} stats."""
    if backup:
        backup_path = input_path.with_suffix(".jsonl.bak")
        shutil.copy2(input_path, backup_path)
        print(f"Backup saved to {backup_path}")

    seen_settled: set[tuple[int, str]] = set()  # (position_ticket, brain_id)
    seen_recorded: set[tuple[str, str, float]] = set()  # (brain_id, timestamp[:16], entry_price)
    stats: dict[str, int] = {"total_in": 0, "total_out": 0, "deduped": 0, "retained": 0}
    per_type_in: dict[str, int] = {}
    per_type_out: dict[str, int] = {}

    # Phase 1: read all lines
    lines: list[dict[str, Any]] = []
    try:
        raw = input_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"File not found: {input_path}")
        return stats

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError:
            # Preserve unparseable lines as-is
            lines.append(line)  # type: ignore[arg-type]
    stats["total_in"] = len(lines)

    # Phase 2: deduplicate and write
    with open(output_path, "w", encoding="utf-8") as out:
        for entry in lines:
            if isinstance(entry, str):
                # Unparseable line — preserve
                out.write(entry + "\n")
                stats["retained"] += 1
                continue

            etype = entry.get("event_type", "")
            per_type_in[etype] = per_type_in.get(etype, 0) + 1

            if etype == "SignalSettled":
                ticket = entry.get("position_ticket", 0)
                brain_id = entry.get("brain_id", "")
                if not isinstance(ticket, int):
                    try:
                        ticket = int(ticket)
                    except (TypeError, ValueError):
                        ticket = 0
                key = (ticket, str(brain_id))
                if key in seen_settled:
                    stats["deduped"] += 1
                    continue
                seen_settled.add(key)
            elif etype == "SignalRecorded":
                brain_id = entry.get("brain_id", "")
                ts = str(entry.get("timestamp", ""))[:16]  # minute precision
                ep = float(entry.get("entry_price", 0) or 0)
                key = (str(brain_id), ts, round(ep, 2))
                if key in seen_recorded:
                    stats["deduped"] += 1
                    continue
                seen_recorded.add(key)

            # Write retained entry
            out.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            per_type_out[etype] = per_type_out.get(etype, 0) + 1
            stats["retained"] += 1

    stats["total_out"] = stats["retained"]
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deduplicate ledger_events.jsonl SignalSettled bloat (DQAF-20260621-030)"
    )
    parser.add_argument("--base-dir", default="data_btc", help="Base data directory")
    parser.add_argument("--backup", action="store_true", help="Create .bak backup before overwriting")
    parser.add_argument("--dry-run", action="store_true", help="Report stats without writing")
    args = parser.parse_args(argv)

    ledger_path = Path(args.base_dir) / "ledger_events.jsonl"
    if not ledger_path.exists():
        print(f"ERROR: {ledger_path} not found")
        return 1

    size_before = ledger_path.stat().st_size

    if args.dry_run:
        # Count only
        seen: set[tuple[int, str]] = set()
        total = 0
        dup = 0
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("event_type") == "SignalSettled":
                total += 1
                ticket = e.get("position_ticket", 0)
                if not isinstance(ticket, int):
                    try:
                        ticket = int(ticket)
                    except (TypeError, ValueError):
                        ticket = 0
                key = (ticket, str(e.get("brain_id", "")))
                if key in seen:
                    dup += 1
                else:
                    seen.add(key)
        print("Dry run — would deduplicate:")
        print(f"  Total SignalSettled: {total}")
        print(f"  Unique (would keep): {len(seen)}")
        print(f"  Duplicates (would remove): {dup}")
        print(f"  Clean ratio: {len(seen)/max(total,1)*100:.1f}%")
        return 0

    tmp_path = ledger_path.with_suffix(".jsonl.tmp")
    stats = deduplicate(ledger_path, tmp_path, backup=args.backup)

    # Atomic replace
    tmp_path.replace(ledger_path)
    size_after = ledger_path.stat().st_size

    print("Deduplication complete:")
    print(f"  Input lines:       {stats['total_in']:,}")
    print(f"  Output lines:      {stats['total_out']:,}")
    print(f"  Duplicates removed: {stats['deduped']:,}")
    print(f"  File size before:  {size_before:,} bytes ({size_before/1024:.0f} KB)")
    print(f"  File size after:   {size_after:,} bytes ({size_after/1024:.0f} KB)")
    print(f"  Reduction:         {(1-size_after/max(size_before,1))*100:.1f}%")
    print()
    print("Per event type:")
    all_types = sorted(set(
        list({k: 0 for k in list(dict.fromkeys(
            list({k:0 for k in []}.keys())
        ))}.keys())
    ))
    # Recalculate: read output for per-type stats
    per_type = {}
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            et = e.get("event_type", "?")
            per_type[et] = per_type.get(et, 0) + 1
        except json.JSONDecodeError:
            per_type["unparseable"] = per_type.get("unparseable", 0) + 1
    for et, cnt in sorted(per_type.items()):
        print(f"  {et}: {cnt:,}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
