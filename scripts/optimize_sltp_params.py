#!/usr/bin/env python
"""BTC H1 SL/TP Parameter Optimizer — finds balanced, positive-EV contracts.

Sweeps (SL, TP, horizon) parameter space over BTC H1 data to identify
contracts that produce non-degenerate label distributions (TP% between
30-70%) with positive or near-positive expected value.

Key insight: SL=3.0/TP=2.0 on H1 produces 87.1% TP → degenerate.
This script finds the Pareto frontier of (balance, EV).

Usage:
  python scripts/optimize_sltp_params.py --csv data_btc/raw/btcusdc_h1_merged.csv
  python scripts/optimize_sltp_params.py --csv data_btc/raw/btcusdc_h1_merged.csv --min-tp-pct 35 --max-tp-pct 65
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.training.train_btc_swing_v9 import (
    compute_labels,
)

# ── Search Space ──────────────────────────────────────────────────────────────

# SL/TP in ATR multiples
SL_RANGE = [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0]
TP_RANGE = [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0]

# Horizon in H1 bars (12 = 12h, 24 = 24h)
HORIZON_RANGE = [12, 24]

# Fixed friction parameters
SPREAD_POINTS = 10.0
SLIPPAGE_POINTS = 10.0
TICK_VALUE = 0.01


def sweep_parameters(
    csv_path: str,
    min_tp_pct: float = 30.0,
    max_tp_pct: float = 70.0,
) -> list[dict]:
    """Sweep SL/TP/horizon space and compute label statistics.

    Returns list of (sl, tp, horizon, n_samples, tp_pct, sl_pct, ev_r, breakeven_wr, verdict)
    sorted by Pareto rank.
    """
    print(f"Loading {csv_path}...")
    df = pd.read_csv(csv_path)
    n_bars = len(df)
    print(f"  {n_bars:,} bars loaded")

    o = df["open"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64)
    c = df["close"].values.astype(np.float64)
    spreads = df.get("spread", pd.Series([SPREAD_POINTS] * n_bars)).values.astype(np.float64)

    results = []
    total_combos = len(SL_RANGE) * len(TP_RANGE) * len(HORIZON_RANGE)
    n_done = 0

    for horizon in HORIZON_RANGE:
        for sl in SL_RANGE:
            for tp in TP_RANGE:
                n_done += 1
                if n_done % 20 == 0:
                    print(f"  ... {n_done}/{total_combos}")

                labels, pnl_r, hold_bars = compute_labels(
                    o,
                    h,
                    l,
                    c,
                    horizon=horizon,
                    sl_atr_mult=sl,
                    tp_atr_mult=tp,
                    spread_points=SPREAD_POINTS,
                    slippage_points=SLIPPAGE_POINTS,
                    tick_value=TICK_VALUE,
                )

                # Filter: valid range (skip first MIN_BARS + horizon bars)
                valid = ~np.isnan(pnl_r)
                labels_v = labels[valid]
                pnl_r_v = pnl_r[valid]

                n_total = len(labels_v)
                if n_total < 100:
                    continue

                n_tp = int(np.sum(labels_v == 1))
                n_sl = int(np.sum(labels_v == -1))
                n_timeout = int(np.sum(labels_v == 0))

                tp_pct = n_tp / max(n_total, 1) * 100
                sl_pct = n_sl / max(n_total, 1) * 100
                timeout_pct = n_timeout / max(n_total, 1) * 100

                # EV (R-multiples): TP wins = tp/sl * sl_atr_mult / tp_atr_mult ratio
                # Actually: pnl_r already in R-multiples from compute_labels
                ev = float(np.nanmean(pnl_r_v)) if n_total > 0 else 0.0

                # Breakeven WR = SL / (SL + TP)
                breakeven_wr = sl / (sl + tp) * 100

                # Win rate on non-timeout samples
                binary_total = n_tp + n_sl
                wr = n_tp / max(binary_total, 1) * 100

                # Verdict
                balanced = min_tp_pct <= tp_pct <= max_tp_pct
                positive_ev = ev > 0

                if balanced and positive_ev:
                    verdict = "BEST"
                elif balanced:
                    verdict = "BALANCED"
                elif positive_ev and tp_pct > max_tp_pct:
                    verdict = "DEGEN_BUT_PROFITABLE"
                elif tp_pct > 80:
                    verdict = "DEGENERATE"
                elif tp_pct < 20:
                    verdict = "DEGENERATE_INVERTED"
                else:
                    verdict = "MARGINAL"

                results.append(
                    {
                        "sl": sl,
                        "tp": tp,
                        "horizon": horizon,
                        "n_samples": n_total,
                        "n_tp": n_tp,
                        "n_sl": n_sl,
                        "n_timeout": n_timeout,
                        "tp_pct": round(tp_pct, 1),
                        "sl_pct": round(sl_pct, 1),
                        "timeout_pct": round(timeout_pct, 1),
                        "wr": round(wr, 1),
                        "breakeven_wr": round(breakeven_wr, 1),
                        "ev_r": round(ev, 4),
                        "verdict": verdict,
                    }
                )

    return results


def print_report(results: list[dict], min_tp_pct: float, max_tp_pct: float):
    """Print formatted report."""

    # Sort: BEST first, then BALANCED, then by abs(ev)
    def sort_key(r):
        v_order = {
            "BEST": 0,
            "BALANCED": 1,
            "MARGINAL": 2,
            "DEGEN_BUT_PROFITABLE": 3,
            "DEGENERATE": 4,
            "DEGENERATE_INVERTED": 5,
        }
        return (v_order.get(r["verdict"], 9), -abs(r["ev_r"]))

    sorted_results = sorted(results, key=sort_key)

    print()
    print(f"{'='*100}")
    print("  BTC H1 SL/TP Parameter Optimization Report")
    print(f"  Target: TP% between {min_tp_pct:.0f}%-{max_tp_pct:.0f}%")
    print(f"{'='*100}")
    print()

    # ── Summary by verdict ──
    from collections import Counter

    verdict_counts = Counter(r["verdict"] for r in results)
    print("  Verdict Distribution:")
    for v, c in verdict_counts.most_common():
        print(f"    {v}: {c} combinations")
    print()

    # ── BEST contracts ──
    best = [r for r in sorted_results if r["verdict"] == "BEST"]
    if best:
        print(f"  {'='*90}")
        print(f"  [BEST] Balanced + Positive EV ({len(best)} contracts)")
        print(f"  {'='*90}")
        print(
            f"  {'SL':>5} {'TP':>5} {'H':>4} {'Samples':>8} {'TP%':>7} {'SL%':>7} {'WR':>7} {'B/E%':>7} {'EV(R)':>8} {'Timeout%':>9}"
        )
        print(f"  {'-'*80}")
        for r in best:
            print(
                f"  {r['sl']:5.2f} {r['tp']:5.2f} {r['horizon']:4d} {r['n_samples']:8,d} {r['tp_pct']:6.1f}% {r['sl_pct']:6.1f}% {r['wr']:6.1f}% {r['breakeven_wr']:6.1f}% {r['ev_r']:+8.4f} {r['timeout_pct']:8.1f}%"
            )
        print()

    # ── BALANCED contracts ──
    balanced = [r for r in sorted_results if r["verdict"] == "BALANCED"]
    if balanced:
        print(f"  [BALANCED] Good label balance, slight negative EV ({len(balanced)} contracts)")
        print(
            f"  {'SL':>5} {'TP':>5} {'H':>4} {'Samples':>8} {'TP%':>7} {'SL%':>7} {'WR':>7} {'B/E%':>7} {'EV(R)':>8} {'Timeout%':>9}"
        )
        print(f"  {'-'*80}")
        for r in balanced[:15]:  # top 15
            print(
                f"  {r['sl']:5.2f} {r['tp']:5.2f} {r['horizon']:4d} {r['n_samples']:8,d} {r['tp_pct']:6.1f}% {r['sl_pct']:6.1f}% {r['wr']:6.1f}% {r['breakeven_wr']:6.1f}% {r['ev_r']:+8.4f} {r['timeout_pct']:8.1f}%"
            )
        if len(balanced) > 15:
            print(f"  ... and {len(balanced) - 15} more")
        print()

    # ── DEGENERATE contracts (for comparison) ──
    degenerate = [r for r in sorted_results if r["verdict"] == "DEGENERATE"]
    if degenerate:
        print(f"  [DEGENERATE] TP% > 80% — label collapse ({len(degenerate)} contracts)")
        print("  Top 5 worst:")
        for r in sorted(degenerate, key=lambda x: -x["tp_pct"])[:5]:
            print(
                f"    SL={r['sl']:.2f} TP={r['tp']:.2f} H={r['horizon']} → TP={r['tp_pct']:.1f}% EV={r['ev_r']:+.4f}"
            )
        print()

    # ── Recommendation ──
    print(f"  {'='*90}")
    print("  RECOMMENDATION")
    print(f"  {'='*90}")
    print()

    if best:
        # Pick the best by: balanced + highest EV + reasonable sample count
        # Prefer symmetric contracts (SL==TP) or slightly asymmetric (TP > SL)
        # with horizon=12 (faster feedback)
        candidates = [r for r in best if r["horizon"] == 12 and r["n_samples"] >= 1000]
        if not candidates:
            candidates = [r for r in best if r["n_samples"] >= 1000]
        if not candidates:
            candidates = best

        # Score: EV * 10 - abs(50 - tp_pct) (penalize deviation from 50%)
        for r in candidates:
            r["_score"] = r["ev_r"] * 10 - abs(50 - r["tp_pct"]) * 0.01

        top = sorted(candidates, key=lambda x: -x["_score"])[:5]
        print("  Top 5 Recommended Contracts:")
        print(
            f"  {'Rank':<6} {'SL':>5} {'TP':>5} {'H':>4} {'Samples':>8} {'TP%':>7} {'SL%':>7} {'WR':>7} {'B/E%':>7} {'EV(R)':>8}"
        )
        print(f"  {'-'*80}")
        for i, r in enumerate(top, 1):
            marker = " ← RECOMMENDED" if i == 1 else ""
            print(
                f"  #{i:<5} {r['sl']:5.2f} {r['tp']:5.2f} {r['horizon']:4d} {r['n_samples']:8,d} {r['tp_pct']:6.1f}% {r['sl_pct']:6.1f}% {r['wr']:6.1f}% {r['breakeven_wr']:6.1f}% {r['ev_r']:+8.4f}{marker}"
            )
        print()

        top1 = top[0]
        print(
            f"  Primary recommendation: SL={top1['sl']:.2f} TP={top1['tp']:.2f} Horizon={top1['horizon']}"
        )
        print(
            f"    TP={top1['tp_pct']:.1f}% / SL={top1['sl_pct']:.1f}% / Timeout={top1['timeout_pct']:.1f}%"
        )
        print(
            f"    EV={top1['ev_r']:+.4f}R  Breakeven WR={top1['breakeven_wr']:.1f}%  Actual WR={top1['wr']:.1f}%"
        )
        print(f"    {top1['n_samples']:,} samples — sufficient for training")
    else:
        print("  No ideal contract found. Consider:")
        print("    1. Widening search range")
        print("    2. Adjusting spread/slippage assumptions")
        print("    3. Using asymmetric SL/TP")

    print()
    print(f"{'='*100}")
    print("  Scan complete.")


def main():
    parser = argparse.ArgumentParser(description="BTC H1 SL/TP Parameter Optimizer")
    parser.add_argument(
        "--csv", default="data_btc/raw/btcusdc_h1_merged.csv", help="BTC H1 CSV path"
    )
    parser.add_argument("--min-tp-pct", type=float, default=30.0, help="Minimum acceptable TP%")
    parser.add_argument("--max-tp-pct", type=float, default=70.0, help="Maximum acceptable TP%")
    args = parser.parse_args()

    results = sweep_parameters(
        csv_path=args.csv,
        min_tp_pct=args.min_tp_pct,
        max_tp_pct=args.max_tp_pct,
    )

    print_report(results, args.min_tp_pct, args.max_tp_pct)


if __name__ == "__main__":
    main()
