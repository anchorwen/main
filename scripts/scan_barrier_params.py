"""
Barrier parameter scanner — find SL/TP/horizon combos that minimize timeout rate.

Usage:
    python scripts/scan_barrier_params.py --data data/raw/xauusdc_m5_merged.csv

Output:
    CSV report of (SL, TP, horizon, timeout_pct, tp_pct, sl_pct, ev_atr, rr_ratio)
    sorted by EV, then by timeout_pct.
"""

import argparse
import csv

import numpy as np

# ── Parameter grid ──────────────────────────────────────────────────────────
SL_MULTS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
TP_MULTS = [0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
HORIZONS = [6, 12, 24, 48]  # M5 bars (30min, 1h, 2h, 4h)
ENTRY_SPACING = 12  # bars between blind entries (avoid overlap)
ATR_PERIOD = 14


def load_ohlc(path: str) -> dict[str, np.ndarray]:
    """Load OHLC CSV into numpy arrays. Returns dict with o, h, l, c arrays."""
    print(f"Loading {path} ...")
    data = np.loadtxt(
        path,
        delimiter=",",
        skiprows=1,
        dtype={
            "names": (
                "time",
                "open",
                "high",
                "low",
                "close",
                "tick_volume",
                "spread",
                "real_volume",
            ),
            "formats": ("U19", "f8", "f8", "f8", "f8", "i8", "i8", "i8"),
        },
    )
    print(f"  {len(data)} bars: {data['time'][0]} → {data['time'][-1]}")
    return {
        "o": data["open"],
        "h": data["high"],
        "l": data["low"],
        "c": data["close"],
        "n": len(data),
    }


def compute_atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> np.ndarray:
    """Wilder's ATR. Returns array same length as inputs (first period-1 values are NaN)."""
    n = len(close)
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    # Wilder smoothing
    atr = np.full(n, np.nan)
    atr[period - 1] = np.mean(tr[:period])
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def _resolve_barrier(
    future_highs: np.ndarray,
    future_lows: np.ndarray,
    tp_price: float,
    sl_price: float,
    horizon: int,
    direction: str = "long",
) -> str:
    """
    Determine which barrier hits first (or timeout). Uses numpy vectorized ops.

    For long: TP hit when high >= tp_price (price rises), SL hit when low <= sl_price (price falls).
    For short: TP hit when low <= tp_price (price falls), SL hit when high >= sl_price (price rises).
    """
    if direction == "long":
        tp_bars = np.flatnonzero(future_highs >= tp_price)
        sl_bars = np.flatnonzero(future_lows <= sl_price)
    else:  # short
        tp_bars = np.flatnonzero(future_lows <= tp_price)
        sl_bars = np.flatnonzero(future_highs >= sl_price)

    tp_first = int(tp_bars[0]) if len(tp_bars) > 0 else horizon + 1
    sl_first = int(sl_bars[0]) if len(sl_bars) > 0 else horizon + 1

    if tp_first > horizon and sl_first > horizon:
        return "timeout"
    if tp_first < sl_first:
        return "tp"
    if sl_first < tp_first:
        return "sl"
    # Same bar — both barriers triggered. Use relative breach depth as tiebreaker.
    if direction == "long":
        tp_breach = (future_highs[tp_first] - tp_price) / tp_price
        sl_breach = (sl_price - future_lows[sl_first]) / sl_price
    else:
        tp_breach = (tp_price - future_lows[tp_first]) / tp_price
        sl_breach = (future_highs[sl_first] - sl_price) / sl_price
    return "tp" if tp_breach >= sl_breach else "sl"


def simulate_barriers_vectorized(
    ohlc: dict[str, np.ndarray],
    atr: np.ndarray,
    sl_mult: float,
    tp_mult: float,
    horizon: int,
    spacing: int,
) -> dict:
    """
    Simulate barrier hits for blind entries (long + short at each entry point).

    Uses numpy vectorized inner resolution — no bar-by-bar Python loops.
    Each parameter combo processes ~58K trades in <1 second.
    """
    n = ohlc["n"]
    h = ohlc["h"]
    l_low = ohlc["l"]
    o = ohlc["o"]

    entry_indices = np.arange(ATR_PERIOD, n - horizon, spacing)

    outcomes = {"tp": 0, "sl": 0, "timeout": 0}
    total = 0

    for entry_idx in entry_indices:
        atr_val = atr[entry_idx]
        if np.isnan(atr_val) or atr_val <= 0:
            continue

        entry_price = o[entry_idx]
        end_idx = min(entry_idx + horizon + 1, n)
        fh = h[entry_idx + 1 : end_idx]
        fl = l_low[entry_idx + 1 : end_idx]

        # Long: TP above, SL below
        outcomes[
            _resolve_barrier(
                fh,
                fl,
                tp_price=entry_price + tp_mult * atr_val,
                sl_price=entry_price - sl_mult * atr_val,
                horizon=horizon,
                direction="long",
            )
        ] += 1

        # Short: TP below, SL above
        outcomes[
            _resolve_barrier(
                fh,
                fl,
                tp_price=entry_price - tp_mult * atr_val,
                sl_price=entry_price + sl_mult * atr_val,
                horizon=horizon,
                direction="short",
            )
        ] += 1

        total += 2

    return {
        "total": total,
        "tp": outcomes["tp"],
        "sl": outcomes["sl"],
        "timeout": outcomes["timeout"],
        "tp_pct": outcomes["tp"] / total * 100 if total else 0,
        "sl_pct": outcomes["sl"] / total * 100 if total else 0,
        "timeout_pct": outcomes["timeout"] / total * 100 if total else 0,
        "ev_atr": (outcomes["tp"] * tp_mult - outcomes["sl"] * sl_mult) / total if total else 0,
    }


def main():
    parser = argparse.ArgumentParser(description="Barrier parameter scanner")
    parser.add_argument("--data", default="data/raw/xauusdc_m5_merged.csv", help="OHLC CSV path")
    parser.add_argument("--out", default="data/barrier_scan_results.csv", help="Output CSV path")
    args = parser.parse_args()

    ohlc = load_ohlc(args.data)
    print(f"Computing ATR({ATR_PERIOD}) ...")
    atr = compute_atr(ohlc["h"], ohlc["l"], ohlc["c"], ATR_PERIOD)

    n_combos = len(SL_MULTS) * len(TP_MULTS) * len(HORIZONS)
    print(
        f"Scanning {n_combos} parameter combinations "
        f"(SL×{len(SL_MULTS)} TP×{len(TP_MULTS)} H×{len(HORIZONS)}) ..."
    )

    results = []
    combo_idx = 0
    for horizon in HORIZONS:
        for sl_mult in SL_MULTS:
            for tp_mult in TP_MULTS:
                combo_idx += 1
                r = simulate_barriers_vectorized(
                    ohlc, atr, sl_mult, tp_mult, horizon, ENTRY_SPACING
                )
                r["sl_mult"] = sl_mult
                r["tp_mult"] = tp_mult
                r["horizon"] = horizon
                r["rr_ratio"] = tp_mult / sl_mult if sl_mult > 0 else float("inf")
                r["combo"] = f"SL={sl_mult:.2f} TP={tp_mult:.2f} H={horizon}"
                results.append(r)

                pct_done = combo_idx / n_combos * 100
                if combo_idx % 20 == 0 or combo_idx == n_combos:
                    print(
                        f"  [{combo_idx}/{n_combos} {pct_done:.0f}%] "
                        f"SL={sl_mult:.2f} TP={tp_mult:.2f} H={horizon:2d} → "
                        f"TO={r['timeout_pct']:.1f}% TP={r['tp_pct']:.1f}% SL={r['sl_pct']:.1f}% "
                        f"EV={r['ev_atr']:+.4f}"
                    )

    # ── Sort by EV descending, then timeout ascending ──
    results.sort(key=lambda x: (-x["ev_atr"], x["timeout_pct"]))

    # ── Write CSV ──
    fieldnames = [
        "combo",
        "sl_mult",
        "tp_mult",
        "horizon",
        "rr_ratio",
        "total",
        "tp",
        "sl",
        "timeout",
        "tp_pct",
        "sl_pct",
        "timeout_pct",
        "ev_atr",
    ]
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)

    print(f"\nSaved {len(results)} results to {args.out}")

    # ── Print top 20 ──
    print(f"\n{'='*100}")
    print(
        f"{'Rank':<5} {'Combo':<28} {'Timeout%':>8} {'TP%':>7} {'SL%':>7} {'EV(ATR)':>9} {'RR':>6}"
    )
    print(f"{'='*100}")
    for i, r in enumerate(results[:25]):
        marker = (
            " ← CURRENT"
            if (r["sl_mult"] == 2.0 and r["tp_mult"] == 3.5 and r["horizon"] == 12)
            else ""
        )
        marker2 = (
            " ← ALT" if (r["sl_mult"] == 1.5 and r["tp_mult"] == 1.5 and r["horizon"] == 12) else ""
        )
        print(
            f"{i+1:<5} {r['combo']:<28} {r['timeout_pct']:>7.1f}% {r['tp_pct']:>6.1f}% "
            f"{r['sl_pct']:>6.1f}% {r['ev_atr']:>+9.4f} {r['rr_ratio']:>5.1f}{marker}{marker2}"
        )

    # ── Filter: timeout < 50%, EV > 0 ──
    viable = [r for r in results if r["timeout_pct"] < 50 and r["ev_atr"] > 0]
    print(f"\nViable combos (timeout < 50%, EV > 0): {len(viable)}")
    if viable:
        print(
            f"Top viable: {viable[0]['combo']} TO={viable[0]['timeout_pct']:.1f}% EV={viable[0]['ev_atr']:+.4f}"
        )


if __name__ == "__main__":
    main()
