"""optimize_meta_threshold.py — R-multiple EV grid search for Stage 2 filter.

Grid-searches the P(TP|signal) threshold that maximizes expected value
in R-multiple space. Since SL/TP distances are fixed by the label contract
(SL=2.0x ATR, TP=3.5x ATR), the R-multiple payoff is constant:

    TP_R = tp_atr_mult / sl_atr_mult = 3.5 / 2.0 = 1.75
    SL_R = 1.0

The expected value per trade in R-units is:

    EV_R = P(TP) * TP_R - (1 - P(TP)) * SL_R

Minimum viable P(TP): SL_R / (TP_R + SL_R) = 1.0 / 2.75 ≈ 0.364

The optimizer finds the probability threshold that maximizes #trades with
positive EV_R, then reports the optimal cutoff for Stage 2 filtering.

Usage:
    python scripts/training/optimize_meta_threshold.py \
        --probs data/training/stage2_probs.npy \
        --labels data/training/stage2_labels.npy \
        --contract configs/training/barrier_12bar_regression_huber.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np


def compute_ev_r(
    probs: np.ndarray,
    y_true: np.ndarray,
    threshold: float,
    tp_r: float = 1.75,
    sl_r: float = 1.0,
) -> dict[str, float]:
    """Compute R-multiple EV metrics at a given probability threshold.

    Args:
        probs: Stage 2 predicted P(TP|signal) probabilities.
        y_true: Binary labels (1=TP, 0=non-TP).
        threshold: Probability cutoff. Signals with P(TP) >= threshold pass.
        tp_r: R-multiple reward for a win.
        sl_r: R-multiple cost of a loss.

    Returns:
        Dict with ev_r, n_trades, n_wins, win_rate, expected_profit_r.
    """
    passed = probs >= threshold
    n_trades = int(np.sum(passed))
    if n_trades == 0:
        return {
            "ev_r": 0.0,
            "n_trades": 0,
            "n_wins": 0,
            "win_rate": 0.0,
            "expected_profit_r": 0.0,
        }

    y_passed = y_true[passed]
    n_wins = int(np.sum(y_passed == 1))
    win_rate = n_wins / n_trades

    ev_r = win_rate * tp_r - (1.0 - win_rate) * sl_r
    expected_profit_r = ev_r * n_trades

    return {
        "ev_r": round(ev_r, 6),
        "n_trades": n_trades,
        "n_wins": n_wins,
        "win_rate": round(win_rate, 6),
        "expected_profit_r": round(expected_profit_r, 6),
    }


def optimize_threshold(
    probs: np.ndarray,
    y_true: np.ndarray,
    *,
    sl_atr_mult: float = 2.0,
    tp_atr_mult: float = 3.5,
    threshold_min: float = 0.30,
    threshold_max: float = 0.75,
    step: float = 0.01,
) -> tuple[float, dict[str, float], list[dict[str, Any]]]:
    """Grid-search the P(TP) threshold that maximizes EV_R * n_trades.

    R-multiple payoff:
        TP_R = tp_atr_mult / sl_atr_mult
        SL_R = 1.0

    Minimum viable P(TP) = SL_R / (TP_R + SL_R) = 1 / (TP_R + 1).

    Returns:
        (best_threshold, best_metrics, full_grid_results).
    """
    tp_r = tp_atr_mult / sl_atr_mult
    sl_r = 1.0
    min_viable = sl_r / (tp_r + sl_r)

    print("[threshold] R-multiple setup:")
    print(f"  SL = {sl_atr_mult}x ATR, TP = {tp_atr_mult}x ATR")
    print(f"  TP_R = {tp_r:.4f}, SL_R = {sl_r:.4f}")
    print(f"  Minimum viable P(TP) = {min_viable:.4f} ({min_viable*100:.1f}%)")
    print(f"  Grid: [{threshold_min:.2f}, {threshold_max:.2f}] step={step:.2f}")
    print(f"  Total samples: {len(probs)}, TP rate: {float(np.mean(y_true)):.4f}")

    thresholds = np.arange(threshold_min, threshold_max + step / 2, step)
    results: list[dict[str, Any]] = []

    best_composite = -999.0
    best_threshold = min_viable
    best_metrics: dict[str, float] = {}

    for thresh in thresholds:
        m = compute_ev_r(probs, y_true, float(thresh), tp_r=tp_r, sl_r=sl_r)
        results.append({"threshold": float(thresh), **m})

        # Composite score: EV_R * sqrt(n_trades) — balances quality with
        # trade frequency. Pure EV_R favors thresholds with 1 trade.
        composite = m["ev_r"] * np.sqrt(max(m["n_trades"], 1))
        if composite > best_composite and m["ev_r"] > 0:
            best_composite = composite
            best_threshold = float(thresh)
            best_metrics = m

    return best_threshold, best_metrics, results


def print_grid(results: list[dict[str, Any]]) -> None:
    """Print the grid search results as a formatted table."""
    print()
    print(
        f"{'Threshold':>10s}  {'EV_R':>8s}  {'WinRate':>8s}  {'Trades':>7s}  {'Profit_R':>9s}  {'Viable':>7s}"
    )
    print("-" * 60)
    for r in results:
        thresh = float(r["threshold"])
        ev = float(r["ev_r"])
        wr = float(r["win_rate"])
        nt = int(r["n_trades"])
        profit = float(r["expected_profit_r"])
        viable = "YES" if ev > 0 else "no"
        marker = " <--" if ev > 0 and nt > 10 else ""
        print(f"{thresh:10.2f}  {ev:8.4f}  {wr:8.4f}  {nt:7d}  {profit:9.2f}  {viable:>7s}{marker}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Optimize Stage 2 P(TP) threshold via R-multiple EV grid search"
    )
    ap.add_argument("--probs", required=True, help="Path to Stage 2 predicted probabilities (.npy)")
    ap.add_argument("--labels", required=True, help="Path to binary labels (.npy)")
    ap.add_argument(
        "--contract", default=None, help="Optional path to TrainingContract for SL/TP multipliers"
    )
    ap.add_argument("--sl-atr", type=float, default=2.0, help="SL ATR multiplier (default: 2.0)")
    ap.add_argument("--tp-atr", type=float, default=3.5, help="TP ATR multiplier (default: 3.5)")
    ap.add_argument(
        "--min-thresh", type=float, default=0.30, help="Min threshold for grid (default: 0.30)"
    )
    ap.add_argument(
        "--max-thresh", type=float, default=0.75, help="Max threshold for grid (default: 0.75)"
    )
    ap.add_argument("--step", type=float, default=0.01, help="Grid step (default: 0.01)")
    ap.add_argument(
        "--output", default=None, help="Optional path to save full grid results (.json)"
    )
    args = ap.parse_args(argv)

    # Load Stage 2 probabilities and labels
    probs_path = Path(args.probs)
    labels_path = Path(args.labels)

    if not probs_path.exists():
        print(f"[threshold] ERROR: probs file not found: {probs_path}")
        return 1
    if not labels_path.exists():
        print(f"[threshold] ERROR: labels file not found: {labels_path}")
        return 1

    probs = np.load(probs_path).ravel().astype(np.float64)
    y_true = np.load(labels_path).ravel().astype(np.int32)

    if len(probs) != len(y_true):
        print(f"[threshold] ERROR: length mismatch — probs={len(probs)}, labels={len(y_true)}")
        return 1

    # Optionally load SL/TP from contract
    sl_atr = args.sl_atr
    tp_atr = args.tp_atr
    if args.contract:
        from core.contracts.training.training_contract import TrainingContract

        contract = TrainingContract.from_file(args.contract)
        sl_atr = contract.label.sl_atr_mult
        tp_atr = contract.label.tp_atr_mult

    best_threshold, best_metrics, results = optimize_threshold(
        probs,
        y_true,
        sl_atr_mult=sl_atr,
        tp_atr_mult=tp_atr,
        threshold_min=args.min_thresh,
        threshold_max=args.max_thresh,
        step=args.step,
    )

    print_grid(results)
    print()
    print(f"[threshold] Optimal cutoff: P(TP) >= {best_threshold:.2f}")
    print(f"  EV_R:           {best_metrics.get('ev_r', 0):.4f}")
    print(f"  Win rate:       {best_metrics.get('win_rate', 0):.4f}")
    print(f"  Trades passed:  {best_metrics.get('n_trades', 0)}")
    print(f"  Total profit R: {best_metrics.get('expected_profit_r', 0):.2f}")

    if best_metrics.get("ev_r", 0) <= 0:
        print()
        print("[threshold] WARNING: No threshold produces positive EV_R.")
        print("  Stage 2 filter may not be viable with current Stage 1 signal quality.")
        print("  Consider: improving Stage 1 training, different meta-features, or wider SL/TP.")

    # Save grid results if requested
    if args.output:
        import json

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"[threshold] Grid results saved to: {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
