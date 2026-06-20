#!/usr/bin/env python
"""MetaFilter Path B — Build 42-dim training dataset.

IC Hardening requirements:
  1. 40-dim V9 feature store vector + entry_spread + brain_confidence → 42-dim
  2. Label: close.pnl > 0 → is_win=1
  3. PIT (Point-in-Time): only features KNOWN at trade open time are used.
     OFI features were removed (FIX-20260621-027) — they came from a single
     global snapshot with look-ahead bias.  PIT OFI reconstruction requires
     historical OFI snapshots at each trade timestamp, which is future work.

Usage:
    python scripts/build_metafilter_dataset.py [--data-dir data_btc]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.runtime.fault_handler import fail_open_guard

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build BTC MetaFilter Path B training dataset")
    p.add_argument("--data-dir", default="data_btc", help="Data directory (default: data_btc)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir

    journal_path = data_dir / "live_trade_journal.jsonl"
    fs_path = data_dir / "feature_store" / "records" / "symbol=BTCUSDc" / "timeframe=M5" / "features.jsonl"
    out_path = data_dir / "models" / "metafilter_path_b_train.jsonl"

    print("=" * 60)
    print("  MetaFilter Path B — Dataset Builder")
    print(f"  Data dir: {data_dir}")
    print("=" * 60)

    # Load journal
    with open(journal_path, encoding="utf-8") as f:
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
    with open(fs_path, encoding="utf-8") as f:
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
    skipped_no_fs = 0
    skipped_dim = 0
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
            skipped_no_fs += 1
            continue

        fs_entry = fs_index[ts]
        values = fs_entry.get("values", {})
        if not values or not isinstance(values, dict):
            skipped_no_fs += 1
            continue

        # 40-dim feature vector (sorted keys for deterministic order)
        feature_names = sorted(values.keys())
        vec = [float(values.get(k, 0.0) or 0.0) for k in feature_names]

        # Verify dimension via Router contract (FIX-092)
        from core.features.feature_router import FeatureRouter
        _router = FeatureRouter()
        _lake = _router.build_lake(legacy_v9_vector=vec)
        try:
            _tensor = _router.dispatch(_lake, "v9_institutional_40")
            if len(_tensor) != 40:
                skipped_dim += 1
                continue  # dimension mismatch, skip
        except Exception:  # BLE001:FOG
            with fail_open_guard("build_metafilter_dataset:main"):
                skipped_dim += 1
                continue

        # Feature 41: entry_spread (cost of entry)
        entry_spread = 0.0
        if isinstance(open_entry.get("entry_context"), dict):
            entry_spread = float(open_entry["entry_context"].get("entry_spread", 0) or 0)
        vec.append(entry_spread)

        # Feature 42: brain confidence
        confidence = float(open_entry.get("confidence", 0.5) or 0.5)
        vec.append(confidence)

        # ── FIX-20260621-027: OFI features REMOVED ──
        # The 5 OFI features (OFI_M5, OFI_ZScore_20, OFI_Cumulative_1H,
        # OFI_Tick_Count, OFI_Total_Volume) were sourced from a single
        # global ofi_snapshot.json file read at dataset-build time —
        # this is look-ahead bias.  Every sample received the SAME OFI
        # values regardless of trade timestamp.  Proper PIT OFI requires
        # historical OFI snapshots at each trade's open time, which is
        # future infrastructure work.
        #
        # Final dimension: 40 V9 + entry_spread + brain_confidence = 42

        # Label
        is_win = 1 if pnl > 0 else 0

        samples.append({
            "ticket": ticket,
            "features": vec,
            "feature_names": feature_names + ["entry_spread", "brain_confidence"],
            "is_win": is_win,
            "pnl": pnl,
            "side": open_entry.get("side", "?"),
            "brain_ids": open_entry.get("brain_ids", []),
        })

    print(f"\nSamples built: {len(samples)}")
    print(f"Filtered: {skipped_noise} noise, {skipped_abnormal} abnormal, "
          f"{skipped_no_fs} no-fs-match, {skipped_dim} dim-mismatch")
    wins = sum(1 for s in samples if s["is_win"])
    losses = len(samples) - wins
    print(f"Wins: {wins}, Losses: {losses}, WR: {wins/len(samples)*100:.1f}%" if samples else "Wins: 0, Losses: 0")

    # By side
    long_samples = [s for s in samples if s["side"] == "long"]
    short_samples = [s for s in samples if s["side"] == "short"]
    print(f"LONG:  {len(long_samples)} samples, wins={sum(1 for s in long_samples if s['is_win'])}")
    print(f"SHORT: {len(short_samples)} samples, wins={sum(1 for s in short_samples if s['is_win'])}")

    # Save
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"\nSaved: {out_path} ({len(samples)} samples, {len(samples[0]['features'])}-dim)" if samples else "\nNo samples saved.")

    print("\n[DONE] All statistics above are the sole source of truth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
