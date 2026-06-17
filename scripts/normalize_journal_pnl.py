#!/usr/bin/env python
"""MT5-authoritative PnL normalization — fix journal PnL format inconsistencies.

DQAF-20260616-005: MT5 deal verification revealed 52% of journal close entries
have PnL in a different unit (display-dollars vs account-cents).  This script
uses MT5 history_deals as the authoritative source to normalize all journal
PnL values to match MT5 profit exactly.

Usage:
  python scripts/normalize_journal_pnl.py --data-dir data
  python scripts/normalize_journal_pnl.py --data-dir data_btc
  python scripts/normalize_journal_pnl.py --data-dir data --dry-run

Safety:
  - Original journal backed up to .bak3 before modification
  - Dry-run mode shows changes without applying
  - Only modifies close entries where MT5 has a different profit value
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(prog="normalize_journal_pnl")
    p.add_argument("--data-dir", type=str, default="data")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--mt5-path", type=str, default=r"D:\exness\MetaTrader 5 EXNESS2\terminal64.exe")
    args = p.parse_args()

    jp = Path(args.data_dir) / "live_trade_journal.jsonl"
    if not jp.exists():
        print(f"[ERROR] Not found: {jp}")
        return 1

    # ── Load journal ──
    entries: list[dict] = []
    with open(jp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"_raw": line})

    close_indices = [i for i, e in enumerate(entries) if e.get("action") == "close" and not e.get("_raw")]
    print(f"Journal: {jp} ({len(entries)} entries, {len(close_indices)} closes)")

    # ── Connect MT5 ──
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("[ERROR] MetaTrader5 not installed")
        return 1

    if not mt5.initialize(path=args.mt5_path):
        print(f"[ERROR] MT5 init failed: {mt5.last_error()}")
        return 1

    # Get all available deals (try last 90 days)
    deals_by_pos: dict[int, any] = {}
    for days in [90, 180, 365]:
        try:
            deals = mt5.history_deals_get(datetime.now() - timedelta(days=days), datetime.now())
            if deals:
                for d in deals:
                    if d.position_id and d.profit != 0:
                        deals_by_pos[d.position_id] = d
                print(f"MT5: {len(deals)} deals loaded ({days}d range), {len(deals_by_pos)} with profit")
                break
        except Exception:
            continue

    if not deals_by_pos:
        print("[WARN] No MT5 deals found — nothing to normalize")
        mt5.shutdown()
        return 0

    # ── Normalize ──
    fixed = 0
    already_ok = 0
    no_mt5_match = 0
    details: list[dict] = []

    for idx in close_indices:
        e = entries[idx]
        tkt = e.get("position_ticket")
        if not tkt:
            no_mt5_match += 1
            continue
        tkt = int(tkt)
        if tkt not in deals_by_pos:
            no_mt5_match += 1
            continue

        deal = deals_by_pos[tkt]
        mt5_profit = float(deal.profit)
        jnl_pnl = e.get("pnl", 0) or 0

        if abs(jnl_pnl - mt5_profit) < 0.001:
            already_ok += 1
        else:
            old_pnl = jnl_pnl
            e["pnl"] = mt5_profit
            e["_pnl_normalized"] = True
            e["_pnl_old"] = old_pnl
            fixed += 1
            details.append({
                "ticket": tkt,
                "label": e.get("label", "?"),
                "old_pnl": old_pnl,
                "new_pnl": mt5_profit,
                "ratio": round(mt5_profit / max(abs(old_pnl), 0.0001), 1),
            })

    mt5.shutdown()

    total = already_ok + fixed + no_mt5_match
    pct_fixed = fixed / max(total, 1) * 100
    pct_ok = already_ok / max(total, 1) * 100

    print(f"\nResults:")
    print(f"  Already correct:   {already_ok} ({pct_ok:.0f}%)")
    print(f"  Fixed (normalized): {fixed} ({pct_fixed:.0f}%)")
    print(f"  No MT5 match:      {no_mt5_match}")

    if details:
        # Show ratio distribution
        ratios = [d["ratio"] for d in details]
        near_100 = sum(1 for r in ratios if 80 < r < 120)
        near_1 = sum(1 for r in ratios if 0.8 < r < 1.2)
        other = len(ratios) - near_100 - near_1
        print(f"\n  Ratio distribution:")
        print(f"    ~1x (same unit, minor drift): {near_1}")
        print(f"    ~100x (USC vs display-dollar): {near_100}")
        print(f"    Other ratios: {other}")

    if args.dry_run:
        print("\n[DRY-RUN] No changes made.")
        if details[:5]:
            print("Sample fixes:")
            for d in details[:5]:
                print(f"  ticket={d['ticket']} {d['label']}: {d['old_pnl']} -> {d['new_pnl']} ({d['ratio']:.0f}x)")
        return 0

    if fixed == 0:
        print("\n[OK] No fixes needed.")
        return 0

    # ── Backup and write ──
    bak = jp.with_suffix(".jsonl.bak3")
    shutil.copy2(jp, bak)
    print(f"\nBackup: {bak}")

    with open(jp, "w", encoding="utf-8") as f:
        for e in entries:
            if e.get("_raw"):
                f.write(e["_raw"] + "\n")
            else:
                # Remove normalization metadata before writing
                clean = {k: v for k, v in e.items() if not k.startswith("_pnl") and k != "_raw"}
                f.write(json.dumps(clean, ensure_ascii=False) + "\n")

    print(f"Normalized journal: {jp} ({fixed} PnL values corrected)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
