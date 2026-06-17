#!/usr/bin/env python
"""MetaFilter Path B — Build 42-dim training dataset.

IC Hardening requirements:
  1. 41-dim feature store vector + brain confidence → 42-dim
  2. Label: close.pnl > 0 → is_win=1

Usage:
    python scripts/build_metafilter_dataset.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
JOURNAL = ROOT / "data_btc" / "live_trade_journal.jsonl"
FS = ROOT / "data_btc" / "feature_store" / "records" / "symbol=BTCUSDc" / "timeframe=M5" / "features.jsonl"
OUT = ROOT / "data_btc" / "models" / "metafilter_path_b_train.jsonl"


def main() -> int:
    print("=" * 60)
    print("  MetaFilter Path B — Dataset Builder")
    print("=" * 60)

    # Load journal
    with open(JOURNAL, encoding="utf-8") as f:
        journal = [json.loads(l) for l in f if l.strip()]
    print(f"Journal: {len(journal)} entries")

    # Map tickets → closes
    ticket_close = {}
    ticket_open = {}
    for j in journal:
        ticket = j.get("position_ticket")
        if not ticket:
            continue
        ticket = str(ticket)
        if j.get("action") == "close":
            pnl = j.get("pnl")
            if pnl is not None:
                ticket_close[ticket] = j
        if j.get("action") in ("open", None) and "eq_btc_swing" in str(j.get("message_id", "")):
            ticket_open[ticket] = j

    print(f"Tickets: {len(ticket_open)} opens, {len(ticket_close)} closes")

    # Load feature store → index by minute
    fs_index: dict[str, dict] = {}
    with open(FS, encoding="utf-8") as f:
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
                fs_index[ts] = entry
    print(f"Feature store: {len(fs_index)} unique minutes")

    # Build samples with quality filtering
    samples = []
    skipped_noise = 0
    skipped_abnormal = 0
    for ticket, open_entry in ticket_open.items():
        if ticket not in ticket_close:
            continue
        close_entry = ticket_close[ticket]
        pnl = close_entry.get("pnl") or 0.0

        # ── Quality filter 1: remove PnL=0 noise (breakevens, system glitches) ──
        if abs(pnl) < 0.001:
            skipped_noise += 1
            continue

        # ── Quality filter 2: remove abnormal closes ──
        reason = ""
        if isinstance(close_entry.get("detail"), dict):
            reason = str(close_entry["detail"].get("reason", "")).lower()
        if reason in ("position_not_found", "client_close", "unknown"):
            skipped_abnormal += 1
            continue

        ts = str(open_entry.get("recorded_at", ""))[:16]
        if ts not in fs_index:
            continue

        fs_entry = fs_index[ts]
        values = fs_entry.get("values", {})
        if not values or not isinstance(values, dict):
            continue

        # 41-dim feature vector (sorted keys for deterministic order)
        feature_names = sorted(values.keys())
        vec = [float(values.get(k, 0.0) or 0.0) for k in feature_names]

        # Verify dimension via Router contract (FIX-092)
        from core.features.feature_router import FeatureRouter
        _router = FeatureRouter()
        _lake = _router.build_lake(legacy_v9_vector=vec)
        try:
            _tensor = _router.dispatch(_lake, "v9_institutional_40")
            if len(_tensor) != 40:
                continue  # dimension mismatch, skip
        except Exception:
            continue

        # Feature 42: entry_spread (cost of entry)
        entry_spread = 0.0
        if isinstance(open_entry.get("entry_context"), dict):
            entry_spread = float(open_entry["entry_context"].get("entry_spread", 0) or 0)
        vec.append(entry_spread)

        # Feature 43: brain confidence
        confidence = float(open_entry.get("confidence", 0.5) or 0.5)
        vec.append(confidence)

        # Features 44-48: OFI data from IPC file (if available)
        import os as _os
        _ofi_path = "data_btc/reports/ofi_snapshot.json"
        if _os.path.exists(_ofi_path):
            try:
                with open(_ofi_path, encoding="utf-8") as _of:
                    _ofi_data = json.loads(_of.read())
                vec.append(float(_ofi_data.get("OFI_M5", 0) or 0))
                vec.append(float(_ofi_data.get("OFI_ZScore_20", 0) or 0))
                vec.append(float(_ofi_data.get("OFI_Cumulative_1H", 0) or 0))
                vec.append(float(_ofi_data.get("OFI_Tick_Count", 0) or 0))
                vec.append(float(_ofi_data.get("OFI_Total_Volume", 0) or 0))
            except Exception:
                vec.extend([0.0, 0.0, 0.0, 0.0, 0.0])  # OFI unavailable
        else:
            vec.extend([0.0, 0.0, 0.0, 0.0, 0.0])

        # Label
        is_win = 1 if pnl > 0 else 0

        samples.append({
            "ticket": ticket,
            "features": vec,
            "feature_names": feature_names + ["brain_confidence"],
            "is_win": is_win,
            "pnl": pnl,
            "side": open_entry.get("side", "?"),
            "brain_ids": open_entry.get("brain_ids", []),
        })

    print(f"\nSamples built: {len(samples)}")
    wins = sum(1 for s in samples if s["is_win"])
    losses = len(samples) - wins
    print(f"Wins: {wins}, Losses: {losses}, WR: {wins/len(samples)*100:.1f}%")

    # By side
    long_samples = [s for s in samples if s["side"] == "long"]
    short_samples = [s for s in samples if s["side"] == "short"]
    print(f"LONG:  {len(long_samples)} samples, wins={sum(1 for s in long_samples if s['is_win'])}")
    print(f"SHORT: {len(short_samples)} samples, wins={sum(1 for s in short_samples if s['is_win'])}")

    # Save
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"\nSaved: {OUT} ({len(samples)} samples, {len(feature_names)+1} dims)")

    print("\n[DONE] All statistics above are the sole source of truth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
