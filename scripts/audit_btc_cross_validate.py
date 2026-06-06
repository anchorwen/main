#!/usr/bin/env python3
"""BTC 机构化交叉验证脚本 v2.0 — 修正字段映射"""

import glob
import json
import os
import re
from collections import defaultdict


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def safe_float(v, default=0.0):
    try:
        return float(v) if v is not None and v != "None" else default
    except:
        return default


def banner(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


yesterday = "2026-06-05"
today = "2026-06-06"

# ═══════════════════════════════════════════════
# SOURCE 1: Journal (action=open/close, ack_status=accepted)
# ═══════════════════════════════════════════════
banner("SOURCE 1: live_trade_journal.jsonl (SSOT)")

journal = load_jsonl("d:/future/data_btc/live_trade_journal.jsonl")
opens = [j for j in journal if j.get("action") == "open" and j.get("ack_status") == "accepted"]
closes = [j for j in journal if j.get("action") == "close" and j.get("ack_status") == "accepted"]
rejected = [j for j in journal if j.get("ack_status") == "rejected"]

print(f"  Total entries: {len(journal)}")
print(f"  Accepted opens:   {len(opens)}")
print(f"  Accepted closes:  {len(closes)}")
print(f"  Rejected:         {len(rejected)}")

trades_by_date = defaultdict(list)
for o in opens:
    ts = o.get("recorded_at", "")[:10]
    trades_by_date[ts].append(o)

for date in sorted(trades_by_date.keys()):
    day_opens = trades_by_date[date]
    day_pnl = sum(safe_float(o.get("pnl")) for o in day_opens)
    day_wins = sum(1 for o in day_opens if safe_float(o.get("pnl")) > 0)
    day_loss = sum(1 for o in day_opens if safe_float(o.get("pnl")) < 0)
    print(f"  {date}: {len(day_opens)} opens, {day_wins}W/{day_loss}L, PnL=${day_pnl:+.2f}")

# Yesterday detail
banner(f"YESTERDAY ({yesterday}) DETAIL")
day_opens = trades_by_date.get(yesterday, [])
if day_opens:
    for o in day_opens:
        ts = o.get("recorded_at", "")[:19]
        print(
            f"  {ts} | ticket={str(o.get('position_ticket','?'))} | {o.get('strategy','?'):15s} | {o.get('side','?'):5s} | vol={o.get('volume','?')} | sl={o.get('sl','?')} | tp={o.get('tp','?')}"
        )
else:
    print("  NO TRADES YESTERDAY")

# Today
banner(f"TODAY ({today}) STATUS")
today_opens = trades_by_date.get(today, [])
today_closes = [c for c in closes if c.get("recorded_at", "")[:10] == today]
print(f"  Opens today:  {len(today_opens)}")
print(f"  Closes today: {len(today_closes)}")
for o in today_opens:
    print(
        f"  {o.get('recorded_at','')[:19]} | ticket={o.get('position_ticket','?')} | {o.get('side','?')} | vol={o.get('volume','?')}"
    )

# Open positions
open_tickets = {}
for o in opens:
    open_tickets[o.get("position_ticket", "")] = o
for c in closes:
    t = c.get("position_ticket", "")
    if t in open_tickets:
        del open_tickets[t]
print(f"  Currently open: {len(open_tickets)} positions")
for t, o in open_tickets.items():
    print(
        f"  ticket={t} | {o.get('strategy','?')} | {o.get('side','?')} | time={o.get('recorded_at','')[:19]}"
    )

# ═══════════════════════════════════════════════
# SOURCE 2: PnL Ledger
# ═══════════════════════════════════════════════
banner("SOURCE 2: PnL Ledger")
ledger = load_json("d:/future/data_btc/brain_pnl_ledger.json")
settled_str = ledger.get("settled", "{}")
if isinstance(settled_str, dict):
    settled = settled_str
else:
    settled = eval(settled_str)

for brain_id, records in settled.items():
    if isinstance(records, list) and records:
        recent = [r for r in records if r.get("entry_time", "")[:10] in (yesterday, today)]
        if recent:
            wins = sum(1 for r in recent if r.get("is_win"))
            total_pnl = sum(r.get("pnl_per_unit", 0) for r in recent)
            print(
                f"  {brain_id}: ledger={len(recent)} | journal_opens={len(day_opens)} | {wins}W/{len(recent)-wins}L | PnL=${total_pnl:+.2f}"
            )

# Ghost signals
banner("P2.6 GHOST SIGNALS")
pending_str = ledger.get("pending", "{}")
pending = pending_str if isinstance(pending_str, dict) else eval(pending_str)
print(f"  Pending records: {len(pending)}")
for brain_id, records in pending.items():
    recs = records if isinstance(records, list) else [records]
    for r in recs:
        print(
            f"  {brain_id}: signal_time={r.get('entry_time','')[:19]} | {r.get('direction','?')} | entry=${r.get('entry_price',0):.2f}"
        )

# ═══════════════════════════════════════════════
# SOURCE 3: Labels
# ═══════════════════════════════════════════════
banner("SOURCE 3: live_labels.jsonl")
labels = load_jsonl("d:/future/data_btc/reports/live_labels.jsonl")
recent_labels = [
    l for l in labels if str(l.get("timestamp", l.get("time", "")))[:10] in (yesterday, today)
]
print(f"  Total labels: {len(labels)}, Recent: {len(recent_labels)}")
label_counts = defaultdict(int)
for l in recent_labels:
    label_counts[str(l.get("label", "unknown"))] += 1
for k, v in sorted(label_counts.items()):
    print(f"    {k}: {v}")

# ═══════════════════════════════════════════════
# SOURCE 4: Golden Master
# ═══════════════════════════════════════════════
banner("SOURCE 4: Golden Master")
for label, path in [
    ("BTC", "d:/future/data_btc/golden_master.jsonl"),
    ("XAU", "d:/future/data/golden_master.jsonl"),
]:
    if os.path.exists(path):
        size = os.path.getsize(path)
        lines = sum(1 for _ in open(path))
        print(f"  {label}: {lines} lines, {size:,} bytes")
        if lines > 0:
            last = json.loads(open(path).readlines()[-1])
            print(f"    Latest: {str(last.get('timestamp',last.get('time','?')))[:19]}")
            print(f"    Keys: {list(last.keys())[:6]}")
    else:
        print(f"  {label}: NOT FOUND")

# ═══════════════════════════════════════════════
# SOURCE 5: Leaderboard
# ═══════════════════════════════════════════════
banner("SOURCE 5: Leaderboard")
for label, path in [
    ("BTC", "d:/future/data_btc/reports/leaderboard.json"),
    ("XAU", "d:/future/data/reports/leaderboard.json"),
]:
    lb = load_json(path)
    brains = lb.get("brains", lb.get("entries", []))
    updated = str(lb.get("updated_at", lb.get("generated_at", "?")))[:19]
    print(f"  {label}: {len(brains)} brains, updated={updated}")
    if not brains:
        print("    ⚠️  EMPTY")

# ═══════════════════════════════════════════════
# KEY FINDING 1: entry_spread
# ═══════════════════════════════════════════════
banner("KEY 1: BTC entry_spread")
spreads = []
for r in settled.get("BTC_Swing_V4", []):
    es = r.get("entry_spread", 0)
    if es > 0:
        spreads.append(es)
if spreads:
    unique = sorted(set(spreads))
    print(f"  Samples: {len(spreads)}")
    print(f"  Unique values: {len(unique)} = {unique}")
    print(f"  Mean: {sum(spreads)/len(spreads):.4f}")
    if len(unique) <= 3:
        print(f"  🔴 PROBLEM: Only {len(unique)} unique spread values — NOT real-time bid/ask")

# ═══════════════════════════════════════════════
# KEY FINDING 2: confidence discriminability
# ═══════════════════════════════════════════════
banner("KEY 2: BTC confidence vs PnL")
win_confs, loss_confs = [], []
for r in settled.get("BTC_Swing_V4", []):
    conf = r.get("confidence", 0)
    if r.get("is_win"):
        win_confs.append(conf)
    else:
        loss_confs.append(conf)
if win_confs and loss_confs:
    w_mean = sum(win_confs) / len(win_confs)
    l_mean = sum(loss_confs) / len(loss_confs)
    print(
        f"  WIN  (n={len(win_confs)}): mean={w_mean:.4f} min={min(win_confs):.4f} max={max(win_confs):.4f} std={__import__('statistics').stdev(win_confs):.4f}"
    )
    print(
        f"  LOSS (n={len(loss_confs)}): mean={l_mean:.4f} min={min(loss_confs):.4f} max={max(loss_confs):.4f} std={__import__('statistics').stdev(loss_confs):.4f}"
    )
    diff = abs(w_mean - l_mean)
    print(f"  Mean diff: {diff:.4f}")
    if diff < 0.02:
        print("  🔴 CRITICAL: ZERO discriminability")
    elif diff < 0.05:
        print("  🟡 WARNING: Weak discriminability")

# ═══════════════════════════════════════════════
# KEY FINDING 3: brain_flip frequency
# ═══════════════════════════════════════════════
banner("KEY 3: brain_flip exit pattern")
flip_closes = [
    c
    for c in closes
    if "brain_flip" in str(c.get("label", "")) or "brain_flip" in str(c.get("comment", ""))
]
print(f"  brain_flip close events: {len(flip_closes)}")
for fc in flip_closes[-5:]:
    ts = fc.get("recorded_at", "")[:19]
    pnl = safe_float(fc.get("pnl"))
    label = fc.get("label", fc.get("comment", "?"))
    print(f"  {ts} | ticket={fc.get('position_ticket','?')} | {str(label)[:60]} | pnl=${pnl:+.2f}")

# ═══════════════════════════════════════════════
# KEY FINDING 4: reentry TTL gap
# ═══════════════════════════════════════════════
banner("KEY 4: Reentry TTL gap")
latest_logs = sorted(glob.glob("d:/future/data_btc/logs/intent_*.log"))[-1]
with open(latest_logs) as f:
    log_lines = [json.loads(l) for l in f if l.strip()]
reentry_checks = [l for l in log_lines if l.get("event") == "reentry_check"]
if reentry_checks:
    lc = reentry_checks[-1]
    elapsed = lc.get("elapsed_since_exit_s", 0)
    ttl = lc.get("ttl_seconds", 0)
    reason = lc.get("reason", "")
    conf = lc.get("confidence", 0)
    match = re.search(r"need_([0-9.]+)", reason)
    required = float(match.group(1)) if match else 0
    print(f"  elapsed: {elapsed:.0f}s = {elapsed/3600:.1f}h")
    print(f"  TTL: {ttl:.0f}s = {ttl/3600:.1f}h")
    print(f"  remaining: {max(0,ttl-elapsed):.0f}s = {max(0,ttl-elapsed)/60:.0f}min")
    print(f"  current conf: {conf:.4f}")
    print(f"  required conf: {required:.3f}")
    print(f"  gap: {required-conf:.3f}")
    print(f"  reason: {reason}")

# ═══════════════════════════════════════════════
# FINAL SUMMARY
# ═══════════════════════════════════════════════
banner("CROSS-VALIDATED SUMMARY")
print(f"  BTC trades yesterday (journal): {len(trades_by_date.get(yesterday,[]))}")
print(f"  BTC trades today (journal):     {len(trades_by_date.get(today,[]))}")
print(f"  BTC open positions now:         {len(open_tickets)}")
print(
    f"  Golden Master BTC: {'OK' if os.path.exists('d:/future/data_btc/golden_master.jsonl') else 'MISSING'}"
)
print(
    f"  Golden Master XAU: {'OK' if os.path.exists('d:/future/data/golden_master.jsonl') else 'MISSING'}"
)
print(f"  entry_spread unique: {len(set(spreads)) if spreads else 0}")
print(f"  confidence diff: {diff:.4f}" if "diff" in dir() else "  confidence diff: N/A")
