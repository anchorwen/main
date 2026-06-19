"""
Deep Full-Stack Audit — Expanded scope & depth
===============================================
Covers: Data, Code, Runtime, Trading Quality, Config, Brain Pipeline,
MetaFilter, Risk/Budget, Alerts, Bridge
"""
from __future__ import annotations

import json, os, glob, time, subprocess, sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

now = time.time()
ISSUES = []

def load_jsonl(path):
    if not Path(path).exists(): return []
    with open(path, encoding='utf-8', errors='replace') as f:
        return [json.loads(l) for l in f if l.strip()]

def load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)

def issue(severity, area, msg):
    tag = {"P0":"[CRIT]","P1":"[WARN]","P2":"[INFO]"}.get(severity, "[??]")
    print(f"  {tag} [{area}] {msg}")
    ISSUES.append((severity, area, msg))

def section(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")

# ═══════════════════════════════════════════════════════════
# 1. DATA MODULE RE-CHECK
# ═══════════════════════════════════════════════════════════
section("1. DATA MODULE (post-fix)")

# 1a. BTC V6/V7/V8 independence
btc_ledger = load_json("data_btc/brain_pnl_ledger.json")
settled = btc_ledger.get("settled", {})
v6 = settled.get("BTC_Swing_V6_MultiTF_LGB_v2", [])
v7 = settled.get("BTC_Swing_V7_MultiTF_LGB_v1", [])
v8 = settled.get("BTC_Swing_V8_MultiTF_LGB_v1", [])
if v6 and v7 and v8:
    v6_last = [round(r.get("pnl_per_unit",0),4) for r in v6[-5:]]
    v7_last = [round(r.get("pnl_per_unit",0),4) for r in v7[-5:]]
    v8_last = [round(r.get("pnl_per_unit",0),4) for r in v8[-5:]]
    if v6_last == v7_last == v8_last:
        issue("P0", "PnL", "BTC V6/V7/V8 still sharing identical records")
    else:
        print("  [OK] BTC V6/V7/V8 records now independent")

# 1b. Label coverage for today only
today = "2026-06-11"
for name, jpath, lpath, sym in [
    ("XAU", "data/live_trade_journal.jsonl", "data/reports/live_labels.jsonl", "XAUUSDc"),
    ("BTC", "data_btc/live_trade_journal.jsonl", "data_btc/reports/live_labels.jsonl", "BTCUSDc"),
]:
    journal = load_jsonl(jpath)
    labels = load_jsonl(lpath)

    j_today = [e for e in journal if today in e.get("recorded_at","")]
    l_today = [lb for lb in labels if lb.get("symbol")==sym and today in (lb.get("open_recorded_at","") or lb.get("close_recorded_at",""))]

    # Extract tickets
    j_tickets = set()
    for e in j_today:
        d = e.get("detail",{})
        if isinstance(d, dict) and d.get("retcode")==10009:
            req = d.get("request",{})
            if isinstance(req,dict) and req.get("action")==1 and req.get("type")==1:
                if d.get("order"):
                    j_tickets.add(d["order"])

    l_tickets = set(lb.get("position_ticket") for lb in l_today if lb.get("position_ticket"))
    unlabeled = j_tickets - l_tickets
    print(f"  {name} today: {len(j_today)} journal entries, {len(j_tickets)} opens, {len(l_today)} labels, {len(unlabeled)} unlabeled")

# 1c. Governance promotion
xau_gov = load_json("data/governance_state.json")
btc_gov = load_json("data_btc/governance_state.json")
for name, gov in [("XAU", xau_gov), ("BTC", btc_gov)]:
    stuck = [(bid, s.get("status")) for bid, s in gov.get("brain_states",{}).items()
             if s.get("status")=="candidate"]
    # Check if any enabled brains are stuck
    print(f"  {name}: {len(stuck)} candidate brains total")
    if stuck:
        wr_info = []
        for bid, _ in stuck[:8]:
            wr = gov["brain_states"][bid].get("performance_metrics",{}).get("win_rate",0)
            wr_info.append(f"{bid.split('_')[-1][:8]}:{wr:.1%}")
        print(f"    Candidates: {', '.join(wr_info)}")

# 1d. Data health freshness
for name, path in [("XAU", "data/state/data_health_state.json"), ("BTC", "data_btc/state/data_health_state.json")]:
    if os.path.exists(path):
        age = (now - os.path.getmtime(path)) / 60
        if age > 30:
            issue("P1", "DataHealth", f"{name} data_health_state {age:.0f}min stale")
        else:
            print(f"  [OK] {name} data_health_state: {age:.0f}min")

# ═══════════════════════════════════════════════════════════
# 2. CODE HEALTH
# ═══════════════════════════════════════════════════════════
section("2. CODE HEALTH")

# 2a. verify.py
result = subprocess.run(["python", "scripts/verify.py", "--quick"], capture_output=True, text=True, timeout=60, cwd="d:/future")
if result.returncode != 0:
    issue("P0", "Verify", f"verify.py --quick FAILED: {result.stdout[-300:]}")
else:
    print("  [OK] verify.py --quick: PASSED")

# 2b. BLE001 count
for fpath in ["core/runtime/live_cycle.py", "scripts/live_intent_loop.py", "core/execution/strategy_line.py"]:
    with open(fpath, encoding='utf-8') as f:
        count = f.read().count("noqa: BLE001")
    print(f"  BLE001 in {fpath.split('/')[-1]}: {count}")

# 2c. Check for common anti-patterns
anti_patterns = {
    "except Exception:": "bare except",
    "except Exception as e:": "bare except named",
    "pass  # noqa": "swallowed exception with noqa",
}
for fpath in ["core/runtime/live_cycle.py", "scripts/live_intent_loop.py"]:
    with open(fpath, encoding='utf-8') as f:
        content = f.read()
    for pattern, desc in anti_patterns.items():
        count = content.count(pattern)
        if count > 50:
            print(f"  [INFO] {fpath.split('/')[-1]}: {count}x '{desc}'")

# ═══════════════════════════════════════════════════════════
# 3. RUNTIME BEHAVIOR (recent intent logs)
# ═══════════════════════════════════════════════════════════
section("3. RUNTIME BEHAVIOR")

for name, pattern in [("XAU", "data/logs/intent_*.log"), ("BTC", "data_btc/logs/intent_*.log")]:
    logs = sorted(glob.glob(pattern))[-30:]  # last 30

    total_cycles = 0
    total_dispatches = 0
    total_promotions = 0
    error_events: list[dict] = Counter()
    no_trade_reasons: dict[str, int] = Counter()
    restart_count = 0

    for log_path in logs:
        prev_event = None
        with open(log_path, encoding='utf-8', errors='replace') as f:
            for line in f:
                if not line.strip(): continue
                try:
                    d = json.loads(line)
                except: continue
                ev = d.get("event","")
                if ev == "cycle_end":
                    total_cycles += 1
                elif ev in ("intent_dispatched","strategy_dispatched"):
                    total_dispatches += 1
                elif ev == "brain_promoted":
                    total_promotions += 1
                elif ev == "multi_strategy_eval":
                    for s in d.get("strategies",[]):
                        if not s.get("should_trade"):
                            reason = s.get("reason","unknown")[:80]
                            no_trade_reasons[reason] += 1
                elif "error" in ev.lower():
                    err = d.get("error", str(d)[:100])
                    error_events[err[:80]] += 1
                elif ev == "live_intent_loop_start":
                    restart_count += 1

    print(f"\n  {name} (last 30 logs):")
    print(f"    Restarts: {restart_count}")
    print(f"    Cycles: {total_cycles} | Dispatches: {total_dispatches} | Promotions: {total_promotions}")
    if total_dispatches == 0 and total_cycles > 5:
        issue("P1", "Runtime", f"{name}: {total_cycles} cycles with 0 dispatches")

    if error_events:
        print("    Top errors:")
        for err, count in error_events.most_common(5):
            print(f"      [{count}x] {err[:100]}")

    print("    Top no-trade reasons:")
    for reason, count in no_trade_reasons.most_common(5):
        print(f"      [{count}x] {reason[:100]}")

# ═══════════════════════════════════════════════════════════
# 4. TRADING QUALITY (today only)
# ═══════════════════════════════════════════════════════════
section("4. TRADING QUALITY (today)")

for name, lpath, sym in [
    ("XAU", "data/reports/live_labels.jsonl", "XAUUSDc"),
    ("BTC", "data_btc/reports/live_labels.jsonl", "BTCUSDc"),
]:
    labels = load_jsonl(lpath)
    today_labels = [lb for lb in labels if lb.get("symbol")==sym and today in (lb.get("open_recorded_at","") or lb.get("close_recorded_at",""))]

    settled = [lb for lb in today_labels if lb.get("pnl") is not None]
    wins = [lb for lb in settled if lb["pnl"] > 0]
    losses = [lb for lb in settled if lb["pnl"] < 0]
    total_pnl = sum(lb["pnl"] for lb in settled)

    print(f"\n  {name}: {len(today_labels)} labels, {len(settled)} settled")
    if settled:
        wr = len(wins)/len(settled)*100
        avg_win = sum(lb["pnl"] for lb in wins)/max(len(wins),1)
        avg_loss = sum(lb["pnl"] for lb in losses)/max(len(losses),1)
        print(f"    WinRate={wr:.0f}% PnL={total_pnl:+.2f} | AvgWin={avg_win:+.2f} AvgLoss={avg_loss:+.2f}")
        if wr < 35 and len(settled) >= 5:
            issue("P1", "Trading", f"{name}: WR={wr:.0f}% over {len(settled)} trades")
        if total_pnl < -5:
            issue("P1", "Trading", f"{name}: Daily PnL={total_pnl:+.2f}")

    # Direction distribution
    sides = Counter(lb.get("side","?") for lb in today_labels)
    print(f"    Sides: {dict(sides)}")

# ═══════════════════════════════════════════════════════════
# 5. CONFIGURATION INTEGRITY
# ═══════════════════════════════════════════════════════════
section("5. CONFIGURATION INTEGRITY")

import yaml
for cpath, label in [("configs/live.yaml","XAU"), ("configs/live_btc.yaml","BTC")]:
    with open(cpath, encoding='utf-8') as f:
        cfg = yaml.safe_load(f)

    # Check brain files exist and are valid JSON
    for entry in cfg.get("brains",{}).get("registry_entries",[]):
        if not entry.get("enabled"): continue
        path = entry["path"]
        if not os.path.exists(path):
            issue("P0", "Config", f"{label}: enabled brain file MISSING: {path}")
            continue
        try:
            with open(path, encoding='utf-8') as f:
                bcfg = json.load(f)
            # Check required fields
            for field in ["brain_id","brain_type","feature_schema_id","artifact_path"]:
                if not bcfg.get(field):
                    issue("P1", "Config", f"{label}: {path} missing {field}")
            # Check artifact exists
            art = bcfg.get("artifact_path","")
            if art and not os.path.exists(art):
                issue("P1", "Config", f"{label}: {bcfg.get('brain_id','?')} artifact missing: {art}")
        except Exception as e:
            issue("P0", "Config", f"{label}: {path} invalid JSON: {e}")

    # Check strategy line brain_type matching
    enabled_btypes = set()
    for entry in cfg.get("brains",{}).get("registry_entries",[]):
        if not entry.get("enabled"): continue
        path = entry["path"]
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                bcfg = json.load(f)
            enabled_btypes.add(bcfg.get("brain_type",""))

    for sl_name, sl in cfg.get("strategy_lines",{}).items():
        if not sl.get("enabled", True): continue
        needed = set(sl.get("brain_types",[]))
        if not (needed & enabled_btypes):
            issue("P1", "Config", f"{label} {sl_name}: needs {needed}, enabled types={enabled_btypes}")

# Check MT5 terminal paths
for cpath, label in [("configs/live.yaml","XAU"), ("configs/live_btc.yaml","BTC")]:
    with open(cpath, encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    mt5_path = cfg.get("mt5",{}).get("terminal_path","")
    if mt5_path and not os.path.exists(mt5_path):
        issue("P0", "Config", f"{label}: MT5 terminal MISSING: {mt5_path}")

print("  [OK] Configuration scan complete")

# ═══════════════════════════════════════════════════════════
# 6. METAFILTER HEALTH
# ═══════════════════════════════════════════════════════════
section("6. METAFILTER HEALTH")

for name, log_pattern in [("XAU", "data/logs/intent_*.log"), ("BTC", "data_btc/logs/intent_*.log")]:
    logs = sorted(glob.glob(log_pattern))[-10:]

    kelly_stats = defaultdict(list)
    for log_path in logs:
        with open(log_path, encoding='utf-8', errors='replace') as f:
            for line in f:
                if "kelly_diag" not in line: continue
                try:
                    d = json.loads(line)
                except: continue
                strategy = d.get("strategy","?")
                p_win = d.get("result_p_win", 0)
                kelly_stats[strategy].append(p_win)

    if kelly_stats:
        print(f"\n  {name} MetaFilter p_win (last 10 logs):")
        for strategy, p_wins in sorted(kelly_stats.items()):
            avg_pw = sum(p_wins)/len(p_wins) if p_wins else 0
            min_pw = min(p_wins) if p_wins else 0
            max_pw = max(p_wins) if p_wins else 0
            flag = ""
            if avg_pw < 0.30:
                flag = " <-- VERY LOW"
            elif avg_pw < 0.40:
                flag = " <-- LOW"
            print(f"    {strategy}: avg={avg_pw:.3f} min={min_pw:.3f} max={max_pw:.3f} (n={len(p_wins)}){flag}")

# ═══════════════════════════════════════════════════════════
# 7. RISK / BUDGET SYSTEM
# ═══════════════════════════════════════════════════════════
section("7. RISK / BUDGET")

for name, path in [("XAU", "data/state/execution_state.json"), ("BTC", "data_btc/state/execution_state.json")]:
    d = load_json(path)
    for bname, budget in d.get("budgets",{}).items():
        daily_pnl = budget.get("daily_pnl_pct",0)
        paused = budget.get("paused", False)
        consec = budget.get("consecutive_losses",0)
        trades = budget.get("total_trades_today",0)

        status = ""
        if paused: status += " [PAUSED]"
        if consec >= 5: status += f" [CONSEC_LOSS={consec}]"
        if daily_pnl < -0.02: status += f" [DAILY_PNL={daily_pnl:.1%}]"
        if status:
            issue("P1", "Risk", f"{name} {bname}: {trades} trades{status}")
        else:
            print(f"  [OK] {name} {bname}: {trades} trades, PnL={daily_pnl:.1%}, consec={consec}")

# Check cooldown registry
for name, path in [("XAU", "data/state/execution_state.json"), ("BTC", "data_btc/state/execution_state.json")]:
    d = load_json(path)
    cooldowns = d.get("cooldown_registry",{})
    if cooldowns:
        for strat, cd in cooldowns.items():
            deadline = cd.get("deadline",0)
            remaining = deadline - now
            if remaining > 0:
                issue("P2", "Risk", f"{name} {strat} in cooldown: {remaining:.0f}s remaining ({cd.get('type','?')})")

# ═══════════════════════════════════════════════════════════
# 8. BRIDGE / MT5 HEALTH
# ═══════════════════════════════════════════════════════════
section("8. BRIDGE / MT5")

for name, jpath in [("XAU", "data/live_trade_journal.jsonl"), ("BTC", "data_btc/live_trade_journal.jsonl")]:
    journal = load_jsonl(jpath)
    today_entries = [e for e in journal if today in e.get("recorded_at","")]

    rc_dist: dict[str, int] = Counter()
    for e in today_entries:
        d = e.get("detail",{})
        if isinstance(d, dict):
            rc_dist[str(d.get("retcode","none"))] += 1

    total = len(today_entries)
    rejected = rc_dist.get("10025",0) + rc_dist.get("10031",0) + rc_dist.get("10006",0)
    reject_rate = rejected/max(total,1)*100

    print(f"\n  {name} today: {total} entries, {reject_rate:.0f}% rejected")
    print(f"    RC dist: {dict(rc_dist.most_common(5))}")
    if reject_rate > 20 and total > 10:
        issue("P1", "Bridge", f"{name}: {reject_rate:.0f}% MT5 reject rate ({rejected}/{total})")

# ═══════════════════════════════════════════════════════════
# 9. ALERT SYSTEM
# ═══════════════════════════════════════════════════════════
section("9. ALERT SYSTEM (last 4 hours)")

four_h_ago = now - 14400
for name, apath in [("XAU", "data/logs/alert_audit.jsonl"), ("BTC", "data_btc/logs/alert_audit.jsonl")]:
    mtime = os.path.getmtime(apath)
    age = (now - mtime) / 60

    alerts = load_jsonl(apath)
    recent = []
    for a in alerts:
        try:
            t = datetime.fromisoformat(a.get("recorded_at",""))
            if t.timestamp() > four_h_ago:
                recent.append(a)
        except: pass

    warn_types: dict[str, int] = Counter()
    critical_count = 0
    for a in recent:
        detail = a.get("detail",{})
        if isinstance(detail, dict):
            sev = detail.get("severity","")
            if sev == "critical":
                critical_count += 1
            rn = detail.get("rule_name","")
            if rn not in ("system_online","trade_notification"):
                warn_types[rn] += 1

    print(f"\n  {name}: {len(recent)} alerts in 4h, {critical_count} critical, file age={age:.0f}min")
    if warn_types:
        print(f"    Warnings: {dict(warn_types.most_common(5))}")
    if critical_count > 10:
        issue("P1", "Alert", f"{name}: {critical_count} critical alerts in 4h")

# ═══════════════════════════════════════════════════════════
# FINAL SUMMARY
# ═══════════════════════════════════════════════════════════
section("FINAL SUMMARY")

p0 = [i for i in ISSUES if i[0]=="P0"]
p1 = [i for i in ISSUES if i[0]=="P1"]
p2 = [i for i in ISSUES if i[0]=="P2"]

print(f"\n  P0 (Critical): {len(p0)}")
for s, a, m in p0:
    print(f"    [{a}] {m}")

print(f"\n  P1 (Warning): {len(p1)}")
for s, a, m in p1:
    print(f"    [{a}] {m}")

print(f"\n  P2 (Info): {len(p2)}")
for s, a, m in p2:
    print(f"    [{a}] {m}")

if not p0 and not p1:
    print("\n  === ALL CLEAN ===")

print(f"\n  Audit completed at {datetime.now().isoformat()[:19]}")
