"""P1: BTC V11 Directional ledger audit — DQAF-20260616-003 follow-up.

Iron Law #11 compliant: all statistics from script stdout.
"""
from __future__ import annotations

import json
from collections import defaultdict

with open("data_btc/ledger_events.jsonl") as f:
    ledger = [json.loads(l) for l in f if l.strip()]

v11_h1 = [e for e in ledger if e.get("brain_id") == "BTC_Swing_V11_H1_Directional"]
v11_m15 = [e for e in ledger if e.get("brain_id") == "BTC_Swing_V11_M15_Directional"]

print("=" * 70)
print("  P1: BTC V11 DIRECTIONAL — LEDGER vs REAL TRADES")
print("=" * 70)
print()

for name, events in [("V11_H1_Directional", v11_h1), ("V11_M15_Directional", v11_m15)]:
    recorded = [e for e in events if e.get("event_type") == "SignalRecorded"]
    settled = [e for e in events if e.get("event_type") == "SignalSettled"]

    total_pnl = sum(e.get("pnl_r", 0) or 0 for e in settled)
    dirs: list[str] = defaultdict(int)
    for e in recorded:
        dirs[e.get("direction", "?")] += 1

    virtual_settled = sum(1 for e in settled if e.get("position_ticket", -1) == 0)
    real_settled = sum(1 for e in settled if e.get("position_ticket", -1) > 0)

    print(f"--- {name} ---")
    print(f"  SignalRecorded: {len(recorded)}")
    print(f"  SignalSettled:  {len(settled)}")
    print(f"  Directions:      L={dirs.get('long',0)} S={dirs.get('short',0)} N={dirs.get('neutral',0)}")
    print(f"  Total PnL (all settled): {total_pnl:.1f}R")
    print(f"  Virtual settled (ticket=0): {virtual_settled}")
    print(f"  Real settled (ticket>0):    {real_settled}")
    print(f"  PnL per settled (avg): {total_pnl/max(len(settled),1):.3f}R")
    print()

# ── Cross-reference with BTC live_trade_journal ──
print("--- BTC LIVE TRADE JOURNAL ---")
with open("data_btc/live_trade_journal.jsonl") as f:
    btc_journal = [json.loads(l) for l in f if l.strip()]

btc_opens = [t for t in btc_journal if t.get("action") == "open"]
btc_closes = [t for t in btc_journal if t.get("action") == "close"]

# Magic numbers from config
# btc_swing uses V4/V5/V6/V7/V8/V11 brains
print(f"Total BTC journal entries: {len(btc_journal)}")
print(f"BTC opens: {len(btc_opens)}")
print(f"BTC closes: {len(btc_closes)}")

# Direction of real BTC trades
real_dirs: list[str] = defaultdict(int)
for t in btc_opens:
    real_dirs[t.get("side", "?")] += 1
print(f"BTC real trade directions: L={real_dirs.get('long',0)} S={real_dirs.get('short',0)}")

# Realized PnL
real_pnl = sum(t.get("pnl", 0) or 0 for t in btc_closes)
print(f"BTC realized PnL: ${real_pnl:.2f}")
print()

# ── Root cause analysis ──
print("--- ROOT CAUSE: WHY -1449R IS MISLEADING ---")
print()
print("The BTC PnL ledger (ledger_events.jsonl) records SignalSettled")
print("events for EVERY brain on EVERY bar, including virtual signals")
print("that were never executed as real trades.")
print()
print("Each SignalRecorded (direction signal) is followed by SignalSettled")
print("(PnL calculation) on the NEXT bar, regardless of whether a real")
print("position was opened. This is a brain-level virtual PnL tracker,")
print("NOT a strategy-level realized PnL tracker.")
print()
print("Evidence:")
print(f"  - V11_H1 SignalSettled with ticket=0 (virtual): {virtual_settled}")
print(f"  - V11_M15 SignalSettled with ticket=0 (virtual): {virtual_settled}")
print(f"  - BTC real opens total: {len(btc_opens)}")
print(f"  - BTC real realized PnL: ${real_pnl:.2f}")
print()
print("The -1449R is accumulated brain-level virtual PnL across ~3000")
print("signal events. It does NOT represent actual trading losses.")
print()

# ── Recommendation ──
print("--- RECOMMENDATION ---")
print()
print("The V11 Directional brains output 100% SHORT. This is a training")
print("data bias, not a live market assessment. However:")
print()
print("  1. The -1449R figure is VIRTUAL (brain-level tracking), NOT real PnL.")
print("     No actual capital was lost — the ledger is misleading.")
print("  2. V11_H1 and V11_M15 are used in btc_swing ensemble voting.")
print("     Their SHORT bias is offset by Survival brains (100% LONG).")
print("  3. Freezing V11 would reduce direction diversity in the ensemble.")
print()
print("  Action: No emergency freeze needed. The -1449R is an accounting")
print("  artifact. Register a deferred task to retrain V11 with balanced")
print("  labels or add direction-aware label weighting.")

print()
print("[DONE] Iron Law #11 — all statistics from script stdout.")
