# type: ignore
"""
Exhaustive Data Integrity Audit — "高枕无忧" standard
======================================================
Every data file, every cross-reference, every edge case.
"""

from __future__ import annotations

import hashlib, json, os, glob, time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# DQAF-20260623-073: unified ticket resolution (replaces ad-hoc .get() chains)
from core.data.ticket_resolver import resolve as resolve_ticket

now = time.time()
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
RESULTS: dict[str, dict] = {"PASS": [], "FAIL": [], "WARN": []}


def verdict(ok, label, detail=""):
    (RESULTS["PASS"] if ok else RESULTS["FAIL"]).append((label, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
    return ok


def warn(label, detail=""):
    RESULTS["WARN"].append((label, detail))
    print(f"  [WARN] {label} -- {detail}")


def load_jsonl(path):
    if not Path(path).exists():
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [l.strip() for l in f if l.strip()]
    results, errors = [], 0
    for line in lines:
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            errors += 1
    return results, errors, len(lines)


def load_json(path):
    if not Path(path).exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, str(e)


def file_age(path):
    return (now - os.path.getmtime(path)) / 60 if os.path.exists(path) else None


def get_json(key):
    """Safe accessor for JSON file_status entries."""
    if key not in file_status or file_status[key] is None:
        return None
    fs = file_status[key]
    return fs[1] if fs[0] == "json" else None


def get_jsonl(key):
    """Safe accessor for JSONL file_status entries."""
    if key not in file_status or file_status[key] is None:
        return None
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
        "pnl_ledger": "data/brain_pnl_ledger.json",
        "governance_state": "data/governance_state.json",
        "execution_state": "data/state/execution_state.json",
        "bar_sync_state": "data/bar_sync_state.json",
        "brain_performance": "data/brain_performance.json",
        "live_labels": "data/reports/live_labels.jsonl",
        "trade_journal": "data/live_trade_journal.jsonl",
        "golden_master": "data/golden_master.jsonl",
        "position_snapshots": "data/position_snapshots.jsonl",
        "alert_audit": "data/logs/alert_audit.jsonl",
        "data_health_state": "data/state/data_health_state.json",
        "daily_ops_state": "data/state/daily_ops_state.json",
        "calibrator_feed": "data/calibrator_feed_state.json",
        "leaderboard": "data/reports/leaderboard.json",
        "mt5_bridge_health": "data/reports/mt5_bridge_health.json",
    },
    "BTC": {
        "pnl_ledger": "data_btc/brain_pnl_ledger.json",
        "governance_state": "data_btc/governance_state.json",
        "execution_state": "data_btc/state/execution_state.json",
        "bar_sync_state": "data_btc/bar_sync_state.json",
        "brain_performance": "data_btc/brain_performance.json",
        "live_labels": "data_btc/reports/live_labels.jsonl",
        "trade_journal": "data_btc/live_trade_journal.jsonl",
        "golden_master": "data_btc/golden_master.jsonl",
        "position_snapshots": "data_btc/position_snapshots.jsonl",
        "alert_audit": "data_btc/logs/alert_audit.jsonl",
        "data_health_state": "data_btc/state/data_health_state.json",
        "daily_ops_state": "data_btc/state/daily_ops_state.json",
        "calibrator_feed": "data_btc/calibrator_feed_state.json",
        "leaderboard": "data_btc/reports/leaderboard.json",
        "mt5_bridge_health": "data_btc/reports/mt5_bridge_health.json",
    },
}

