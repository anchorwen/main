#!/usr/bin/env python
"""One-shot migration: export existing brain_pnl_ledger.json → ledger_events.jsonl.

FIX-20260611-021: Event Sourcing Foundation — Step 3 Data Migration.

Reads the existing brain_pnl_ledger.json for BOTH symbols (XAU, BTC),
converts every settled entry to a PnLEvent, and appends them to the
event stream with source="migration".

Usage::

    python scripts/migration/migrate_to_event_stream.py
    python scripts/migration/migrate_to_event_stream.py --base-dir data_btc --symbol BTCUSDc

The original brain_pnl_ledger.json is NOT modified or deleted.
After migration, verify with::

    wc -l data/ledger_events.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Ensure project root is on sys.path
_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from core.contracts.events import DataSource, PnLEvent
from core.data.event_writer import EventWriter


def migrate_ledger(
    ledger_path: Path,
    writer: EventWriter,
    symbol: str,
) -> int:
    """Migrate one brain_pnl_ledger.json to the event stream.

    Returns the number of events written.
    """
    if not ledger_path.exists():
        print(f"[SKIP] {ledger_path} not found — nothing to migrate")
        return 0

    with open(ledger_path, encoding="utf-8") as fh:
        ledger = json.load(fh)

    settled_raw = ledger.get("settled", {})
    if isinstance(settled_raw, list):
        # Old format: flat list
        settled_list = settled_raw
    elif isinstance(settled_raw, dict):
        # Current format: {brain_id: [entries]}
        settled_list = []
        for brain_entries in settled_raw.values():
            if isinstance(brain_entries, list):
                settled_list.extend(brain_entries)
    else:
        settled_list = []

    if not settled_list:
        print(f"[SKIP] {ledger_path}: 0 settled entries")
        return 0

    written = 0
    skipped = 0

    for entry in settled_list:
        try:
            # Parse timestamp — try multiple field names
            ts_str = (
                entry.get("close_time")
                or entry.get("settled_at")
                or entry.get("entry_time")
                or ""
            )
            try:
                timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                timestamp = datetime.now(UTC)

            # PnL: try pnl_r first, then pnl_per_unit, then 0
            pnl_val = entry.get("pnl_r") or entry.get("pnl_per_unit") or 0

            event = PnLEvent(
                timestamp=timestamp,
                source=DataSource.MIGRATION,
                event_type="SignalSettled",
                brain_id=entry.get("brain_id", "unknown"),
                symbol=entry.get("symbol", symbol),
                direction=entry.get("direction"),
                entry_price=entry.get("entry_price"),
                exit_price=entry.get("close_price") or entry.get("exit_price"),
                pnl_r=float(pnl_val),
                confidence=float(entry.get("confidence", 0.5) or 0.5),
                position_ticket=entry.get("position_ticket"),
                generated_by="migration_script.v1",
            )
            writer.write(event)
            written += 1
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            print(f"[WARN] Skipping entry: {exc}")
            skipped += 1

    print(f"[DONE] {ledger_path}: {written} migrated, {skipped} skipped")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate brain_pnl_ledger.json → ledger_events.jsonl",
    )
    parser.add_argument(
        "--base-dir",
        nargs="+",
        default=["data", "data_btc"],
        help="Base data directories to scan (default: data data_btc)",
    )
    parser.add_argument(
        "--symbol",
        nargs="+",
        default=["XAUUSDc", "BTCUSDc"],
        help="Symbols corresponding to each base-dir (default: XAUUSDc BTCUSDc)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be migrated without writing",
    )
    args = parser.parse_args()

    if len(args.base_dir) != len(args.symbol):
        print("ERROR: --base-dir and --symbol must have the same count")
        sys.exit(1)

    for base_dir, symbol in zip(args.base_dir, args.symbol, strict=False):
        ledger_path = Path(base_dir) / "brain_pnl_ledger.json"
        if args.dry_run:
            if ledger_path.exists():
                with open(ledger_path, encoding="utf-8") as fh:
                    ledger = json.load(fh)
                settled_raw = ledger.get("settled", {})
                if isinstance(settled_raw, list):
                    total = len(settled_raw)
                elif isinstance(settled_raw, dict):
                    total = sum(
                        len(v) for v in settled_raw.values() if isinstance(v, list)
                    )
                else:
                    total = 0
                print(f"[DRY-RUN] {ledger_path}: would migrate {total} entries")
            else:
                print(f"[DRY-RUN] {ledger_path}: not found")
        else:
            writer = EventWriter(Path(base_dir) / "ledger_events.jsonl")
            try:
                migrate_ledger(ledger_path, writer, symbol)
                print(f"  → Wrote {writer.line_count} lines to {writer.path}")
            finally:
                writer.close()

    print("\nMigration complete.")


if __name__ == "__main__":
    main()
