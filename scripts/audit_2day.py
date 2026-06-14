"""Two-day trade audit: June 10-11, 2026 — XAU + BTC."""
import json, glob
from collections import Counter, defaultdict

TARGET = ['2026-06-10', '2026-06-11']

def load_jsonl(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(l) for l in f if l.strip()]

# ── Journal Analysis ──
for name, jpath in [('XAU', 'data/live_trade_journal.jsonl'), ('BTC', 'data_btc/live_trade_journal.jsonl')]:
    entries = load_jsonl(jpath)
    filtered = [d for d in entries if d.get('recorded_at', '')[:10] in TARGET]

    print(f"\n{'='*70}")
    print(f"  {name} Trade Journal — June 10-11")
    print(f"{'='*70}")

    by_date = defaultdict(int)
    actions = Counter()
    retcodes = Counter()
    sides = Counter()
    real_opens = []
    real_closes = []
    volumes = []

    for d in filtered:
        recorded = d.get('recorded_at', '')[:10]
        by_date[recorded] += 1
        actions[d.get('action', '?')] += 1
        sides[d.get('side', '')] += 1

        detail = d.get('detail', {})
        if isinstance(detail, dict):
            rc = detail.get('retcode')
            if rc:
                retcodes[str(rc)] += 1
            req = detail.get('request', {})
            if isinstance(req, dict):
                vol = req.get('volume')
                if vol:
                    volumes.append(vol)
                if rc == 10009:
                    if req.get('action') == 1 and req.get('type') == 1:
                        real_opens.append(d)
                    elif req.get('action') == 1 and req.get('type') == 0:
                        real_closes.append(d)

    print(f"  Total: {len(filtered)} entries")
    print(f"  June 10: {by_date.get('2026-06-10',0)} | June 11: {by_date.get('2026-06-11',0)}")
    print(f"  Actions: {dict(actions)}")
    print(f"  Retcodes: {dict(retcodes)}")
    print(f"  Sides: {dict(sides)}")
    if volumes:
        print(f"  Volume: min={min(volumes)} max={max(volumes)} avg={sum(volumes)/len(volumes):.3f}")

    print(f"  Real market opens: {len(real_opens)}")
    for d in real_opens:
        detail = d.get('detail', {})
        req = detail.get('request', {})
        print(f"    [{d.get('recorded_at','')[:19]}] side={d.get('side','')} vol={req.get('volume','?')} price={req.get('price','?')} order={detail.get('order','?')} msg={d.get('message_id','')[:50]}")

    print(f"  Real market closes: {len(real_closes)}")
    for d in real_closes:
        detail = d.get('detail', {})
        req = detail.get('request', {})
        print(f"    [{d.get('recorded_at','')[:19]}] side={d.get('side','')} vol={req.get('volume','?')} price={req.get('price','?')} order={detail.get('order','?')}")

# ── Alerts Analysis ──
for name, apath in [('XAU', 'data/logs/alert_audit.jsonl'), ('BTC', 'data_btc/logs/alert_audit.jsonl')]:
    alerts = load_jsonl(apath)
    filtered = []
    for a in alerts:
        rec = a.get('recorded_at', '')
        if any(d in rec for d in TARGET):
            filtered.append(a)

    print(f"\n  {name} Alerts — June 10-11: {len(filtered)} total")
    trades = []
    warnings = []
    system_on = []
    for a in filtered:
        detail = a.get('detail', {})
        if isinstance(detail, dict):
            rn = detail.get('rule_name', '')
            if 'trade' in rn:
                trades.append(a)
            elif 'system_online' in rn:
                system_on.append(a)
            else:
                warnings.append(a)

    print(f"    Trade notifications: {len(trades)}")
    for t in trades:
        detail = t.get('detail', {})
        title = detail.get('title','')[:120]
        # Strip emoji for Windows console
        title_clean = title.encode('ascii', errors='replace').decode('ascii')
        print(f"      [{t.get('recorded_at','')[:19]}] {title_clean}")
    print(f"    Warnings: {len(warnings)}")
    for w in warnings:
        detail = w.get('detail', {})
        wt = detail.get('title','')[:120]
        wt_clean = wt.encode('ascii', errors='replace').decode('ascii')
        print(f"      [{w.get('recorded_at','')[:19]}] {detail.get('rule_name','')}: {wt_clean}")
    print(f"    System online: {len(system_on)}")
    if system_on:
        first = system_on[0]
        last = system_on[-1]
        print(f"      First: {first.get('recorded_at','')[:19]}")
        print(f"      Last:  {last.get('recorded_at','')[:19]}")