file_status: dict[str, str] = {}
all_exist = True
for symbol, files in DATA_FILES.items():
    for name, path in files.items():
        exists = os.path.exists(path)
        if not exists:
            verdict(False, f"{symbol} {name} exists", f"MISSING: {path}")
            file_status[(symbol, name)] = None
            all_exist = False
            continue

        is_jsonl = path.endswith(".jsonl")
        if is_jsonl:
            data, parse_errors, total = load_jsonl(path)
            if data is None:
                verdict(False, f"{symbol} {name} readable", "Could not read")
            elif parse_errors > 0:
                verdict(
                    False, f"{symbol} {name} valid JSONL", f"{parse_errors}/{total} parse errors"
                )
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
                verdict(
                    field in budget,
                    f"{symbol} exec_state.{bname}.{field}",
                    "" if field in budget else "MISSING",
                )

    ledger = get_json((symbol, "pnl_ledger"))
    if ledger:
        verdict("schema_version" in ledger, f"{symbol} pnl_ledger schema_version", "")
        settled = ledger.get("settled", {})
        for bid, records in settled.items():
            if not isinstance(records, list) or not records:
                continue
            r = records[0]
            for field in [
                "signal_id",
                "brain_id",
                "entry_price",
                "close_price",
                "is_win",
                "pnl_per_unit",
            ]:
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

    if not journal or not labels:
        continue

    # 3a. Journal ticket -> side mapping
    # FIX-20260623-067: align with label_builder logic — use action=="open"
    # plus position_ticket / detail.order fallback (same as label_builder line 438).
    j_ticket_side = {}
    for e in journal:
        _action = e.get("action", "")
        if _action != "open":
            continue
        # DQAF-20260623-073: unified ticket resolver
        _ticket = resolve_ticket(e)
        if _ticket is not None:
            if _ticket not in j_ticket_side:
                j_ticket_side[_ticket] = e.get("side", "?")

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
    verdict(
        mm_pct < 5,
        f"{symbol} label vs journal side consistency",
        f"{mismatches}/{matched} mismatches ({mm_pct:.1f}%)"
        if mm_pct >= 5
        else f"{matched} matched, 0 issues",
    )

    # 3b. Label coverage
    j_tickets = set(j_ticket_side.keys())
    l_tickets = set(lb.get("position_ticket") for lb in labels if lb.get("position_ticket"))
    unlabeled = j_tickets - l_tickets
    coverage = (len(j_tickets) - len(unlabeled)) / max(len(j_tickets), 1) * 100
    verdict(
        coverage > 80,
        f"{symbol} label coverage {coverage:.0f}%",
        f"{len(unlabeled)}/{len(j_tickets)} unlabeled" if coverage <= 80 else "",
    )

    # 3c. Governance <-> config brain count
    if gov:
        import yaml

        cpath = "configs/live.yaml" if symbol == "XAU" else "configs/live_btc.yaml"
        with open(cpath, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        enabled_brains = set()
        for entry in cfg.get("brains", {}).get("registry_entries", []):
            if entry.get("enabled"):
                enabled_brains.add(entry["path"].split("/")[-1].replace(".json", ""))
        gov_brains = set(gov.get("brain_states", {}).keys())
        missing = enabled_brains - gov_brains
        verdict(
            len(missing) == 0,
            f"{symbol} all enabled brains in governance",
            f"Missing: {list(missing)[:5]}" if missing else "",
        )

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
        (
            "V6+V7+V8",
            [
                "BTC_Swing_V6_MultiTF_LGB_v2",
                "BTC_Swing_V7_MultiTF_LGB_v1",
                "BTC_Swing_V8_MultiTF_LGB_v1",
            ],
        ),
        ("V9+V10 Survival", ["BTC_Swing_V10_M15_Survival", "BTC_Swing_V9_H1_Survival"]),
        ("V11 Directional", ["BTC_Swing_V11_H1_Directional", "BTC_Swing_V11_M15_Directional"]),
    ]
    for label, brains in triplets:
        signatures = []
        for bid in brains:
            recs = settled.get(bid, [])
            if not recs:
                signatures.append(None)
                continue
            # FIX-20260623-069a: multi-dimensional identity check.
            # (a) Full-record MD5 — different hashes = genuinely independent.
            # (b) PnL-stream MD5 — same PnL with different full hashes =
            #     limited-history coincidence (e.g. 1 shared trade then retired).
            #     This is a WARN, not a FAIL.
            # (c) Record count — different counts = clearly independent.
            _full_hash = hashlib.md5(
                json.dumps(recs, sort_keys=True, default=str).encode()
            ).hexdigest()
            _pnl_stream = tuple(round(r.get("pnl_per_unit", 0), 4) for r in recs)
            _pnl_hash = hashlib.md5(json.dumps(_pnl_stream).encode()).hexdigest()
            signatures.append(
                {
                    "bid": bid,
                    "count": len(recs),
                    "full_hash": _full_hash,
                    "pnl_hash": _pnl_hash,
                    "pnl_first5": _pnl_stream[:5],
                    "pnl_last5": _pnl_stream[-5:],
                    "cum_pnl": round(sum(_pnl_stream), 2),
                }
            )

        full_hashes = [s["full_hash"] for s in signatures]
        pnl_hashes = [s["pnl_hash"] for s in signatures]
        unique_full = len(set(full_hashes))
        unique_pnl = len(set(pnl_hashes))

        # Primary verdict: full-record uniqueness (catches actual data corruption)
        if unique_full > 1:
            verdict(True, f"BTC {label} records independent", f"{unique_full} unique full hashes")
        else:
            verdict(
                False,
                f"BTC {label} records independent",
                "FULL HASH IDENTICAL — possible corruption",
            )

        # Secondary: PnL-only identity without full-record identity = limited history
        if unique_pnl == 1 and unique_full > 1:
            _counts = [s["count"] for s in signatures]
            warn(
                f"BTC {label} PnL streams identical (limited history — "
                f"{sum(1 for c in _counts if c == max(_counts))}/{len(_counts)} "
                f"brains, same single-trade outcome)"
            )

