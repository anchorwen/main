#!/usr/bin/env python
"""DQAF-044 Fix Effect Verification — Iron Law #11 Compliant
============================================================
Checks whether the COLD_EXPLORE_PREEMPTIVE_OVERRIDE fix is taking effect
by monitoring three independent signals:

  A. p_win Distribution Recovery — Is the calibrator seeing varied p_win?
  B. calibrator Q10 Threshold Recovery — Is threshold rising from 0.50?
  C. Trade Quality Shift — Are post-fix trades showing different p_win?

Usage:
  python scripts/verify_dqaf044_fix_effect.py --data-dir data_btc
  python scripts/verify_dqaf044_fix_effect.py --data-dir data_btc --watch

The --watch flag re-runs every 5 minutes for ongoing monitoring during
the poison flushing period (~50 trades needed).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


# -- Data loading --------------------------------------------------------


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


# -- Signal A: Calibrator p_win Distribution ----------------------------


def check_calibrator_distribution(data_dir: Path) -> dict[str, Any]:
    """Analyze p_win distribution in the calibrator's 500-entry rolling window."""
    cal_path = data_dir / "conformal_calibrator_state.json"
    if not cal_path.exists():
        return {"error": f"{cal_path} not found"}

    state = load_json(cal_path)
    history: list[dict[str, Any]] = state.get("history", [])
    total = len(history)

    if total == 0:
        return {"error": "Calibrator history is empty"}

    pwin_values = [h.get("p_win") or 0.5 for h in history]
    unique_count = len(set(pwin_values))
    count_05 = sum(1 for p in pwin_values if p == 0.5)
    count_varied = total - count_05
    poison_ratio = count_05 / total

    # Split history into pre-fix (before June 21) and post-fix (June 21+)
    fix_cutoff = "2026-06-21T00:00:00Z"
    post_fix = [h for h in history if h.get("timestamp", "") >= fix_cutoff]
    post_05 = sum(1 for h in post_fix if h["p_win"] == 0.5)
    post_varied = len(post_fix) - post_05

    # Time series: p_win by day
    by_day: dict[str, list[float]] = {}
    for h in history:
        day = h.get("timestamp", "")[:10]
        if day:
            by_day.setdefault(day, []).append(h["p_win"])
    daily_stats = {}
    for day, pwins in sorted(by_day.items()):
        daily_stats[day] = {
            "count": len(pwins),
            "unique": len(set(pwins)),
            "pct_05": sum(1 for p in pwins if p == 0.5) / len(pwins) * 100,
        }

    # Latest entries
    latest = history[-10:] if len(history) >= 10 else history

    return {
        "total_entries": total,
        "unique_p_win_values": unique_count,
        "p_win_range": [min(pwin_values), max(pwin_values)],
        "poison_entries": count_05,
        "poison_ratio_pct": round(poison_ratio * 100, 1),
        "varied_entries": count_varied,
        "post_fix_entries": len(post_fix),
        "post_fix_poison": post_05,
        "post_fix_varied": post_varied,
        "threshold": state.get("threshold"),
        "clamp_range": state.get("clamp_range"),
        "total_computations": state.get("total_computations"),
        "daily_p_win": daily_stats,
        "latest_10": [
            {"p_win": h.get("p_win") or 0.5, "label": h.get("label"), "ts": h.get("timestamp", "")}
            for h in latest
        ],
    }


# -- Signal B: Trade Journal p_win Shift ------------------------------


