"""Monitor DQAF-044-bis fix effectiveness — watches new journal entries.

Usage:
    python scripts/monitor_pwin_fix.py --data-dir data_btc
    python scripts/monitor_pwin_fix.py --data-dir data_btc --watch  # continuous

After restart, run this to verify:
  1. p_win_source is present in new open entries (Tier 3)
  2. p_win values are transitioning away from 0.50 (Tier 2 + poison flush)
"""

import argparse
import contextlib
import json
import sys
import time
from collections import Counter
from pathlib import Path


def load_journal(data_dir: str) -> list[dict]:
    journal = Path(data_dir) / "live_trade_journal.jsonl"
    if not journal.exists():
        print(f"JOURNAL NOT FOUND: {journal}")
        return []
    entries = []
    with open(journal, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                with contextlib.suppress(json.JSONDecodeError):
                    entries.append(json.loads(line))
    return entries


def analyze(entries: list[dict], since_ts: str | None = None):
    opens = [e for e in entries if e.get("action") == "open"]
    if since_ts:
        opens = [e for e in opens if e.get("recorded_at", "") >= since_ts]

    if not opens:
        print("No open entries found in the target window.")
        return None

    total = len(opens)
    sources = Counter(e.get("p_win_source", "MISSING") for e in opens)
    p_win_vals = [e.get("p_win", 0.5) for e in opens]
    p_win_05 = sum(1 for p in p_win_vals if p == 0.5)
    non_05 = [p for p in p_win_vals if p is not None and p != 0.5]

    print(f"\n{'='*60}")
    print(f"Open trades: {total}")
    print("p_win_source distribution:")
    for src, cnt in sources.most_common():
        pct = cnt / total * 100
        print(f"  {src:40s} {cnt:4d}  ({pct:.1f}%)")
    print(f"\np_win = 0.50: {p_win_05}/{total} ({p_win_05/total*100:.1f}%)")
    if non_05:
        print(f"p_win != 0.50: {len(non_05)} entries, range=[{min(non_05):.3f}, {max(non_05):.3f}]")
    else:
        print("p_win != 0.50: 0 entries")
    print(f"{'='*60}\n")

    # Tier 2 check: any non-cold_explore p_win?
    non_cold = [e for e in opens if e.get("p_win_source") not in ("cold_explore_neutral", "MISSING", None)]
    if non_cold:
        print(f"✅ Tier 2 PASS: {len(non_cold)} opens with non-cold_explore p_win_source")
        for e in non_cold[-3:]:
            print(f"   {e['recorded_at'][:19]} p_win={e.get('p_win'):.3f} source={e.get('p_win_source')}")
    else:
        print("⏳ Tier 2 PENDING: No non-cold_explore p_win yet (poison flush in progress)")

    # Tier 3 check: is p_win_source present?
    missing = sources.get("MISSING", 0)
    if missing == 0:
        print("✅ Tier 3 PASS: All open entries have p_win_source field")
    else:
        print(f"❌ Tier 3 FAIL: {missing}/{total} open entries MISSING p_win_source")

    # Poison check
    cold_count = sources.get("cold_explore_neutral", 0)
    if cold_count > 0 and total > 0:
        cold_pct = cold_count / total * 100
        print(f"🟡 Poison flush: {cold_count}/{total} ({cold_pct:.1f}%) still cold_explore_neutral")
        print("   Expected: 0% after ~50 trades (5-7 days)")

    return sources


def main():
    parser = argparse.ArgumentParser(description="Monitor DQAF-044-bis fix")
    parser.add_argument("--data-dir", default="data_btc")
    parser.add_argument("--watch", action="store_true", help="Continuous monitoring")
    parser.add_argument("--since", default=None, help="Filter entries after this timestamp")
    args = parser.parse_args()

    entries = load_journal(args.data_dir)
    if not entries:
        sys.exit(1)

    # Default: check entries AFTER the fix was deployed (14:15 UTC June 21)
    since = args.since or "2026-06-21T14:15:00"

    if args.watch:
        print(f"Watching for new open entries since {since}...")
        last_count = 0
        while True:
            entries = load_journal(args.data_dir)
            opens = [
                e for e in entries
                if e.get("action") == "open" and e.get("recorded_at", "") >= since
            ]
            if len(opens) > last_count:
                print(f"\n[{time.strftime('%H:%M:%S')}] New open detected!")
                analyze(entries, since)
                last_count = len(opens)
            time.sleep(30)
    else:
        # Show status of ALL opens (pre-fix vs post-fix comparison)
        all_opens = [e for e in entries if e.get("action") == "open"]
        pre_fix = [e for e in all_opens if e.get("recorded_at", "") < since]
        post_fix = [e for e in all_opens if e.get("recorded_at", "") >= since]

        print(f"Pre-fix opens (before 14:15 UTC):  {len(pre_fix)}")
        print(f"Post-fix opens (since 14:15 UTC):  {len(post_fix)}")
        print(f"Total opens:                       {len(all_opens)}")

        if pre_fix:
            print("\n── Pre-fix baseline ──")
            analyze(pre_fix, None)

        if post_fix:
            print("\n── Post-fix (new code) ──")
            analyze(post_fix, None)
        else:
            print("\n⏳ No post-fix opens yet. Waiting for next BTC signal...")
            print("   This is normal — BTC averages 2-8 opens/day on M5.")
            last_open = max(all_opens, key=lambda e: e.get("recorded_at", ""))
            print(f"   Last open: {last_open.get('recorded_at','?')[:19]}")

        # Also show last 5 modify_sltp to confirm Tier 3 bridge is working
        modifies = [e for e in entries if e.get("action") == "modify_sltp" and e.get("recorded_at", "") >= since]
        if modifies:
            print("\n── Tier 3 bridge confirmation (modify_sltp since restart) ──")
            for e in modifies[-3:]:
                pws = e.get("p_win_source", "ABSENT")
                print(f"  {e['recorded_at'][:19]} p_win_source={pws}")
            has_unknown = any(e.get("p_win_source") == "unknown" for e in modifies)
            if has_unknown:
                print("  ✅ Bridge worker Tier 3 confirmed — p_win_source field is being written")


if __name__ == "__main__":
    main()
