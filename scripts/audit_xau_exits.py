#!/usr/bin/env python3
"""XAU Exit Quality Audit — Iron Law #11.

Audits: PnL distribution, exit reasons, holding time, direction bias,
win rate by exit reason, brain attribution, trail telemetry coverage.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc


def load_journal(data_dir: str) -> list[dict]:
    path = Path(data_dir) / "live_trade_journal.jsonl"
    if not path.exists():
        return []
    trades = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return trades


def main() -> int:
    import io as _io

    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")  # FIX-20260611-022
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    journal = load_journal(args.data_dir)

    # Filter to closes
    closes = [t for t in journal if t.get("action") == "close"]
    print("=== XAU Exit Audit ===")
    print(f"  Total journal entries: {len(journal)}")
    print(f"  Close entries: {len(closes)}")

    # ── Q1: PnL Distribution ──
    print("\n── Q1: PnL Distribution ──")
    pnls = [t.get("pnl") for t in closes if t.get("pnl") is not None]
    pnl_null = sum(1 for t in closes if t.get("pnl") is None)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    breakevens = sum(1 for p in pnls if p == 0)
    total_pnl = sum(pnls)

    print(f"  PnL null: {pnl_null}/{len(closes)} ({pnl_null/max(len(closes),1)*100:.1f}%)")
    print(f"  Wins: {wins} | Losses: {losses} | Breakevens: {breakevens}")
    if pnls:
        print(f"  Win rate: {wins/len(pnls)*100:.1f}%")
        print(f"  Total PnL: ${total_pnl:.2f}")
        print(f"  Avg win: ${sum(p for p in pnls if p>0)/max(wins,1):.2f}")
        print(f"  Avg loss: ${sum(p for p in pnls if p<0)/max(losses,1):.2f}")

    # ── Q2: Exit Reasons ──
    print("\n── Q2: Exit Reasons ──")
    exit_labels = Counter(str(t.get("label", "?")) for t in closes)
    for label, count in exit_labels.most_common(15):
        # Show PnL for this label
        label_pnls = [t.get("pnl") for t in closes if str(t.get("label","?")) == label if t.get("pnl") is not None]
        label_wr = sum(1 for p in label_pnls if p > 0) / max(len(label_pnls), 1) if label_pnls else 0
        label_total = sum(label_pnls) if label_pnls else 0
        print(f"  [{count:3d}] {label:40s} WR={label_wr:.1%} PnL=${label_total:.2f}")

    # ── Q3: Exit by Strategy ──
    print("\n── Q3: Exit by Strategy ──")
    by_strategy = defaultdict(list)
    for t in closes:
        s = t.get("strategy", "unknown")
        pnl = t.get("pnl")
        if pnl is not None:
            by_strategy[s].append(pnl)
    for s, spnls in sorted(by_strategy.items()):
        wr = sum(1 for p in spnls if p > 0) / max(len(spnls), 1)
        print(f"  {s:25s}: {len(spnls):>4d} trades, WR={wr:.1%}, PnL=${sum(spnls):.2f}")

    # ── Q4: Direction Bias in Exits ──
    print("\n── Q4: Exit Direction Bias ──")
    dir_pnls = defaultdict(list)
    for t in closes:
        side = t.get("side", "?")
        pnl = t.get("pnl")
        if pnl is not None and side in ("long", "short"):
            dir_pnls[side].append(pnl)
    for side, spnls in sorted(dir_pnls.items()):
        wr = sum(1 for p in spnls if p > 0) / max(len(spnls), 1)
        print(f"  {side:6s}: {len(spnls):>4d} closes, WR={wr:.1%}, PnL=${sum(spnls):.2f}")

    # ── Q5: Recent Closes (last 20) ──
    print("\n── Q5: Last 20 Closes ──")
    sorted_closes = sorted(closes, key=lambda t: str(t.get("recorded_at", "")), reverse=True)
    for t in sorted_closes[:20]:
        pnl = t.get("pnl")
        pnl_s = f"${pnl:.2f}" if pnl is not None else "$NULL"
        label = str(t.get("label", "?"))[:35]
        strategy = t.get("strategy", "?")[:15]
        side = t.get("side", "?")
        ticket = t.get("position_ticket", "?")
        recorded = str(t.get("recorded_at", "?"))[:19]
        entry = t.get("entry_price", "?")
        exit_p = t.get("exit_price", "?")
        trail = "[TRAIL]" if t.get("trail_contribution") else ""
        brain = "[BRAIN]" if t.get("brain_ids") else ""
        print(f"  {recorded} {strategy:15s} {side:5s} tkt={str(ticket):>15s} pnl={pnl_s:>8s} "
              f"entry={str(entry):>10s} exit={str(exit_p):>10s} {label:35s} {trail} {brain}")

    # ── Q6: Data Quality Flags ──
    print("\n── Q6: Data Quality Flags ──")
    flags = []
    if pnl_null > len(closes) * 0.2:
        flags.append(f"HIGH_PNL_NULL: {pnl_null}/{len(closes)}")
    missing_entry = sum(1 for t in closes if not t.get("entry_price"))
    if missing_entry > len(closes) * 0.5:
        flags.append(f"MISSING_ENTRY_PRICE: {missing_entry}/{len(closes)}")
    missing_exit = sum(1 for t in closes if not t.get("exit_price"))
    if missing_exit > len(closes) * 0.5:
        flags.append(f"MISSING_EXIT_PRICE: {missing_exit}/{len(closes)}")
    no_brain = sum(1 for t in closes if not t.get("brain_ids"))
    if no_brain > len(closes) * 0.5:
        flags.append(f"NO_BRAIN_IDS: {no_brain}/{len(closes)}")
    no_trail = sum(1 for t in closes if not t.get("trail_contribution"))
    if no_trail > len(closes) * 0.8:
        flags.append(f"NO_TRAIL_TELEMETRY: {no_trail}/{len(closes)}")

    if flags:
        for f in flags:
            print(f"  🚩 {f}")
    else:
        print("  ✅ No critical data quality flags")

    return 0


if __name__ == "__main__":
    sys.exit(main())
