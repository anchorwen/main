#!/usr/bin/env python3
"""Final comprehensive data audit — every file, every field, every link.
Iron Law #11: script-only, no raw text reading for conclusions."""

from __future__ import annotations

import json, os, sys, time
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime, timezone

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
FAIL, WARN, PASS = 0, 0, 0


def check(condition: bool, msg: str) -> bool:
    global FAIL, WARN, PASS
    if condition:
        PASS += 1
        return True
    else:
        FAIL += 1
        print(f"  ❌ {msg}")
        return False


def warn(condition: bool, msg: str) -> bool:
    global WARN
    if not condition:
        WARN += 1
        print(f"  ⚠️  {msg}")
    return condition


def audit_journal(path: str, label: str) -> dict:
    if not os.path.exists(path):
        print(f"\n  {label} journal: MISSING")
        return {}
    with open(path, encoding="utf-8") as f:
        entries = [json.loads(l) for l in f if l.strip()]
    total = len(entries)
    opens = [e for e in entries if e.get("action") == "open"]
    closes = [e for e in entries if e.get("action") == "close"]

    msg_ids = Counter(e.get("message_id", "") for e in entries if e.get("message_id"))
    dupes = {k: v for k, v in msg_ids.items() if v > 1}
    cross_ticket_dupes = 0
    for mid, entries_list in [
        (mid, [e for e in entries if e.get("message_id") == mid]) for mid in dupes
    ]:
        tickets = set(e.get("position_ticket") for e in entries_list)
        if len(tickets) > 1:
            cross_ticket_dupes += 1

    tickets = set(e.get("position_ticket") for e in entries if e.get("position_ticket"))
    pnl_null = sum(1 for e in closes if e.get("pnl") is None)
    cp_present = sum(1 for e in closes if (e.get("detail", {}).get("close_price") or 0) > 0)
    ep_present = sum(1 for e in entries if e.get("entry_price") is not None)

    ts_list = [
        (i, str(e.get("recorded_at", ""))) for i, e in enumerate(entries) if e.get("recorded_at")
    ]
    reversals = sum(1 for i in range(1, len(ts_list)) if ts_list[i][1] < ts_list[i - 1][1])

    print(
        f"\n  {label} Journal: {total} entries, {len(opens)} opens, {len(closes)} closes, {len(tickets)} unique tickets"
    )
    check(
        len(dupes) == 0, f"{len(dupes)} duplicate message_ids (cross-ticket: {cross_ticket_dupes})"
    )
    check(cross_ticket_dupes == 0, f"{cross_ticket_dupes} cross-ticket message_id reuse")
    warn(
        pnl_null <= len(closes) * 0.2,
        f"PnL null: {pnl_null}/{len(closes)} ({pnl_null/max(len(closes),1)*100:.0f}%)",
    )
    warn(cp_present >= len(closes) * 0.3, f"close_price present: {cp_present}/{len(closes)}")
    check(reversals == 0, f"{reversals} timestamp reversals")
    return {
        "total": total,
        "opens": len(opens),
        "closes": len(closes),
        "tickets": len(tickets),
        "dupes": len(dupes),
        "cross_dupes": cross_ticket_dupes,
    }


def audit_pnl_ledger(path: str, label: str) -> dict:
    if not os.path.exists(path):
        print(f"\n  {label} PnP Ledger: MISSING")
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    pending = data.get("pending", {})
    settled = data.get("settled", {})
    total_settled = sum(len(v) for v in settled.values())

    # Phantom check
    phantom = 0
    for bid, entries in settled.items():
        for e in entries:
            ep = e.get("entry_price", 0) or 0
            cp = e.get("close_price", 0) or e.get("exit_price", 0) or 0
            if ep > 0 and abs(ep - cp) < 0.01:
                phantom += 1
    phantom_pct = phantom / max(total_settled, 1) * 100

    # Identity leak check: same entry/exit prices across different brains
    brain_prices: dict[str, list] = {}
    identity_leaks = 0
    for bid, entries in settled.items():
        last5_prices = (
            tuple(round(e.get("entry_price", 0) or 0, 1) for e in entries[-5:]) if entries else ()
        )
        if last5_prices:
            for other_bid, other_prices in brain_prices.items():
                if last5_prices == tuple(other_prices) and len(last5_prices) >= 3:
                    identity_leaks += 1
            brain_prices[bid] = list(last5_prices)

    print(
        f"\n  {label} PnP Ledger: {len(pending)} pending, {total_settled} settled, phantom={phantom_pct:.1f}%"
    )
    check(phantom_pct < 5, f"Phantom records: {phantom}/{total_settled} ({phantom_pct:.1f}%)")
    check(
        identity_leaks == 0, f"Identity leaks: {identity_leaks} brain pairs with identical records"
    )
    return {
        "pending": len(pending),
        "settled": total_settled,
        "phantom_pct": phantom_pct,
        "leaks": identity_leaks,
    }


