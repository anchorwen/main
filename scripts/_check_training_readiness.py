import json
import os
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

print('=== Training Trigger Conditions ===')
print()

# 1. BTC trades
with open('data_btc/live_trade_journal.jsonl', encoding='utf-8') as f:
    journal = [json.loads(l) for l in f if l.strip()]
tickets = set()
for j in journal:
    t = j.get('position_ticket')
    if t and j.get('action') == 'close':
        tickets.add(str(t))
btc_trades = len(tickets)
print(f'1. BTC MetaFilter Path B: {btc_trades}/200 trades')
if btc_trades >= 200:
    print('   [READY] >=200 trades -- trigger MetaFilter retraining')
else:
    print(f'   [PENDING] need {200-btc_trades} more')

# 2. Calibrator
cal_path = 'data_btc/calibrator_feed_state.json'
if os.path.exists(cal_path):
    with open(cal_path, encoding='utf-8') as f:
        cal = json.load(f)
    samples = cal.get('sample_count', 0)
    phase = cal.get('phase', '?')
    print(f'2. BTC Calibrator: {samples} samples, phase={phase}')
    if phase == 'HOT' or samples >= 200:
        print('   [READY] HOT phase')
    else:
        print(f'   [PENDING] need {200-samples} more for HOT')

# 3. Retraining signals
for sym, base in [('XAU', 'data'), ('BTC', 'data_btc')]:
    path = f'{base}/reports/retraining_signal_prev.json'
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            sig = json.load(f)
        print(f'3. {sym} Retraining: urgency={sig.get("overall_urgency","?")} assessed={sig.get("total_brains_assessed",0)} degraded={sig.get("degraded_count",0)}')

# 4. Label coverage
for sym, base in [('XAU', 'data'), ('BTC', 'data_btc')]:
    labels_path = f'{base}/reports/live_labels.jsonl'
    if os.path.exists(labels_path):
        with open(labels_path, encoding='utf-8') as f:
            labels = [json.loads(l) for l in f if l.strip()]
        unlabeled = [l for l in labels if l.get('label') == 'unlabeled']
        pct = (1 - len(unlabeled)/max(len(labels),1)) * 100
        print(f'4. {sym} Label coverage: {pct:.1f}% ({len(unlabeled)} unlabeled of {len(labels)})')
        print(f'   {"[OK] >90%" if pct > 90 else "[WARN] <90%"}')

# 5. Feature store freshness
for sym, base, symbol in [('XAU', 'data', 'XAUUSDc'), ('BTC', 'data_btc', 'BTCUSDc')]:
    fs_path = f'{base}/feature_store/records/symbol={symbol}/timeframe=M5/features.jsonl'
    if os.path.exists(fs_path):
        mtime = datetime.fromtimestamp(os.path.getmtime(fs_path))
        age_min = (datetime.now() - mtime).total_seconds() / 60
        print(f'5. {sym} Feature store: {age_min:.0f}min old')
        print(f'   {"[OK] fresh" if age_min < 30 else "[WARN] stale"}')
    else:
        print(f'5. {sym} Feature store: NOT FOUND')

# Summary
print()
print('=== READINESS SUMMARY ===')
ready = []
if btc_trades >= 200: ready.append('MetaFilter Path B')
print(f'Ready: {ready if ready else "(none)"}')
print(f'Next check: BTC MetaFilter in {200-btc_trades} trades (~{(200-btc_trades)/19:.0f} days at current rate)')
