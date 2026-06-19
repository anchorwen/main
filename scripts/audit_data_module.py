"""
Data Module Full Integrity Audit
=================================
Checks: PnL Ledger, Labels, Trade Journal, Brain Performance,
Feature Store, Execution State, Governance, Bar Sync, Golden Master
"""
from __future__ import annotations

import json, os, glob, time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def load_jsonl(path):
    if not Path(path).exists():
        return []
    with open(path, encoding='utf-8', errors='replace') as f:
        return [json.loads(l) for l in f if l.strip()]


def load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def check(label, ok=True, msg=""):
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {label}{' -- ' + msg if msg else ''}")
    return ok


now = time.time()
all_ok = True

# ═══════════════════════════════════════════════════════════════
# 1. PnL LEDGER
# ═══════════════════════════════════════════════════════════════
print("=" * 60)
print("1. PnL LEDGER")
print("=" * 60)

for name, path in [("XAU", "data/brain_pnl_ledger.json"), ("BTC", "data_btc/brain_pnl_ledger.json")]:
    d = load_json(path)
    settled = d.get("settled", {})
    total_records = sum(len(v) for v in settled.values() if isinstance(v, list))
    total_brains = len(settled)

    # Dedup
    dup_count = 0
    for brain_id, records in settled.items():
        if not isinstance(records, list):
            continue
        sids = [r.get("signal_id", "") for r in records]
        dup_count += len(sids) - len(set(sids))

    # Phantom (entry==exit)
    phantom_count = 0
    for brain_id, records in settled.items():
        if not isinstance(records, list):
            continue
        for r in records:
            ep = r.get("entry_price", 0) or 0
            cp = r.get("close_price", 0) or 0
            if abs(ep - cp) < 0.01 and ep > 0:
                phantom_count += 1

    # File age
    mtime = os.path.getmtime(path)
    age_min = (now - mtime) / 60

    print(f"\n  {name}: {total_brains} brains, {total_records} records")
    check("No duplicates", dup_count == 0, f"{dup_count} duplicates" if dup_count else "")
    check("No phantoms", phantom_count <= total_records * 0.001, f"{phantom_count} phantoms ({phantom_count/max(total_records,1)*100:.2f}%)" if phantom_count else "")
    check("File fresh", age_min < 30, f"{age_min:.0f}min old" if age_min >= 30 else f"{age_min:.0f}min")

    # Check for identical records across different brains (BTC V6/V7/V8 issue)
    if name == "BTC":
        v6 = settled.get("BTC_Swing_V6_MultiTF_LGB_v2", [])
        v7 = settled.get("BTC_Swing_V7_MultiTF_LGB_v1", [])
        v8 = settled.get("BTC_Swing_V8_MultiTF_LGB_v1", [])
        if v6 and v7 and v8:
            v6_pnls = [round(r.get("pnl_per_unit", 0), 4) for r in v6[-5:]]
            v7_pnls = [round(r.get("pnl_per_unit", 0), 4) for r in v7[-5:]]
            v8_pnls = [round(r.get("pnl_per_unit", 0), 4) for r in v8[-5:]]
            same = v6_pnls == v7_pnls == v8_pnls
            check("V6/V7/V8 records independent", not same, "Still sharing identical records!" if same else "Fixed")


# ═══════════════════════════════════════════════════════════════
# 2. LABELS + JOURNAL CROSS-REFERENCE
# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print("2. LABELS vs JOURNAL CROSS-REFERENCE")
print("=" * 60)

