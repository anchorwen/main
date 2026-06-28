#!/usr/bin/env python3
"""One-shot backfill script for XAU legacy journal orphans.

🔴 ARCHITECTURE COMMITTEE MANDATE (2026-06-28):
  - ONLY process XAUUSDc orphans with recorded_at < 2026-06-10
  - NEVER backfill BTC rejected closes (ack_status: "rejected") — those are
    ghost closes that must remain in journal_orphan_quarantine.jsonl
  - Synthetic open entries are tagged _source: "orphan_backfill" for audit

Mechanism:
  Scans the journal for close entries whose position_ticket has no matching
  open entry.  For each qualifying XAU orphan, inserts a minimal synthetic
  open entry immediately before the close entry in the journal.

Usage:
  python scripts/backfill_journal_orphans.py --journal data/live_trade_journal.jsonl --dry-run
  python scripts/backfill_journal_orphans.py --journal data/live_trade_journal.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


# ── Hard gates per IC Mandate ──────────────────────────────────────────────
CUTOFF_DATE = "2026-06-10"
ALLOWED_SYMBOL = "XAUUSDc"
BACKFILL_SOURCE_TAG = "orphan_backfill"


def _parse_dt(ts: str | None) -> str:
    """Normalize timestamp to YYYY-MM-DD for comparison."""
    if not ts:
        return ""
    return ts[:10]


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill XAU legacy journal orphans")
    parser.add_argument("--journal", required=True, help="Path to live_trade_journal.jsonl")
    parser.add_argument("--dry-run", action="store_true", help="Report only, do not modify")
    parser.add_argument(
        "--cutoff",
        default=CUTOFF_DATE,
        help=f"Max recorded_at date for backfill (default: {CUTOFF_DATE})",
    )
    parser.add_argument(
        "--symbol",
        default=ALLOWED_SYMBOL,
        help=f"Allowed symbol filter (default: {ALLOWED_SYMBOL})",
    )
    args = parser.parse_args()

    journal_path = Path(args.journal)
    if not journal_path.exists():
        print(f"ERROR: journal not found: {journal_path}")
        return 1

    # ── Pass 1: collect open and close tickets ─────────────────────────
    lines: list[str] = []
    open_tickets: set[int] = set()
    close_entries: list[dict[str, Any]] = []
    close_line_indices: list[int] = []

    raw = journal_path.read_text(encoding="utf-8")
    for i, line in enumerate(raw.strip().split("\n")):
        if not line:
            continue
        lines.append(line)
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        action = entry.get("action", "")
        ticket = entry.get("position_ticket")
        if not isinstance(ticket, int) or ticket <= 0:
            continue

        if action == "open":
            open_tickets.add(ticket)
        elif action == "close":
            close_entries.append(entry)
            close_line_indices.append(i)

    # ── Pass 2: find qualifying orphans ────────────────────────────────
    orphans: list[tuple[int, dict[str, Any]]] = []  # (line_index, close_entry)
    skipped_btc: int = 0
    skipped_recent: int = 0
    skipped_wrong_symbol: int = 0

    for idx, entry in zip(close_line_indices, close_entries, strict=False):
        ticket = entry.get("position_ticket")
        if ticket in open_tickets:
            continue  # has matching open — not an orphan

        symbol = str(entry.get("symbol", "")).strip()
        recorded_at = str(entry.get("recorded_at", "")).strip()
        ack_status = str(entry.get("ack_status", "")).strip().lower()

        # ── 🔴 IC Mandate: NEVER backfill BTC rejected closes ─────────
        if ack_status == "rejected":
            skipped_btc += 1
            continue

        # ── 🔴 IC Mandate: ONLY XAUUSDc ───────────────────────────────
        if symbol != args.symbol:
            skipped_wrong_symbol += 1
            continue

        # ── 🔴 IC Mandate: ONLY pre-cutoff ────────────────────────────
        entry_date = _parse_dt(recorded_at)
        if entry_date >= args.cutoff:
            skipped_recent += 1
            continue

        orphans.append((idx, entry))

    if not orphans:
        print("No qualifying orphans found.")
        print(
            f"  Skipped: {skipped_btc} BTC rejected, {skipped_wrong_symbol} wrong symbol, {skipped_recent} post-cutoff"
        )
        return 0

    print(f"Found {len(orphans)} qualifying XAU legacy orphans (pre-{args.cutoff})")
    print(
        f"  Skipped: {skipped_btc} BTC rejected, {skipped_wrong_symbol} wrong symbol, {skipped_recent} post-cutoff"
    )

    if args.dry_run:
        print("\n[Dry-run] Would insert synthetic opens for:")
        for _idx, entry in orphans[:10]:
            ticket = entry.get("position_ticket", "?")
            recorded = entry.get("recorded_at", "?")
            print(f"  ticket={ticket}  recorded_at={recorded}")
        if len(orphans) > 10:
            print(f"  ... and {len(orphans) - 10} more")
        return 0

    # ── Pass 3: build new journal with synthetic opens inserted ────────
    # For each orphan close, insert a synthetic open BEFORE it in the journal.
    # Process in reverse line order to preserve indices during insertion.
    synthetic_count = 0
    for idx, entry in sorted(orphans, key=lambda x: x[0], reverse=True):
        ticket = entry["position_ticket"]
        recorded_at = entry.get("recorded_at", "")
        symbol = entry.get("symbol", ALLOWED_SYMBOL)
        volume = entry.get("volume", 0.01)
        magic = entry.get("magic", 0)
        strategy = entry.get("strategy", "")

        synthetic_open = {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": recorded_at,
            "message_id": f"orphan_backfill_{ticket}",
            "target": "journal_backfill",
            "ack_status": "synthetic",
            "action": "open",
            "symbol": symbol,
            "strategy": strategy,
            "side": entry.get("side", ""),
            "position_ticket": ticket,
            "volume": volume,
            "magic": magic,
            "entry_price": entry.get("entry_price", 0.0),
            "sl": entry.get("sl", 0.0),
            "tp": entry.get("tp", 0.0),
            "confidence": 0.0,
            "label": "orphan_backfill",
            "comment": "Synthetic open for legacy orphan close — backfill per IC Mandate 2026-06-28",
            "brain_ids": [],
            "brain_votes": {},
            "entry_context": {},
            "pnl": None,
            "detail": {"backfill_source": BACKFILL_SOURCE_TAG},
            "_source": BACKFILL_SOURCE_TAG,
        }
        synthetic_json = json.dumps(synthetic_open, ensure_ascii=False)
        lines.insert(idx, synthetic_json)
        synthetic_count += 1

    # ── Write back ───────────────────────────────────────────────────
    backup_path = journal_path.with_suffix(".jsonl.bak_orphan_backfill")
    journal_path.rename(backup_path)
    print(f"Backup saved to: {backup_path}")

    with open(journal_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Done. Inserted {synthetic_count} synthetic open entries.")
    print(f"Journal updated: {journal_path} ({len(lines)} lines)")

    # ── Verify ────────────────────────────────────────────────────────
    verify_tickets: set[int] = set()
    verify_orphans = 0
    for line in lines:
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = e.get("action", "")
        ticket = e.get("position_ticket")
        if not isinstance(ticket, int):
            continue
        if action == "open":
            verify_tickets.add(ticket)
        elif action == "close":
            if ticket not in verify_tickets:
                verify_orphans += 1

    print(f"Post-backfill orphans remaining: {verify_orphans}")
    print("Backfill complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