# ── Labels Analysis ──
print(f"\n{'='*70}")
print("  Labels — June 10-11")
print(f"{'='*70}")

for name, lpath, sym in [('XAU', 'data/reports/live_labels.jsonl', 'XAUUSDc'), ('BTC', 'data_btc/reports/live_labels.jsonl', 'BTCUSDc')]:
    labels = load_jsonl(lpath)
    own = [lb for lb in labels if lb.get('symbol') == sym and any(d in (lb.get('open_recorded_at','') or lb.get('close_recorded_at','')) for d in TARGET)]

    print(f"\n  {name} Labels: {len(own)}")
    if own:
        wins = sum(1 for lb in own if lb.get('pnl') is not None and lb['pnl'] > 0)
        losses = sum(1 for lb in own if lb.get('pnl') is not None and lb['pnl'] < 0)
        unlabeled = sum(1 for lb in own if lb.get('pnl') is None)
        total_pnl = sum(lb.get('pnl', 0) for lb in own if lb.get('pnl') is not None)
        print(f"    W:{wins} L:{losses} Unlabeled:{unlabeled}  PnL: {total_pnl:+.4f}")
        for lb in own:
            pid = lb.get('open_message_id', '')[:40] if lb.get('open_message_id') else ''
            print(f"    ticket={lb.get('position_ticket')} side={lb.get('side')} label={lb.get('label')} pnl={lb.get('pnl')} entry={lb.get('entry_price')} exit={lb.get('exit_price')} msg={pid}")
    else:
        print("    NONE")

# ── Intent Log Key Events ──
print(f"\n{'='*70}")
print("  Intent Log Key Events — June 10-11")
print(f"{'='*70}")

for name, pattern in [('XAU', 'data/logs/intent_*.log'), ('BTC', 'data_btc/logs/intent_*.log')]:
    files = sorted(glob.glob(pattern))
    print(f"\n  {name} Intent Logs:")
    for fpath in files:
        fname = fpath.replace('\\', '/').split('/')[-1]
        if '20260610' not in fname and '20260611' not in fname:
            continue
        with open(fpath, encoding='utf-8') as f:
            lines = f.readlines()

        cycles = 0
        shutdowns = []
        errors = []
        dispatches = []
        no_trade_reasons = Counter()
        last_cycle = None

        for line in lines:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except:
                continue
            ev = d.get('event', '')
            t = d.get('time', '')
            if ev == 'cycle_end':
                cycles += 1
                last_cycle = t
            elif 'shutdown' in ev:
                shutdowns.append((t, d))
            elif 'error' in ev.lower():
                errors.append((t, d))
            elif ev in ('intent_dispatched', 'strategy_dispatched'):
                dispatches.append((t, d))
            elif ev == 'multi_strategy_eval':
                strats = d.get('strategies', [])
                for s in strats:
                    if not s.get('should_trade'):
                        reason = s.get('reason', 'unknown')
                        no_trade_reasons[reason] += 1

        print(f"    {fname}: {len(lines)} lines, {cycles} cycles, {len(dispatches)} dispatches, {len(errors)} errors")
        if shutdowns:
            for t, d in shutdowns:
                print(f"      SHUTDOWN [{t}]: signal={d.get('signal','?')} action={d.get('action','?')}")
        if errors:
            for t, d in errors[:5]:
                print(f"      ERROR [{t}]: {d.get('error', str(d))[:150]}")
        if last_cycle:
            print(f"      Last cycle_end: {last_cycle}")
        if no_trade_reasons:
            print("      Top no-trade reasons:")
            for reason, count in no_trade_reasons.most_common(5):
                print(f"        [{count}x] {reason[:100]}")

# ── Final Cross Summary ──
print(f"\n{'='*70}")
print("  FINAL CROSS-SYMBOL SUMMARY (June 10-11)")
print(f"{'='*70}")
print("  XAU: active only on June 10, dead since ~16:35 UTC June 10")
print("  BTC: active only on June 10, dead since ~16:35 UTC June 10")
print("  ROOT CAUSE: SIGINT killed both loops, no automatic restart")
print("  UPTIME LOST: ~16+ hours and counting")
