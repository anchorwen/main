"""Sandbox: High Recall + High Precision architecture backtest.

Simulates the "loose upstream (Huber) + tight downstream (MetaFilter)"
combination to find the optimal trade frequency vs. EV balance.

Usage:
  python scripts/backtest/backtest_high_recall_precision.py
  python scripts/backtest/backtest_high_recall_precision.py --output report.json
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


def simulate_candidate_pools(
    labels: np.ndarray,
    p_win_arr: np.ndarray,
    upstream_thresholds: list[float],
    meta_thresholds: list[float],
) -> dict[str, Any]:
    """Simulate different upstream looseness + MetaFilter combinations.

    upstream_threshold: Simulates different Huber confidence thresholds.
      Lower = more candidates (higher recall, lower avg quality).
      We model this by stratifying signals by their p_win and including
      progressively more lower-quality signals as the threshold drops.

    meta_threshold: Actual MetaFilter p_win threshold for pass/block.
    """
    n = len(labels)

    results = []
    for up_th in upstream_thresholds:
        combinations: list[dict[str, object]] = []
        row: dict[str, object] = {"upstream_threshold": up_th, "combinations": combinations}
        # Simulate upstream: include all signals where p_win >= up_th
        # This models "Huber generates candidates, some are low quality"
        candidate_mask = p_win_arr >= up_th
        n_candidates = int(np.sum(candidate_mask))
        candidate_labels = labels[candidate_mask]
        candidate_p_win = p_win_arr[candidate_mask]

        if n_candidates < 10:
            combinations.append(
                {
                    "meta_threshold": 0.0,
                    "n_candidates": n_candidates,
                    "n_passed": 0,
                    "pass_rate": 0.0,
                    "frequency_pct": 0.0,
                    "wr": 0.0,
                    "total_pnl_r": 0.0,
                    "profit_factor": 0.0,
                    "avg_pnl_per_trade_r": 0.0,
                }
            )
            results.append(row)
            continue

        for meta_th in meta_thresholds:
            pm = candidate_p_win >= meta_th
            n_passed = int(np.sum(pm))
            if n_passed == 0:
                combinations.append(
                    {
                        "meta_threshold": meta_th,
                        "n_candidates": n_candidates,
                        "n_passed": 0,
                        "pass_rate": 0.0,
                        "frequency_pct": 0.0,
                        "wr": 0.0,
                        "total_pnl_r": 0.0,
                        "profit_factor": 0.0,
                        "avg_pnl_per_trade_r": 0.0,
                    }
                )
                continue

            passed_labels = candidate_labels[pm]
            wins = int(np.sum(passed_labels == 1))
            losses = int(np.sum(passed_labels == -1))
            timeouts = int(np.sum(passed_labels == 0))
            wr = wins / max(wins + losses, 1)
            pnl = np.where(passed_labels == 1, 1.0, np.where(passed_labels == -1, -1.0, 0.0))
            total_pnl = float(np.sum(pnl))
            gross_profit = float(np.sum(np.maximum(pnl, 0)))
            gross_loss = float(np.sum(np.abs(np.minimum(pnl, 0))))
            pf = gross_profit / max(gross_loss, 1e-8)
            pass_rate = n_passed / n_candidates
            frequency_pct = n_passed / n * 100.0
            avg_pnl_per_trade = total_pnl / n_passed

            combinations.append(
                {
                    "meta_threshold": meta_th,
                    "n_candidates": n_candidates,
                    "n_passed": n_passed,
                    "pass_rate": round(pass_rate, 4),
                    "frequency_pct": round(frequency_pct, 1),
                    "wr": round(wr, 4),
                    "total_pnl_r": round(total_pnl, 2),
                    "profit_factor": round(pf, 2),
                    "avg_pnl_per_trade_r": round(avg_pnl_per_trade, 4),
                }
            )
        results.append(row)

    return {"simulations": results, "n_total_signals": n}


def run_backtest(
    data_path: Path,
    model_dir: Path,
) -> dict[str, Any]:
    """Run the High Recall + High Precision architecture backtest."""
    data = np.load(data_path, allow_pickle=True)
    X: np.ndarray = data["X"]
    feature_names: list[str] = list(data.get("feature_names", []))

    y_be: np.ndarray = data.get("y_breakeven", data.get("y"))
    y_pnl: np.ndarray = data.get("y_signal_pnl", data.get("pnl"))

    # Derive ternary labels from actual PnL
    if y_pnl is None:
        y_pnl = np.zeros(len(y_be), dtype=np.float64)
    labels = np.where(y_pnl > 0, 1, np.where(y_pnl < 0, -1, 0)).astype(np.int32)

    print(f"Dataset: {X.shape[0]} signals, {X.shape[1]} features")
    unique, counts = np.unique(labels, return_counts=True)
    dist = {str(int(k)): int(v) for k, v in zip(unique, counts, strict=False)}
    n_valid = int(np.sum(labels != 0))
    blind_wr = float(np.sum(labels == 1)) / max(n_valid, 1)
    print(f"Labels: {dist}")
    print(f"Blind WR (ex-timeout): {blind_wr:.1%}")
    print()

    # Load meta-filter and get p_win for all signals
    adapter = load_meta_filter(model_dir, 0.50)
    p_win_arr = np.zeros(len(X))
    for i in range(len(X)):
        feat_dict = {feature_names[j]: float(X[i, j]) for j in range(len(feature_names))}
        p_win_arr[i] = adapter.predict_proba(feat_dict)

    print(f"p_win distribution: mean={p_win_arr.mean():.4f}, std={p_win_arr.std():.4f}")
    print(f"  min={p_win_arr.min():.4f}, q25={np.percentile(p_win_arr, 25):.4f}")
    print(f"  q50={np.percentile(p_win_arr, 50):.4f}, q75={np.percentile(p_win_arr, 75):.4f}")
    print(f"  max={p_win_arr.max():.4f}")

    # ── Scenario A: Baseline (current approach) ──
    # Blind: all signals pass
    blind_pnl = np.where(labels == 1, 1.0, np.where(labels == -1, -1.0, 0.0))
    blind_total = float(np.sum(blind_pnl))
    blind_wins = int(np.sum(labels == 1))
    blind_losses = int(np.sum(labels == -1))
    blind_wr_rate = blind_wins / max(blind_wins + blind_losses, 1)

    print("\n=== Scenario A: Blind (all signals) ===")
    print(f"  Trades: {len(labels)}, WR: {blind_wr_rate:.1%}, PnL: {blind_total:+.1f}R")

    # ── Scenario B: MetaFilter only at various thresholds ──
    print("\n=== Scenario B: MetaFilter threshold sweep ===")
    print(f"{'Th':>5} {'Pass%':>7} {'WR':>7} {'PnL':>8} {'PF':>6} {'Avg/Trade':>10}")
    meta_thresholds = np.linspace(0.35, 0.70, 8)
    best_pnl = -999.0
    best_th = 0.50
    for th in meta_thresholds:
        pm = p_win_arr >= th
        n_pass = int(np.sum(pm))
        if n_pass == 0:
            continue
        pl = labels[pm]
        w = int(np.sum(pl == 1))
        l = int(np.sum(pl == -1))
        wr = w / max(w + l, 1)
        pnl_vec = np.where(pl == 1, 1.0, np.where(pl == -1, -1.0, 0.0))
        total = float(np.sum(pnl_vec))
        gp = float(np.sum(np.maximum(pnl_vec, 0)))
        gl = float(np.sum(np.abs(np.minimum(pnl_vec, 0))))
        pf = gp / max(gl, 1e-8)
        avg = total / n_pass
        marker = " <-- BEST" if total > best_pnl else ""
        if total > best_pnl:
            best_pnl = total
            best_th = th
        print(
            f"{th:>5.2f} {n_pass/len(labels)*100:>7.1f}% {wr:>7.1%} {total:>+8.1f} {pf:>6.2f} {avg:>+10.4f}{marker}"
        )

    print(f"\n  Optimal MetaFilter threshold: {best_th:.2f} (PnL={best_pnl:+.1f}R)")

    # ── Scenario C: High Recall + High Precision ──
    # Simulate different upstream looseness levels
    # upstream_threshold simulates different Huber confidence thresholds
    # Lower upstream_threshold = more candidates from Huber
    print("\n=== Scenario C: High Recall + High Precision ===")
    print("(Simulating loose Huber upstream + tight MetaFilter downstream)")
    print()

    upstream_levels = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55]
    result = simulate_candidate_pools(labels, p_win_arr, upstream_levels, meta_thresholds)

    # Find optimal combination
    print(
        f"{'UpTh':>5} {'Cand':>6} {'MetaTh':>7} {'Pass':>6} {'Freq%':>7} {'WR':>7} {'PnL':>8} {'PF':>6} {'Avg/Trade':>10}"
    )
    best_combo: dict[str, object] | None = None
    best_combo_pnl = -999.0
    for sim in result["simulations"]:
        up_th = sim["upstream_threshold"]
        for c in sim["combinations"]:
            if c["n_passed"] == 0:
                continue
            marker = ""
            if c["total_pnl_r"] > best_combo_pnl:
                best_combo_pnl = c["total_pnl_r"]
                best_combo = {"upstream": up_th, **c}
                marker = " <--"
            if c["n_passed"] >= 20:
                print(
                    f"{up_th:>5.2f} {c['n_candidates']:>6} {c['meta_threshold']:>7.2f} "
                    f"{c['n_passed']:>6} {c['frequency_pct']:>7.1f}% {c['wr']:>7.1%} "
                    f"{c['total_pnl_r']:>+8.1f} {c['profit_factor']:>6.2f} "
                    f"{c['avg_pnl_per_trade_r']:>+10.4f}{marker}"
                )

    if best_combo is not None:
        print(
            f"\n  Best combination: upstream={best_combo['upstream']:.2f}, "
            f"meta_threshold={best_combo['meta_threshold']:.2f} "
            f"-> {best_combo['n_passed']} trades, WR={best_combo['wr']:.1%}, "
            f"PnL={best_combo['total_pnl_r']:+.1f}R"
        )
    else:
        print("\n  No valid combination found.")

    # ── Scenario D: Full surface ──
    print("\n=== Scenario D: Full precision-recall surface ===")
    print("(upstream_threshold vs meta_threshold -> total PnL)")

    # Build a compact summary table
    print(f"{'':>8}", end="")
    for mt in meta_thresholds:
        print(f" Meta{mt:.2f}", end="")
    print()
    for sim in result["simulations"]:
        up_th = sim["upstream_threshold"]
        print(f"Up={up_th:.2f}  ", end="")
        for c in sim["combinations"]:
            if c["n_passed"] > 0:
                print(f" {c['total_pnl_r']:+6.1f}", end="")
            else:
                print(f" {'--':>6}", end="")
        print()

    return {
        "dataset": str(data_path),
        "n_signals": int(len(labels)),
        "blind": {
            "trades": int(len(labels)),
            "wr": round(blind_wr_rate, 4),
            "total_pnl_r": round(blind_total, 2),
        },
        "meta_filter_sweep": {
            "optimal_threshold": round(best_th, 2),
            "optimal_pnl_r": round(best_pnl, 2),
        },
        "high_recall_precision": result,
        "best_combination": best_combo,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backtest_high_recall_precision")
    p.add_argument(
        "--data",
        type=Path,
        default=PROJECT_ROOT / "data/training/meta_labeling_v3/full.npz",
    )
    p.add_argument(
        "--model",
        type=Path,
        default=PROJECT_ROOT / "data/models/meta_filter_v3",
    )
    p.add_argument("--output", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_backtest(args.data, args.model)

    if args.output:
        Path(args.output).write_text(
            json.dumps(result, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nReport saved to: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
