"""Audit entry_spread coverage in live_trade_journal.jsonl.

Iron Law #11 compliant: all statistics from script stdout, not context reading.

Usage:
    python scripts/audit_entry_spread_coverage.py --data-dir data_btc
    python scripts/audit_entry_spread_coverage.py --data-dir data_btc --json
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def load_journal(data_dir: Path) -> list[dict]:
    """Load live_trade_journal.jsonl entries."""
    journal_path = data_dir / "live_trade_journal.jsonl"
    if not journal_path.exists():
        print(f"ERROR: journal not found at {journal_path}", file=sys.stderr)
        sys.exit(1)
    entries = []
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def main():
    parser = argparse.ArgumentParser(
        description="Audit entry_spread coverage in live trade journal"
    )
    parser.add_argument("--data-dir", default="data_btc", help="Data directory")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    entries = load_journal(data_dir)

    # ── Partition: opens vs closes ──
    opens = [e for e in entries if e.get("action") == "open"]
    closes = [e for e in entries if e.get("action") == "close"]

    # ── entry_spread presence ──
    has_spread = []
    no_spread = []
    spread_zero = []
    for e in opens:
        ec = e.get("entry_context") or {}
        spread_val = ec.get("entry_spread")
        if spread_val is not None:
            if spread_val > 0:
                has_spread.append(e)
            else:
                spread_zero.append(e)
        else:
            no_spread.append(e)

    total_opens = len(opens)

    # ── Time-based analysis: when did entry_spread appear? ──
    first_with_spread = None
    last_without_spread = None
    for e in opens:
        ec = e.get("entry_context") or {}
        spread_val = ec.get("entry_spread")
        ts = e.get("recorded_at", "")
        if spread_val is not None and spread_val > 0:
            if first_with_spread is None or ts < first_with_spread:
                first_with_spread = ts
        if spread_val is None:
            last_without_spread = ts

    # ── By strategy ──
    by_strategy: Counter = Counter()
    for e in has_spread:
        by_strategy[e.get("strategy", "unknown")] += 1
    missing_by_strategy: Counter = Counter()
    for e in no_spread:
        missing_by_strategy[e.get("strategy", "unknown")] += 1

    # ── Spread value distribution ──
    spread_values = []
    for e in has_spread:
        ec = e.get("entry_context") or {}
        spread_values.append(ec["entry_spread"])

    # ── Output ──
    if args.json:
        output = {
            "total_opens": total_opens,
            "has_spread": len(has_spread),
            "has_spread_pct": round(len(has_spread) / total_opens * 100, 1) if total_opens else 0,
            "spread_zero": len(spread_zero),
            "no_spread": len(no_spread),
            "first_with_spread": first_with_spread,
            "last_without_spread": last_without_spread,
            "spread_mean": round(sum(spread_values) / len(spread_values), 2)
            if spread_values
            else 0,
            "spread_min": round(min(spread_values), 2) if spread_values else 0,
            "spread_max": round(max(spread_values), 2) if spread_values else 0,
            "strategies_with_spread": dict(by_strategy.most_common()),
            "strategies_missing": dict(missing_by_strategy.most_common()),
        }
        print(json.dumps(output, indent=2))
    else:
        print("=" * 60)
        print("  Entry Spread Coverage Audit")
        print("=" * 60)
        print()
        print("--- Overall ---")
        print(f"  Total opens:         {total_opens}")
        print(f"  Has entry_spread>0:  {len(has_spread)} ({len(has_spread)/total_opens*100:.1f}%)")
        print(
            f"  Has entry_spread=0:  {len(spread_zero)} ({len(spread_zero)/total_opens*100:.1f}%)"
        )
        print(f"  No entry_spread:     {len(no_spread)} ({len(no_spread)/total_opens*100:.1f}%)")
        print()

        print("--- Timeline ---")
        print(f"  First open with spread:   {first_with_spread}")
        print(f"  Last open without spread:  {last_without_spread}")
        if first_with_spread and last_without_spread and last_without_spread > first_with_spread:
            print("  !! WARNING: opens without spread exist AFTER first spread entry")
        else:
            print("  [OK] Spread coverage starts after last no-spread entry")
        print()

        print("--- Spread Value Distribution ---")
        if spread_values:
            avg = sum(spread_values) / len(spread_values)
            print(f"  Mean:    {avg:.2f}")
            print(f"  Min:     {min(spread_values):.2f}")
            print(f"  Max:     {max(spread_values):.2f}")
            pct_gt_5 = sum(1 for v in spread_values if v > 5) / len(spread_values) * 100
            pct_gt_10 = sum(1 for v in spread_values if v > 10) / len(spread_values) * 100
            print(f"  Pct >5:  {pct_gt_5:.1f}%")
            print(f"  Pct >10: {pct_gt_10:.1f}%")
        print()

        print("--- By Strategy (has spread) ---")
        for strat, count in by_strategy.most_common():
            print(f"  {strat}: {count}")
        print()

        print("--- By Strategy (missing spread) ---")
        if missing_by_strategy:
            for strat, count in missing_by_strategy.most_common():
                print(f"  {strat}: {count}")
        else:
            print("  (none)")
        print()

        # ── Verdict ──
        print("--- Verdict ---")
        if len(no_spread) == 0:
            print("  [PASS] All opens have entry_spread in entry_context.")
        else:
            coverage_pct = len(has_spread) / total_opens * 100 if total_opens else 0
            if coverage_pct >= 80:
                print(f"  [PASS] entry_spread coverage={coverage_pct:.1f}% (>=80% threshold)")
            else:
                print(f"  [WARN] entry_spread coverage={coverage_pct:.1f}% (<80% threshold)")
                print(f"         {len(no_spread)} opens missing entry_spread")
        print()
        print("[DONE] All statistics above are the sole source of truth.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