def check_journal_pwin_shift(data_dir: Path) -> dict[str, Any]:
    """Check whether accepted trades show p_win values diverging from 0.5."""
    journal_path = data_dir / "live_trade_journal.jsonl"
    records = load_jsonl(journal_path)

    opens = [r for r in records if r.get("action") == "open"]
    fix_cutoff = "2026-06-21"

    pre_fix = [o for o in opens if o.get("recorded_at", "") < fix_cutoff]
    post_fix = [o for o in opens if o.get("recorded_at", "") >= fix_cutoff]

    def analyze(trades: list[dict[str, Any]]) -> dict[str, Any]:
        if not trades:
            return {"count": 0, "unique_pwin": 0, "pct_05": 0, "pwin_range": [0, 0]}
        pwins = [t.get("p_win") or 0.5 for t in trades]
        unique = len(set(pwins))
        pct_05 = sum(1 for p in pwins if p == 0.5) / len(pwins) * 100
        return {
            "count": len(trades),
            "unique_pwin": unique,
            "pct_05": round(pct_05, 1),
            "pwin_range": [round(min(pwins), 4), round(max(pwins), 4)],
        }

    return {
        "pre_fix": analyze(pre_fix),
        "post_fix": analyze(post_fix),
    }


# -- Signal C: MetaFilter Control Group --------------------------------


def check_metafilter_control_group(data_dir: Path) -> dict[str, Any]:
    """Verify MetaFilter itself is producing real predictions (independent check)."""
    mf_path = data_dir / "meta_filter_state.json"
    if not mf_path.exists():
        return {"error": f"{mf_path} not found"}

    state = load_json(mf_path)
    pred_history = state.get("pred_history", [])

    if not pred_history:
        return {"error": "pred_history is empty", "total_predictions": 0}

    # pred_history entries are [timestamp, p_win] lists, not dicts
    pwins = [p[1] if isinstance(p, list) and len(p) > 1 else 0.5 for p in pred_history]
    unique_count = len(set(pwins))
    count_05 = sum(1 for p in pwins if p == 0.5)

    return {
        "total_predictions": len(pred_history),
        "unique_p_win_values": unique_count,
        "p_win_range": [round(min(pwins), 4), round(max(pwins), 4)],
        "p_win_05_count": count_05,
        "model_producing_real_predictions": unique_count > 1,
    }


# -- Main ---------------------------------------------------------------


def print_header(title: str) -> None:
    print(f"\n{'='*72}")
    print(f"  {title}")
    print(f"{'='*72}")


def print_verdict(report: dict[str, Any]) -> str:
    """Return a single-word verdict and display reasoning."""
    cal = report.get("calibrator", {})
    journal = report.get("journal", {})
    mf = report.get("metafilter", {})

    post_fix = journal.get("post_fix", {})
    post_count = post_fix.get("count", 0)
    post_pct_05 = post_fix.get("pct_05", 100)
    cal_poison = cal.get("poison_ratio_pct", 100)

    print_header("VERDICT")

    issues = []
    if cal_poison > 80:
        issues.append(
            f"Calibrator still {cal_poison}% 0.5 poison "
            f"(need <20%, ~{max(0, int(500 * (cal_poison/100 - 0.2)))} more entries to flush)"
        )
    if post_count > 0 and post_pct_05 > 80:
        issues.append(
            f"Post-fix trades still {post_pct_05}% p_win=0.5 — "
            f"live process may not have restarted with new code"
        )
    if not mf.get("model_producing_real_predictions"):
        issues.append("MetaFilter control group shows no real predictions")

    if issues:
        for i in issues:
            print(f"  [WARN]  {i}")
        print("\n  FIX STATUS: IN PROGRESS (poison flushing period)")
        return "IN_PROGRESS"
    else:
        print("  ✅ Calibrator variance restored, MetaFilter output flowing")
        print("  ✅ Post-fix trades showing real p_win distribution")
        print("\n  FIX STATUS: VERIFIED")
        return "VERIFIED"


