#!/usr/bin/env python3
"""
P3.3: M30/H1_V2 probation evaluation — Iron Law #11 compliant.

Evaluates btc_swing_m30 and btc_swing_h1_v2 performance for promotion
eligibility.  Criteria: >= 50 trades, PF > 1.0, Directional WR > 48%.

Usage: python scripts/_evaluate_probation_m30_h1v2.py --data-dir data_btc
Stdout is the sole source of truth.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_journal(data_dir: str) -> list[dict[str, Any]]:
    path = Path(data_dir) / "live_trade_journal.augmented.jsonl"
    entries: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except FileNotFoundError:
        print(f"ERROR: journal not found at {path}")
        sys.exit(1)
    return entries


def evaluate_strategy(name: str, trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Return evaluation dict for a single strategy."""
    wins = [t for t in trades if t.get("label") == "win"]
    losses = [t for t in trades if t.get("label") == "loss"]
    total = len(trades)

    win_pnl = sum(t.get("pnl", 0) or 0 for t in wins)
    loss_pnl = abs(sum(t.get("pnl", 0) or 0 for t in losses))
    pf = win_pnl / loss_pnl if loss_pnl > 0 else (999.0 if win_pnl > 0 else 0.0)
    wr = (len(wins) / total * 100) if total > 0 else 0.0

    # Directional breakdown
    longs = [t for t in trades if str(t.get("side", "")).upper() in ("LONG", "BUY")]
    shorts = [t for t in trades if str(t.get("side", "")).upper() in ("SHORT", "SELL")]
    long_wins = sum(1 for t in longs if t.get("label") == "win")
    short_wins = sum(1 for t in shorts if t.get("label") == "win")

    net_pnl = sum(t.get("pnl", 0) or 0 for t in trades)

    # Volume-weighted stats
    volumes = [abs(t.get("volume", 0) or 0) for t in trades]
    total_volume = sum(volumes)

    return {
        "strategy": name,
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(wr, 1),
        "profit_factor": round(pf, 2),
        "net_pnl": round(net_pnl, 2),
        "total_volume": round(total_volume, 4),
        "long_trades": len(longs),
        "long_wr_pct": round(long_wins / len(longs) * 100, 1) if longs else 0.0,
        "short_trades": len(shorts),
        "short_wr_pct": round(short_wins / len(shorts) * 100, 1) if shorts else 0.0,
        "avg_pnl_per_trade": round(net_pnl / total, 2) if total > 0 else 0.0,
    }


def check_promotion(eval_dict: dict[str, Any]) -> dict[str, Any]:
    """Determine promotion eligibility."""
    total = eval_dict["total_trades"]
    pf = eval_dict["profit_factor"]
    wr = eval_dict["win_rate_pct"]
    long_wr = eval_dict["long_wr_pct"]
    short_wr = eval_dict["short_wr_pct"]

    checks = {
        "min_50_trades": (total >= 50, f"{total}/50"),
        "pf_gt_1_0": (pf > 1.0, f"{pf:.2f}/1.00"),
        "overall_wr_gt_48": (wr > 48.0, f"{wr:.1f}%/48%"),
        "directional_wr_gt_48": (
            (long_wr > 48.0 or eval_dict["long_trades"] < 5)
            and (short_wr > 48.0 or eval_dict["short_trades"] < 5),
            f"long={long_wr:.1f}% short={short_wr:.1f}%/48%",
        ),
    }

    all_pass = all(v[0] for v in checks.values())
    return {
        "eligible": all_pass,
        "checks": {k: {"pass": v[0], "detail": v[1]} for k, v in checks.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data_btc")
    args = parser.parse_args()

    entries = load_journal(args.data_dir)

    strategies = {"btc_swing_m30", "btc_swing_h1_v2"}
    closes = [e for e in entries if e.get("action") == "close" and e.get("strategy") in strategies]

    by_strat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in closes:
        by_strat[e["strategy"]].append(e)

    print("=" * 60)
    print("P3.3: M30 / H1_V2 PROBATION EVALUATION")
    print(f"Data: {args.data_dir}/live_trade_journal.augmented.jsonl")
    print(f"Total close entries analyzed: {len(closes)}")
    print("=" * 60)

    for s in sorted(by_strat.keys()):
        trades = by_strat[s]
        ev = evaluate_strategy(s, trades)
        promo = check_promotion(ev)

        print(f"\n─── {s} ───")
        print(f"  Trades:      {ev['total_trades']:>5}  (wins={ev['wins']}, losses={ev['losses']})")
        print(f"  Win Rate:    {ev['win_rate_pct']:>6.1f}%")
        print(f"  Profit Factor: {ev['profit_factor']:>5.2f}")
        print(f"  Net PnL:     ${ev['net_pnl']:>8.2f}")
        print(f"  Avg PnL:     ${ev['avg_pnl_per_trade']:>8.2f} / trade")
        print(f"  Total Vol:   {ev['total_volume']:>8.4f} lots")
        print(f"  LONG:        {ev['long_trades']:>5} trades, WR={ev['long_wr_pct']:.1f}%")
        print(f"  SHORT:       {ev['short_trades']:>5} trades, WR={ev['short_wr_pct']:.1f}%")

        # Date range
        dates = sorted(t.get("recorded_at", "")[:10] for t in trades if t.get("recorded_at"))
        if dates:
            print(f"  Date range:  {dates[0]} → {dates[-1]}")

        # Promotion checks
        print("\n  Promotion checks:")
        for check_name, result in promo["checks"].items():
            icon = "✅" if result["pass"] else "❌"
            print(f"    {icon} {check_name}: {result['detail']}")

        if promo["eligible"]:
            print("\n  🟢 ELIGIBLE for promotion to live (vote_weight=1.0)")
        else:
            failed = [k for k, v in promo["checks"].items() if not v["pass"]]
            print(f"\n  🔴 NOT eligible — failing: {', '.join(failed)}")

    # Summary
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    eligible = []
    for s in sorted(by_strat.keys()):
        ev = evaluate_strategy(s, by_strat[s])
        promo = check_promotion(ev)
        status = "🟢 PROMOTE" if promo["eligible"] else "🔴 HOLD"
        eligible.append((s, status, ev["total_trades"], ev["profit_factor"], ev["win_rate_pct"]))
        print(
            f"  {status}: {s} ({ev['total_trades']} trades, PF={ev['profit_factor']:.2f}, WR={ev['win_rate_pct']:.1f}%)"
        )

    if any(e[0] == "🟢 PROMOTE" for e in eligible):
        print("\n  → Run: python scripts/brain.py promote <brain_id> --data-dir data_btc")
    print("=" * 60)


if __name__ == "__main__":
    main()
