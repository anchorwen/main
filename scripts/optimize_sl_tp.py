"""SL/TP parameter sweep for paper trade simulator.

Sweeps SL and TP ATR multipliers to find the combination that maximizes
profit factor and win rate for paper trade labeling quality.

Usage:
  python scripts/optimize_sl_tp.py              # coarse sweep → fine sweep
  python scripts/optimize_sl_tp.py --sample 500 # quick test with 500 decisions
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.paper_trade_simulator import (
    collect_decisions,
    find_bar,
    load_features,
    load_ohlc,
    simulate_trade,
)

OHLC_FILE = PROJECT_ROOT / "data" / "raw" / "xauusdc_m5_1y.csv"
OHLC_RECENT_FILE = PROJECT_ROOT / "data" / "raw" / "xauusdc_m5_recent.csv"
FEATURE_FILE = (
    PROJECT_ROOT
    / "data"
    / "feature_store"
    / "records"
    / "symbol=XAUUSDc"
    / "timeframe=M5"
    / "features.jsonl"
)

# Coarse grid: 6 x 7 = 42 combos
SL_COARSE = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]
TP_COARSE = [1.5, 2.0, 2.5, 3.0, 3.5, 4.5, 6.0]

# Fine grid offset from best coarse: +/- 0.25 in 0.1 steps
FINE_STEPS = 5  # -0.5, -0.25, 0, +0.25, +0.5


def _profit_factor(trades: list[dict]) -> float:
    """Gross profit / gross loss (absolute). Higher is better."""
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    return round(gross_profit / gross_loss, 4) if gross_loss > 0 else float("inf")


def _sharpe_like(trades: list[dict]) -> float:
    """Mean PnL / Std PnL (approximate Sharpe)."""
    pnls = [t["pnl"] for t in trades]
    if len(pnls) < 2:
        return 0.0
    mean = sum(pnls) / len(pnls)
    variance = sum((x - mean) ** 2 for x in pnls) / (len(pnls) - 1)
    return round(mean / (variance**0.5), 4) if variance > 0 else 0.0


def _score(trades: list[dict]) -> float:
    """Composite score: profit_factor * sqrt(trade_count) * win_rate."""
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t["label"] == "tp_hit_first")
    wr = wins / len(trades)
    pf = _profit_factor(trades)
    # Encourage more trades (statistical significance) but not overwhelmingly
    return round(pf * (len(trades) ** 0.3) * wr, 4)


def sweep(
    decisions: list[dict],
    bars: list[dict],
    features: dict[str, float],
    sl_values: list[float],
    tp_values: list[float],
    max_hold_bars: int = 288,
) -> list[dict[str, Any]]:
    """Sweep SL/TP combinations and return ranked results."""
    results: list[dict[str, Any]] = []
    total = len(sl_values) * len(tp_values)
    n = 0

    for sl in sl_values:
        for tp in tp_values:
            n += 1
            if tp <= sl:
                continue  # TP must exceed SL distance

            trades: list[dict] = []
            last_entry_time: datetime | None = None
            cooldown = 300

            for dec in decisions:
                et = dec["event_time"]
                if last_entry_time and (et - last_entry_time).total_seconds() < cooldown:
                    continue
                bar_idx = find_bar(bars, et)
                if bar_idx is None or bar_idx + 6 >= len(bars):
                    continue
                trade = simulate_trade(dec, bars, bar_idx, features, sl, tp, max_hold_bars)
                if trade is None:
                    continue
                trades.append(trade)
                last_entry_time = et

            if not trades:
                continue

            wins = sum(1 for t in trades if t["label"] == "tp_hit_first")
            wr = wins / len(trades)
            tp_count = sum(1 for t in trades if t["exit_reason"] == "tp_hit")
            sl_count = sum(1 for t in trades if t["exit_reason"] == "sl_hit")
            timeout_count = sum(1 for t in trades if t["exit_reason"] == "timeout")
            total_pnl = sum(t["pnl"] for t in trades)
            pf = _profit_factor(trades)
            sh = _sharpe_like(trades)

            avg_hold = (
                sum(
                    abs(
                        (
                            datetime.fromisoformat(t["close_time"])
                            - datetime.fromisoformat(t["entry_time"])
                        ).total_seconds()
                    )
                    for t in trades
                )
                / len(trades)
                / 3600
            )

            composite = _score(trades)

            results.append(
                {
                    "sl": sl,
                    "tp": tp,
                    "trades": len(trades),
                    "win_rate": round(wr, 4),
                    "total_pnl": round(total_pnl, 2),
                    "avg_pnl": round(total_pnl / len(trades), 2),
                    "profit_factor": pf,
                    "sharpe_like": sh,
                    "tp_exits": tp_count,
                    "sl_exits": sl_count,
                    "timeouts": timeout_count,
                    "avg_hold_hours": round(avg_hold, 1),
                    "composite": composite,
                }
            )

            print(
                f"  [{n}/{total}] SL={sl:.1f} TP={tp:.1f} → "
                f"trades={len(trades)} wr={wr:.1%} pf={pf:.2f} pnl={total_pnl:+.0f}"
            )

    results.sort(key=lambda r: r["composite"], reverse=True)
    return results


def main() -> int:
    p = argparse.ArgumentParser(prog="optimize_sl_tp")
    p.add_argument("--sample", type=int, default=0, help="Sample N decisions (0=all)")
    p.add_argument("--max-hold-bars", type=int, default=288, help="Max hold bars (288 = 24h)")
    args = p.parse_args()

    print("=" * 60)
    print("SL/TP PARAMETER SWEEP")
    print("=" * 60)

    # Load data
    print("\n[1/3] Loading OHLC + features...")
    bars = load_ohlc(OHLC_FILE)
    if OHLC_RECENT_FILE.exists():
        recent = load_ohlc(OHLC_RECENT_FILE)
        existing = {b["time"] for b in bars}
        for b in recent:
            if b["time"] not in existing:
                bars.append(b)
        bars.sort(key=lambda b: b["time"])
    features = load_features(FEATURE_FILE)
    print(f"  {len(bars)} bars, {len(features)} feature timestamps")

    # Load decisions
    print("\n[2/3] Loading decisions...")
    decisions = collect_decisions()
    print(f"  {len(decisions)} OPEN decisions")
    if args.sample and args.sample < len(decisions):
        step = len(decisions) // args.sample
        decisions = decisions[::step][: args.sample]
        print(f"  → sampled to {len(decisions)}")

    # Coarse sweep
    print(f"\n[3/3] Coarse sweep ({len(SL_COARSE)} SL x {len(TP_COARSE)} TP)...")
    t0 = time.time()
    coarse = sweep(decisions, bars, features, SL_COARSE, TP_COARSE, args.max_hold_bars)
    elapsed = time.time() - t0
    print(f"\n  Coarse sweep done in {elapsed:.0f}s\n")

    # Print top 10
    print(
        f"{'Rank':<5} {'SL':<6} {'TP':<6} {'Trades':<7} {'Win%':<8} "
        f"{'PF':<7} {'Total PnL':<10} {'Avg PnL':<9} {'Hold(h)':<8} {'Composite'}"
    )
    print("-" * 85)
    for i, r in enumerate(coarse[:15]):
        print(
            f"{i+1:<5} {r['sl']:<6.2f} {r['tp']:<6.2f} {r['trades']:<7} "
            f"{r['win_rate']:<8.1%} {r['profit_factor']:<7.2f} "
            f"{r['total_pnl']:<+10.1f} {r['avg_pnl']:<+9.1f} "
            f"{r['avg_hold_hours']:<8.1f} {r['composite']:.3f}"
        )

    # Fine sweep around best
    best = coarse[0]
    print(f"\n=== Fine sweep around best (SL={best['sl']}, TP={best['tp']}) ===")
    fine_sl = [best["sl"] + d * 0.1 for d in range(-5, 6)]
    fine_tp = [best["tp"] + d * 0.1 for d in range(-5, 6)]
    fine_sl = [round(x, 2) for x in fine_sl if x > 0]
    fine_tp = [round(x, 2) for x in fine_tp if x > 0]

    fine = sweep(decisions, bars, features, fine_sl, fine_tp, args.max_hold_bars)

    print(
        f"\n{'Rank':<5} {'SL':<6} {'TP':<6} {'Trades':<7} {'Win%':<8} "
        f"{'PF':<7} {'Total PnL':<10} {'Avg PnL':<9} {'Hold(h)':<8} {'Composite'}"
    )
    print("-" * 85)
    for i, r in enumerate(fine[:10]):
        marker = " ← BEST" if i == 0 else ""
        print(
            f"{i+1:<5} {r['sl']:<6.2f} {r['tp']:<6.2f} {r['trades']:<7} "
            f"{r['win_rate']:<8.1%} {r['profit_factor']:<7.2f} "
            f"{r['total_pnl']:<+10.1f} {r['avg_pnl']:<+9.1f} "
            f"{r['avg_hold_hours']:<8.1f} {r['composite']:.3f}{marker}"
        )

    # Current baseline comparison
    current = next((r for r in coarse if r["sl"] == 2.0 and r["tp"] == 3.5), None)
    print("\n=== Baseline vs Best ===")
    if current:
        print(
            f"Current (SL=2.0, TP=3.5): wr={current['win_rate']:.1%} "
            f"pf={current['profit_factor']:.2f} pnl={current['total_pnl']:+.1f}"
        )
    print(
        f"Best (SL={best['sl']:.1f}, TP={best['tp']:.1f}):  "
        f"wr={best['win_rate']:.1%} pf={best['profit_factor']:.2f} "
        f"pnl={best['total_pnl']:+.1f}"
    )

    # Recommend
    print("\nRecommended config:")
    print(f"  SL_ATR_MULT = {best['sl']:.1f}")
    print(f"  TP_ATR_MULT = {best['tp']:.1f}")
    print(f"  Expected win rate: {best['win_rate']:.1%}")
    print(f"  Expected profit factor: {best['profit_factor']:.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
