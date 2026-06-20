# type: ignore
#!/usr/bin/env python
"""BTC data pipeline integrity audit — Iron Law #11 compliant"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.runtime.fault_handler import fail_open_guard

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(UTC)
CUTOFF_24H = NOW - timedelta(hours=24)
CUTOFF_7D = NOW - timedelta(days=7)

print("=" * 70)
print("  BTC DATA PIPELINE INTEGRITY AUDIT")
print(f"  {NOW.strftime('%Y-%m-%d %H:%M')} UTC")
print("=" * 70)

# ── 1. JOURNAL ──
print("\n── 1. Trade Journal ──")
journal = ROOT / "data_btc" / "live_trade_journal.jsonl"
opens_all, closes_all = 0, 0
opens_24h, closes_24h = 0, 0
opens_with_ctx = 0
opens_missing_vec = 0
tickets_open: dict[str, dict] = {}
tickets_close: dict[str, dict] = {}

with open(journal, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = entry.get("action")
        ticket = str(entry.get("position_ticket", ""))
        ts = entry.get("recorded_at", "")

        if action == "close":
            if entry.get("pnl") is not None:
                closes_all += 1
                tickets_close[ticket] = entry
                if ts > CUTOFF_24H.isoformat():
                    closes_24h += 1
        elif action in ("open", None):
            if "eq_btc_swing" in str(entry.get("message_id", "")):
                opens_all += 1
                tickets_open[ticket] = entry
                if ts > CUTOFF_24H.isoformat():
                    opens_24h += 1
                ctx = entry.get("entry_context", {})
                if isinstance(ctx, dict) and ctx:
                    opens_with_ctx += 1
                    if not ctx.get("vector"):
                        opens_missing_vec += 1

print(f"  Total opens:             {opens_all}")
print(f"  Total closes:            {closes_all}")
print(f"  Opens (24h):             {opens_24h}")
print(f"  Closes (24h):            {closes_24h}")
if opens_all:
    print(f"  Opens w/ entry_context:  {opens_with_ctx}/{opens_all} ({opens_with_ctx/opens_all*100:.0f}%)")
print(f"  Missing feature vector:  {opens_missing_vec}")

# ── 2. FEATURE STORE ──
print("\n── 2. Feature Store (M5) ──")
fs = ROOT / "data_btc" / "feature_store" / "records" / "symbol=BTCUSDc" / "timeframe=M5" / "features.jsonl"
fs_total = 0
fs_24h = 0
fs_7d = 0
fs_latest = ""
fs_fields = 0
fs_has_ou = False
fs_has_hurst = False

with open(fs, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        fs_total += 1
        ts = entry.get("event_time", "")
        if ts > CUTOFF_24H.isoformat():
            fs_24h += 1
            fs_latest = ts
        if ts > CUTOFF_7D.isoformat():
            fs_7d += 1
        if fs_total == 1:
            vals = entry.get("values", {})
            if isinstance(vals, dict):
                fs_fields = len(vals)
                fs_has_ou = any("OU" in k for k in vals)
                fs_has_hurst = any("Hurst" in k for k in vals)

expected = 288  # M5 bars in 24h
fill = fs_24h / expected * 100 if expected else 0
print(f"  Total records:           {fs_total}")
print(f"  Records (24h):           {fs_24h}/{expected} ({fill:.0f}%)")
print(f"  Records (7d):            {fs_7d}")
print(f"  Fields/record:           {fs_fields}")
print(f"  OU features:             {'YES' if fs_has_ou else 'MISSING'}")
print(f"  Hurst features:          {'YES' if fs_has_hurst else 'MISSING'}")
print(f"  Latest:                  {fs_latest}")

# Gap detection
gaps = []
prev = ""
with open(fs, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = entry.get("event_time", "")
        if ts < CUTOFF_24H.isoformat():
            continue
        if prev:
            try:
                t1 = datetime.fromisoformat(prev)
                t2 = datetime.fromisoformat(ts)
                gm = (t2 - t1).total_seconds() / 60
                if gm > 15:
                    gaps.append(f"{prev} -> {ts}: {gm:.0f}min")
            except ValueError:
                pass
        prev = ts

if gaps:
    print(f"  GAPS (>15min, 24h):     {len(gaps)}")
    for g in gaps[:5]:
        print(f"    {g}")
    if len(gaps) > 5:
        print(f"    ... +{len(gaps)-5} more")
else:
    print("  GAPS:                    NONE")

# ── 3. GOLDEN MASTER ──
print("\n── 3. Golden Master ──")
gm = ROOT / "data_btc" / "golden_master.jsonl"
gm_total = 0
gm_24h = 0
gm_latest = ""
gm_regime = False

with open(gm, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        gm_total += 1
        ts = entry.get("timestamp_utc", "")
        if ts > CUTOFF_24H.isoformat():
            gm_24h += 1
            gm_latest = ts
        if gm_total == 1:
            inp = entry.get("inputs", {})
            if isinstance(inp, dict):
                gm_regime = "trend_direction" in inp

gm_fill = gm_24h / expected * 100 if expected else 0
print(f"  Total cycles:            {gm_total}")
print(f"  Cycles (24h):            {gm_24h}/{expected} ({gm_fill:.0f}%)")
print(f"  Regime data:             {'YES' if gm_regime else 'MISSING'}")
print(f"  Latest:                  {gm_latest}")

# ── 4. OFI ──
print("\n── 4. OFI Tick Data ──")
ofi = ROOT / "data_btc" / "reports" / "ofi_snapshot.json"
if ofi.exists():
    try:
        od = json.loads(ofi.read_text(encoding="utf-8"))
        print("  Status:                  ACTIVE")
        print(f"  OFI_M5:                  {od.get('OFI_M5', 'N/A')}")
        print(f"  OFI_Tick_Count:          {od.get('OFI_Tick_Count', 'N/A')}")
        print(f"  OFI_Total_Volume:        {od.get('OFI_Total_Volume', 'N/A')}")
        print(f"  OFI_ZScore_20:           {od.get('OFI_ZScore_20', 'N/A')}")
        print(f"  OFI_Cumulative_1H:       {od.get('OFI_Cumulative_1H', 'N/A')}")
    except Exception as e:  # BLE001:FOG (Sev 4, Phase 3b)
        with fail_open_guard("data_pipeline_audit:L193"):
            print(f"  Status:                  CORRUPTED ({e})")
else:
    print("  Status:                  MISSING")

# ── 5. MATCH RATE ──
print("\n── 5. Open -> Feature Match (R4 critical) ──")
fs_index: dict[str, bool] = {}
with open(fs, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = entry.get("event_time", "")[:16]
        if ts:
            fs_index[ts] = True

matched = 0
unmatched = 0
unmatched_samples: list[str] = []
weekly_m = 0
weekly_u = 0

for ticket, oe in tickets_open.items():
    ts = str(oe.get("recorded_at", ""))[:16]
    recent = oe.get("recorded_at", "")[:10] >= CUTOFF_7D.strftime("%Y-%m-%d")
    if ts in fs_index:
        matched += 1
        if recent:
            weekly_m += 1
    else:
        unmatched += 1
        if recent:
            weekly_u += 1
        if len(unmatched_samples) < 3:
            unmatched_samples.append(f"ticket={ticket} ts={ts}")

total_t = matched + unmatched
mr = matched / total_t * 100 if total_t else 0
print(f"  Matchable:               {matched}/{total_t} ({mr:.0f}%)")
print(f"  Unmatched:               {unmatched}")
print(f"  Weekly matched:          {weekly_m}")
print(f"  Weekly unmatched:        {weekly_u}")
if unmatched_samples:
    for s in unmatched_samples:
        print(f"    Sample: {s}")

# ── 6. POSITION SNAPSHOTS ──
print("\n── 6. Position Snapshots ──")
snap = ROOT / "data_btc" / "position_snapshots.jsonl"
snap_total = 0
snap_24h = 0
with open(snap, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        snap_total += 1
        if entry.get("time", "") > CUTOFF_24H.isoformat():
            snap_24h += 1
print(f"  Total:                   {snap_total}")
print(f"  24h:                     {snap_24h}")

# ── 7. BRIDGE ──
print("\n── 7. MT5 Bridge ──")
bh = ROOT / "data_btc" / "reports" / "mt5_bridge_health.json"
if bh.exists():
    try:
        bd = json.loads(bh.read_text(encoding="utf-8"))
        print(f"  Connected:               {bd.get('mt5_connected', 'N/A')}")
        print(f"  Last heartbeat:          {bd.get('last_heartbeat_utc', 'N/A')}")
        print(f"  PID:                     {bd.get('pid', 'N/A')}")
        print(f"  Transport:               {bd.get('transport', 'N/A')}")
        print(f"  Outbox pending:          {bd.get('outbox_pending', 'N/A')}")
    except Exception:  # BLE001:FOG
        with fail_open_guard("data_pipeline_audit:L275"):
            print("  CORRUPTED")
else:
    print("  MISSING")

# ── VERDICT ──
print(f"\n{'='*70}")
issues = []
if fill < 80:
    issues.append(f"FS fill {fill:.0f}% < 80%")
if gm_fill < 80:
    issues.append(f"GM fill {gm_fill:.0f}% < 80%")
if not ofi.exists():
    issues.append("OFI missing — TickPoller may not be running")
if mr < 80:
    issues.append(f"Match rate {mr:.0f}% < 80%")
if opens_24h == 0:
    issues.append("No opens in 24h")
if fs_24h == 0:
    issues.append("No FS writes in 24h")

if not issues:
    print("  VERDICT: HEALTHY")
else:
    print(f"  VERDICT: {len(issues)} ISSUE(S):")
    for i in issues:
        print(f"    ! {i}")

print("\n── R4/MetaFilter ETA ──")
print(f"  Current matchable:       {matched}")
print("  Target:                  200")
print(f"  Gap:                     {200 - matched}")
print(f"  Weekly rate:             {weekly_m}/week")
if weekly_m > 0:
    wks = (200 - matched) / weekly_m
    eta = NOW + timedelta(weeks=wks)
    print(f"  ETA 200:                 {eta.strftime('%Y-%m-%d')} (~{wks:.1f} weeks)")
else:
    print("  ETA:                     cannot estimate (0/week)")

print("\n[DONE] All statistics above are the sole source of truth.")