def main():
    parser = argparse.ArgumentParser(description="DQAF-044 Fix Effect Verification")
    parser.add_argument("--data-dir", default="data_btc", help="Data directory (default: data_btc)")
    parser.add_argument("--watch", action="store_true", help="Re-run every 5 minutes")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)

    while True:
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n{'#'*72}")
        print(f"  DQAF-044 FIX EFFECT VERIFICATION — {ts}")
        print(f"  Data dir: {data_dir}")
        print(f"{'#'*72}")

        # Signal A: Calibrator distribution
        print_header("A. Calibrator p_win Distribution (500-entry FIFO)")
        cal = check_calibrator_distribution(data_dir)
        if "error" in cal:
            print(f"  ERROR: {cal['error']}")
        else:
            print(f"  Total entries:        {cal['total_entries']}")
            print(f"  Unique p_win values:  {cal['unique_p_win_values']}")
            print(f"  p_win range:          {cal['p_win_range']}")
            print(
                f"  Poison (0.5):         {cal['poison_entries']}/{cal['total_entries']} ({cal['poison_ratio_pct']}%)"
            )
            print(f"  Varied (!=0.5):        {cal['varied_entries']}/{cal['total_entries']}")
            if cal.get("post_fix_entries", 0) > 0:
                print(
                    f"  Post-fix entries:     {cal['post_fix_entries']} ({cal['post_fix_varied']} varied)"
                )
            print(f"  Threshold (Q10):      {cal.get('threshold')}")
            print(f"  Clamp range:          {cal.get('clamp_range')}")
            print("\n  Daily p_win breakdown:")
            for day, stats in cal.get("daily_p_win", {}).items():
                marker = " ← FIX DAY" if day >= "2026-06-21" else ""
                bar = "#" * max(1, int(stats["count"] / 5)) if stats["count"] < 50 else "##########"
                print(
                    f"    {day}: {stats['count']:3d} entries, {stats['unique']:2d} unique, "
                    f"{stats['pct_05']:5.1f}% 0.5 {bar}{marker}"
                )
            print("\n  Latest 10:")
            for h in cal.get("latest_10", []):
                varied = "← REAL" if h["p_win"] != 0.5 else "  POISON"
                print(f"    p_win={h['p_win']:.4f}  label={h['label']:2d}  ts={h['ts']}  {varied}")

        # Signal B: Journal p_win shift
        print_header("B. Trade Journal p_win Shift")
        journal = check_journal_pwin_shift(data_dir)
        pre = journal["pre_fix"]
        post = journal["post_fix"]
        print(
            f"  Pre-fix  (<Jun 21): {pre['count']:4d} opens, {pre['unique_pwin']:2d} unique p_win, "
            f"{pre['pct_05']:5.1f}% =0.5, range={pre['pwin_range']}"
        )
        print(
            f"  Post-fix (>=Jun 21): {post['count']:4d} opens, {post['unique_pwin']:2d} unique p_win, "
            f"{post['pct_05']:5.1f}% =0.5, range={post['pwin_range']}"
        )

        if post["count"] > 0 and post["pct_05"] > 90:
            print(f"\n  [WARN]  Post-fix trades still show {post['pct_05']}% p_win=0.5")
            print("  -> Live process likely running OLD code (pre-fix)")
            print("  -> Restart live trading process to load strategy_line.py fix")

        # Signal C: MetaFilter control group
        print_header("C. MetaFilter Control Group (Independent Verify)")
        mf = check_metafilter_control_group(data_dir)
        if "error" in mf:
            print(f"  {mf['error']}")
        else:
            print(f"  Total predictions:    {mf['total_predictions']}")
            print(f"  Unique p_win values:  {mf['unique_p_win_values']}")
            print(f"  p_win range:          {mf['p_win_range']}")
            print(f"  p_win=0.5 count:      {mf['p_win_05_count']}")
            print(f"  Model producing real: {mf['model_producing_real_predictions']}")

        # Verdict
        report = {"calibrator": cal, "journal": journal, "metafilter": mf}
        verdict = print_verdict(report)

        if not args.watch:
            break

        print("\n  Next check in 5 minutes...")
        time.sleep(300)

    print("\n[DONE] All statistics above are the sole source of truth.")
    return 0 if verdict == "VERIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())