for name, jpath, lpath, sym in [
    ("XAU", "data/live_trade_journal.jsonl", "data/reports/live_labels.jsonl", "XAUUSDc"),
    ("BTC", "data_btc/live_trade_journal.jsonl", "data_btc/reports/live_labels.jsonl", "BTCUSDc"),
]:
    journal = load_jsonl(jpath)
    labels = load_jsonl(lpath)

    # Extract tickets from journal (successful opens: rc=10009, type=1)
    j_tickets = set()
    j_rc_dist: dict[str, int] = Counter()
    for entry in journal:
        detail = entry.get("detail", {})
        if isinstance(detail, dict):
            rc = detail.get("retcode")
            j_rc_dist[str(rc)] += 1
            order = detail.get("order")
            req = detail.get("request", {})
            if isinstance(req, dict) and rc == 10009 and req.get("action") == 1 and req.get("type") == 1:
                if order:
                    j_tickets.add(order)

    # Extract tickets from labels
    l_tickets = set()
    for lb in labels:
        if lb.get("symbol") == sym:
            t = lb.get("position_ticket")
            if t:
                l_tickets.add(t)

    unlabeled = j_tickets - l_tickets
    extra_labels = l_tickets - j_tickets

    print(f"\n  {name}:")
    print(f"    Journal: {len(journal)} entries, {len(j_tickets)} unique open tickets")
    print(f"    Labels:  {len(labels)} entries, {len(l_tickets)} unique tickets")
    print(f"    Unlabeled: {len(unlabeled)} | Extra labels: {len(extra_labels)}")
    print(f"    Journal rc dist: {dict(j_rc_dist.most_common(5))}")  # type: ignore[attr-defined]

    unlabeled_pct = len(unlabeled) / max(len(j_tickets), 1) * 100
    check("Label coverage > 80%", unlabeled_pct < 20, f"{unlabeled_pct:.0f}% unlabeled ({len(unlabeled)}/{len(j_tickets)})")

    # Check label quality
    own_labels = [lb for lb in labels if lb.get("symbol") == sym]
    null_tickets = sum(1 for lb in own_labels if lb.get("position_ticket") is None)
    null_pnl = sum(1 for lb in own_labels if lb.get("pnl") is None and lb.get("label") != "unlabeled")
    check("No null tickets", null_tickets == 0, f"{null_tickets} labels with null ticket")
    check("PnL populated for closed", null_pnl <= len(own_labels) * 0.05, f"{null_pnl} closed labels missing PnL")


# ═══════════════════════════════════════════════════════════════
# 3. EXECUTION STATE
# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print("3. EXECUTION STATE")
print("=" * 60)

for name, path in [("XAU", "data/state/execution_state.json"), ("BTC", "data_btc/state/execution_state.json")]:
    d = load_json(path)
    age_min = (now - os.path.getmtime(path)) / 60

    cb = d.get("circuit_breaker_tripped", False)
    dd = d.get("intraday_dd_active", False)
    stale = d.get("consecutive_stale_cycles", 0)

    print(f"\n  {name}: age={age_min:.0f}min")
    check("No circuit breaker", not cb, f"TRIPPED: {d.get('circuit_breaker_trip_reason','')}")
    check("No intraday DD", not dd, "Active!")
    check("No stale cycles", stale == 0, f"{stale} stale cycles")
    check("File fresh", age_min < 15, f"{age_min:.0f}min old")

    for bname, budget in d.get("budgets", {}).items():
        paused = budget.get("paused", False)
        consec = budget.get("consecutive_losses", 0)
        if paused:
            check(f"{bname} not paused", False, f"PAUSED, consec_losses={consec}")
        if consec >= 5:
            check(f"{bname} consec_losses < 5", False, f"consec_losses={consec}")


# ═══════════════════════════════════════════════════════════════
# 4. GOVERNANCE STATE
# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print("4. GOVERNANCE STATE")
print("=" * 60)

for name, gpath, cpath in [
    ("XAU", "data/governance_state.json", "configs/live.yaml"),
    ("BTC", "data_btc/governance_state.json", "configs/live_btc.yaml"),
]:
    import yaml
    with open(cpath, encoding='utf-8') as f:
        cfg = yaml.safe_load(f)

    gov = load_json(gpath)
    states = gov.get("brain_states", {})

    # Check config-enabled brains that should be at least probation
    config_enabled = set()
    for entry in cfg.get("brains", {}).get("registry_entries", []):
        if entry.get("enabled", False):
            bid = entry.get("path", "").split("/")[-1].replace(".json", "")
            config_enabled.add(bid)

    stuck_candidate = []
    for bid in config_enabled:
        gs = states.get(bid, {})
        if gs.get("status") == "candidate":
            wr = gs.get("performance_metrics", {}).get("win_rate", 0)
            pf = gs.get("performance_metrics", {}).get("profit_factor", 0)
            stuck_candidate.append((bid, wr, pf))

    print(f"\n  {name}: {len(states)} brains tracked, {len(config_enabled)} enabled")
    if stuck_candidate:
        check("All enabled brains promoted", len(stuck_candidate) == 0,
              f"{len(stuck_candidate)} stuck in candidate: {[(b, f'{w:.1%}') for b, w, _ in stuck_candidate]}")
    else:
        check("All enabled brains promoted", True)

    # Check for archived brains still enabled
    archived_enabled = []
    for bid in config_enabled:
        gs = states.get(bid, {})
        if gs.get("status") in ("archived", "retired", "frozen"):
            archived_enabled.append((bid, gs.get("status")))
    if archived_enabled:
        check("No archived brains enabled", len(archived_enabled) == 0,
              f"{archived_enabled}")


# ═══════════════════════════════════════════════════════════════
# 5. FEATURE STORE
# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print("5. FEATURE STORE")
print("=" * 60)

