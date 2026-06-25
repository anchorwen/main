"""
Phase 0: Base Strategy Edge Validation (Strategy Combine)
=========================================================
Evaluates _barrier_rule, _micro_rule, _statarb_rule under the v1.2.1
barrier contract (SL=2.0, TP=1.25, H=12) on historical XAUUSD M5 data.

Goal: Determine which strategies have positive edge (EV > 0) and
      sufficient signal frequency for downstream MetaFilter training.

Output:
    Terminal report sorted by EV(ATR) — direct comparison with blind-entry baseline.
    CSV saved to data/phase0_strategy_edge_report.csv

Usage:
    python scripts/backtest_rule_strategies.py
    python scripts/backtest_rule_strategies.py --data data/raw/xauusdc_m5_merged.csv
    python scripts/backtest_rule_strategies.py --sl 2.0 --tp 1.25 --horizon 12 --cooldown 6
"""

import argparse
import csv
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np

# ── Reuse existing infrastructure ──────────────────────────────────────────────
# Import from sibling scan script (same directory)
sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.scan_barrier_params import (
    ATR_PERIOD as DEFAULT_ATR_PERIOD,
)
from scripts.scan_barrier_params import (  # noqa: E402
    _resolve_barrier,
    compute_atr,
    load_ohlc,
)

# ── Strategy Rule Implementations (numpy-based) ────────────────────────────────
# Faithfully replicate the logic from core/backtest/strategy_adapter.py,
# adapted for numpy array inputs instead of Bar objects.


def barrier_signals(ohlc: dict[str, np.ndarray], atr: np.ndarray, warmup: int = 50) -> list[dict]:
    """Barrier Breakout: trend-following on resistance/support breaks.

    BUGFIX vs original _barrier_rule(): the original includes the current bar's
    high/low in the resistance/support window, making `close > max(highs[-12:])`
    impossible (close <= high always).  We exclude the current bar (lookback only)
    so `close > max(highs[-12:-1])` can actually trigger on genuine breakouts.

    Logic:
    - 20-bar MA of closes (lookback only, excludes current bar)
    - 12-bar resistance = max(highs[-12:-1])
    - 12-bar support = min(lows[-12:-1])
    - Long: close > resistance AND close > MA
    - Short: close < support AND close < MA
    """
    n = ohlc["n"]
    closes = ohlc["c"]
    highs = ohlc["h"]
    lows = ohlc["l"]

    signals: list[dict] = []
    for i in range(warmup, n - 1):
        if np.isnan(atr[i]) or atr[i] <= 0:
            continue

        # 20-bar MA (lookback only, excluding current bar)
        ma = float(np.mean(closes[i - 20 : i]))
        # 12-bar resistance/support (EXCLUDING current bar — close > high is impossible)
        resistance = float(np.max(highs[i - 12 : i]))
        support = float(np.min(lows[i - 12 : i]))
        close_i = float(closes[i])
        atr_i = float(atr[i])

        if close_i > resistance and close_i > ma:
            signals.append(
                {
                    "bar_idx": i,
                    "direction": "long",
                    "close": close_i,
                    "atr": atr_i,
                }
            )
        elif close_i < support and close_i < ma:
            signals.append(
                {
                    "bar_idx": i,
                    "direction": "short",
                    "close": close_i,
                    "atr": atr_i,
                }
            )

    return signals


def micro_signals(ohlc: dict[str, np.ndarray], atr: np.ndarray, warmup: int = 50) -> list[dict]:
    """Micro Mean-Reversion: pullback entries in trending regimes.

    Logic from _micro_rule():
    - 8-bar recent_high, recent_low, mid = (high+low)/2
    - range_pct = (high-low) / close must be > 0.001 (0.1%)
    - Long: close < mid - 0.3*ATR
    - Short: close > mid + 0.3*ATR
    """
    n = ohlc["n"]
    closes = ohlc["c"]
    highs = ohlc["h"]
    lows = ohlc["l"]

    signals: list[dict] = []
    for i in range(warmup, n - 1):
        if np.isnan(atr[i]) or atr[i] <= 0:
            continue

        close_i = float(closes[i])
        recent_high = float(np.max(highs[i - 7 : i + 1]))
        recent_low = float(np.min(lows[i - 7 : i + 1]))
        mid = (recent_high + recent_low) / 2.0
        range_pct = (recent_high - recent_low) / max(close_i, 1e-6)

        # Only trade meaningful ranges (> 0.1% of price)
        if range_pct < 0.001:
            continue

        atr_i = float(atr[i])

        if close_i < mid - 0.3 * atr_i:
            signals.append(
                {
                    "bar_idx": i,
                    "direction": "long",
                    "close": close_i,
                    "atr": atr_i,
                }
            )
        elif close_i > mid + 0.3 * atr_i:
            signals.append(
                {
                    "bar_idx": i,
                    "direction": "short",
                    "close": close_i,
                    "atr": atr_i,
                }
            )

    return signals


