#!/usr/bin/env python
"""Backtest the 2D regime matrix + Schmitt trigger for OU strategies.

Compares three filtering approaches on historical data:
  1. No filtering (baseline)
  2. Pure H1 Hurst threshold (ranging < 0.4, trending > 0.6)
  3. 2D regime matrix with Schmitt trigger hysteresis

For each regime state, evaluates subsequent OU-style Z-score performance:
  - Entry when |Z| >= z_entry
  - Exit when |Z| < z_exit OR max_hold bars reached

Usage:
  python scripts/backtest/backtest_regime_2d.py \
    --price-data data/raw/xauusdc_m5_1y.csv \
    --output data/backtest/regime_2d/
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

# ── Constants ──

Z_LOOKBACK = 20  # Z-score MA window
Z_ENTRY = 1.5  # enter when |Z| >= this
Z_EXIT = 0.3  # exit when |Z| < this
MAX_HOLD_BARS = 8  # max holding period
MIN_BARS_FOR_RV = 500  # minimum bars before RV percentile is reliable
MVS_THRESHOLD = 0.20  # minimum viable signal (for R3, used here for sizing)


def load_ohlc(path: str | Path) -> dict[str, np.ndarray]:
    """Load OHLC data from CSV. Expects columns: time,open,high,low,close,volume."""
    import csv

    opens, highs, lows, closes, volumes = [], [], [], [], []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            opens.append(float(row.get("open", 0)))
            highs.append(float(row.get("high", 0)))
            lows.append(float(row.get("low", 0)))
            closes.append(float(row.get("close", 0)))
            volumes.append(float(row.get("volume", 0)))
    return {
        "open": np.array(opens, dtype=np.float64),
        "high": np.array(highs, dtype=np.float64),
        "low": np.array(lows, dtype=np.float64),
        "close": np.array(closes, dtype=np.float64),
        "volume": np.array(volumes, dtype=np.float64),
        "n_bars": len(closes),
    }


def compute_z_score(closes: np.ndarray, i: int, lookback: int = Z_LOOKBACK) -> float:
    """Compute Z-score (price-MA)/std at bar i using only past data."""
    if i < lookback:
        return 0.0
    window = closes[i - lookback : i]
    mean = float(np.mean(window))
    std = float(np.std(window))
    if std < 1e-10:
        return 0.0
    return float((closes[i] - mean) / std)


def compute_rv_percentile(rv_values: np.ndarray, i: int) -> float:
    """Compute percentile rank of rv_values[i] in rv_values[:i+1]."""
    if i < MIN_BARS_FOR_RV:
        return 0.5
    window = rv_values[max(0, i - MIN_BARS_FOR_RV + 1) : i + 1]
    current = rv_values[i]
    rank = float(np.searchsorted(np.sort(window), current, side="right"))
    return rank / float(len(window))


def resample_to_h1(closes: np.ndarray) -> np.ndarray:
    """Resample M5 closes to H1 (12 bars per H1)."""
    n = len(closes) // 12
    return closes[: n * 12 : 12].copy()


def compute_hurst_vr(price: np.ndarray, i: int, window: int = 60, k: int = 6) -> float:
    """Compute Hurst-like metric using Variance Ratio at bar i.

    VR(k) = Var(k-bar return) / (k * Var(1-bar return))
    VR ≈ 1 → random walk (H ≈ 0.5)
    VR < 1 → mean-reverting (H < 0.5)
    VR > 1 → trending (H > 0.5)

    Maps VR to [0, 1] via: H_est = 0.5 + 0.5 * (VR - 1) / (|VR-1| + 1)
    So VR=0.5 → H≈0.33, VR=1.0 → H≈0.5, VR=1.5 → H≈0.67.
    """
    if i < window:
        return 0.5
    p = price[max(0, i - window + 1) : i + 1]
    n = len(p)
    if n < k + 1:
        return 0.5

    log_p = np.log(p)
    ret_1bar = np.diff(log_p)

    var_1bar = float(np.var(ret_1bar))
    if var_1bar < 1e-12:
        return 0.5

    ret_kbar = log_p[k:] - log_p[:-k]
    var_kbar = float(np.var(ret_kbar))
    if var_kbar < 1e-12:
        return 0.5

    vr = var_kbar / (k * var_1bar)
    # Map VR to Hurst-like [0, 1]: VR=1 → 0.5, VR<1 → <0.5, VR>1 → >0.5
    hurst_est = 0.5 + 0.5 * (vr - 1.0) / (abs(vr - 1.0) + 1.0)
    return float(max(0.01, min(0.99, hurst_est)))


def simulate_ou_trade(
    closes: np.ndarray,
    entry_bar: int,
    direction: int,
    z_entry: float = Z_ENTRY,
    z_exit: float = Z_EXIT,
    max_hold: int = MAX_HOLD_BARS,
) -> tuple[float, int, str]:
    """Simulate one OU trade from entry_bar.

    direction: 1=long, -1=short (short when z_score > z_entry, long when z_score < -z_entry)
    Returns (pnl_r, bars_held, exit_reason).
    """
    entry_price = closes[entry_bar]
    entry_atr = 0.0
    # Compute ATR at entry
    if entry_bar >= 14:
        tr_vals = []
        for j in range(entry_bar - 13, entry_bar + 1):
            if j > 0:
                tr = max(
                    closes[j] - closes[j - 1],
                    abs(closes[j] - closes[j - 1]),
                )
                tr_vals.append(abs(tr) if j > 0 else 0.0)
        entry_atr = (
            float(np.mean(tr_vals[1:])) if len(tr_vals) > 1 else abs(closes[entry_bar] * 0.001)
        )

    if entry_atr < 1e-10:
        entry_atr = abs(entry_price) * 0.001

    end = min(entry_bar + max_hold, len(closes) - 1)

    for j in range(entry_bar + 1, end + 1):
        z = compute_z_score(closes, j)
        current_price = closes[j]

        if direction == 1:  # long (z < -z_entry → long)
            pnl_r = (current_price - entry_price) / entry_atr
        else:  # short (z > z_entry → short)
            pnl_r = (entry_price - current_price) / entry_atr

        # Z-score zero-cross exit
        if abs(z) < z_exit:
            return pnl_r, j - entry_bar, f"z_cross_{abs(z):.2f}"

    # Max hold reached
    final_price = closes[end]
    if direction == 1:
        pnl_r = (final_price - entry_price) / entry_atr
    else:
        pnl_r = (entry_price - final_price) / entry_atr
    return pnl_r, end - entry_bar, f"max_hold_{end - entry_bar}"


def run_backtest(ohlc: dict[str, np.ndarray]) -> dict:
    """Run the full regime 2D backtest."""
    closes = ohlc["close"]
    n = len(closes)

    # ── Pre-compute features ──
    print("  Computing Z-scores...")
    z_scores = np.array([compute_z_score(closes, i) for i in range(n)], dtype=np.float64)

    print("  Computing 12-bar RV and RV percentiles...")
    rv_values = np.zeros(n, dtype=np.float64)
    for i in range(12, n):
        window = closes[i - 11 : i + 1]
        log_rets = np.log(window[1:] / window[:-1])
        rv_values[i] = float(np.std(log_rets))

    rv_pcts = np.array([compute_rv_percentile(rv_values, i) for i in range(n)], dtype=np.float64)

    print("  Computing H1 Hurst (Variance Ratio)...")
    h1_closes = resample_to_h1(closes)
    h1_hursts = np.zeros(len(h1_closes), dtype=np.float64)
    for i in range(60, len(h1_closes)):
        h1_hursts[i] = compute_hurst_vr(h1_closes, i, window=60, k=6)

    # Map H1 Hurst back to M5 bars
    h1_hurst_m5 = np.zeros(n, dtype=np.float64)
    for i in range(n):
        h1_idx = i // 12
        if h1_idx < len(h1_hursts):
            h1_hurst_m5[i] = h1_hursts[h1_idx]

    # ── Schmitt trigger state machine ──
    force_off = False
    cooldown = 0
    schmitt_states = np.zeros(n, dtype=bool)  # True = FORCE-OFF

    for i in range(n):
        if rv_pcts[i] >= 0.95:
            force_off = True
            cooldown = 0
        elif force_off:
            if rv_pcts[i] < 0.80:
                cooldown += 1
                if cooldown >= 3:
                    force_off = False
                    cooldown = 0
            else:
                cooldown = 0
        schmitt_states[i] = force_off

    # ── Run three strategies ──
    strategies: dict[str, dict[str, Any]] = {
        "no_filter": {"trades": [], "pnls": [], "wins": 0, "total": 0, "equity": [0.0]},
        "hurst_only": {"trades": [], "pnls": [], "wins": 0, "total": 0, "equity": [0.0]},
        "regime_2d": {"trades": [], "pnls": [], "wins": 0, "total": 0, "equity": [0.0]},
    }

    warmup = max(Z_LOOKBACK, MIN_BARS_FOR_RV, 500)
    print(f"  Running backtest from bar {warmup} to {n}...")

    for i in range(warmup, n - MAX_HOLD_BARS):
        z = z_scores[i]
        if abs(z) < Z_ENTRY:
            continue  # no signal

        direction = -1 if z > Z_ENTRY else 1  # short when z > +entry, long when z < -entry
        h1_h = h1_hurst_m5[i]
        rv_p = rv_pcts[i]
        force_off_i = schmitt_states[i]

        # Determine regime zones
        if h1_h < 0.4:
            hurst_zone = "ranging"
            hurst_factor = 1.0
        elif h1_h > 0.6:
            hurst_zone = "trending"
            hurst_factor = 0.0
        else:
            hurst_zone = "mild"
            hurst_factor = 0.5

        if force_off_i:
            regime_factor = 0.0
        elif rv_p >= 0.95:
            regime_factor = 0.0
        elif rv_p >= 0.80:
            regime_factor = hurst_factor * 0.5 if hurst_zone == "ranging" else 0.0
        else:
            regime_factor = hurst_factor

        # Simulate trade outcome
        pnl_r, bars_held, exit_reason = simulate_ou_trade(closes, i, direction)

        # ── No filter: always trade ──
        strategies["no_filter"]["trades"].append(
            {
                "bar": i,
                "direction": direction,
                "pnl_r": round(pnl_r, 4),
                "bars_held": bars_held,
                "exit": exit_reason,
                "hurst_zone": hurst_zone,
                "rv_pct": round(rv_p, 3),
            }
        )
        strategies["no_filter"]["pnls"].append(pnl_r)
        strategies["no_filter"]["total"] += 1
        if pnl_r > 0:
            strategies["no_filter"]["wins"] += 1

        # ── Hurst only: trade if ranging or mild ──
        if hurst_factor > 0:
            strategies["hurst_only"]["trades"].append(
                {
                    "bar": i,
                    "direction": direction,
                    "pnl_r": round(pnl_r, 4),
                    "bars_held": bars_held,
                    "exit": exit_reason,
                    "hurst_zone": hurst_zone,
                    "rv_pct": round(rv_p, 3),
                }
            )
            strategies["hurst_only"]["pnls"].append(pnl_r)
            strategies["hurst_only"]["total"] += 1
            if pnl_r > 0:
                strategies["hurst_only"]["wins"] += 1

        # ── 2D regime: trade only if regime_factor > 0 ──
        if regime_factor > 0:
            strategies["regime_2d"]["trades"].append(
                {
                    "bar": i,
                    "direction": direction,
                    "pnl_r": round(pnl_r, 4),
                    "bars_held": bars_held,
                    "exit": exit_reason,
                    "hurst_zone": hurst_zone,
                    "rv_pct": round(rv_p, 3),
                    "regime_factor": regime_factor,
                    "force_off": force_off_i,
                }
            )
            strategies["regime_2d"]["pnls"].append(pnl_r)
            strategies["regime_2d"]["total"] += 1
            if pnl_r > 0:
                strategies["regime_2d"]["wins"] += 1

    # ── Compute equity curves ──
    for name in strategies:
        eq = [0.0]
        for pnl in strategies[name]["pnls"]:
            eq.append(eq[-1] + pnl)
        strategies[name]["equity"] = eq

    # ── Compute metrics ──
    results = {}
    for name in strategies:
        s = strategies[name]
        pnls_arr = np.array(s["pnls"]) if s["pnls"] else np.array([0.0])
        wr = s["wins"] / max(s["total"], 1)
        avg_pnl = float(np.mean(pnls_arr))
        std_pnl = float(np.std(pnls_arr)) if len(pnls_arr) > 1 else 0.0
        sharpe = avg_pnl / std_pnl if std_pnl > 0 else 0.0
        total_r = float(np.sum(pnls_arr))

        # Max drawdown
        eq_arr = np.array(s["equity"])
        peak = np.maximum.accumulate(eq_arr)
        dd = eq_arr - peak
        max_dd = float(np.min(dd))

        # Sortino
        neg = pnls_arr[pnls_arr < 0]
        down_std = float(np.std(neg)) if len(neg) > 1 else std_pnl
        sortino = avg_pnl / down_std if down_std > 0 else 0.0

        results[name] = {
            "n_trades": s["total"],
            "win_rate": round(wr, 4),
            "avg_pnl_r": round(avg_pnl, 6),
            "std_pnl_r": round(std_pnl, 6),
            "total_r": round(total_r, 4),
            "sharpe": round(sharpe, 4),
            "sortino": round(sortino, 4),
            "max_drawdown_r": round(max_dd, 4),
            "profit_factor": round(
                float(np.sum(pnls_arr[pnls_arr > 0]))
                / max(abs(float(np.sum(pnls_arr[pnls_arr < 0]))), 0.001),
                4,
            )
            if np.any(pnls_arr < 0)
            else 999.0,
        }

    # ── Per-regime breakdown (2D only) ──
    regime_breakdown: dict[str, dict[str, Any]] = {}
    for t in strategies["no_filter"]["trades"]:
        key = f"{t['hurst_zone']}_{t['rv_pct']:.0f}"
        if key not in regime_breakdown:
            regime_breakdown[key] = {"pnls": [], "wins": 0, "total": 0}
        regime_breakdown[key]["pnls"].append(t["pnl_r"])
        regime_breakdown[key]["total"] += 1
        if t["pnl_r"] > 0:
            regime_breakdown[key]["wins"] += 1

    regime_summary = {}
    for key, data in sorted(regime_breakdown.items()):
        p = np.array(data["pnls"])
        regime_summary[key] = {
            "n": data["total"],
            "win_rate": round(data["wins"] / max(data["total"], 1), 4),
            "avg_pnl_r": round(float(np.mean(p)), 6),
            "total_r": round(float(np.sum(p)), 4),
        }

    return {
        "strategies": results,
        "regime_breakdown": regime_summary,
        "config": {
            "z_entry": Z_ENTRY,
            "z_exit": Z_EXIT,
            "max_hold": MAX_HOLD_BARS,
            "z_lookback": Z_LOOKBACK,
            "min_bars_rv": MIN_BARS_FOR_RV,
        },
        "schmitt_events": {
            "force_off_periods": int(np.sum(schmitt_states)),
            "force_off_pct": round(float(np.mean(schmitt_states)), 4),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backtest_regime_2d")
    p.add_argument("--price-data", type=Path, required=True, help="Path to M5 OHLC CSV")
    p.add_argument("--output", type=Path, default=None, help="Output directory for results")
    p.add_argument("--z-entry", type=float, default=Z_ENTRY, help="Z-score entry threshold")
    p.add_argument("--z-exit", type=float, default=Z_EXIT, help="Z-score exit threshold")
    p.add_argument("--max-hold", type=int, default=MAX_HOLD_BARS, help="Max hold bars")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    data_path = Path(args.price_data)
    if not data_path.exists():
        print(f"[ERROR] Price data not found: {data_path}")
        return 1

    print(f"[1/2] Loading: {data_path}")
    ohlc = load_ohlc(data_path)
    print(f"       {ohlc['n_bars']} M5 bars")

    print("[2/2] Running 2D regime backtest...")
    results = run_backtest(ohlc)

    # ── Print results ──
    print()
    print("=" * 70)
    print("  Strategy Comparison")
    print("=" * 70)
    headers = [
        "Strategy",
        "Trades",
        "Win Rate",
        "Avg PnL(R)",
        "Total R",
        "Sharpe",
        "Sortino",
        "Max DD(R)",
    ]
    fmt = "  {:<20} {:>7} {:>10} {:>12} {:>10} {:>8} {:>8} {:>12}"
    print(fmt.format(*headers))
    print("  " + "-" * 68)
    for name in ["no_filter", "hurst_only", "regime_2d"]:
        r = results["strategies"][name]
        print(
            fmt.format(
                name,
                r["n_trades"],
                f"{r['win_rate']:.1%}",
                f"{r['avg_pnl_r']:.4f}",
                f"{r['total_r']:.2f}",
                f"{r['sharpe']:.2f}",
                f"{r['sortino']:.2f}",
                f"{r['max_drawdown_r']:.2f}",
            )
        )

    print()
    print("=" * 70)
    print("  Regime Breakdown (all trades)")
    print("=" * 70)
    print(
        "  {:<25} {:>6} {:>10} {:>12} {:>10}".format(
            "Regime", "N", "Win Rate", "Avg PnL(R)", "Total R"
        )
    )
    print("  " + "-" * 65)
    for key, data in results["regime_breakdown"].items():
        print(
            "  {:<25} {:>6} {:>10} {:>12} {:>10}".format(
                key,
                data["n"],
                f"{data['win_rate']:.1%}",
                f"{data['avg_pnl_r']:.4f}",
                f"{data['total_r']:.2f}",
            )
        )

    print()
    print(
        f"  Schmitt: {results['schmitt_events']['force_off_periods']} bars FORCE-OFF "
        f"({results['schmitt_events']['force_off_pct']:.1%})"
    )

    # ── Save ──
    if args.output:
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"regime_2d_{ts}.json"
        results["timestamp"] = ts
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False, default=str)
        print(f"\n  Results saved to: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