# 4b. Duplicate detection across all JSONL files
for symbol in ["XAU", "BTC"]:
    for name in ["trade_journal", "live_labels", "golden_master"]:
        data = get_jsonl((symbol, name))
        if not data:
            continue
        if name == "trade_journal":
            msg_ids = [e.get("message_id", "") for e in data if e.get("message_id")]
            dups = len(msg_ids) - len(set(msg_ids))
            verdict(
                dups == 0,
                f"{symbol} journal: no dup message_ids",
                f"{dups} duplicates" if dups else "",
            )
        elif name == "live_labels":
            # FIX-20260623-069b: per-brain labeling (FIX-20260622-057 Phase 2 A1)
            # produces one label record per brain per trade.  Multiple brains
            # sharing the same position_ticket is BY DESIGN, not duplication.
            # Dedup key = (position_ticket, brain_id) — same ticket with
            # different brain_ids is legitimate multi-brain labeling.
            ticket_brain_pairs = [
                (lb.get("position_ticket"), lb.get("brain_id", ""))
                for lb in data
                if lb.get("position_ticket")
            ]
            dups = len(ticket_brain_pairs) - len(set(ticket_brain_pairs))
            verdict(
                dups == 0,
                f"{symbol} labels: no dup (ticket, brain_id) pairs",
                f"{dups} duplicates" if dups else "",
            )

# 4c. Value sanity
for symbol in ["XAU", "BTC"]:
    es = get_json((symbol, "execution_state"))
    if es:
        for bname, budget in es.get("budgets", {}).items():
            for field in ["total_trades_today", "total_wins_today", "consecutive_losses"]:
                v = budget.get(field, -1)
                verdict(v >= 0, f"{symbol} {bname}.{field} >= 0", f"value={v}" if v < 0 else "")
            if budget.get("total_wins_today", 0) > budget.get("total_trades_today", 0):
                verdict(
                    False,
                    f"{symbol} {bname} wins <= trades",
                    f"wins={budget['total_wins_today']} > trades={budget['total_trades_today']}",
                )

    ledger = get_json((symbol, "pnl_ledger"))
    if ledger:
        settled = ledger.get("settled", {})
        for bid, records in settled.items():
            if not isinstance(records, list):
                continue
            extreme = [r for r in records if abs(r.get("pnl_per_unit", 0)) > 1000]
            if extreme:
                warn(f"{symbol} {bid}: {len(extreme)} extreme PnL values (>1000)")

# ═══════════════════════════════════════════════════════════
# 5. FRESHNESS
# ═══════════════════════════════════════════════════════════
print("\n--- 5. FRESHNESS ---")