def statarb_signals(ohlc: dict[str, np.ndarray], atr: np.ndarray, warmup: int = 50) -> list[dict]:
    """StatArb Z-Score: OU mean-reversion on price deviations.

    Logic from _statarb_rule():
    - 50-bar rolling mean and std of closes
    - Z-score = (close - mean) / std
    - Long: z_score < -2.0 (oversold)
    - Short: z_score > 2.0 (overbought)
    """
    n = ohlc["n"]
    closes = ohlc["c"]

    signals: list[dict] = []
    for i in range(warmup, n - 1):
        if np.isnan(atr[i]) or atr[i] <= 0:
            continue

        window = closes[i - 49 : i + 1]  # 50 bars including current
        rolling_mean = float(np.mean(window))
        rolling_std = float(np.std(window))
        if rolling_std < 1e-6:
            continue

        close_i = float(closes[i])
        z_score = (close_i - rolling_mean) / rolling_std
        atr_i = float(atr[i])

        if z_score < -2.0:
            signals.append(
                {
                    "bar_idx": i,
                    "direction": "long",
                    "close": close_i,
                    "atr": atr_i,
                }
            )
        elif z_score > 2.0:
            signals.append(
                {
                    "bar_idx": i,
                    "direction": "short",
                    "close": close_i,
                    "atr": atr_i,
                }
            )

    return signals


def blind_signals(
    ohlc: dict[str, np.ndarray],
    atr: np.ndarray,
    spacing: int = 12,
    warmup: int = 50,
) -> list[dict]:
    """Blind entry baseline: long+short at every N bars (no strategy logic)."""
    n = ohlc["n"]
    closes = ohlc["c"]

    signals: list[dict] = []
    for i in range(warmup, n - 1, spacing):
        if np.isnan(atr[i]) or atr[i] <= 0:
            continue
        atr_i = float(atr[i])
        close_i = float(closes[i])
        signals.append(
            {
                "bar_idx": i,
                "direction": "long",
                "close": close_i,
                "atr": atr_i,
            }
        )
        signals.append(
            {
                "bar_idx": i,
                "direction": "short",
                "close": close_i,
                "atr": atr_i,
            }
        )

    return signals


# ── Strategy Registry ──────────────────────────────────────────────────────────

SignalFn = Callable[..., list[dict]]

STRATEGIES: dict[str, SignalFn] = {
    "Barrier_Breakout": barrier_signals,
    "Micro_MeanRev": micro_signals,
    "StatArb_ZScore": statarb_signals,
    "Blind_Baseline": blind_signals,
}


# ── Cooldown Filter ────────────────────────────────────────────────────────────


def apply_cooldown(signals: list[dict], cooldown_bars: int) -> list[dict]:
    """Filter signals: only keep those spaced >= cooldown_bars apart."""
    if cooldown_bars <= 0:
        return signals
    filtered: list[dict] = []
    last_idx = -9999
    for s in signals:
        if s["bar_idx"] - last_idx >= cooldown_bars:
            filtered.append(s)
            last_idx = s["bar_idx"]
    return filtered


# ── Main Evaluation Loop ───────────────────────────────────────────────────────


