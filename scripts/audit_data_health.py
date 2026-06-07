#!/usr/bin/env python3
"""实盘数据收集完整性审计"""
import json, os, time
from datetime import datetime, timezone

def banner(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def age_min(ts_str):
    try:
        ts = datetime.fromisoformat(ts_str.replace('Z','+00:00'))
        return (datetime.now(timezone.utc) - ts).total_seconds() / 60
    except (ValueError, TypeError, OSError):
        return 99999

# 1. PnL Ledger
banner("1. PnL Ledger")
for label, path in [('BTC','data_btc/brain_pnl_ledger.json'),('XAU','data/brain_pnl_ledger.json')]:
    if not os.path.exists(path): print(f"  {label}: MISSING"); continue
    size = os.path.getsize(path)
    with open(path) as f: d = json.load(f)
    settled = eval(d.get('settled','{}')) if isinstance(d.get('settled'),str) else d.get('settled',{})
    pending = eval(d.get('pending','{}')) if isinstance(d.get('pending'),str) else d.get('pending',{})
    total_s = sum(len(v) if isinstance(v,list) else 0 for v in settled.values())
    total_p = sum(len(v) if isinstance(v,list) else 0 for v in pending.values())
    latest = ""
    for records in settled.values():
        if isinstance(records,list) and records:
            ts = records[-1].get('entry_time','')
            if ts > latest: latest = ts
    age = age_min(latest) if latest else 99999
    print(f"  {label}: {size:,}B | settled={total_s} pending={total_p} | latest={latest[:19]} | age={age:.0f}min")

# 2. Feature Store
banner("2. Feature Store")
for label, path in [('BTC','data_btc/feature_store/records/symbol=BTCUSDc/timeframe=M5/features.jsonl'),
                     ('XAU','data/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl')]:
    if not os.path.exists(path): print(f"  {label}: MISSING"); continue
    size = os.path.getsize(path); lines = sum(1 for _ in open(path))
    with open(path) as f:
        for line in f: pass
        last = json.loads(line.strip())
    vals = last.get('values',{})
    zeros = sum(1 for v in vals.values() if v==0 or v==0.0)
    nans = sum(1 for v in vals.values() if isinstance(v,float) and str(v)=='nan')
    age = age_min(last.get('event_time',''))
    print(f"  {label}: {lines} lines {size:,}B | dims={len(vals)} zeros={zeros} nans={nans} | age={age:.0f}min")

# 3. Bar Sync
banner("3. Bar Sync")
for label, path in [('BTC','data_btc/bar_sync_state.json'),('XAU','data/bar_sync_state.json')]:
    if not os.path.exists(path): print(f"  {label}: MISSING"); continue
    with open(path) as f: d = json.load(f)
    age = age_min(d.get('last_sync_utc',''))
    print(f"  {label}: bars={d.get('total_bars_seen',0)} lag={d.get('lag_count',0)} | age={age:.0f}min")

# 4. Golden Master
banner("4. Golden Master")
for label, path in [('BTC','data_btc/golden_master.jsonl'),('XAU','data/golden_master.jsonl')]:
    if not os.path.exists(path): print(f"  {label}: MISSING"); continue
    size = os.path.getsize(path); lines = sum(1 for _ in open(path))
    with open(path) as f:
        for line in f: pass
        last = json.loads(line.strip())
    age = age_min(last.get('timestamp_utc',''))
    print(f"  {label}: {lines} lines {size:,}B | age={age:.0f}min")

# 5. Trade Journal
banner("5. Trade Journal")
for label, path in [('BTC','data_btc/live_trade_journal.jsonl'),('XAU','data/live_trade_journal.jsonl')]:
    if not os.path.exists(path): print(f"  {label}: MISSING"); continue
    size = os.path.getsize(path)
    opens = closes = rejects = 0; latest = ""
    with open(path) as f:
        for line in f:
            d = json.loads(line.strip())
            ts = d.get('recorded_at','')
            if ts > latest: latest = ts
            if d.get('ack_status')=='accepted':
                if d.get('action')=='open': opens+=1
                elif d.get('action')=='close': closes+=1
            elif d.get('ack_status')=='rejected': rejects+=1
    age = age_min(latest) if latest else 99999
    print(f"  {label}: {size:,}B | opens={opens} closes={closes} rejects={rejects} | age={age:.0f}min")

# 6. Governance & Execution State
banner("6. Governance + Execution State")
for label, gov_p, exec_p in [('BTC','data_btc/governance_state.json','data_btc/state/execution_state.json'),
                               ('XAU','data/governance_state.json','data/state/execution_state.json')]:
    if os.path.exists(gov_p):
        with open(gov_p) as f: g = json.load(f)
        brains = g.get('brain_states',{})
        live = sum(1 for b in brains.values() if b.get('status')=='live')
        print(f"  {label} gov: {len(brains)} brains live={live} | transitions={len(g.get('transition_log',[]))}")
    if os.path.exists(exec_p):
        with open(exec_p) as f: e = json.load(f)
        age = age_min(e.get('saved_at_utc',''))
        cb = e.get('circuit_breaker_tripped',False)
        dd = e.get('intraday_dd_active',False)
        print(f"  {label} exec: age={age:.0f}min CB={cb} DD={dd}")

# 7. Leaderboard
banner("7. Leaderboard")
for label, path in [('BTC','data_btc/reports/leaderboard.json'),('XAU','data/reports/leaderboard.json')]:
    if not os.path.exists(path): print(f"  {label}: MISSING"); continue
    with open(path) as f: d = json.load(f)
    brains = d.get('brains',[])
    print(f"  {label}: {len(brains)} brains | schema={d.get('schema','?')}")

# 8. Daily Ops
banner("8. Daily Ops")
for label, base in [('BTC','data_btc'),('XAU','data')]:
    recap = os.path.join(base,'reports','daily_recap.json')
    labels = os.path.join(base,'reports','live_labels.jsonl')
    if os.path.exists(recap):
        size = os.path.getsize(recap)
        with open(recap) as f: d = json.load(f)
        age = age_min(d.get('generated_at',''))
        print(f"  {label} recap: {size:,}B age={age:.0f}min")
    if os.path.exists(labels):
        lines = sum(1 for _ in open(labels))
        print(f"  {label} labels: {lines} entries")

print(f"\n{'='*60}")
print("  AUDIT COMPLETE")
print(f"{'='*60}")