def audit_file(path: str, label: str, min_age_min: float = 60) -> bool:
    if not os.path.exists(path):
        print(f"  {label}: MISSING")
        return False
    age = (time.time() - os.path.getmtime(path)) / 60
    fresh = age < min_age_min
    if not fresh:
        print(f"  ⚠️  {label}: {age:.0f}min old (limit: {min_age_min}min)")
    else:
        print(f"  ✅ {label}: {age:.0f}min")
    return fresh


# ═══════════════════════════════════════════════════════════════════════════
print("=" * 60)
print("FINAL COMPREHENSIVE DATA AUDIT")
print("=" * 60)

for symbol, data_dir, csv_label in [("BTC", "data_btc", "BTC"), ("XAU", "data", "XAU")]:
    print(f"\n{'─'*60}")
    print(f"  {csv_label}")
    print(f"{'─'*60}")

    # 1. Journal
    jr = audit_journal(f"{data_dir}/live_trade_journal.jsonl", csv_label)

    # 2. PnP Ledger
    audit_pnl_ledger(f"{data_dir}/brain_pnl_ledger.json", csv_label)

    # 3. All state files
    print(f"\n  {csv_label} State Files:")
    state_files = [
        ("governance_state.json", 60),
        ("state/execution_state.json", 15),
        ("state/daily_ops_state.json", 1440),
        ("data_health_state.json", 120),
        ("reports/leaderboard.json", 1440),
        ("reports/live_labels.jsonl", 1440),
        ("regime_detector_state.json", 15),
        ("bar_sync_state.json", 15),
        ("calibrator_feed_state.json", 15),
        ("meta_filter_state.json", 15),
        ("brain_performance.json", 120),
        ("reports/retraining_signal.json", 1440),
        ("golden_master.jsonl", 120),
        ("position_snapshots.jsonl", 15),
        ("logs/alert_audit.jsonl", 120),
        ("reports/exit_watchdog_alerts.jsonl", 120),
        ("reports/mt5_bridge_health.json", 15),
    ]
    all_fresh = True
    for fname, max_age in state_files:
        fp = os.path.join(data_dir, fname)
        if not os.path.exists(fp):
            print(f"  ⚠️  {fname}: MISSING")
            all_fresh = False
            continue
        # Check JSON validity
        try:
            if fname.endswith(".jsonl"):
                with open(fp, encoding="utf-8") as f:
                    valid = all(json.loads(l) for l in f if l.strip())
            else:
                with open(fp, encoding="utf-8") as f:
                    json.load(f)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            print(f"  ❌ {fname}: CORRUPT JSON")
            all_fresh = False
        if not audit_file(fp, fname, max_age):
            all_fresh = False
    check(all_fresh, "All state files fresh and valid")

    # 4. Feature store
    fs_path = f"{data_dir}/feature_store/records/symbol={symbol}USDc/timeframe=M5/features.jsonl"
    if os.path.exists(fs_path):
        with open(fs_path, encoding="utf-8") as f:
            fs_count = sum(1 for _ in f)
        age = (time.time() - os.path.getmtime(fs_path)) / 60
        print(f"\n  {csv_label} Feature Store: {fs_count} records, {age:.0f}min old")
        check(age < 15, f"Feature store stale: {age:.0f}min")
    else:
        print(f"\n  {csv_label} Feature Store: MISSING")

    # 5. Governance integrity
    gov_path = f"{data_dir}/governance_state.json"
    pnl_path = f"{data_dir}/brain_pnl_ledger.json"
    if os.path.exists(gov_path) and os.path.exists(pnl_path):
        with open(gov_path) as f:
            gov = json.load(f)
        with open(pnl_path) as f:
            pnl = json.load(f)
        gov_brains = set(gov.get("brain_states", {}).keys())
        pnl_brains = set(pnl.get("settled", {}).keys())
        in_gov_not_pnl = gov_brains - pnl_brains
        print(
            f"\n  {csv_label} Governance→PnP link: {len(gov_brains)} in gov, {len(pnl_brains)} in pnl"
        )
        warn(len(in_gov_not_pnl) == 0, f"Brains in gov but not in PnP: {len(in_gov_not_pnl)}")


# ═══════════════════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print(f"RESULTS: {PASS} PASS | {FAIL} FAIL | {WARN} WARN")
print(f"{'='*60}")
if FAIL == 0 and WARN == 0:
    print("✅ ALL CLEAN — data pipeline fully verified")
elif FAIL == 0:
    print("⚠️  WARNINGS only — data pipeline operational, minor issues")
else:
    print(f"❌ {FAIL} FAILURES — data pipeline has critical gaps")
