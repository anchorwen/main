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

    # Build samples
    samples = []
    for ticket, open_entry in ticket_open.items():
        if ticket not in ticket_close:
            continue
        close_entry = ticket_close[ticket]
        ts = str(open_entry.get("recorded_at", ""))[:16]
        if ts not in fs_index:
            continue

        fs_entry = fs_index[ts]
        values = fs_entry.get("values", {})
        if not values or not isinstance(values, dict):
            continue

        # 41-dim feature vector
        feature_names = sorted(values.keys())
        vec = [float(values.get(k, 0.0) or 0.0) for k in feature_names]

        # Get brain confidence from open entry
        confidence = float(open_entry.get("confidence", 0.5) or 0.5)
        vec.append(confidence)  # 42nd dimension

        # Label
        pnl = close_entry.get("pnl") or 0.0
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
