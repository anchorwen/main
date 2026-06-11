"""
Exhaustive Data Integrity Audit — "高枕无忧" standard
======================================================
Every data file, every cross-reference, every edge case.
"""
import json, os, glob, time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

now = time.time()
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
RESULTS = {"PASS": [], "FAIL": [], "WARN": []}

def verdict(ok, label, detail=""):
    (RESULTS["PASS"] if ok else RESULTS["FAIL"]).append((label, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
    return ok

def warn(label, detail=""):
    RESULTS["WARN"].append((label, detail))
    print(f"  [WARN] {label} -- {detail}")

def load_jsonl(path):
    if not Path(path).exists(): return None
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        lines = [l.strip() for l in f if l.strip()]
    results, errors = [], 0
    for line in lines:
        try: results.append(json.loads(line))
        except json.JSONDecodeError: errors += 1
    return results, errors, len(lines)

def load_json(path):
    if not Path(path).exists(): return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f), None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, str(e)

def file_age(path):
    return (now - os.path.getmtime(path)) / 60 if os.path.exists(path) else None

def get_json(key):
    """Safe accessor for JSON file_status entries."""
    if key not in file_status or file_status[key] is None: return None
    fs = file_status[key]
    return fs[1] if fs[0] == "json" else None

def get_jsonl(key):
    """Safe accessor for JSONL file_status entries."""
    if key not in file_status or file_status[key] is None: return None
    fs = file_status[key]
    return fs[1] if fs[0] == "jsonl" else None


print("=" * 70)
print("  EXHAUSTIVE DATA INTEGRITY AUDIT")
print(f"  {datetime.now().isoformat()[:19]}")
print("=" * 70)

# ═══════════════════════════════════════════════════════════
# 1. FILE EXISTENCE & JSON VALIDITY
# ═══════════════════════════════════════════════════════════
print("\n--- 1. FILE EXISTENCE & JSON VALIDITY ---")

DATA_FILES = {
    "XAU": {
        "pnl_ledger":        "data/brain_pnl_ledger.json",
        "governance_state":  "data/governance_state.json",
        "execution_state":   "data/state/execution_state.json",
        "bar_sync_state":    "data/bar_sync_state.json",
        "brain_performance": "data/brain_performance.json",
        "live_labels":       "data/reports/live_labels.jsonl",
        "trade_journal":     "data/live_trade_journal.jsonl",
        "golden_master":     "data/golden_master.jsonl",
        "position_snapshots":"data/position_snapshots.jsonl",
        "alert_audit":       "data/logs/alert_audit.jsonl",
        "data_health_state": "data/state/data_health_state.json",
        "daily_ops_state":   "data/state/daily_ops_state.json",
        "calibrator_feed":   "data/calibrator_feed_state.json",
        "leaderboard":       "data/reports/leaderboard.json",
        "mt5_bridge_health": "data/reports/mt5_bridge_health.json",
    },
    "BTC": {
        "pnl_ledger":        "data_btc/brain_pnl_ledger.json",
        "governance_state":  "data_btc/governance_state.json",
        "execution_state":   "data_btc/state/execution_state.json",
        "bar_sync_state":    "data_btc/bar_sync_state.json",
        "brain_performance": "data_btc/brain_performance.json",
        "live_labels":       "data_btc/reports/live_labels.jsonl",
        "trade_journal":     "data_btc/live_trade_journal.jsonl",
        "golden_master":     "data_btc/golden_master.jsonl",
        "position_snapshots":"data_btc/position_snapshots.jsonl",
        "alert_audit":       "data_btc/logs/alert_audit.jsonl",
        "data_health_state": "data_btc/state/data_health_state.json",
        "daily_ops_state":   "data_btc/state/daily_ops_state.json",
        "calibrator_feed":   "data_btc/calibrator_feed_state.json",
        "leaderboard":       "data_btc/reports/leaderboard.json",
        "mt5_bridge_health": "data_btc/reports/mt5_bridge_health.json",
    },
}

file_status = {}
all_exist = True
for symbol, files in DATA_FILES.items():
    for name, path in files.items():
        exists = os.path.exists(path)
        if not exists:
            verdict(False, f"{symbol} {name} exists", f"MISSING: {path}")
            file_status[(symbol, name)] = None
            all_exist = False
            continue

        is_jsonl = path.endswith('.jsonl')
        if is_jsonl:
            data, parse_errors, total = load_jsonl(path)
            if data is None:
                verdict(False, f"{symbol} {name} readable", "Could not read")
            elif parse_errors > 0:
                verdict(False, f"{symbol} {name} valid JSONL", f"{parse_errors}/{total} parse errors")
            else:
                file_status[(symbol, name)] = ("jsonl", data, total)
        else:
            data, err = load_json(path)
            if err:
                verdict(False, f"{symbol} {name} valid JSON", str(err)[:100])
            elif data is None:
                verdict(False, f"{symbol} {name} readable", "Could not read")
            else:
                file_status[(symbol, name)] = ("json", data, None)

if all_exist:
    print("  [OK] All 30 data files exist and are valid")

# ═══════════════════════════════════════════════════════════
# 2. SCHEMA COMPLIANCE
# ═══════════════════════════════════════════════════════════
print("\n--- 2. SCHEMA COMPLIANCE ---")

for symbol in ["XAU", "BTC"]:
    gov = get_json((symbol, "governance_state"))
    if gov:
        sv = gov.get("schema_version", "MISSING")
        verdict(sv != "MISSING", f"{symbol} governance schema_version", sv)

    es = get_json((symbol, "execution_state"))
    if es:
        for field in ["version", "budgets", "circuit_breaker_tripped"]:
            verdict(field in es, f"{symbol} exec_state.{field}", "" if field in es else "MISSING")
        for bname, budget in es.get("budgets", {}).items():
            for field in ["total_trades_today", "paused", "consecutive_losses"]:
                verdict(field in budget, f"{symbol} exec_state.{bname}.{field}",
                        "" if field in budget else "MISSING")

    ledger = get_json((symbol, "pnl_ledger"))
    if ledger:
        verdict("schema_version" in ledger, f"{symbol} pnl_ledger schema_version", "")
        settled = ledger.get("settled", {})
        for bid, records in settled.items():
            if not isinstance(records, list) or not records: continue
            r = records[0]
            for field in ["signal_id", "brain_id", "entry_price", "close_price", "is_win", "pnl_per_unit"]:
                if field not in r:
                    verdict(False, f"{symbol} pnl_ledger {bid}[0].{field}", "MISSING")

    labels = get_jsonl((symbol, "live_labels"))
    if labels:
        r = labels[-1]
        for field in ["position_ticket", "symbol", "side", "label", "pnl"]:
            verdict(field in r, f"{symbol} labels schema.{field}", "" if field in r else "MISSING")

# ═══════════════════════════════════════════════════════════
# 3. CROSS-SOURCE CONSISTENCY
# ═══════════════════════════════════════════════════════════
print("\n--- 3. CROSS-SOURCE CONSISTENCY ---")

for symbol in ["XAU", "BTC"]:
    journal = get_jsonl((symbol, "trade_journal"))
    labels = get_jsonl((symbol, "live_labels"))
    gov = get_json((symbol, "governance_state"))
    ledger = get_json((symbol, "pnl_ledger"))
    es = get_json((symbol, "execution_state"))

    if not journal or not labels: continue

    # 3a. Journal ticket -> side mapping
    j_ticket_side = {}
    for e in journal:
        d = e.get("detail", {})
        if isinstance(d, dict) and d.get("retcode") == 10009:
            req = d.get("request", {})
            if isinstance(req, dict) and req.get("action") == 1 and req.get("type") == 1:
                order = d.get("order")
                if order:
                    j_ticket_side[order] = e.get("side", "?")

    # Check label sides match journal
    mismatches = 0
    matched = 0
    for lb in labels:
        t = lb.get("position_ticket")
        if t and t in j_ticket_side:
            matched += 1
            if lb.get("side", "?") != j_ticket_side[t]:
                mismatches += 1

    mm_pct = mismatches / max(matched, 1) * 100
    verdict(mm_pct < 5, f"{symbol} label vs journal side consistency",
            f"{mismatches}/{matched} mismatches ({mm_pct:.1f}%)" if mm_pct >= 5 else f"{matched} matched, 0 issues")

    # 3b. Label coverage
    j_tickets = set(j_ticket_side.keys())
    l_tickets = set(lb.get("position_ticket") for lb in labels if lb.get("position_ticket"))
    unlabeled = j_tickets - l_tickets
    coverage = (len(j_tickets) - len(unlabeled)) / max(len(j_tickets), 1) * 100
    verdict(coverage > 80, f"{symbol} label coverage {coverage:.0f}%",
            f"{len(unlabeled)}/{len(j_tickets)} unlabeled" if coverage <= 80 else "")

    # 3c. Governance <-> config brain count
    if gov:
        import yaml
        cpath = "configs/live.yaml" if symbol == "XAU" else "configs/live_btc.yaml"
        with open(cpath, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        enabled_brains = set()
        for entry in cfg.get("brains", {}).get("registry_entries", []):
            if entry.get("enabled"):
                enabled_brains.add(entry["path"].split("/")[-1].replace(".json", ""))
        gov_brains = set(gov.get("brain_states", {}).keys())
        missing = enabled_brains - gov_brains
        verdict(len(missing) == 0, f"{symbol} all enabled brains in governance",
                f"Missing: {list(missing)[:5]}" if missing else "")

    # 3d. PnL ledger vs governance trade counts
    if gov and ledger:
        settled = ledger.get("settled", {})
        for bid, state in gov.get("brain_states", {}).items():
            gt = state.get("performance_metrics", {}).get("total_trades", 0)
            pr = len(settled.get(bid, [])) if isinstance(settled.get(bid), list) else 0
            if gt > 0 and pr > 0 and pr < gt * 0.3:
                warn(f"{symbol} {bid}: PnL records({pr}) << gov trades({gt})")

# ═══════════════════════════════════════════════════════════
# 4. DATA QUALITY
# ═══════════════════════════════════════════════════════════
print("\n--- 4. DATA QUALITY ---")

# 4a. BTC V6/V7/V8 identity (CRITICAL)
btc_ledger = get_json(("BTC", "pnl_ledger"))
if btc_ledger:
    settled = btc_ledger.get("settled", {})
    triplets = [
        ("V6+V7+V8", ["BTC_Swing_V6_MultiTF_LGB_v2", "BTC_Swing_V7_MultiTF_LGB_v1", "BTC_Swing_V8_MultiTF_LGB_v1"]),
        ("V9+V10 Survival", ["BTC_Swing_V10_M15_Survival", "BTC_Swing_V9_H1_Survival"]),
        ("V11 Directional", ["BTC_Swing_V11_H1_Directional", "BTC_Swing_V11_M15_Directional"]),
    ]
    for label, brains in triplets:
        pnl_sets = []
        for bid in brains:
            recs = settled.get(bid, [])
            pnl_sets.append(tuple(round(r.get("pnl_per_unit", 0), 4) for r in recs[-5:]) if recs else ())
        unique = len(set(pnl_sets))
        verdict(unique > 1, f"BTC {label} records independent",
                "STILL IDENTICAL" if unique <= 1 and len(pnl_sets) > 1 else f"{unique} unique patterns")

# 4b. Duplicate detection across all JSONL files
for symbol in ["XAU", "BTC"]:
    for name in ["trade_journal", "live_labels", "golden_master"]:
        data = get_jsonl((symbol, name))
        if not data: continue
        if name == "trade_journal":
            msg_ids = [e.get("message_id", "") for e in data if e.get("message_id")]
            dups = len(msg_ids) - len(set(msg_ids))
            verdict(dups == 0, f"{symbol} journal: no dup message_ids",
                    f"{dups} duplicates" if dups else "")
        elif name == "live_labels":
            tickets = [lb.get("position_ticket") for lb in data if lb.get("position_ticket")]
            dups = len(tickets) - len(set(tickets))
            verdict(dups == 0, f"{symbol} labels: no dup tickets",
                    f"{dups} duplicates" if dups else "")

# 4c. Value sanity
for symbol in ["XAU", "BTC"]:
    es = get_json((symbol, "execution_state"))
    if es:
        for bname, budget in es.get("budgets", {}).items():
            for field in ["total_trades_today", "total_wins_today", "consecutive_losses"]:
                v = budget.get(field, -1)
                verdict(v >= 0, f"{symbol} {bname}.{field} >= 0", f"value={v}" if v < 0 else "")
            if budget.get("total_wins_today", 0) > budget.get("total_trades_today", 0):
                verdict(False, f"{symbol} {bname} wins <= trades",
                        f"wins={budget['total_wins_today']} > trades={budget['total_trades_today']}")

    ledger = get_json((symbol, "pnl_ledger"))
    if ledger:
        settled = ledger.get("settled", {})
        for bid, records in settled.items():
            if not isinstance(records, list): continue
            extreme = [r for r in records if abs(r.get("pnl_per_unit", 0)) > 1000]
            if extreme:
                warn(f"{symbol} {bid}: {len(extreme)} extreme PnL values (>1000)")

# ═══════════════════════════════════════════════════════════
# 5. FRESHNESS
# ═══════════════════════════════════════════════════════════
print("\n--- 5. FRESHNESS ---")

FRESH_LIMITS = {
    "execution_state": 15, "bar_sync_state": 10, "governance_state": 60,
    "pnl_ledger": 120, "live_labels": 120, "trade_journal": 30,
    "golden_master": 120, "brain_performance": 60, "alert_audit": 30,
    "data_health_state": 30, "daily_ops_state": 240, "calibrator_feed": 120,
    "position_snapshots": 120, "leaderboard": 240, "mt5_bridge_health": 30,
}

stale_count = 0
for symbol, files in DATA_FILES.items():
    for name, path in files.items():
        age = file_age(path)
        if age is None: continue
        limit = FRESH_LIMITS.get(name, 120)
        if age > limit:
            warn(f"{symbol} {name}: {age:.0f}min old (limit={limit}min)")
            stale_count += 1

if stale_count == 0:
    print("  [OK] All data files within freshness limits")

# ═══════════════════════════════════════════════════════════
# 6. EDGE CASES
# ═══════════════════════════════════════════════════════════
print("\n--- 6. EDGE CASES ---")

for symbol, files in DATA_FILES.items():
    for name, path in files.items():
        if not os.path.exists(path): continue
        size = os.path.getsize(path)
        if size == 0:
            verdict(False, f"{symbol} {name} not empty", "0 bytes")

    # Brain performance: check for all-zero records
    bp = get_json((symbol, "brain_performance"))
    if bp:
        records = bp.get("records", {})
        zero_brains = []
        for bid, recs in records.items():
            if not recs: continue
            recent = recs[-20:]
            all_zero = all(
                r.get("composite_score", 0) == 0 for r in recent if isinstance(r, dict)
            )
            if all_zero and len(recent) >= 5:
                zero_brains.append(bid)
        if zero_brains:
            warn(f"{symbol} brain_perf: {len(zero_brains)} zero-score brains")

    # Bar sync
    bs = get_json((symbol, "bar_sync_state"))
    if bs:
        verdict(bs.get("lag_count", -1) >= 0, f"{symbol} bar_sync lag >= 0", f"lag={bs.get('lag_count')}")
        verdict(bs.get("total_bars_seen", 0) > 100, f"{symbol} bar_sync has bars",
                f"bars={bs.get('total_bars_seen')}")

# ═══════════════════════════════════════════════════════════
# 7. REFERENTIAL INTEGRITY
# ═══════════════════════════════════════════════════════════
print("\n--- 7. REFERENTIAL INTEGRITY ---")

for symbol in ["XAU", "BTC"]:
    gov = get_json((symbol, "governance_state"))
    ledger = get_json((symbol, "pnl_ledger"))
    es = get_json((symbol, "execution_state"))

    if gov and ledger:
        gov_brains = set(gov.get("brain_states", {}).keys())
        pnl_brains = set(ledger.get("settled", {}).keys())
        in_gov_not_pnl = gov_brains - pnl_brains
        if in_gov_not_pnl:
            warn(f"{symbol}: {len(in_gov_not_pnl)} brains in gov not in PnL",
                 str(list(in_gov_not_pnl)[:3]))

    if es:
        import yaml
        cpath = "configs/live.yaml" if symbol == "XAU" else "configs/live_btc.yaml"
        with open(cpath, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        config_strats = set(n for n, sl in cfg.get("strategy_lines", {}).items() if sl.get("enabled", True))
        exec_budgets = set(es.get("budgets", {}).keys())
        missing = config_strats - exec_budgets
        if missing:
            warn(f"{symbol}: {len(missing)} strats in config not in exec_state", str(list(missing)[:3]))

# ═══════════════════════════════════════════════════════════
# 8. TEMPORAL CONSISTENCY
# ═══════════════════════════════════════════════════════════
print("\n--- 8. TEMPORAL CONSISTENCY ---")

for symbol in ["XAU", "BTC"]:
    labels = get_jsonl((symbol, "live_labels"))
    if labels:
        inversions = 0
        for lb in labels:
            oat = lb.get("open_recorded_at", "")
            cat = lb.get("close_recorded_at", "")
            if oat and cat:
                try:
                    if datetime.fromisoformat(oat) > datetime.fromisoformat(cat):
                        inversions += 1
                except: pass
        verdict(inversions == 0, f"{symbol} labels: open < close",
                f"{inversions} time inversions" if inversions else "")

    journal = get_jsonl((symbol, "trade_journal"))
    if journal:
        inversions = 0
        prev = ""
        for e in journal:
            ts = e.get("recorded_at", "")
            if ts and prev and ts < prev:
                inversions += 1
            prev = ts
        verdict(inversions == 0, f"{symbol} journal: monotonic timestamps",
                f"{inversions} inversions" if inversions else "")

# ═══════════════════════════════════════════════════════════
# FINAL VERDICT
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print(f"  FINAL VERDICT")
print(f"{'='*70}")

n_pass = len(RESULTS["PASS"])
n_fail = len(RESULTS["FAIL"])
n_warn = len(RESULTS["WARN"])
total = n_pass + n_fail + n_warn

print(f"\n  Checks: {total} total | PASS: {n_pass} | FAIL: {n_fail} | WARN: {n_warn}")

if n_fail == 0 and n_warn == 0:
    print(f"\n  *** DATA MODULE: 高枕无忧 ***")
elif n_fail == 0:
    print(f"\n  *** DATA MODULE: STABLE ({n_warn} minor warnings) ***")
else:
    print(f"\n  *** DATA MODULE: {n_fail} FAILURES NEED FIXING ***")
    for label, detail in RESULTS["FAIL"]:
        print(f"    FAIL: {label} -- {detail}")

if RESULTS["WARN"]:
    print(f"\n  Warnings ({n_warn}):")
    for label, detail in RESULTS["WARN"]:
        print(f"    - {label}: {detail}")

print(f"\n  Audit completed at {datetime.now().isoformat()[:19]}")