def evaluate_strategy(
    ohlc: dict[str, np.ndarray],
    atr: np.ndarray,
    signal_list: list[dict],
    sl_mult: float,
    tp_mult: float,
    horizon: int,
    label: str,
) -> dict:
    """Simulate barrier outcomes for all signals from one strategy."""
    n = ohlc["n"]
    h = ohlc["h"]
    l_low = ohlc["l"]

    outcomes = {"tp": 0, "sl": 0, "timeout": 0}
    pnl_atr_sum = 0.0
    longs = 0
    shorts = 0

    for sig in signal_list:
        entry_idx = sig["bar_idx"]
        entry_price = sig["close"]
        entry_atr = sig["atr"]
        direction = sig["direction"]

        # Skip signals too close to end of data (no room for horizon bars)
        if entry_idx + horizon + 1 >= n:
            continue

        fh = h[entry_idx + 1 : entry_idx + horizon + 1]
        fl = l_low[entry_idx + 1 : entry_idx + horizon + 1]

        if direction == "long":
            tp_price = entry_price + tp_mult * entry_atr
            sl_price = entry_price - sl_mult * entry_atr
            outcome = _resolve_barrier(fh, fl, tp_price, sl_price, horizon, direction="long")
            outcomes[outcome] += 1
            if outcome == "tp":
                pnl_atr_sum += tp_mult
            elif outcome == "sl":
                pnl_atr_sum -= sl_mult
            longs += 1
        else:  # short
            tp_price = entry_price - tp_mult * entry_atr
            sl_price = entry_price + sl_mult * entry_atr
            outcome = _resolve_barrier(fh, fl, tp_price, sl_price, horizon, direction="short")
            outcomes[outcome] += 1
            if outcome == "tp":
                pnl_atr_sum += tp_mult
            elif outcome == "sl":
                pnl_atr_sum -= sl_mult
            shorts += 1

    total = outcomes["tp"] + outcomes["sl"] + outcomes["timeout"]
    ev = pnl_atr_sum / total if total > 0 else 0.0

    return {
        "strategy": label,
        "signals_total": len(signal_list),
        "resolved": total,
        "longs": longs,
        "shorts": shorts,
        "tp": outcomes["tp"],
        "sl": outcomes["sl"],
        "timeout": outcomes["timeout"],
        "tp_pct": outcomes["tp"] / total * 100 if total > 0 else 0.0,
        "sl_pct": outcomes["sl"] / total * 100 if total > 0 else 0.0,
        "timeout_pct": outcomes["timeout"] / total * 100 if total > 0 else 0.0,
        "ev_atr": ev,
        "win_rate_conditional": (
            outcomes["tp"] / (outcomes["tp"] + outcomes["sl"]) * 100
            if (outcomes["tp"] + outcomes["sl"]) > 0
            else 0.0
        ),
    }