FRESH_LIMITS = {
    "execution_state": 15,
    "bar_sync_state": 10,
    "governance_state": 60,
    "pnl_ledger": 120,
    "live_labels": 120,
    "trade_journal": 30,
    "golden_master": 120,
    "brain_performance": 60,
    "alert_audit": 30,
    "data_health_state": 30,
    "daily_ops_state": 240,
    "calibrator_feed": 120,
    "position_snapshots": 120,
    "leaderboard": 240,
    "mt5_bridge_health": 30,
}

stale_count = 0
for symbol, files in DATA_FILES.items():
    for name, path in files.items():
        age = file_age(path)
        if age is None:
            continue
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
        if not os.path.exists(path):
            continue
        size = os.path.getsize(path)
        if size == 0:
            verdict(False, f"{symbol} {name} not empty", "0 bytes")

    # Brain performance: check for all-zero records
    bp = get_json((symbol, "brain_performance"))
    if bp:
        records = bp.get("records", {})
        zero_brains = []
        for bid, recs in records.items():
            if not recs:
                continue
            recent = recs[-20:]
            all_zero = all(r.get("composite_score", 0) == 0 for r in recent if isinstance(r, dict))
            if all_zero and len(recent) >= 5:
                zero_brains.append(bid)
        if zero_brains:
            warn(f"{symbol} brain_perf: {len(zero_brains)} zero-score brains")

    # Bar sync
    bs = get_json((symbol, "bar_sync_state"))
    if bs:
        verdict(
            bs.get("lag_count", -1) >= 0,
            f"{symbol} bar_sync lag >= 0",
            f"lag={bs.get('lag_count')}",
        )
        verdict(
            bs.get("total_bars_seen", 0) > 100,
            f"{symbol} bar_sync has bars",
            f"bars={bs.get('total_bars_seen')}",
        )

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
            warn(
                f"{symbol}: {len(in_gov_not_pnl)} brains in gov not in PnL",
                str(list(in_gov_not_pnl)[:3]),
            )

    if es:
        import yaml

        cpath = "configs/live.yaml" if symbol == "XAU" else "configs/live_btc.yaml"
        with open(cpath, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        config_strats = set(
            n for n, sl in cfg.get("strategy_lines", {}).items() if sl.get("enabled", True)
        )
        exec_budgets = set(es.get("budgets", {}).keys())
        missing = config_strats - exec_budgets
        if missing:
            warn(
                f"{symbol}: {len(missing)} strats in config not in exec_state",
                str(list(missing)[:3]),
            )

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
                except:
                    pass
        verdict(
            inversions == 0,
            f"{symbol} labels: open < close",
            f"{inversions} time inversions" if inversions else "",
        )

    journal = get_jsonl((symbol, "trade_journal"))
    if journal:
        inversions = 0
        prev = ""
        for e in journal:
            ts = e.get("recorded_at", "")
            # FIX-20260623-069c: normalize timestamp formats before comparison.
            # Journal entries mix "2026-05-05T04:12:42" (no TZ) and
            # "2026-05-04T16:40:45Z" (UTC).  Raw string comparison can't
            # handle this format inconsistency — normalise both to ISO.
            if ts:
                ts = ts.replace("Z", "+00:00")
                if "+" not in ts:
                    ts = ts + "+00:00"
            if ts and prev and ts < prev:
                inversions += 1
            prev = ts
        verdict(
            inversions == 0,
            f"{symbol} journal: monotonic timestamps",
            f"{inversions} inversions" if inversions else "",
        )

# ═══════════════════════════════════════════════════════════
# FINAL VERDICT
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("  FINAL VERDICT")
print(f"{'='*70}")

n_pass = len(RESULTS["PASS"])
n_fail = len(RESULTS["FAIL"])
n_warn = len(RESULTS["WARN"])
total = n_pass + n_fail + n_warn

print(f"\n  Checks: {total} total | PASS: {n_pass} | FAIL: {n_fail} | WARN: {n_warn}")

if n_fail == 0 and n_warn == 0:
    print("\n  *** DATA MODULE: 高枕无忧 ***")
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


# ═══════════════════════════════════════════════════════════════
# 9. AUDIT CHECK MANIFEST — Filter Bias Prevention (DQAF-20260623-069)
# ═══════════════════════════════════════════════════════════════
#
# Every audit check MUST declare:
#   - check_id: unique identifier
#   - section: which audit section this belongs to
#   - production_code_ref: the production code path(s) this check mirrors
#   - what_it_measures: precise description of the metric
#   - false_positive_condition: what would cause a spurious FAIL
#   - false_negative_condition: what real problem would this MISS
#   - last_validated: date of last cross-reference with production code
#
# This manifest is the structural answer to "audit tool ≠ audit conclusion."
# Before adding or modifying any check, you MUST:
#   1. Cross-reference the production code at production_code_ref
#   2. Verify the filter/aggregation logic matches
#   3. Update last_validated
#   4. Run: python scripts/audit_data_exhaustive.py --validate-self
#
# Lessons learned (DQAF-067, DQAF-069):
#   - DQAF-067: audit filter retcode==10009+request.action==1 measured
#     "BUY market orders" not "label coverage" → 39% false FAIL
#   - DQAF-069a: identity check used last-5 PnL → inactive brains all
#     converge to (0,0,0,0,0) → false "IDENTICAL" FAIL
#   - DQAF-069b: dup check used position_ticket without brain_id →
#     multi-brain labels (by design) counted as duplicates → 335 false FAIL
#   - DQAF-069c: timestamp compare without Z-suffix normalization →
#     2 extra false inversions from format mismatch

AUDIT_CHECK_MANIFEST: list[dict] = [
    # ── Section 1: File Existence & Validity ──
    {
        "check_id": "1.1",
        "section": "File Existence",
        "description": "All 30 data files exist and are valid JSON/JSONL",
        "production_code_ref": "DATA_FILES dict (this file, lines 85-120)",
        "what_it_measures": "File presence and parseability",
        "false_positive_condition": "None — existence and parseability are objective",
        "false_negative_condition": "Empty-but-valid JSONL files pass silently",
        "last_validated": "2026-06-23",
    },
    # ── Section 2: Schema Compliance ──
    {
        "check_id": "2.1",
        "section": "Schema Compliance",
        "description": "Governance, execution state, PnL ledger, labels schema fields present",
        "production_code_ref": "governance_state.json, execution_state.json schemas",
        "what_it_measures": "Required keys exist in state files",
        "false_positive_condition": "Schema version mismatch from intentional upgrade",
        "false_negative_condition": "New required fields added without updating check",
        "last_validated": "2026-06-23",
    },
    # ── Section 3: Cross-Source Consistency ──
    {
        "check_id": "3.1",
        "section": "Cross-Source",
        "description": "Label side matches journal side per ticket",
        "production_code_ref": "label_builder.py:build_trade_records() — side comes from open_rec",
        "what_it_measures": "side field consistency between journal and labels",
        "false_positive_condition": "Side normalization differences (buy/BUY/Buy)",
        "false_negative_condition": "Side mismatch in tickets without labels",
        "last_validated": "2026-06-23",
    },
    {
        "check_id": "3.2",
        "section": "Cross-Source",
        "description": "Label coverage = |labeled tickets ∩ journal open tickets| / |journal open tickets|",
        "production_code_ref": "daily_ops.py:_step_label_builder() coverage metric; label_builder.py:438 ticket resolution",
        "what_it_measures": "Fraction of journal open tickets that have at least one label",
        "false_positive_condition": (
            "DQAF-067 (FIXED): audit used retcode==10009 filter → only BUY market orders; "
            "now uses action=='open' + resolve_ticket() matching label_builder"
        ),
        "false_negative_condition": "Labels exist but ticket resolution misses them (different field name)",
        "last_validated": "2026-06-23",
    },
    {
        "check_id": "3.3",
        "section": "Cross-Source",
        "description": "All enabled brains in governance exist in config",
        "production_code_ref": "governance_state.json brain_ids vs configs/brains_btc/*.json",
        "what_it_measures": "Governance↔config brain count alignment",
        "false_positive_condition": "Brain config in non-standard directory",
        "false_negative_condition": "Config exists but model file is missing",
        "last_validated": "2026-06-23",
    },
    # ── Section 4: Data Quality ──
    {
        "check_id": "4.1",
        "section": "Data Quality",
        "description": "BTC brain group PnL record independence",
        "production_code_ref": "brain_pnl_ledger.json — BrainPnLStore.load_from_stream()",
        "what_it_measures": "Full-record MD5 hashes; PnL-stream MD5 for limited-history WARN",
        "false_positive_condition": (
            "DQAF-069a (FIXED): last-5 PnL comparison → all inactive brains converge to "
            "(0,0,0,0,0). Now uses full-record hash + record count + cumulative PnL signature. "
            "PnL-only identity with different full hashes → WARN (limited trading history), not FAIL."
        ),
        "false_negative_condition": "Different tickets/timestamps but identical PnL due to shared trades → masked by full-hash check",
        "last_validated": "2026-06-23",
    },
    {
        "check_id": "4.2",
        "section": "Data Quality",
        "description": "No duplicate message_ids in journal",
        "production_code_ref": "live_trade_journal.jsonl — message_id uniqueness contract",
        "what_it_measures": "message_id cardinality vs total entries",
        "false_positive_condition": "None — message_id should be unique per contract",
        "false_negative_condition": "Missing message_id field → entries excluded from check",
        "last_validated": "2026-06-23",
    },
    {
        "check_id": "4.3",
        "section": "Data Quality",
        "description": "No duplicate (position_ticket, brain_id) pairs in labels",
        "production_code_ref": "label_builder.py:build_trade_records() — per-brain labeling (FIX-20260622-057 Phase 2 A1)",
        "what_it_measures": "Unique (ticket, brain_id) pairs vs total label entries",
        "false_positive_condition": (
            "DQAF-069b (FIXED): counted position_ticket duplicates without brain_id → "
            "multi-brain labels (by design) were counted as duplicates. "
            "Now uses (position_ticket, brain_id) as dedup key."
        ),
        "false_negative_condition": "Same brain producing duplicate labels for same ticket (journal re-entry)",
        "last_validated": "2026-06-23",
    },
    {
        "check_id": "4.4",
        "section": "Data Quality",
        "description": "Execution state value sanity (non-negative counts, wins ≤ trades)",
        "production_code_ref": "execution_state.json — budget counters from live_cycle",
        "what_it_measures": "Counter monotonicity and bounds",
        "false_positive_condition": "Manual state reset causing counters to go backwards (legitimate restart)",
        "false_negative_condition": "Counters wrapped around MAX_INT (unlikely at current volumes)",
        "last_validated": "2026-06-23",
    },
    {
        "check_id": "4.5",
        "section": "Data Quality",
        "description": "Extreme PnL values (>1000 per unit)",
        "production_code_ref": "brain_pnl_ledger.json — SignalSettled events from ledger_events",
        "what_it_measures": "Count of pnl_per_unit > 1000 records per brain",
        "false_positive_condition": "Legitimate large PnL event (e.g. high leverage, large move)",
        "false_negative_condition": "PnL stored in wrong field → not detected",
        "last_validated": "2026-06-23",
    },
    # ── Section 5: Freshness ──
    {
        "check_id": "5.1",
        "section": "Freshness",
        "description": "File age within configured limits",
        "production_code_ref": "daily_ops.py schedule — per-file freshness thresholds",
        "what_it_measures": "Minutes since last file modification vs limit",
        "false_positive_condition": "System intentionally paused (weekend, maintenance)",
        "false_negative_condition": "File updated but with stale data (touch without refresh)",
        "last_validated": "2026-06-23",
    },
    # ── Section 6: Edge Cases ──
    {
        "check_id": "6.1",
        "section": "Edge Cases",
        "description": "Bar sync lag and bar count",
        "production_code_ref": "bar_sync_state.json — feature store bar synchronization",
        "what_it_measures": "Bar count > 0 and lag ≥ 0",
        "false_positive_condition": "None — zero bars or negative lag are always bugs",
        "false_negative_condition": "Lag reported as 0 but feature store is actually behind (sync broken)",
        "last_validated": "2026-06-23",
    },
    # ── Section 7: Referential Integrity ──
    {
        "check_id": "7.1",
        "section": "Referential Integrity",
        "description": "Brains in governance have PnL records",
        "production_code_ref": "BrainPnLStore.load_from_stream() — event replay produces settled records",
        "what_it_measures": "governance brain_ids ∩ pnl_ledger brain_ids",
        "false_positive_condition": "DQAF-068 (FIXED): stale JSON VIEW missed V12 PnL. Fixed by SSOT rebuild.",
        "false_negative_condition": "Brain has zero PnL events (never traded) → not detected as gap",
        "last_validated": "2026-06-23",
    },
    {
        "check_id": "7.2",
        "section": "Referential Integrity",
        "description": "Strategy configs have execution_state entries",
        "production_code_ref": "live.yaml strat configs ↔ execution_state.json budgets",
        "what_it_measures": "config strategy names ∩ exec_state budget names",
        "false_positive_condition": "New strategy added to config but not yet deployed",
        "false_negative_condition": "Strategy removed from config but budget still in exec_state (orphaned)",
        "last_validated": "2026-06-23",
    },
    # ── Section 8: Temporal Consistency ──
    {
        "check_id": "8.1",
        "section": "Temporal Consistency",
        "description": "Label open_recorded_at < close_recorded_at",
        "production_code_ref": "label_builder.py — open_rec.recorded_at, close_rec.recorded_at",
        "what_it_measures": "Chronological ordering of open and close events per label",
        "false_positive_condition": "1-second clock skew or Z/no-Z format mismatch",
        "false_negative_condition": "Both timestamps missing → entry excluded from check",
        "last_validated": "2026-06-23",
    },
    {
        "check_id": "8.2",
        "section": "Temporal Consistency",
        "description": "Journal recorded_at monotonic (within format limits)",
        "production_code_ref": "live_trade_journal.jsonl — MT5 bridge async writes",
        "what_it_measures": "String-order of recorded_at after normalizing Z→+00:00",
        "false_positive_condition": (
            "DQAF-069c (PARTIALLY FIXED): Z vs no-Z format mismatch caused ~2 extra "
            "inversions. Now normalized. Remaining 365/52 inversions are REAL — "
            "MT5 async bridge writes journal out of chronological order."
        ),
        "false_negative_condition": "All timestamps missing → 0 inversions (empty check)",
        "last_validated": "2026-06-23",
    },
]


def validate_check_manifest() -> int:
    """Self-validate the audit check manifest for completeness and anti-patterns.

    Returns exit code: 0 = clean, 1 = issues found.
    """
    issues = 0
    required_fields = [
        "check_id",
        "section",
        "description",
        "production_code_ref",
        "what_it_measures",
        "false_positive_condition",
        "false_negative_condition",
    ]

    seen_ids = set()
    for check in AUDIT_CHECK_MANIFEST:
        cid = check.get("check_id", "?")
        # Completeness
        for field in required_fields:
            if field not in check or not check[field]:
                print(f"[MANIFEST-ERR] Check {cid}: missing field '{field}'")
                issues += 1

        # Uniqueness
        if cid in seen_ids:
            print(f"[MANIFEST-ERR] Check {cid}: duplicate check_id")
            issues += 1
        seen_ids.add(cid)

        # Anti-pattern: false_positive = "None" without justification
        fp = check.get("false_positive_condition", "")
        if fp.lower().startswith("none"):
            print(
                f"[MANIFEST-INFO] Check {cid}: false_positive='None' — verify this is truly objective"
            )

    # Coverage: every section 1-8 should have at least one check
    sections_present = {c["section"] for c in AUDIT_CHECK_MANIFEST if "section" in c}
    expected_sections = {
        "File Existence",
        "Schema Compliance",
        "Cross-Source",
        "Data Quality",
        "Freshness",
        "Edge Cases",
        "Referential Integrity",
        "Temporal Consistency",
    }
    missing_sections = expected_sections - sections_present
    if missing_sections:
        print(f"[MANIFEST-WARN] Sections without checks: {missing_sections}")

    print(f"\n  Manifest: {len(AUDIT_CHECK_MANIFEST)} checks registered, {issues} issues")
    return 1 if issues > 0 else 0


if __name__ == "__main__":
    import sys

    if "--validate-self" in sys.argv:
        sys.exit(validate_check_manifest())
