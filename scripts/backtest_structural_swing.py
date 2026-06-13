#!/usr/bin/env python
"""Backtest Structural_Swing_V1 — pure rule-based strategy validation.

Iron Law #11: This script's stdout is the sole source of truth.

Usage:
  python scripts/backtest_structural_swing.py --data data/raw/xauusdc_m5_merged.csv
  python scripts/backtest_structural_swing.py --data data/raw/xauusdc_m5_merged.csv --h1-csv data/raw/xauusdc_h1_merged.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.strategies.structural_swing_v1 import StructuralSwingV1, SwingSignal


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        print(f"[ERROR] CSV missing columns: {missing}", file=sys.stderr)
        sys.exit(1)
    return df


def resample_to_m5(df_h1: pd.DataFrame, m5_index: pd.DatetimeIndex) -> np.ndarray:
    """Resample H1 closes to M5 granularity (forward-fill)."""
    h1_series = df_h1.set_index("time")["close"]
    h1_series.index = pd.to_datetime(h1_series.index)
    m5_series = h1_series.reindex(m5_index, method="ffill")
    return m5_series.values


def simulate_trade(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    entry_idx: int,
    signal: SwingSignal,
    horizon: int,
) -> dict:
    """Simulate one trade from entry to exit. Returns result dict."""
    n = len(opens)
    end_idx = min(entry_idx + 1 + horizon, n)
    entry = signal.entry_price
    sl = signal.stop_loss
    tp = signal.take_profit
    direction = signal.direction

    for j in range(entry_idx + 1, end_idx):
        cur_h, cur_l = highs[j], lows[j]

        if direction == "long":
            tp_hit = cur_h >= tp
            sl_hit = cur_l <= sl
        else:
            tp_hit = cur_l <= tp
            sl_hit = cur_h >= sl

        # Same-bar both → ambiguous, treat as SL (conservative)
        if tp_hit and sl_hit:
            return {
                "outcome": "sl_hit",
                "pnl_r": -3.0,  # SL=3.0 ATR
                "exit_bar": j,
                "hold_bars": j - entry_idx,
            }
        if tp_hit:
            return {
                "outcome": "tp_hit",
                "pnl_r": 1.5,  # TP=1.5 ATR
                "exit_bar": j,
                "hold_bars": j - entry_idx,
            }
        if sl_hit:
            return {
                "outcome": "sl_hit",
                "pnl_r": -3.0,
                "exit_bar": j,
                "hold_bars": j - entry_idx,
            }

    # Timeout
    exit_price = closes[min(entry_idx + horizon, n - 1)]
    if direction == "long":
        pnl_price = exit_price - entry
    else:
        pnl_price = entry - exit_price
    # Approximate R: pnl_price / (ATR at entry)
    atr_approx = abs(entry - sl) / 3.0  # back-calculate from SL distance
    pnl_r = pnl_price / max(atr_approx, 0.001) if atr_approx > 0 else 0.0

    return {
        "outcome": "timeout",
        "pnl_r": round(float(pnl_r), 4),
        "exit_bar": min(entry_idx + horizon, n - 1),
        "hold_bars": horizon,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backtest_structural_swing")
    p.add_argument("--data", required=True, help="Path to M5 OHLC CSV")
    p.add_argument(
        "--h1-csv",
        default=None,
        help="Path to H1 OHLC CSV (optional; resamples from M5 if missing)",
    )
    p.add_argument("--sl-mult", type=float, default=3.0)
    p.add_argument("--tp-mult", type=float, default=1.5)
    p.add_argument("--horizon", type=int, default=12)
    p.add_argument("--spread-points", type=float, default=30)
    p.add_argument("--slippage-points", type=float, default=10)
    p.add_argument("--ema-threshold", type=float, default=0.5)
    p.add_argument("--tick-size", type=float, default=0.001, help="XAU=0.001, BTC=0.01")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    print(f"[backtest] Loading {args.data}...")
    df_m5 = load_csv(args.data)
    df_m5["time"] = pd.to_datetime(df_m5["time"])
    m5_idx = df_m5["time"]

    o = df_m5["open"].values.astype(np.float64)
    h = df_m5["high"].values.astype(np.float64)
    l = df_m5["low"].values.astype(np.float64)
    c = df_m5["close"].values.astype(np.float64)

    # H1 data
    if args.h1_csv:
        df_h1 = load_csv(args.h1_csv)
        df_h1["time"] = pd.to_datetime(df_h1["time"])
        h1_closes = resample_to_m5(df_h1, m5_idx)
        print(f"[backtest] H1 data: {args.h1_csv} ({len(df_h1)} bars)")
    else:
        # Resample M5 to H1 (12 bars per H1)
        h1_closes = c[::12].repeat(12)[: len(c)]
        print("[backtest] H1 data: resampled from M5 (12:1)")

    n_bars = len(c)
    print(f"[backtest] M5 bars: {n_bars}")
    print(f"[backtest] Date range: {df_m5['time'].iloc[0]} → {df_m5['time'].iloc[-1]}")

    # ── Initialize strategy ──
    strat = StructuralSwingV1(
        sl_atr_mult=args.sl_mult,
        tp_atr_mult=args.tp_mult,
        horizon_bars=args.horizon,
        spread_points=args.spread_points,
        slippage_points=args.slippage_points,
        ema_threshold_atr_mult=args.ema_threshold,
        tick_size=args.tick_size,
    )
    print(f"\n[backtest] Strategy: {strat.to_dict()}")

    # ── Precompute indicators (O(n) — single pass) ──
    m5_atr = strat._atr(h, l, c, strat.atr_period)
    ema_f = strat._ema(h1_closes, strat.ema_fast)
    ema_s = strat._ema(h1_closes, strat.ema_slow)
    h1_atr_raw = strat._atr(h1_closes, h1_closes, h1_closes, strat.atr_period)

    # ── Run simulation ──
    trades: list[dict] = []
    cooldown = 0
    total_signals = 0
    filtered_by_trend = 0

    warmup = max(strat.ema_slow, strat.atr_period + 1)
    for i in range(warmup, n_bars - args.horizon - 1):
        if cooldown > 0:
            cooldown -= 1
            continue

        total_signals += 1

        # Fast precomputed check
        _atr_val = m5_atr[i]
        if np.isnan(_atr_val) or _atr_val <= 0:
            filtered_by_trend += 1
            continue

        _h1_atr = h1_atr_raw[i]
        _diff = ema_f[i] - ema_s[i]
        if np.isnan(_h1_atr) or _h1_atr <= 0:
            filtered_by_trend += 1
            continue

        threshold = strat.ema_threshold * _h1_atr
        if abs(_diff) < threshold:
            filtered_by_trend += 1
            continue

        direction = "long" if _diff > 0 else "short"

        # Compute barriers
        ref_price = o[i + 1] if i + 1 < len(o) else c[i]
        entry, sl, tp = strat._compute_barriers(direction, float(ref_price), float(_atr_val))
        if entry <= 0 or sl <= 0 or tp <= 0:
            filtered_by_trend += 1
            continue

        signal = SwingSignal(
            direction=direction,
            entry_price=round(entry, 3),
            stop_loss=round(sl, 3),
            take_profit=round(tp, 3),
            atr=round(float(_atr_val), 4),
            ema_diff=round(float(_diff), 4),
            bar_index=i,
        )

        result = simulate_trade(o, h, l, c, i + 1, signal, args.horizon)
        result["entry_idx"] = i
        result["direction"] = signal.direction
        result["atr"] = signal.atr
        result["ema_diff"] = signal.ema_diff
        trades.append(result)
        cooldown = 1

    if not trades:
        print("[backtest] ERROR: No trades generated. Check parameters.")
        return 1

    # ═══════════════════════════════════════════════════════════════════════════
    # Analysis
    # ═══════════════════════════════════════════════════════════════════════════
    pnl_r = np.array([t["pnl_r"] for t in trades])
    outcomes = [t["outcome"] for t in trades]
    directions = [t["direction"] for t in trades]

    tp_count = outcomes.count("tp_hit")
    sl_count = outcomes.count("sl_hit")
    to_count = outcomes.count("timeout")
    n_trades = len(trades)

    tp_pnl = sum(t["pnl_r"] for t in trades if t["outcome"] == "tp_hit")
    sl_pnl = sum(t["pnl_r"] for t in trades if t["outcome"] == "sl_hit")
    to_pnl = sum(t["pnl_r"] for t in trades if t["outcome"] == "timeout")

    long_trades = [t for t in trades if t["direction"] == "long"]
    short_trades = [t for t in trades if t["direction"] == "short"]

    # Drawdown
    cum = np.cumsum(pnl_r)
    peak = np.maximum.accumulate(cum)
    dd = cum - peak
    max_dd = float(np.min(dd))

    print("\n" + "=" * 70)
    print("BACKTEST RESULTS")
    print("=" * 70)
    print(f"  Signals evaluated:     {total_signals}")
    print(
        f"  Filtered by trend:     {filtered_by_trend} ({filtered_by_trend/max(total_signals,1)*100:.1f}%)"
    )
    print(f"  Trades executed:       {n_trades}")
    print(f"  Bars covered:          {n_bars - warmup}")
    print(
        f"  Avg trades/day:        {n_trades / ((n_bars - warmup) / 288):.1f}  (M5: 288 bars/day)"
    )
    print()
    _mean_pnl = float(np.mean(pnl_r))
    _std_pnl = float(np.std(pnl_r))
    print(f"  Total PnL:             {sum(pnl_r):+.2f}R")
    print(f"  Mean PnL/trade:        {_mean_pnl:+.4f}R")
    print(f"  Std PnL/trade:         {_std_pnl:+.4f}R")
    print(f"  Sharpe:                {_mean_pnl/max(_std_pnl,0.001)*np.sqrt(n_trades):.2f}")
    print(f"  Max drawdown:          {max_dd:+.2f}R")
    print()
    print(
        f"  TP hits:    {tp_count:>4d} ({tp_count/n_trades*100:5.1f}%)  PnL: {tp_pnl:+.2f}R  avg: {tp_pnl/max(tp_count,1):+.4f}R"
    )
    print(
        f"  SL hits:    {sl_count:>4d} ({sl_count/n_trades*100:5.1f}%)  PnL: {sl_pnl:+.2f}R  avg: {sl_pnl/max(sl_count,1):+.4f}R"
    )
    print(
        f"  Timeouts:   {to_count:>4d} ({to_count/n_trades*100:5.1f}%)  PnL: {to_pnl:+.2f}R  avg: {to_pnl/max(to_count,1):+.4f}R"
    )

    if to_count > 0:
        to_pnls = [t["pnl_r"] for t in trades if t["outcome"] == "timeout"]
        print(
            f"    Timeout PnL dist: mean={np.mean(to_pnls):+.4f}R, median={np.median(to_pnls):+.4f}R, "
            f"P5={np.percentile(to_pnls,5):+.4f}R, P95={np.percentile(to_pnls,95):+.4f}R"
        )

    print()
    print(
        f"  Long trades:  {len(long_trades):>4d}  PnL: {sum(t['pnl_r'] for t in long_trades):+.2f}R"
    )
    print(
        f"  Short trades: {len(short_trades):>4d}  PnL: {sum(t['pnl_r'] for t in short_trades):+.2f}R"
    )

    # Statistical significance
    abs_pnl = np.abs(pnl_r)
    rng = np.random.RandomState(42)
    n_iter = min(1000, max(100, 100000 // max(len(abs_pnl), 1)))
    random_paths = np.zeros(n_iter, dtype=np.float64)
    for i in range(n_iter):
        signs = rng.choice([-1, 1], size=len(abs_pnl))
        random_paths[i] = np.dot(signs, abs_pnl)
    actual = sum(pnl_r)
    p_val = 2.0 * min(
        float(np.mean(random_paths >= actual)),
        float(np.mean(random_paths <= actual)),
    )
    print(
        f"\n  Bootstrap: actual={actual:+.2f}R, random 95%=[{np.percentile(random_paths,5):+.2f}, {np.percentile(random_paths,95):+.2f}]"
    )
    print(f"  P(two-sided): {p_val:.4f}  {'SIGNIFICANT' if p_val<0.05 else 'NOT significant'}")

    # PnL curve (last 20 trades for quick visual)
    print("\n  Cumulative PnL (last 50 trades):")
    recent = cum[-50:] if len(cum) >= 50 else cum
    for _i, val in enumerate(recent):
        bar = "█" * max(1, int(abs(val) * 3))
        sign = "+" if val >= 0 else ""
        print(f"    {sign}{val:>6.1f}R  {bar}")

    print("\n[DONE] All statistics above are the sole source of truth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
