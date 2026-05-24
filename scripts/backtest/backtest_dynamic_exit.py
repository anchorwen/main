#!/usr/bin/env python
"""Backtest PnL-aware dynamic Z-score exit vs fixed TP/SL exit.

Compares three exit strategies on the same OU Z-score entry signals:
  1. Fixed TP/SL: TP=1.5 ATR, SL=3.0 ATR (baseline)
  2. Pure Z-score exit: exit when |z| < 0.3 (no PnL awareness)
  3. PnL-aware Z-score exit + toxic flow stop + time deadline

Usage:
  python scripts/backtest/backtest_dynamic_exit.py \
    --price-data data/raw/xauusdc_m5_1y.csv \
    --output data/backtest/dynamic_exit/
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

# ── Shared helpers (duplicated to keep scripts self-contained) ──


def load_ohlc(path):
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


def compute_z_score(closes, i, lookback=20):
    if i < lookback:
        return 0.0
    window = closes[i - lookback : i]
    mean = float(np.mean(window))
    std = float(np.std(window))
    if std < 1e-10:
        return 0.0
    return float((closes[i] - mean) / std)


# ── Constants ──

Z_LOOKBACK = 20
Z_ENTRY = 1.5
Z_EXIT = 0.3
MAX_HOLD = 8
SL_ATR = 3.0
TP_ATR = 1.5
MIN_BARS_FOR_RV = 500


def compute_atr(closes: np.ndarray, i: int, period: int = 14) -> float:
    """Compute ATR at bar i."""
    if i < period:
        return abs(closes[i]) * 0.001
    trs = []
    for j in range(i - period + 1, i + 1):
        if j > 0:
            trs.append(abs(closes[j] - closes[j - 1]))
    return float(np.mean(trs)) if trs else abs(closes[i]) * 0.001


# ── Exit strategy simulators ──


def simulate_fixed_tpsl(
    closes: np.ndarray,
    entry_bar: int,
    direction: int,
    entry_atr: float,
    sl_atr: float = SL_ATR,
    tp_atr: float = TP_ATR,
    max_hold: int = MAX_HOLD,
) -> tuple[float, int, str]:
    """Simulate trade with fixed TP/SL barrier."""
    entry_price = closes[entry_bar]
    if direction == 1:  # long
        tp = entry_price + tp_atr * entry_atr
        sl = entry_price - sl_atr * entry_atr
    else:  # short
        tp = entry_price - tp_atr * entry_atr
        sl = entry_price + sl_atr * entry_atr

    end = min(entry_bar + max_hold, len(closes) - 1)
    for j in range(entry_bar + 1, end + 1):
        high = closes[j]
        low = closes[j]
        if direction == 1:
            if high >= tp:
                return tp_atr, j - entry_bar, "tp_hit"
            if low <= sl:
                return -sl_atr, j - entry_bar, "sl_hit"
        else:
            if low <= tp:
                return tp_atr, j - entry_bar, "tp_hit"
            if high >= sl:
                return -sl_atr, j - entry_bar, "sl_hit"

    final = closes[end]
    if direction == 1:
        pnl = (final - entry_price) / entry_atr
    else:
        pnl = (entry_price - final) / entry_atr
    return pnl, end - entry_bar, f"timeout_{end - entry_bar}"


def simulate_pure_z_exit(
    closes: np.ndarray,
    entry_bar: int,
    direction: int,
    entry_atr: float,
    z_exit: float = Z_EXIT,
    max_hold: int = MAX_HOLD,
    z_lookback: int = Z_LOOKBACK,
) -> tuple[float, int, str]:
    """Pure Z-score exit: exit when |z| < z_exit (no PnL awareness)."""
    entry_price = closes[entry_bar]
    end = min(entry_bar + max_hold, len(closes) - 1)

    for j in range(entry_bar + 1, end + 1):
        z = compute_z_score(closes, j, z_lookback)
        if abs(z) < z_exit:
            if direction == 1:
                pnl = (closes[j] - entry_price) / entry_atr
            else:
                pnl = (entry_price - closes[j]) / entry_atr
            return pnl, j - entry_bar, f"z_cross_{abs(z):.2f}"

    final = closes[end]
    if direction == 1:
        pnl = (final - entry_price) / entry_atr
    else:
        pnl = (entry_price - final) / entry_atr
    return pnl, end - entry_bar, f"timeout_{end - entry_bar}"


def simulate_pnl_aware_z_exit(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    opens: np.ndarray,
    entry_bar: int,
    direction: int,
    entry_atr: float,
    z_exit: float = Z_EXIT,
    max_hold: int = MAX_HOLD,
    z_lookback: int = Z_LOOKBACK,
) -> tuple[float, int, str]:
    """PnL-aware Z-score exit with toxic flow stop.

    Decision tree:
      1. |z| < z_exit AND PnL > 0 → exit (profitable reversion)
      2. |z| < z_exit AND PnL < 0 → mean drift trap, DON'T exit
      3. bars ≥ 6 → check toxic flow (2-bar M5 engulfing)
      4. bars ≥ max_hold → hard deadline
      5. PnL ≤ -2.0 ATR → hard stop
    """
    entry_price = closes[entry_bar]
    end = min(entry_bar + max_hold, len(closes) - 1)

    for j in range(entry_bar + 1, end + 1):
        z = compute_z_score(closes, j, z_lookback)
        bars_in = j - entry_bar

        if direction == 1:
            pnl = (closes[j] - entry_price) / entry_atr
        else:
            pnl = (entry_price - closes[j]) / entry_atr

        # Hard stop
        if pnl <= -2.0:
            return pnl, bars_in, f"hard_stop_r{pnl:.2f}"

        # Profitable reversion
        if abs(z) < z_exit:
            if pnl > 0:
                return pnl, bars_in, f"profit_revert_z{abs(z):.2f}_r{pnl:.2f}"
            # Mean drift trap — DON'T exit, continue

        # Toxic flow check (bars 6+)
        if bars_in >= 6 and bars_in < max_hold:
            side = "long" if direction == 1 else "short"
            if _detect_toxic_flow_m5(opens, highs, lows, closes, j, side, entry_atr):
                return pnl, bars_in, f"toxic_flow_bar{bars_in}_r{pnl:.2f}"

    final = closes[end]
    if direction == 1:
        pnl = (final - entry_price) / entry_atr
    else:
        pnl = (entry_price - final) / entry_atr
    bars_held = end - entry_bar
    return pnl, bars_held, f"deadline_bar{bars_held}_r{pnl:.2f}"


def _detect_toxic_flow_m5(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    bar_idx: int,
    side: str,
    atr: float,
) -> bool:
    """Detect toxic flow from last 2 M5 bars.

    Short: 2 consecutive bullish engulfing bars (body > 0.3 × ATR)
    Long: 2 consecutive bearish engulfing bars
    """
    if bar_idx < 2:
        return False

    body_threshold = 0.3 * atr
    b0_open, b0_close, b0_high, b0_low = (
        opens[bar_idx - 1],
        closes[bar_idx - 1],
        highs[bar_idx - 1],
        lows[bar_idx - 1],
    )
    b1_open, b1_close, b1_high, b1_low = (
        opens[bar_idx],
        closes[bar_idx],
        highs[bar_idx],
        lows[bar_idx],
    )

    body0 = abs(b0_close - b0_open)
    body1 = abs(b1_close - b1_open)

    if side == "short":
        # Bullish engulfing: both close > open, bodies above threshold
        if b0_close <= b0_open or body0 < body_threshold:
            return False
        if b1_close <= b1_open or body1 < body_threshold:
            return False
        # Second bar engulfs first
        if b1_high > b0_high and b1_low < b0_low:
            return True
    elif side == "long":
        # Bearish engulfing
        if b0_close >= b0_open or body0 < body_threshold:
            return False
        if b1_close >= b1_open or body1 < body_threshold:
            return False
        if b1_high > b0_high and b1_low < b0_low:
            return True

    return False


# ── Backtest ──


def run_backtest(ohlc: dict[str, np.ndarray]) -> dict:
    """Run full exit strategy comparison backtest."""
    closes = ohlc["close"]
    highs = ohlc["high"]
    lows = ohlc["low"]
    opens = ohlc["open"]
    n = len(closes)

    # Pre-compute Z-scores
    print("  Computing Z-scores...")
    z_scores = np.array([compute_z_score(closes, i) for i in range(n)], dtype=np.float64)

    # Pre-compute ATRs
    print("  Computing ATRs...")
    atrs = np.array([compute_atr(closes, i) for i in range(n)], dtype=np.float64)

    strategies: dict[str, dict] = {
        "fixed_tpsl": {
            "trades": [],
            "pnls": [],
            "wins": 0,
            "total": 0,
            "equity": [0.0],
            "mean_drifts": [],
        },
        "pure_z_exit": {
            "trades": [],
            "pnls": [],
            "wins": 0,
            "total": 0,
            "equity": [0.0],
            "mean_drifts": [],
        },
        "pnl_aware_z": {
            "trades": [],
            "pnls": [],
            "wins": 0,
            "total": 0,
            "equity": [0.0],
            "mean_drifts": [],
        },
    }

    warmup = max(Z_LOOKBACK, 500)
    print(f"  Running backtest from bar {warmup} to {n}...")

    for i in range(warmup, n - MAX_HOLD):
        z = z_scores[i]
        if abs(z) < Z_ENTRY:
            continue

        direction = -1 if z > Z_ENTRY else 1
        entry_atr = atrs[i]

        # Strategy 1: Fixed TP/SL
        pnl1, bars1, reason1 = simulate_fixed_tpsl(closes, i, direction, entry_atr)
        strategies["fixed_tpsl"]["trades"].append(
            {
                "bar": i,
                "direction": direction,
                "pnl_r": round(pnl1, 4),
                "bars": bars1,
                "reason": reason1,
            }
        )
        strategies["fixed_tpsl"]["pnls"].append(pnl1)
        strategies["fixed_tpsl"]["total"] += 1
        if pnl1 > 0:
            strategies["fixed_tpsl"]["wins"] += 1

        # Strategy 2: Pure Z-score exit
        pnl2, bars2, reason2 = simulate_pure_z_exit(closes, i, direction, entry_atr)
        strategies["pure_z_exit"]["trades"].append(
            {
                "bar": i,
                "direction": direction,
                "pnl_r": round(pnl2, 4),
                "bars": bars2,
                "reason": reason2,
            }
        )
        strategies["pure_z_exit"]["pnls"].append(pnl2)
        strategies["pure_z_exit"]["total"] += 1
        if pnl2 > 0:
            strategies["pure_z_exit"]["wins"] += 1

        # Strategy 3: PnL-aware Z-score exit
        pnl3, bars3, reason3 = simulate_pnl_aware_z_exit(
            closes, highs, lows, opens, i, direction, entry_atr
        )
        strategies["pnl_aware_z"]["trades"].append(
            {
                "bar": i,
                "direction": direction,
                "pnl_r": round(pnl3, 4),
                "bars": bars3,
                "reason": reason3,
            }
        )
        strategies["pnl_aware_z"]["pnls"].append(pnl3)
        strategies["pnl_aware_z"]["total"] += 1
        if pnl3 > 0:
            strategies["pnl_aware_z"]["wins"] += 1
        # Track mean drift traps
        if "profit_revert" not in reason3 and "z_cross" not in reason3 and pnl3 < 0:
            # Check if z crossed but PnL was negative
            for j in range(i + 1, min(i + MAX_HOLD, n)):
                if abs(compute_z_score(closes, j)) < Z_EXIT:
                    if direction == 1:
                        pnl_at_cross = (closes[j] - closes[i]) / entry_atr
                    else:
                        pnl_at_cross = (closes[i] - closes[j]) / entry_atr
                    if pnl_at_cross < 0:
                        strategies["pnl_aware_z"]["mean_drifts"].append(
                            {
                                "bar": i,
                                "cross_bar": j,
                                "pnl_at_cross": round(pnl_at_cross, 4),
                                "final_pnl": round(pnl3, 4),
                                "final_reason": reason3,
                            }
                        )
                    break

    # ── Compute equity curves and metrics ──
    for name in strategies:
        eq = [0.0]
        for pnl in strategies[name]["pnls"]:
            eq.append(eq[-1] + pnl)
        strategies[name]["equity"] = eq

    results = {}
    for name in strategies:
        s = strategies[name]
        pnls_arr = np.array(s["pnls"]) if s["pnls"] else np.array([0.0])
        wr = s["wins"] / max(s["total"], 1)
        avg_pnl = float(np.mean(pnls_arr))
        std_pnl = float(np.std(pnls_arr)) if len(pnls_arr) > 1 else 0.0
        total_r = float(np.sum(pnls_arr))
        sharpe = avg_pnl / std_pnl if std_pnl > 0 else 0.0

        eq_arr = np.array(s["equity"])
        peak = np.maximum.accumulate(eq_arr)
        max_dd = float(np.min(eq_arr - peak))

        neg = pnls_arr[pnls_arr < 0]
        down_std = float(np.std(neg)) if len(neg) > 1 else std_pnl
        sortino = avg_pnl / down_std if down_std > 0 else 0.0

        # Avg profit / avg loss
        wins_arr = pnls_arr[pnls_arr > 0]
        losses_arr = pnls_arr[pnls_arr < 0]
        avg_win = float(np.mean(wins_arr)) if len(wins_arr) > 0 else 0.0
        avg_loss = float(np.mean(losses_arr)) if len(losses_arr) > 0 else 0.0

        results[name] = {
            "n_trades": s["total"],
            "win_rate": round(wr, 4),
            "avg_pnl_r": round(avg_pnl, 6),
            "std_pnl_r": round(std_pnl, 6),
            "total_r": round(total_r, 4),
            "sharpe": round(sharpe, 4),
            "sortino": round(sortino, 4),
            "max_drawdown_r": round(max_dd, 4),
            "avg_win_r": round(avg_win, 4),
            "avg_loss_r": round(avg_loss, 4),
            "profit_factor": round(
                float(np.sum(pnls_arr[pnls_arr > 0]))
                / max(abs(float(np.sum(pnls_arr[pnls_arr < 0]))), 0.001),
                4,
            )
            if np.any(pnls_arr < 0)
            else 999.0,
        }

        if name == "pnl_aware_z":
            results[name]["n_mean_drifts"] = len(s["mean_drifts"])

    # ── Exit reason breakdown (pnl_aware_z) ──
    exit_breakdown: dict[str, dict] = {}
    for t in strategies["pnl_aware_z"]["trades"]:
        reason_type = t["reason"].split("_")[0]
        if reason_type not in exit_breakdown:
            exit_breakdown[reason_type] = {"pnls": [], "wins": 0, "total": 0}
        exit_breakdown[reason_type]["pnls"].append(t["pnl_r"])
        exit_breakdown[reason_type]["total"] += 1
        if t["pnl_r"] > 0:
            exit_breakdown[reason_type]["wins"] += 1

    exit_summary: dict[str, dict] = {}
    for key, data in sorted(exit_breakdown.items()):
        p = np.array(data["pnls"])
        exit_summary[key] = {
            "n": data["total"],
            "pct": round(data["total"] / max(results["pnl_aware_z"]["n_trades"], 1), 4),
            "win_rate": round(data["wins"] / max(data["total"], 1), 4),
            "avg_pnl_r": round(float(np.mean(p)), 6),
            "total_r": round(float(np.sum(p)), 4),
        }

    return {
        "strategies": results,
        "exit_breakdown": exit_summary,
        "config": {
            "z_entry": Z_ENTRY,
            "z_exit": Z_EXIT,
            "max_hold": MAX_HOLD,
            "sl_atr": SL_ATR,
            "tp_atr": TP_ATR,
            "z_lookback": Z_LOOKBACK,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backtest_dynamic_exit")
    p.add_argument("--price-data", type=Path, required=True)
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--z-entry", type=float, default=Z_ENTRY)
    p.add_argument("--z-exit", type=float, default=Z_EXIT)
    p.add_argument("--max-hold", type=int, default=MAX_HOLD)
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

    print("[2/2] Running exit strategy comparison...")
    results = run_backtest(ohlc)

    print()
    print("=" * 80)
    print("  Exit Strategy Comparison")
    print("=" * 80)
    headers = [
        "Exit",
        "Trades",
        "Win Rate",
        "Avg PnL(R)",
        "Total R",
        "Avg Win",
        "Avg Loss",
        "Sharpe",
        "Sortino",
        "Max DD",
    ]
    fmt = "  {:<18} {:>6} {:>10} {:>12} {:>10} {:>9} {:>10} {:>8} {:>8} {:>8}"
    print(fmt.format(*headers))
    print("  " + "-" * 78)
    for name in ["fixed_tpsl", "pure_z_exit", "pnl_aware_z"]:
        r = results["strategies"][name]
        print(
            fmt.format(
                name,
                r["n_trades"],
                f"{r['win_rate']:.1%}",
                f"{r['avg_pnl_r']:.4f}",
                f"{r['total_r']:.2f}",
                f"{r['avg_win_r']:.3f}",
                f"{r['avg_loss_r']:.3f}",
                f"{r['sharpe']:.2f}",
                f"{r['sortino']:.2f}",
                f"{r['max_drawdown_r']:.1f}",
            )
        )

    print()
    print("=" * 80)
    print("  PnL-Aware Exit Breakdown")
    print("=" * 80)
    print(
        "  {:<20} {:>6} {:>8} {:>10} {:>12} {:>10}".format(
            "Exit Type", "N", "Pct", "Win Rate", "Avg PnL(R)", "Total R"
        )
    )
    print("  " + "-" * 68)
    for key, data in results["exit_breakdown"].items():
        print(
            "  {:<20} {:>6} {:>8} {:>10} {:>12} {:>10}".format(
                key,
                data["n"],
                f"{data['pct']:.1%}",
                f"{data['win_rate']:.1%}",
                f"{data['avg_pnl_r']:.4f}",
                f"{data['total_r']:.2f}",
            )
        )

    md_count = results["strategies"]["pnl_aware_z"].get("n_mean_drifts", 0)
    if md_count > 0:
        print(f"\n  Mean drift traps detected: {md_count}")

    if args.output:
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"dynamic_exit_{ts}.json"
        results["timestamp"] = ts
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False, default=str)
        print(f"\n  Results saved to: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
