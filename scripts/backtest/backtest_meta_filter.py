"""Sandbox backtest: OU signal → meta-filter → simplified PnL curve.

Loads the meta-labeling validation set (chronological hold-out), runs
the LightGBM meta-filter on each signal, and compares blind vs filtered
PnL curves.

PnL model:
  - TP hit first (breakeven triggered): +1R
  - SL hit first (stop-loss hit): -1R
  - Timeout (neither hit within 12 bars): 0R (exit at cost)

This is a simplified sandbox — it does NOT simulate trail/breakeven
execution mechanics.  It answers: "does the meta-filter improve signal
quality on out-of-sample data?"

Usage:
    python scripts/backtest/backtest_meta_filter.py \
        --data data/training/meta_labeling_v3/val.npz \
        --model data/models/meta_filter_v3 \
        --threshold 0.50
"""

from __future__ import annotations

import argparse
import json
import warnings
from pathlib import Path
from typing import Any

import numpy as np

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def load_meta_filter(model_dir: Path, threshold: float) -> Any:
    from core.brains.adapters.meta_filter_adapter import MetaFilterAdapter

    adapter = MetaFilterAdapter(
        model_path=model_dir / "meta_filter_lightgbm.pkl",
        feature_names_path=model_dir / "feature_names.json",
        threshold=threshold,
    )
    adapter.load()
    return adapter


def simulate_pnl(
    labels: np.ndarray,
    passed_mask: np.ndarray,
) -> dict[str, Any]:
    """Simulate PnL curve with blind (pass all) vs filtered (pass only passed_mask).

    labels: 1=tp_hit, 0=timeout, -1=sl_hit
    """
    n = len(labels)

    # Blind: pass all signals
    blind_pnl = np.where(labels == 1, 1.0, np.where(labels == -1, -1.0, 0.0))
    blind_equity = np.cumsum(blind_pnl)
    blind_trades = n
    blind_wins = int(np.sum(labels == 1))
    blind_losses = int(np.sum(labels == -1))
    blind_timeouts = int(np.sum(labels == 0))
    blind_wr = blind_wins / max(blind_wins + blind_losses, 1)
    blind_total_pnl = float(blind_equity[-1])

    # Filtered: only trade passed signals
    filtered_pnl = np.where(passed_mask, blind_pnl, 0.0)
    filtered_equity = np.cumsum(filtered_pnl)
    filtered_trades = int(np.sum(passed_mask))
    filtered_wins = int(np.sum((labels == 1) & passed_mask))
    filtered_losses = int(np.sum((labels == -1) & passed_mask))
    filtered_timeouts = int(np.sum((labels == 0) & passed_mask))
    filtered_wr = filtered_wins / max(filtered_wins + filtered_losses, 1)
    filtered_total_pnl = float(filtered_equity[-1])
    pass_rate = filtered_trades / max(n, 1)

    # Blocked signals: what would we have missed?
    blocked_mask = ~passed_mask
    blocked_wins = int(np.sum((labels == 1) & blocked_mask))
    blocked_losses = int(np.sum((labels == -1) & blocked_mask))
    blocked_timeouts = int(np.sum((labels == 0) & blocked_mask))

    # Savings from blocking losers
    losses_avoided = float(np.sum((labels == -1) & blocked_mask))
    wins_forgone = float(np.sum((labels == 1) & blocked_mask))

    # Profit factor
    blind_profit = float(np.sum(np.maximum(blind_pnl, 0)))
    blind_loss = float(np.sum(np.abs(np.minimum(blind_pnl, 0))))
    blind_pf = blind_profit / max(blind_loss, 1e-8)

    filtered_profit = float(np.sum(np.maximum(filtered_pnl, 0)))
    filtered_loss = float(np.sum(np.abs(np.minimum(filtered_pnl, 0))))
    filtered_pf = filtered_profit / max(filtered_loss, 1e-8)

    # Max drawdown
    blind_peak = np.maximum.accumulate(blind_equity)
    blind_dd: float = float(np.max(blind_peak - blind_equity))
    filtered_peak = np.maximum.accumulate(filtered_equity)
    filtered_dd: float = float(np.max(filtered_peak - filtered_equity))

    # Sharpe-like (assuming 300 trades/year for annualization)
    blind_returns = np.diff(blind_equity, prepend=0)
    filtered_returns = np.diff(filtered_equity, prepend=0)
    blind_sharpe = float(float(np.mean(blind_returns)) / max(float(np.std(blind_returns)), 1e-8))
    filtered_sharpe = float(
        float(np.mean(filtered_returns)) / max(float(np.std(filtered_returns)), 1e-8)
    )

    return {
        "blind": {
            "trades": blind_trades,
            "wins": blind_wins,
            "losses": blind_losses,
            "timeouts": blind_timeouts,
            "wr": round(blind_wr, 4),
            "total_pnl_r": round(blind_total_pnl, 2),
            "profit_factor": round(blind_pf, 2),
            "max_dd_r": round(float(blind_dd), 2),
            "sharpe": round(blind_sharpe, 2),
        },
        "filtered": {
            "trades": filtered_trades,
            "wins": filtered_wins,
            "losses": filtered_losses,
            "timeouts": filtered_timeouts,
            "wr": round(filtered_wr, 4),
            "total_pnl_r": round(filtered_total_pnl, 2),
            "profit_factor": round(filtered_pf, 2),
            "max_dd_r": round(float(filtered_dd), 2),
            "sharpe": round(filtered_sharpe, 2),
            "pass_rate": round(pass_rate, 4),
        },
        "blocked": {
            "wins_forgone": int(wins_forgone),
            "losses_avoided": int(losses_avoided),
            "timeouts": blocked_timeouts,
            "net_saved_r": round(losses_avoided - wins_forgone, 2),
        },
    }


