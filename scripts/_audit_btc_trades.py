#!/usr/bin/env python
"""Audit BTC trade activity from multiple independent sources.

Iron Law #11: Script output is the sole source of truth.
Cross-references: ledger events, position snapshots, journal, governance state.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

DATA_DIR = Path("data_btc")


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def main() -> None:
    print("=" * 60)
    print("BTC Trade Activity Audit — Multi-Source Cross-Reference")
    print("=" * 60)

    # ── Source 1: Ledger events ──
    ledger = load_jsonl(DATA_DIR / "ledger_events.jsonl")
    event_types = Counter(e.get("event_type", "?") for e in ledger)
    print(f"\n1. Ledger Events: {len(ledger)} total")
    for et, count in event_types.most_common():
        print(f"   {et}: {count}")

    # SignalSettled events (these are trade PnL settlements)
    settled = [e for e in ledger if e.get("event_type") == "SignalSettled"]
    print(f"\n2. SignalSettled (trade PnL): {len(settled)}")
    if settled:
        tickets = set(e.get("position_ticket") for e in settled)
        tickets.discard(0)
        tickets.discard(None)
        print(f"   Unique non-zero position_tickets: {len(tickets)}")
        # PnL distribution
        pnls = [e.get("pnl_r", 0) or 0 for e in settled]
        pos = sum(1 for p in pnls if p > 0)
        neg = sum(1 for p in pnls if p < 0)
        zero = sum(1 for p in pnls if p == 0)
        print(f"   PnL: {pos} positive, {neg} negative, {zero} breakeven")
        if pnls:
            print(f"   Total PnL(R): {sum(pnls):+.2f}")

    # ── Source 2: Position snapshots ──
    snapshots = load_jsonl(DATA_DIR / "position_snapshots.jsonl")
    print(f"\n3. Position Snapshots: {len(snapshots)}")
    if snapshots:
        tickets = set(s.get("position_ticket") for s in snapshots if s.get("position_ticket"))
        print(f"   Unique position_tickets: {len(tickets)}")
        sides = Counter(s.get("side", "?") for s in snapshots)
        print(f"   Sides: {dict(sides)}")

    # ── Source 3: Journal (live_trade_journal) ──
    journal = load_jsonl(DATA_DIR / "golden_master.jsonl")
    opens = [e for e in journal if e.get("action") == "open"]
    closes = [e for e in journal if e.get("action") == "close"]
    print(f"\n4. Golden Master Journal: {len(journal)} entries")
    print(f"   Opens: {len(opens)}, Closes: {len(closes)}")
    if opens:
        tickets = set(e.get("position_ticket") for e in opens if e.get("position_ticket"))
        print(f"   Open position_tickets: {len(tickets)}")
    if closes:
        labels = Counter(e.get("label", "?") for e in closes)
        print(f"   Close labels: {dict(labels)}")

    # ── Source 4: Governance state ──
    gov = json.loads((DATA_DIR / "governance_state.json").read_text(encoding="utf-8"))
    brains = gov.get("brain_states", {})
    print(f"\n5. Governance: {len(brains)} brains registered")
    for bid, b in brains.items():
        m = b.get("performance_metrics", {})
        print(f"   {bid}: status={b.get('status')}, trades={m.get('total_trades',0)}, PnL={m.get('pnl_r',0):.1f}R")

    # ── Source 5: Daily ops state ──
    daily = DATA_DIR / "state" / "daily_ops_state.json"
    if daily.exists():
        ds = json.loads(daily.read_text(encoding="utf-8"))
        print(f"\n6. Daily Ops State: {json.dumps(ds, indent=2)[:500]}")

    # ── Source 6: Reports ──
    lb = DATA_DIR / "reports" / "leaderboard.json"
    if lb.exists():
        lb_data = json.loads(lb.read_text(encoding="utf-8"))
        print(f"\n7. Leaderboard: {lb_data.get('total_decisions', '?')} decisions, "
              f"{lb_data.get('total_brains', '?')} brains")

    print("\n" + "=" * 60)
    print("[DONE] All statistics above are the sole source of truth.")


if __name__ == "__main__":
    main()