for name, store_dir in [("XAU", "data/feature_store"), ("BTC", "data_btc/feature_store")]:
    for sym in ["XAUUSDc", "BTCUSDc"]:
        for tf in ["M5"]:
            feat_path = f"{store_dir}/records/symbol={sym}/timeframe={tf}/features.jsonl"
            if not os.path.exists(feat_path):
                continue
            mtime = os.path.getmtime(feat_path)
            age_min = (now - mtime) / 60

            if sym == "XAUUSDc" and name == "BTC":
                continue  # BTC tracking XAU is expected

            # Count recent records
            with open(feat_path, encoding='utf-8', errors='replace') as f:
                lines = [l for l in f if l.strip()]

            check(f"{name} {sym}/{tf}", age_min < 15,
                  f"{age_min:.0f}min old, {len(lines)} records" if age_min >= 15 else f"{age_min:.0f}min, {len(lines)} records")


# ═══════════════════════════════════════════════════════════════
# 6. BAR SYNC
# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print("6. BAR SYNC")
print("=" * 60)

for name, path in [("XAU", "data/bar_sync_state.json"), ("BTC", "data_btc/bar_sync_state.json")]:
    d = load_json(path)
    lag = d.get("lag_count", 0)
    last_sync = d.get("last_sync_utc", "")
    age_min = (now - os.path.getmtime(path)) / 60

    check(f"{name} lag=0", lag == 0, f"lag={lag}")
    check(f"{name} fresh", age_min < 10, f"last sync {age_min:.0f}min ago: {last_sync[:19]}")


# ═══════════════════════════════════════════════════════════════
# 7. GOLDEN MASTER
# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print("7. GOLDEN MASTER")
print("=" * 60)

for name, path in [("XAU", "data/golden_master.jsonl"), ("BTC", "data_btc/golden_master.jsonl")]:
    if not os.path.exists(path):
        check(f"{name} exists", False, "MISSING")
        continue
    mtime = os.path.getmtime(path)
    age_min = (now - mtime) / 60

    with open(path, encoding='utf-8', errors='replace') as f:
        lines = [l for l in f if l.strip()]

    # Check for empty/zero records
    empty_count = 0
    for line in lines:
        try:
            d = json.loads(line)
            if not d or all(v == 0 for v in d.values() if isinstance(v, (int, float))):
                empty_count += 1
        except:
            empty_count += 1

    check(f"{name} has records", len(lines) > 0, "EMPTY")
    check(f"{name} no corrupt entries", empty_count == 0, f"{empty_count} corrupt/empty")
    check(f"{name} fresh", age_min < 120, f"{age_min:.0f}min old")


# ═══════════════════════════════════════════════════════════════
# 8. BRAIN PERFORMANCE
# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print("8. BRAIN PERFORMANCE")
print("=" * 60)

for name, path in [("XAU", "data/brain_performance.json"), ("BTC", "data_btc/brain_performance.json")]:
    d = load_json(path)
    brain_ids = d.get("brain_ids", [])
    records = d.get("records", {})

    # Count records per brain
    rec_counts = {bid: len(records.get(bid, [])) for bid in brain_ids}
    empty_brains = [bid for bid, c in rec_counts.items() if c == 0]

    check(f"{name} {len(brain_ids)} brains tracked", len(brain_ids) > 0, "No brains!")
    if empty_brains:
        check(f"{name} all brains have records", len(empty_brains) == 0, f"{len(empty_brains)} empty: {empty_brains[:5]}")

    # Check file age
    age_min = (now - os.path.getmtime(path)) / 60
    check(f"{name} fresh", age_min < 60, f"{age_min:.0f}min old")


# ═══════════════════════════════════════════════════════════════
# 9. DATA HEALTH MONITOR
# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print("9. DATA HEALTH STATE")
print("=" * 60)

for name, path in [("XAU", "data/state/data_health_state.json"), ("BTC", "data_btc/state/data_health_state.json")]:
    if not os.path.exists(path):
        check(f"{name} data_health_state", False, "MISSING")
        continue

    d = load_json(path)
    age_min = (now - os.path.getmtime(path)) / 60

    critical = d.get("critical_fail_count", d.get("data_health_critical_fail_count", 0))
    warn = d.get("warn_count", d.get("data_health_warn_count", 0))

    check(f"{name} no critical failures", critical == 0, f"{critical} critical!")
    check(f"{name} warnings <= 2", warn <= 2, f"{warn} warnings")
    check(f"{name} fresh", age_min < 30, f"{age_min:.0f}min old")


# ═══════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print("SUMMARY")
print("=" * 60)
print(f"  Audit complete at {datetime.now().isoformat()[:19]}")
print("  All checks above are the sole source of truth per Iron Law #11.")