# ── Main ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Phase 0: Base Strategy Edge Validation")
    parser.add_argument(
        "--data",
        default="data/raw/xauusdc_m5_merged.csv",
        help="OHLC CSV path",
    )
    parser.add_argument(
        "--out",
        default="data/phase0_strategy_edge_report.csv",
        help="Output CSV path",
    )
    parser.add_argument("--sl", type=float, default=2.0, help="SL ATR multiplier")
    parser.add_argument("--tp", type=float, default=1.25, help="TP ATR multiplier")
    parser.add_argument("--horizon", type=int, default=12, help="Horizon in M5 bars")
    parser.add_argument(
        "--cooldown",
        type=int,
        default=6,
        help="Minimum bars between signals (0=no cooldown, default=6 = 30min)",
    )
    parser.add_argument(
        "--blind-spacing",
        type=int,
        default=12,
        help="Bar spacing for blind baseline entries",
    )
    parser.add_argument(
        "--strategies",
        nargs="*",
        default=["Barrier_Breakout", "Micro_MeanRev", "StatArb_ZScore", "Blind_Baseline"],
        help="Which strategies to run (default: all)",
    )
    args = parser.parse_args()

    # ── Load data ──────────────────────────────────────────────────────────
    print("=" * 80)
    print("PHASE 0: BASE STRATEGY EDGE VALIDATION")
    print(f"Contract: SL={args.sl}xATR  TP={args.tp}xATR  H={args.horizon} (M5)")
    print(f"Cooldown: {args.cooldown} bars  |  Blind spacing: {args.blind_spacing} bars")
    print(f"Time: {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 80)

    ohlc = load_ohlc(args.data)
    print(f"Computing ATR({DEFAULT_ATR_PERIOD}) ...")
    atr = compute_atr(ohlc["h"], ohlc["l"], ohlc["c"], DEFAULT_ATR_PERIOD)

    # ── Run strategies ─────────────────────────────────────────────────────
    results: list[dict] = []

    for name in args.strategies:
        if name not in STRATEGIES:
            print(f"  Unknown strategy: {name} — skipping")
            continue

        rule_fn = STRATEGIES[name]
        print(f"\n{'─' * 60}")
        print(f"Evaluating: {name}")

        # Generate signals
        if name == "Blind_Baseline":
            raw_signals = rule_fn(ohlc, atr, spacing=args.blind_spacing)
        else:
            raw_signals = rule_fn(ohlc, atr)

        print(f"  Raw signals: {len(raw_signals)}")

        # Apply cooldown (skip for blind baseline — spacing already controlled)
        if name != "Blind_Baseline" and args.cooldown > 0:
            signals = apply_cooldown(raw_signals, args.cooldown)
            print(f"  After cooldown({args.cooldown}): {len(signals)}")
        else:
            signals = raw_signals

        # Evaluate
        result = evaluate_strategy(
            ohlc,
            atr,
            signals,
            sl_mult=args.sl,
            tp_mult=args.tp,
            horizon=args.horizon,
            label=name,
        )
        results.append(result)

        # Per-strategy summary
        print(f"  Resolved: {result['resolved']}  " f"(L={result['longs']} S={result['shorts']})")
        print(
            f"  TP: {result['tp_pct']:.1f}%  "
            f"SL: {result['sl_pct']:.1f}%  "
            f"Timeout: {result['timeout_pct']:.1f}%"
        )
        print(
            f"  EV: {result['ev_atr']:+.4f} ATR  "
            f"Conditional WR: {result['win_rate_conditional']:.1f}%"
        )

    # ── Final Report ────────────────────────────────────────────────────────
    print(f"\n{'=' * 80}")
    print("PHASE 0: STRATEGY EDGE REPORT — FINAL RANKING")
    print(f"{'=' * 80}")
    header = (
        f"{'Rank':<5} {'Strategy':<22} {'Signals':>8} {'TP%':>7} {'SL%':>7} "
        f"{'TO%':>7} {'CondWR%':>8} {'EV(ATR)':>9} {'Verdict':<12}"
    )
    print(header)
    print("-" * 80)

    # Sort by EV descending
    results.sort(key=lambda r: -r["ev_atr"])

    verdicts: list[dict] = []
    for rank, r in enumerate(results, 1):
        ev = r["ev_atr"]
        sigs = r["resolved"]
        # Verdict logic
        if ev >= 0.02:
            verdict = "[PROMOTE]"
        elif ev >= -0.02:
            verdict = "[MARGINAL]"
        elif ev >= -0.05:
            verdict = "[WEAK]"
        else:
            verdict = "[REJECT]"

        # Frequency check
        if sigs < 5000 and ev > 0:
            verdict += " (low-N)"
        elif sigs < 5000:
            verdict += " (sparse)"

        print(
            f"{rank:<5} {r['strategy']:<22} {sigs:>8} "
            f"{r['tp_pct']:>6.1f}% {r['sl_pct']:>6.1f}% "
            f"{r['timeout_pct']:>6.1f}% {r['win_rate_conditional']:>7.1f}% "
            f"{ev:>+9.4f} {verdict:<12}"
        )
        verdicts.append({"strategy": r["strategy"], "verdict": verdict, "ev_atr": ev})

    print("-" * 80)

    # ── Institutional Verdict ───────────────────────────────────────────────
    print(f"\n{'─' * 60}")
    print("INSTITUTIONAL VERDICT (裁决标准)")
    print(f"{'─' * 60}")

    absolute_pass = [r for r in results if r["ev_atr"] >= -0.02]
    frequency_pass = [r for r in absolute_pass if r["resolved"] >= 5000]

    print(
        f"  EV Gate (EV >= -0.02 ATR):       "
        f"{len(absolute_pass)}/{len(results)} pass — "
        f"{[r['strategy'] for r in absolute_pass]}"
    )

    print(
        f"  Frequency Gate (N >= 5000):       "
        f"{len(frequency_pass)}/{len(absolute_pass)} pass — "
        f"{[r['strategy'] for r in frequency_pass]}"
    )

    if frequency_pass:
        winner = frequency_pass[0]
        print(
            f"\n  *** PROMOTED TO PHASE 1: {winner['strategy']} "
            f"(EV={winner['ev_atr']:+.4f} ATR, N={winner['resolved']})"
        )
    else:
        print("\n  ** NO STRATEGY PASSES BOTH GATES.")
        print("  -> MetaFilter training infeasible with current rule strategies.")
        print(
            "  -> Recommended: improve strategy logic or add regime filter "
            "before retrying Phase 0."
        )

    # ── Write CSV ───────────────────────────────────────────────────────────
    fieldnames = [
        "strategy",
        "signals_total",
        "resolved",
        "longs",
        "shorts",
        "tp",
        "sl",
        "timeout",
        "tp_pct",
        "sl_pct",
        "timeout_pct",
        "ev_atr",
        "win_rate_conditional",
    ]
    csv_path = Path(args.out)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)
    print(f"\nCSV saved to {csv_path}")

    # ── Comparison with blind baseline ──────────────────────────────────────
    blind = next((r for r in results if r["strategy"] == "Blind_Baseline"), None)
    if blind:
        print(f"\n{'─' * 60}")
        print("EDGE OVER BLIND BASELINE")
        print(f"{'─' * 60}")
        print(f"  Blind EV: {blind['ev_atr']:+.4f} ATR")
        for r in results:
            if r["strategy"] == "Blind_Baseline":
                continue
            delta = r["ev_atr"] - blind["ev_atr"]
            sign = "+" if delta > 0 else ""
            print(
                f"  {r['strategy']:<22}: EV={r['ev_atr']:+.4f}  "
                f"Δ={sign}{delta:.4f}  "
                f"{'[EDGE]' if delta > 0.01 else '[FLAT]' if abs(delta) <= 0.01 else '[WORSE]'}"
            )

    return 0 if frequency_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
