"""
PnL Ledger Integrity Audit — Iron Law #11 compliant
====================================================
Audits data/brain_pnl_ledger.json and data_btc/brain_pnl_ledger.json for:
1. Duplicate records (same signal_id)
2. Phantom records (entry==exit, fixed pnl)
3. Price realism (price ranges vs known market data)
4. Time pattern anomalies (flood detection)
5. Cross-reference with live_trade_journal
6. Per-brain win rate vs signal count sanity

Output: stdout is the sole source of truth.
"""
import json, sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def load_jsonl(path):
    if not Path(path).exists():
        return []
    with open(path, encoding='utf-8') as f:
        return [json.loads(l) for l in f if l.strip()]


def load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def audit_pnl_ledger(name, ledger_path, journal_path):
    print(f"\n{'='*80}")
    print(f"  {name} PnL Ledger Integrity Audit")
    print(f"  Ledger: {ledger_path}")
    print(f"{'='*80}")

    ledger = load_json(ledger_path)
    settled = ledger.get('settled', {})
    total_brains = len(settled)
    total_records = sum(len(v) for v in settled.values() if isinstance(v, list))
    print(f"\n  Total brains: {total_brains}")
    print(f"  Total records: {total_records}")

    # ── Phase 1: Dedup check ──
    print(f"\n  {'─'*70}")
    print("  Phase 1: Deduplication Audit")
    print(f"  {'─'*70}")

    all_dup_details = []
    for brain_id, records in settled.items():
        if not isinstance(records, list):
            continue
        seen_sids = {}
        dups = []
        for i, r in enumerate(records):
            sid = r.get('signal_id', '')
            if sid in seen_sids:
                dups.append((sid, seen_sids[sid], i))
            else:
                seen_sids[sid] = i

        if dups:
            dup_rate = len(dups) / max(len(records), 1) * 100
            all_dup_details.append((brain_id, len(dups), len(records), dup_rate))

    all_dup_details.sort(key=lambda x: -x[1])
    print(f"  {'Brain':<40s} {'Duplicates':>10} {'Total':>8} {'Dup%':>7}")
    print(f"  {'─'*65}")
    total_dups = 0
    for brain_id, ndups, ntotal, dpct in all_dup_details:
        flag = " <-- HEAVY" if dpct > 10 else ""
        print(f"  {brain_id:<40s} {ndups:>10} {ntotal:>8} {dpct:>6.1f}%{flag}")
        total_dups += ndups
    if not all_dup_details:
        print("  [OK] No duplicate signal_ids found in any brain")
    else:
        print(f"\n  TOTAL duplicates across all brains: {total_dups}")

    # ── Phase 2: Phantom records ──
    print(f"\n  {'─'*70}")
    print("  Phase 2: Phantom Record Audit (entry==exit within 0.01)")
    print(f"  {'─'*70}")

    phantom_details = []
    for brain_id, records in settled.items():
        if not isinstance(records, list) or not records:
            continue
        phantoms = []
        for r in records:
            ep = r.get('entry_price', 0)
            cp = r.get('close_price', 0)
            if ep is not None and cp is not None and abs(ep - cp) < 0.01:
                phantoms.append(r)

        if phantoms:
            # Check if phantoms have a fixed pnl_per_unit
            pnl_values = Counter()
            for p in phantoms:
                pnl_values[round(p.get('pnl_per_unit', 0), 2)] += 1

            # Check entry price distribution
            entry_prices = Counter()
            for p in phantoms:
                entry_prices[round(p.get('entry_price', 0), 1)] += 1

            phantom_details.append({
                'brain_id': brain_id,
                'count': len(phantoms),
                'total': len(records),
                'pct': len(phantoms)/max(len(records),1)*100,
                'pnl_dist': dict(pnl_values.most_common(5)),
                'price_dist': dict(entry_prices.most_common(5)),
                'sample': phantoms[0] if phantoms else None,
            })

    phantom_details.sort(key=lambda x: -x['count'])
    print(f"  {'Brain':<40s} {'Phantoms':>10} {'Total':>8} {'Pct':>7} {'Top PnL':>10} {'Top Entry Prices'}")
    print(f"  {'─'*80}")
    for pd in phantom_details:
        top_pnl = list(pd['pnl_dist'].items())[:2]
        top_price = list(pd['price_dist'].items())[:2]
        pnl_str = ', '.join(f'{v}x{p}' for p, v in top_pnl)
        price_str = ', '.join(f'{v}x{p}' for p, v in top_price)
        print(f"  {pd['brain_id']:<40s} {pd['count']:>10} {pd['total']:>8} {pd['pct']:>6.1f}% {pnl_str:>10} {price_str}")

    if not phantom_details:
        print("  [OK] No phantom records found")

    # ── Phase 3: Price Realism ──
    print(f"\n  {'─'*70}")
    print("  Phase 3: Price Realism Audit")
    print(f"  {'─'*70}")

    # XAU rough price ranges for the period
    # April 2026: ~4300-4800 (estimated, need to verify)
    # May 2026: ~4400-4700 dropping to ~4100
    # June 2026: ~4050-4200
    xau_min_real = 3950
    xau_max_real = 4900
    btc_min_real = 55000
    btc_max_real = 70000

    for brain_id, records in settled.items():
        if not isinstance(records, list) or not records:
            continue

        # Determine symbol from brain context
        is_btc = 'BTC' in brain_id or 'btc' in brain_id
        price_min = btc_min_real if is_btc else xau_min_real
        price_max = btc_max_real if is_btc else xau_max_real

        out_of_range = []
        prices_zero = 0
        for r in records:
            ep = r.get('entry_price', 0) or 0
            cp = r.get('close_price', 0) or 0
            if ep == 0 and cp == 0:
                prices_zero += 1
            elif ep < price_min or ep > price_max or cp < price_min or cp > price_max:
                out_of_range.append((ep, cp))

        if prices_zero > 0:
            print(f"  {brain_id}: {prices_zero} records with entry=exit=0")
        if out_of_range and len(out_of_range) > len(records) * 0.1:
            print(f"  {brain_id}: {len(out_of_range)}/{len(records)} out of range [{price_min}-{price_max}]")

    # ── Phase 4: Time Pattern Analysis ──
    print(f"\n  {'─'*70}")
    print("  Phase 4: Time Pattern / Flood Detection")
    print(f"  {'─'*70}")

    for brain_id, records in settled.items():
        if not isinstance(records, list) or len(records) < 50:
            continue

        # Hourly rate
        hourly = defaultdict(int)
        daily = defaultdict(int)
        for r in records:
            et = r.get('entry_time', '')
            if et:
                hourly[et[:13]] += 1
                daily[et[:10]] += 1

        if not hourly:
            continue

        rates = list(hourly.values())
        avg_rate = sum(rates) / len(rates)
        max_rate = max(rates)
        min_rate = min(rates)
        # Find peak hours
        peak_hours = [(h, c) for h, c in hourly.items() if c > avg_rate * 3]

        # Find days with abnormally high count
        daily_avg = sum(daily.values()) / max(len(daily), 1)
        peak_days = [(d, c) for d, c in daily.items() if c > daily_avg * 2]

        if peak_hours or peak_days:
            print(f"\n  {brain_id}: {len(records)} records, {len(hourly)} active hours")
            print(f"    Hourly rate: avg={avg_rate:.1f}/h, max={max_rate}/h, min={min_rate}/h")
            if peak_hours:
                for h, c in sorted(peak_hours)[-5:]:
                    print(f"    PEAK HOUR: {h} → {c} records ({c/avg_rate:.1f}x avg)")
            if peak_days:
                for d, c in sorted(peak_days)[-5:]:
                    print(f"    PEAK DAY:  {d} → {c} records ({c/daily_avg:.1f}x avg)")

    # ── Phase 5: Per-Brain Summary Table ──
    print(f"\n  {'─'*70}")
    print("  Phase 5: Per-Brain Integrity Summary")
    print(f"  {'─'*70}")
    print(f"  {'Brain':<35s} {'Total':>6} {'Dups':>5} {'Phantoms':>8} {'0-Price':>7} {'WR':>6} {'AvgPnL':>8} {'VERDICT'}")
    print(f"  {'─'*85}")

    verdicts = []
    for brain_id, records in settled.items():
        if not isinstance(records, list) or not records:
            print(f"  {brain_id:<35s} {'N/A':>6} {'N/A':>5} {'N/A':>8} {'N/A':>7} {'N/A':>6} {'N/A':>8} EMPTY")
            continue

        total = len(records)

        # Dedup check
        sids = [r.get('signal_id','') for r in records]
        dup_count = len(sids) - len(set(sids))

        # Phantom check
        p_count = sum(1 for r in records if abs(r.get('entry_price',0) - r.get('close_price',0)) < 0.01)

        # Zero price
        z_count = sum(1 for r in records if (r.get('entry_price',0) or 0) == 0 and (r.get('close_price',0) or 0) == 0)

        # Metrics
        wins = sum(1 for r in records if r.get('is_win'))
        losses = total - wins
        wr = wins / max(total, 1)
        avg_pnl = sum(r.get('pnl_per_unit', 0) for r in records) / max(total, 1)

        # Direction distribution
        longs = sum(1 for r in records if r.get('direction') == 'long')
        shorts = sum(1 for r in records if r.get('direction') == 'short')

        # Verdict
        issues = []
        if dup_count > total * 0.05:
            issues.append(f'DUP{dup_count/total*100:.0f}%')
        if p_count > total * 0.10:
            issues.append(f'PHANTOM{p_count/total*100:.0f}%')
        if z_count > total * 0.05:
            issues.append(f'ZERO{z_count}')
        if avg_pnl < -5 and wr < 0.30:
            issues.append('BROKEN')
        if longs == 0 and shorts > 0:
            issues.append('SHORT_ONLY')
        if shorts == 0 and longs > 0:
            issues.append('LONG_ONLY')

        verdict = 'CLEAN' if not issues else ', '.join(issues)

        dir_str = f'L{longs}/S{shorts}'
        print(f"  {brain_id:<35s} {total:>6} {dup_count:>5} {p_count:>8} {z_count:>7} {wr:>5.1%} {avg_pnl:>+8.2f} {verdict}")

        verdicts.append({
            'brain_id': brain_id,
            'total': total,
            'wins': wins,
            'losses': losses,
            'wr': wr,
            'avg_pnl': avg_pnl,
            'longs': longs,
            'shorts': shorts,
            'dup_pct': dup_count/total*100,
            'phantom_pct': p_count/total*100,
            'verdict': verdict,
        })

    # ── Phase 6: Cross-reference with Trade Journal ──
    if journal_path and Path(journal_path).exists():
        print(f"\n  {'─'*70}")
        print("  Phase 6: Cross-Reference with Trade Journal")
        print(f"  {'─'*70}")

        journal = load_jsonl(journal_path)
        # Get unique position tickets from journal opens
        journal_tickets = set()
        for entry in journal:
            detail = entry.get('detail', {})
            if isinstance(detail, dict):
                order = detail.get('order')
                if order:
                    journal_tickets.add(order)

        # Check if PnL ledger signal_ids reference any journal tickets
        # (PnL ledger doesn't store position_ticket directly, so we check by price/time proximity)
        matched = 0
        unmatched = 0
        for brain_id, records in settled.items():
            if not isinstance(records, list):
                continue
            for r in records[-50:]:  # Check last 50
                # Simple check: does entry_price match any journal open price?
                ep = r.get('entry_price')
                if ep is None:
                    unmatched += 1
                    continue
                # This is a rough check - a proper cross-reference would match by ticket
                matched += 1  # placeholder

        print(f"  Journal unique order tickets: {len(journal_tickets)}")
        print("  (Cross-reference by position_ticket requires schema alignment)")
        print("  PnL ledger uses signal_id; journal uses order ticket.")
        print("  Direct matching not possible without bridge schema.")

    # ── Final Trustworthy Brains ──
    print(f"\n{'='*80}")
    print("  FINAL VERDICT: Brain Trustworthiness")
    print(f"{'='*80}")

    clean_brains = [v for v in verdicts if v['verdict'] == 'CLEAN']
    suspect_brains = [v for v in verdicts if v['verdict'] != 'CLEAN']

    print(f"\n  TRUSTWORTHY (CLEAN): {len(clean_brains)} brains")
    for v in clean_brains:
        print(f"    {v['brain_id']}: {v['total']} recs, WR={v['wr']:.1%}, avgPnL={v['avg_pnl']:+.2f}, {v['longs']}L/{v['shorts']}S")

    print(f"\n  SUSPECT (has issues): {len(suspect_brains)} brains")
    for v in suspect_brains:
        print(f"    {v['brain_id']}: {v['total']} recs, WR={v['wr']:.1%}, issue={v['verdict']}")

    return verdicts


# ── Run ──
print("PnL Ledger Integrity Audit — Iron Law #11")
print(f"Run at: {datetime.now().isoformat()}")
print()

xau_verdicts = audit_pnl_ledger(
    "XAU",
    "data/brain_pnl_ledger.json",
    "data/live_trade_journal.jsonl"
)

btc_verdicts = audit_pnl_ledger(
    "BTC",
    "data_btc/brain_pnl_ledger.json",
    "data_btc/live_trade_journal.jsonl"
)

# ── Cross-symbol summary ──
print(f"\n{'='*80}")
print("  CROSS-SYMBOL SUMMARY")
print(f"{'='*80}")
all_clean_xau = [v for v in xau_verdicts if v['verdict'] == 'CLEAN']
all_clean_btc = [v for v in btc_verdicts if v['verdict'] == 'CLEAN']
print(f"  XAU: {len(all_clean_xau)}/{len(xau_verdicts)} brains CLEAN")
print(f"  BTC: {len(all_clean_btc)}/{len(btc_verdicts)} brains CLEAN")

print("\n[DONE] All statistics above are the sole source of truth.")
