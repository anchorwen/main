#!/usr/bin/env python
"""DQAF-20260622-NNN: BTC journal_mt5 Sev2 — Targeted Diagnostic Script.

Deep-dive into the specific integrity anomalies discovered in the
institutional full-chain data audit. This script produces a structured
evidence report for the DQAF diagnosis.

Usage:
    python scripts/diagnose_journal_mt5_sev2.py --data-dir data_btc
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def parse_ts(ts_str: str) -> datetime | None:
    """Parse ISO 8601 timestamps with optional Z suffix and sub-second precision."""
    if not ts_str:
        return None
    ts_str = ts_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        # Try truncating sub-second part
        if "." in ts_str:
            try:
                return datetime.fromisoformat(
                    ts_str.split(".")[0] + ts_str[ts_str.index(".") + 26 :]
                    if "+" in ts_str or "-" in ts_str[10:]
                    else ts_str.split(".")[0] + "+00:00"
                )
            except Exception:
                pass
        return None


def load_journal(data_dir: str) -> list[dict]:
    """Load all journal entries."""
    jp = Path(data_dir) / "live_trade_journal.jsonl"
    entries = []
    with open(jp, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def main():
    parser = argparse.ArgumentParser(description="BTC journal_mt5 Sev2 diagnosis")
    parser.add_argument("--data-dir", default="data_btc")
    args = parser.parse_args()

    entries = load_journal(args.data_dir)
    print("=== BTC JOURNAL DIAGNOSTIC: journal_mt5 Sev2 ===\n")
    print(f"Total entries: {len(entries)}")

    # ── CHECK 1: Empty strategy field ──
    print(f"\n{'='*60}")
    print("CHECK 1: Empty Strategy Field (Attribution Loss)")
    print(f"{'='*60}")
    empty_strat = [e for e in entries if e.get("strategy", "") == ""]
    print(
        f"Entries with empty strategy: {len(empty_strat)} ({len(empty_strat)/len(entries)*100:.1f}%)"
    )

    # Breakdown by action
    by_action = Counter(e.get("action") for e in empty_strat)
    for action, count in by_action.most_common():
        print(f"  action={action}: {count}")

    # Breakdown by magic
    by_magic = Counter(e.get("magic") for e in empty_strat)
    print("  By magic number:")
    for magic, count in by_magic.most_common():
        print(f"    magic={magic}: {count}")

    # Which opens/closes have empty strategy?
    empty_opens = [e for e in empty_strat if e.get("action") == "open"]
    empty_closes = [e for e in empty_strat if e.get("action") == "close"]
    print(f"  Opens with empty strategy: {len(empty_opens)}")
    print(f"  Closes with empty strategy: {len(empty_closes)}")

    # Check if magic→strategy mapping is broken
    magic_to_strategies = defaultdict(set)
    for e in entries:
        m = e.get("magic")
        s = e.get("strategy", "")
        if m and s:
            magic_to_strategies[m].add(s)
    print("\n  Known magic→strategy mappings:")
    for magic, strats in sorted(magic_to_strategies.items()):
        print(f"    magic={magic}: {strats}")

    # Check for magic numbers that NEVER have strategy info
    all_magic_strats = defaultdict(set)
    for e in entries:
        all_magic_strats[e.get("magic")].add(e.get("strategy", ""))
    print("\n  Magics with ONLY empty strategy:")
    for magic, strats in sorted(all_magic_strats.items()):
        if strats == {""}:
            print(
                f"    magic={magic}: NEVER has strategy (count={sum(1 for e in entries if e.get('magic')==magic)})"
            )

    # ── CHECK 2: PnL Mismatch (detail.pnl vs top-level pnl) ──
    print(f"\n{'='*60}")
    print("CHECK 2: PnL Field Mismatch (detail.pnl vs top-level pnl)")
    print(f"{'='*60}")
    closes = [e for e in entries if e.get("action") == "close"]
    print(f"Total close entries: {len(closes)}")

    pnl_mismatches = []
    for c in closes:
        top_pnl = c.get("pnl")
        detail_pnl = None
        detail = c.get("detail", {})
        if isinstance(detail, dict):
            detail_pnl = detail.get("pnl")
        if top_pnl is not None and detail_pnl is not None:
            if abs(top_pnl - detail_pnl) > 0.005:
                pnl_mismatches.append(
                    {
                        "message_id": c.get("message_id", "?")[:20],
                        "recorded_at": c.get("recorded_at", "?"),
                        "top_pnl": top_pnl,
                        "detail_pnl": detail_pnl,
                        "gap": top_pnl - detail_pnl,
                        "reason": detail.get("reason", "?"),
                        "strategy": c.get("strategy", ""),
                        "position_ticket": c.get("position_ticket"),
                        "label": c.get("label", "?"),
                    }
                )

    print(
        f"PnL mismatches (>$0.005): {len(pnl_mismatches)} ({len(pnl_mismatches)/len(closes)*100:.1f}%)"
    )

    # Categorize by gap size
    gaps = [abs(m["gap"]) for m in pnl_mismatches]
    if gaps:
        print(f"  Min gap: ${min(gaps):.2f}")
        print(f"  Max gap: ${max(gaps):.2f}")
        print(f"  Mean gap: ${sum(gaps)/len(gaps):.2f}")
        print(f"  Gaps > $1.00: {sum(1 for g in gaps if g > 1.0)}")
        print(f"  Gaps > $5.00: {sum(1 for g in gaps if g > 5.0)}")
        print(f"  Gaps > $10.00: {sum(1 for g in gaps if g > 10.0)}")

    # Categorize by reason
    by_reason = Counter(m["reason"] for m in pnl_mismatches)
    print("\n  Mismatches by reason:")
    for reason, count in by_reason.most_common():
        print(f"    reason={reason}: {count}")

    # Show top 10 worst mismatches
    pnl_mismatches.sort(key=lambda x: abs(x["gap"]), reverse=True)
    print("\n  Top 10 worst mismatches:")
    for m in pnl_mismatches[:10]:
        print(
            f"    {m['recorded_at']} | {m['strategy']:15s} | top=${m['top_pnl']:8.2f} detail=${m['detail_pnl']:8.2f} gap=${m['gap']:8.2f} | reason={m['reason']} | label={m['label']}"
        )

    # Detail structure analysis
    print("\n  Detail key patterns in closes:")
    detail_key_counts: Counter[tuple[str, ...]] = Counter()
    for c in closes:
        detail = c.get("detail", {})
        if isinstance(detail, dict):
            keys = tuple(sorted(detail.keys()))
            detail_key_counts[keys] += 1
    for keys, count in detail_key_counts.most_common(10):
        print(f"    {keys}: {count}")

    # ── CHECK 3: volume=0.0 ──
    print(f"\n{'='*60}")
    print("CHECK 3: Volume=0.0 Anomalies")
    print(f"{'='*60}")
    zero_vol = [e for e in entries if e.get("volume", 1) == 0.0]
    print(f"Entries with volume=0.0: {len(zero_vol)}")
    for z in zero_vol:
        print(
            f"  {z.get('recorded_at')} | action={z.get('action')} | strategy={z.get('strategy')} | magic={z.get('magic')} | ticket={z.get('position_ticket')} | pnl={z.get('pnl')} | ack={z.get('ack_status')}"
        )

    # ── CHECK 4: Missing close_price ──
    print(f"\n{'='*60}")
    print("CHECK 4: Missing close_price in Close Entries")
    print(f"{'='*60}")
    no_close_price = []
    for c in closes:
        detail = c.get("detail", {})
        if isinstance(detail, dict):
            if "close_price" not in detail:
                no_close_price.append(c)
    print(
        f"Close entries without detail.close_price: {len(no_close_price)} ({len(no_close_price)/len(closes)*100:.1f}%)"
    )

    # Time distribution
    if no_close_price:
        earliest = min(
            parse_ts(c.get("recorded_at", "3000")) or datetime(3000, 1, 1, tzinfo=UTC)
            for c in no_close_price
        )
        latest = max(
            parse_ts(c.get("recorded_at", "1900")) or datetime(1900, 1, 1, tzinfo=UTC)
            for c in no_close_price
        )
        if earliest and latest and earliest.year < 3000 and latest.year > 1900:
            print(f"  Time range: {earliest} to {latest}")

        # By strategy
        by_strat = Counter(c.get("strategy", "") for c in no_close_price)
        print("  By strategy:")
        for s, cnt in by_strat.most_common():
            print(f"    strategy='{s}': {cnt}")

    # ── CHECK 5: Orphan closes (no matching open_message_id) ──
    print(f"\n{'='*60}")
    print("CHECK 5: Open↔Close Link Integrity")
    print(f"{'='*60}")
    opens_by_msgid = {}
    for e in entries:
        if e.get("action") == "open":
            opens_by_msgid[e.get("message_id")] = e

    orphan_closes = []
    linked_closes = []
    for c in closes:
        omid = c.get("open_message_id")
        if omid and omid in opens_by_msgid:
            linked_closes.append(c)
        else:
            orphan_closes.append(c)

    print(f"Opens: {len(opens_by_msgid)}")
    print(f"Closes: {len(closes)}")
    print(f"Linked closes (open_message_id → open): {len(linked_closes)}")
    print(
        f"Orphan closes (no matching open): {len(orphan_closes)} ({len(orphan_closes)/max(len(closes),1)*100:.1f}%)"
    )

    # Check orphan close reasons
    orphan_by_reason: Counter[str] = Counter()
    for c in orphan_closes:
        detail = c.get("detail", {})
        reason = detail.get("reason", "no_detail") if isinstance(detail, dict) else "no_detail"
        orphan_by_reason[reason] += 1
    print("  Orphan reasons:")
    for reason, count in orphan_by_reason.most_common():
        print(f"    reason={reason}: {count}")

    # Check how many orphan closes have null open_message_id
    no_omid = sum(1 for c in orphan_closes if not c.get("open_message_id"))
    print(f"  Orphans with null open_message_id: {no_omid}")

    # ── CHECK 6: Reconciliation gap — MT5 deal matching key analysis ──
    print(f"\n{'='*60}")
    print("CHECK 6: Position Ticket Uniqueness (MT5 Reconciliation Key)")
    print(f"{'='*60}")
    close_tickets = [c.get("position_ticket") for c in closes if c.get("position_ticket")]
    ticket_counts = Counter(close_tickets)
    dup_tickets = {t: c for t, c in ticket_counts.items() if c > 1}
    print(f"Unique position_tickets in closes: {len(set(close_tickets))}")
    print(f"Duplicate position_tickets in closes: {len(dup_tickets)}")
    if dup_tickets:
        print("  Top dups:")
        for t, count in sorted(dup_tickets.items(), key=lambda x: -x[1])[:10]:
            print(f"    ticket={t}: {count} close entries")
            # Show the duplicates
            for e in entries:
                if e.get("position_ticket") == t and e.get("action") == "close":
                    print(
                        f"      message_id={e.get('message_id','?')[:30]} pnl={e.get('pnl')} recorded_at={e.get('recorded_at')}"
                    )

    # ── CHECK 7: Open entries with missing critical fields ──
    print(f"\n{'='*60}")
    print("CHECK 7: Open Entry Field Completeness")
    print(f"{'='*60}")
    opens = [e for e in entries if e.get("action") == "open"]
    print(f"Total opens: {len(opens)}")

    missing_entry_context = [o for o in opens if not o.get("entry_context")]
    missing_brain_ids = [o for o in opens if not o.get("brain_ids")]
    missing_confidence = [o for o in opens if o.get("confidence") is None]
    missing_pwin = [o for o in opens if o.get("p_win") is None]
    missing_kelly = [o for o in opens if o.get("kelly_mult") is None]

    print(f"  Missing entry_context: {len(missing_entry_context)}")
    print(f"  Missing brain_ids: {len(missing_brain_ids)}")
    print(f"  Missing confidence: {len(missing_confidence)}")
    print(f"  Missing p_win: {len(missing_pwin)}")
    print(f"  Missing kelly_mult: {len(missing_kelly)}")

    if missing_entry_context:
        print("  Opens without entry_context:")
        for o in missing_entry_context:
            print(
                f"    {o.get('recorded_at')} | {o.get('strategy')} | msg_id={o.get('message_id','?')[:30]} | kelly_mult={o.get('kelly_mult')}"
            )

    # ── CHECK 8: Consecutive reject spikes ──
    print(f"\n{'='*60}")
    print("CHECK 8: Consecutive Rejected Entries (Execution Health)")
    print(f"{'='*60}")
    rejected = [e for e in entries if e.get("ack_status") == "rejected"]
    print(f"Total rejected: {len(rejected)} ({len(rejected)/len(entries)*100:.1f}%)")

    # Find runs of consecutive rejects
    max_run = 0
    current_run = 0
    runs = []
    for e in entries:
        if e.get("ack_status") == "rejected":
            current_run += 1
        else:
            if current_run > 0:
                runs.append(current_run)
                max_run = max(max_run, current_run)
            current_run = 0
    if current_run > 0:
        runs.append(current_run)
        max_run = max(max_run, current_run)

    print(f"  Consecutive reject runs: {len(runs)}")
    print(f"  Max consecutive rejects: {max_run}")
    print(f"  Run length distribution: {dict(Counter(runs))}")

    # By action
    rejected_by_action = Counter(e.get("action") for e in rejected)
    print("  By action:")
    for action, count in rejected_by_action.most_common():
        print(f"    action={action}: {count}")

    # Time distribution
    rejected_by_day: Counter[str] = Counter()
    for e in rejected:
        ts = parse_ts(e.get("recorded_at", ""))
        if ts:
            rejected_by_day[ts.strftime("%Y-%m-%d")] += 1
    print("  By day:")
    for day, count in sorted(rejected_by_day.items()):
        print(f"    {day}: {count}")

    # ── SUMMARY ──
    print(f"\n{'='*60}")
    print("SUMMARY: Sev2 Anomalies Requiring Action")
    print(f"{'='*60}")
    issues = []

    if len(empty_strat) > 0:
        sev = "Sev2" if len(empty_opens) > 0 or len(empty_closes) > 0 else "Sev3"
        issues.append(
            f"{sev}: {len(empty_strat)} entries ({len(empty_strat)/len(entries)*100:.1f}%) with empty strategy field — attribution loss, {len(empty_opens)} opens + {len(empty_closes)} closes affected"
        )

    if pnl_mismatches:
        issues.append(
            f"Sev2: {len(pnl_mismatches)} detail.pnl vs top-level pnl mismatches ({len(pnl_mismatches)/len(closes)*100:.1f}%) — max gap ${max(abs(m['gap']) for m in pnl_mismatches):.2f}"
        )

    if zero_vol:
        issues.append(
            f"Sev2: {len(zero_vol)} close entries with volume=0.0 — physically impossible"
        )

    if no_close_price:
        pct = len(no_close_price) / max(len(closes), 1) * 100
        sev = "Sev2" if pct > 10 else "Sev3"
        issues.append(
            f"{sev}: {len(no_close_price)} close entries ({pct:.1f}%) missing detail.close_price — incomplete fill info"
        )

    if orphan_closes:
        pct = len(orphan_closes) / max(len(closes), 1) * 100
        sev = "Sev2" if pct > 20 else "Sev3"
        issues.append(
            f"{sev}: {len(orphan_closes)} orphan closes ({pct:.1f}%) — no matching open_message_id, {no_omid} with null open_message_id"
        )

    if max_run > 5:
        issues.append(f"Sev3: Max {max_run} consecutive rejects — possible execution stall")

    for i, issue in enumerate(issues, 1):
        print(f"  {i}. {issue}")

    if not issues:
        print("  No Sev2 issues detected in offline audit.")
    else:
        print(f"\n  Total Sev2-level issues: {sum(1 for i in issues if i.startswith('Sev2'))}")

    print(f"\n[DONE] — All statistics computed from live_trade_journal.jsonl ({args.data_dir})")


if __name__ == "__main__":
    main()