def run_backtest(
    data_path: Path,
    model_dir: Path,
    threshold: float = 0.50,
    threshold_sweep: bool = False,
) -> dict[str, Any]:
    """Run sandbox backtest on meta-labeling dataset."""
    data = np.load(data_path, allow_pickle=True)
    X: np.ndarray = data["X"]  # (n_samples, 47)
    feature_names: list[str] = list(data.get("feature_names", []))

    # The meta-labeling NPZ stores:
    #   y_breakeven: 1=tp_hit_first, 0=timeout, -1=sl_hit_first (ternary)
    #   y_signal_pnl: raw PnL in R
    y_be: np.ndarray = data.get("y_breakeven", data.get("y"))
    y_pnl: np.ndarray = data.get("y_signal_pnl", np.zeros_like(y_be))

    print(f"Loaded: {X.shape[0]} samples, {X.shape[1]} features")
    unique, counts = np.unique(y_be, return_counts=True)
    label_dist = {str(int(k)): int(v) for k, v in zip(unique, counts, strict=False)}
    print(f"Label distribution: {label_dist}")

    # Derive ternary labels from actual PnL (y_breakeven is binary, not ternary)
    # >0 → win (+1), ==0 → timeout (0), <0 → loss (-1)
    labels_ternary = np.where(y_pnl > 0, 1, np.where(y_pnl < 0, -1, 0)).astype(np.int32)
    unique_t, counts_t = np.unique(labels_ternary, return_counts=True)
    ternary_dist = {str(int(k)): int(v) for k, v in zip(unique_t, counts_t, strict=False)}
    print(f"Ternary distribution (from y_signal_pnl): {ternary_dist}")

    # Load meta-filter
    adapter = load_meta_filter(model_dir, threshold)

    # Run filter on all samples
    p_wins = []
    passed = []
    for i in range(len(X)):
        # Build feature dict from row
        row = X[i]
        feat_dict = {}
        for j, name in enumerate(feature_names):
            feat_dict[name] = float(row[j])
        result = adapter.filter(feat_dict)
        p_wins.append(result["p_win"])
        passed.append(result["passed"])

    passed_mask = np.array(passed, dtype=bool)
    p_win_arr = np.array(p_wins)

    # ── Single-threshold backtest ──
    result = simulate_pnl(labels_ternary, passed_mask)
    result["meta"] = {
        "threshold": threshold,
        "p_win_mean": round(float(np.mean(p_win_arr)), 4),
        "p_win_std": round(float(np.std(p_win_arr)), 4),
        "n_signals": int(len(y_be)),
        "n_features": int(X.shape[1]),
    }

    # ── Threshold sweep ──
    if threshold_sweep:
        thresholds = np.linspace(0.30, 0.70, 9)
        sweep = []
        for th in thresholds:
            pm = p_win_arr >= th
            r = simulate_pnl(labels_ternary, pm)
            sweep.append(
                {
                    "threshold": round(th, 2),
                    "pass_rate": r["filtered"]["pass_rate"],
                    "wr": r["filtered"]["wr"],
                    "total_pnl_r": r["filtered"]["total_pnl_r"],
                    "profit_factor": r["filtered"]["profit_factor"],
                    "sharpe": r["filtered"]["sharpe"],
                    "losses_avoided": r["blocked"]["losses_avoided"],
                    "wins_forgone": r["blocked"]["wins_forgone"],
                }
            )
        result["threshold_sweep"] = sweep

    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backtest_meta_filter")
    p.add_argument("--data", type=Path, required=True, help="Meta-labeling NPZ (val split)")
    p.add_argument(
        "--model",
        type=Path,
        required=True,
        help="Model directory (contains .pkl + feature_names.json)",
    )
    p.add_argument("--threshold", type=float, default=0.50, help="p_win threshold for pass/block")
    p.add_argument("--sweep", action="store_true", help="Run threshold sweep [0.30..0.70]")
    p.add_argument("--output", type=Path, default=None, help="Save JSON report")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    result = run_backtest(
        data_path=args.data,
        model_dir=args.model,
        threshold=args.threshold,
        threshold_sweep=args.sweep,
    )

    # ── Print report ──
    b = result["blind"]
    f = result["filtered"]
    blk = result["blocked"]
    m = result["meta"]

    print(f"\n{'='*60}")
    print("SANDBOX BACKTEST: Meta-Filter vs Blind")
    print(f"{'='*60}")
    print(
        f"  Signals: {m['n_signals']} | Features: {m['n_features']} | Threshold: {m['threshold']}"
    )
    print(f"  p_win distribution: μ={m['p_win_mean']:.4f} σ={m['p_win_std']:.4f}")
    print()
    print(f"  {'':<25} {'BLIND':>10} {'FILTERED':>10} {'Δ':>10}")
    print(f"  {'─'*25} {'─'*10} {'─'*10} {'─'*10}")
    print(f"  {'Trades':<25} {b['trades']:>10} {f['trades']:>10} {f['trades']-b['trades']:>+10}")
    print(f"  {'Win Rate':<25} {b['wr']:>10.1%} {f['wr']:>10.1%} {f['wr']-b['wr']:>+10.1%}")
    print(
        f"  {'Total PnL (R)':<25} {b['total_pnl_r']:>+10.2f} {f['total_pnl_r']:>+10.2f} {f['total_pnl_r']-b['total_pnl_r']:>+10.2f}"
    )
    print(
        f"  {'Profit Factor':<25} {b['profit_factor']:>10.2f} {f['profit_factor']:>10.2f} {f['profit_factor']-b['profit_factor']:>+10.2f}"
    )
    print(
        f"  {'Max DD (R)':<25} {b['max_dd_r']:>10.2f} {f['max_dd_r']:>10.2f} {f['max_dd_r']-b['max_dd_r']:>+10.2f}"
    )
    print(
        f"  {'Sharpe':<25} {b['sharpe']:>10.2f} {f['sharpe']:>10.2f} {f['sharpe']-b['sharpe']:>+10.2f}"
    )
    print(f"  {'Pass Rate':<25} {'100.0%':>10} {f['pass_rate']:>10.1%}")
    print()
    print(f"  Losses avoided: {blk['losses_avoided']}R | Wins forgone: {blk['wins_forgone']}R")
    print(f"  Net R saved by blocking: {blk['net_saved_r']:+.1f}R")
    print(f"  ({blk['timeouts']} timeouts also blocked — costless)")

    # ── Threshold sweep ──
    if "threshold_sweep" in result:
        print(f"\n{'─'*70}")
        print("THRESHOLD SWEEP")
        print(
            f"{'Th':>5} {'Pass%':>7} {'WR':>7} {'PnL':>8} {'PF':>6} {'Sharpe':>7} {'LossSaved':>10} {'WinLost':>9}"
        )
        for s in result["threshold_sweep"]:
            print(
                f"{s['threshold']:>5.2f} {s['pass_rate']:>7.1%} {s['wr']:>7.1%} "
                f"{s['total_pnl_r']:>+8.1f} {s['profit_factor']:>6.2f} {s['sharpe']:>+7.2f} "
                f"{s['losses_avoided']:>10.0f} {s['wins_forgone']:>9.0f}"
            )

    # ── Save ──
    if args.output:
        report_path = Path(args.output)
        # Convert numpy types for JSON serialization
        report_path.write_text(
            json.dumps(result, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nReport saved to: {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
