"""
FIX-20260620-074: XAU Governance Intervention
Promote Swing_V9_M15_V2 probation→live (WR=63.65%, 883 trades).
Backs up governance_state.json before modification.
"""
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

gov_path = Path('data/governance_state.json')
backup_path = Path('data/governance_state_backup_20260620_0730.json')

# Backup
shutil.copy(gov_path, backup_path)
print(f"[BACKUP] {backup_path}")

# Read
gv = json.loads(gov_path.read_text(encoding='utf-8'))
brains = gv.get('brain_states', {})

# Promote
bs = brains.get('Swing_V9_M15_V2')
assert bs is not None, "Swing_V9_M15_V2 not found!"
old_status = bs['status']
assert old_status == 'probation', f"Expected probation, got {old_status}"

bs['status'] = 'live'
bs['last_transition_at'] = datetime.now(UTC).isoformat()
bs['transition_count'] = bs.get('transition_count', 0) + 1
bs['vote_weight'] = 1.0

print(f"[PROMOTE] Swing_V9_M15_V2: {old_status} -> live")
print(f"  WR: {bs['performance_metrics'].get('win_rate')}")
print(f"  Trades: {bs['performance_metrics'].get('total_trades')}")

# Write
gov_path.write_text(json.dumps(gv, indent=2, ensure_ascii=False), encoding='utf-8')

# Verify
gv2 = json.loads(gov_path.read_text(encoding='utf-8'))
bs2 = gv2.get('brain_states', {}).get('Swing_V9_M15_V2', {})
assert bs2['status'] == 'live'
live_count = sum(1 for b in gv2.get('brain_states', {}).values()
                 if isinstance(b, dict) and b.get('status') == 'live')
print(f"[VERIFIED] Live brains: {live_count}")
for bid, b in gv2.get('brain_states', {}).items():
    if isinstance(b, dict) and b.get('status') == 'live':
        pm = b.get('performance_metrics', {})
        print(f"  LIVE: {bid} (WR={pm.get('win_rate')}, trades={pm.get('total_trades')})")
print("[DONE]")
