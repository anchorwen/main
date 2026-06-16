#!/usr/bin/env python
"""Profitability-calibrated label builder.

Phase 1: Computes profitability surface over (SL, TP) grid from historical OHLC.
Phase 2: Selects the optimal barrier configuration with positive expected value.
Phase 3: Generates training labels using the calibrated barriers.

This replaces the previous approach of using fixed SL=2.0/TP=3.5 which produced
a mathematically unprofitable label set (10% TP hit rate -> EV < 0).

Usage:
  # Phase 1+2+3: Full pipeline for M5 XAUUSD
  python scripts/training/build_profitable_labels.py \\
    --price-data data/raw/xauusdc_m5_1y.csv \\
    --output data/labels/calibrated_barrier_labels.jsonl \\
    --report data/reports/profitability_surface.json

  # Phase 1 only: Just compute and display the profitability surface
  python scripts/training/build_profitable_labels.py \\
    --price-data data/raw/xauusdc_m5_1y.csv \\
    --surface-only

  # Custom grid
  python scripts/training/build_profitable_labels.py \\
    --price-data data/raw/xauusdc_m5_1y.csv \\
    --sl-range 0.5,1.0,1.5,2.0,2.5 \\
    --tp-range 2.0,3.0,4.0,5.0,6.0,8.0 \\
    --horizon 12
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

import numpy as np

from core.contracts.training.label_contract import _build_barrier_labels_array
from core.training.profitability_calibrator import (
    compute_profitability_surface,
    recommend_label_contract,
    surface_to_report,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


from core.training.utils import utc_now_iso as _utc_now_iso  # noqa: F401

# ── Data loading ────────────────────────────────────────────────────────────


def load_ohlc_csv(csv_path: Path) -> dict[str, np.ndarray]:
    """Load OHLC data from a CSV file. Expects columns: time,open,high,low,close,tick_volume.

    Also handles MT5-exported CSVs with tab separators and header rows.
    """
    import csv

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    opens: list[float] = []
    timestamps: list[str] = []

    with open(csv_path, encoding="utf-8-sig") as f:
        # Detect dialect
        sample = f.read(4096)
        f.seek(0)
        sniffer = csv.Sniffer()
        try:
            dialect = sniffer.sniff(sample)
            has_header = sniffer.has_header(sample)
        except csv.Error:
            dialect = "excel"  # type: ignore[assignment]
            has_header = True

        reader = csv.reader(f, dialect)
        for i, row in enumerate(reader):
            if has_header and i == 0:
                continue
            if len(row) < 5:
                continue
            try:
                # Columns: time, open, high, low, close, ...
                ts = row[0]
                o = float(row[1])
                h = float(row[2])
                l = float(row[3])
                c = float(row[4])
            except (ValueError, IndexError):
                continue

            if h < l or c <= 0:
                continue

            opens.append(o)
            highs.append(h)
            lows.append(l)
            closes.append(c)
            timestamps.append(ts)

    return {
        "high": np.array(highs, dtype=np.float64),
        "low": np.array(lows, dtype=np.float64),
        "close": np.array(closes, dtype=np.float64),
        "open": np.array(opens, dtype=np.float64),
        "timestamp": timestamps,
        "n_bars": len(closes),
    }


# ── Label generation with calibrated barriers ───────────────────────────────


def build_calibrated_labels(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    timestamps: list[str],
    *,
    sl_atr_mult: float,
    tp_atr_mult: float,
    horizon_bars: int,
    atr_period: int = 14,
    warmup_bars: int = 100,
    entry_stride: int = 1,
    sides: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Generate training labels using calibrated barrier multipliers.

    For each entry bar, determines which barrier (TP/SL) hits first.
    Returns a list of label records in training_label.v1 schema.
    """
    if sides is None:
        sides = ["long", "short"]

    n_bars = len(closes)
    max_horizon_idx = n_bars - horizon_bars - 1
    entries = range(warmup_bars, max_horizon_idx, entry_stride)

    labels: list[dict[str, Any]] = []

    for entry_idx in entries:
        for side in sides:
            result = _build_barrier_labels_array(
                highs,
                lows,
                closes,
                entry_idx=entry_idx,
                side=side,
                sl_atr_mult=sl_atr_mult,
                tp_atr_mult=tp_atr_mult,
                horizon_bars=horizon_bars,
                atr_period=atr_period,
            )

            entry_price = float(closes[entry_idx])
            entry_time = timestamps[entry_idx] if entry_idx < len(timestamps) else ""

            # Numeric label: 1=tp_hit_first, -1=sl_hit_first, 0=timeout
            label_map = {"tp_hit_first": 1, "sl_hit_first": -1, "timeout": 0}
            numeric_label = label_map.get(result.label, 0)

            # Simulated P&L in R-units (for Sharpe-aligned training)
            if result.label == "tp_hit_first":
                pnl_r = tp_atr_mult
            elif result.label == "sl_hit_first":
                pnl_r = -sl_atr_mult
            else:
                # Timeout: approximate return from close at horizon to entry
                exit_idx = min(entry_idx + horizon_bars, n_bars - 1)
                exit_price = float(closes[exit_idx])
                if side == "long":
                    pnl_r = (exit_price - entry_price) / (tp_atr_mult * result.atr_at_entry)
                else:
                    pnl_r = (entry_price - exit_price) / (tp_atr_mult * result.atr_at_entry)
                # Clamp timeout P&L to [-sl, tp] range
                pnl_r = max(-sl_atr_mult, min(tp_atr_mult, pnl_r))

            labels.append(
                {
                    "schema_version": "training_label.v1",
                    "label_id": f"{entry_time}_{side}_{entry_idx}",
                    "entry_time": entry_time,
                    "entry_idx": int(entry_idx),
                    "entry_price": round(entry_price, 6),
                    "side": side,
                    "label": numeric_label,
                    "label_class": result.label,
                    "sl_price": round(result.sl_price, 6),
                    "tp_price": round(result.tp_price, 6),
                    "atr_at_entry": round(result.atr_at_entry, 6),
                    "horizon_bars": horizon_bars,
                    "pnl_r": round(pnl_r, 6),
                    "hit_bar_index": result.hit_bar_index,
                    "hit_price": round(result.hit_price, 6) if result.hit_price else None,
                    "contract": {
                        "sl_atr_mult": sl_atr_mult,
                        "tp_atr_mult": tp_atr_mult,
                        "horizon_bars": horizon_bars,
                        "atr_period": atr_period,
                    },
                    "generated_at": _utc_now_iso(),
                }
            )

    return labels


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="build_profitable_labels",
        description="Profitability-calibrated label builder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--price-data",
        type=Path,
        required=True,
        help="OHLC CSV file (time,open,high,low,close,...)",
    )
    p.add_argument("--output", type=Path, default=None, help="Output JSONL path for labels")
    p.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Output JSON path for profitability surface report",
    )

    # Grid search parameters
    p.add_argument(
        "--sl-range",
        type=str,
        default="0.5,1.0,1.5,2.0,2.5,3.0",
        help="Comma-separated SL multipliers (default: 0.5,1.0,...,3.0)",
    )
    p.add_argument(
        "--tp-range",
        type=str,
        default="1.0,2.0,3.0,4.0,5.0,6.0,8.0",
        help="Comma-separated TP multipliers (default: 1.0,2.0,...,8.0)",
    )
    p.add_argument("--horizon", type=int, default=12, help="Barrier horizon in bars (default: 12)")
    p.add_argument("--atr-period", type=int, default=14, help="ATR lookback period (default: 14)")

    # Selection criteria
    p.add_argument(
        "--min-expected-pnl",
        type=float,
        default=0.03,
        help="Minimum expected PnL in R-units (default: 0.03)",
    )
    p.add_argument(
        "--min-rr", type=float, default=1.5, help="Minimum reward:risk ratio (default: 1.5)"
    )

    # ── DQAF-20260616-003/P1: Directional balance constraints ────────────
    p.add_argument(
        "--min-directional-ev",
        type=float,
        default=0.01,
        help="Minimum EV for EACH direction separately (R-units). "
        "Rejects configs where LONG or SHORT EV < threshold. "
        "Set to 0 to disable. (default: 0.01)",
    )
    p.add_argument(
        "--max-directional-skew",
        type=float,
        default=3.0,
        help="Maximum LONG:SHORT EV ratio allowed. "
        "Rejects configs where max(EV_long, EV_short) / min(EV_long, EV_short) > skew. "
        "Prevents extreme 5:1 imbalances. (default: 3.0)",
    )
    p.add_argument(
        "--directional-balance-report",
        type=Path,
        default=None,
        help="Output JSON path for directional balance analysis report",
    )

    # Modes
    p.add_argument(
        "--surface-only",
        action="store_true",
        help="Only compute and display profitability surface, no label gen",
    )
    p.add_argument(
        "--force-sl", type=float, default=None, help="Force a specific SL multiplier (skip search)"
    )
    p.add_argument(
        "--force-tp", type=float, default=None, help="Force a specific TP multiplier (skip search)"
    )
    p.add_argument(
        "--sides",
        type=str,
        default="both",
        choices=["long", "short", "both"],
        help="Entry directions to simulate (default: both)",
    )
    p.add_argument(
        "--entry-stride",
        type=int,
        default=1,
        help="Test every Nth bar (default: 1, higher = faster but noisier)",
    )

    return p


# ── DQAF-20260616-003/P1: Directional balance filter ─────────────────────


def _compute_directional_balance(
    args: argparse.Namespace,
    data: dict[str, Any],
    combined_surface: Any,  # ProfitabilitySurface
) -> dict[str, Any]:
    """Compute directional EV split and filter imbalanced configurations.

    Runs the profitability surface separately for LONG and SHORT, then
    cross-references each (SL, TP) config to flag those where one direction
    dominates.  Returns a report dict with per-config analysis.
    """
    from core.training.profitability_calibrator import (
        ProfitabilitySurface,
        compute_profitability_surface,
    )

    print("\n[DIR-BAL] Computing directional balance...")
    print(f"          min-directional-ev={args.min_directional_ev}")
    print(f"          max-directional-skew={args.max_directional_skew}")

    # Compute separate surfaces for LONG and SHORT
    surface_long = compute_profitability_surface(
        data["high"], data["low"], data["close"],
        horizon_bars=args.horizon, atr_period=args.atr_period,
        sl_range=[float(x) for x in args.sl_range.split(",")],
        tp_range=[float(x) for x in args.tp_range.split(",")],
        entry_stride=args.entry_stride, side="long",
        symbol="XAUUSDc", timeframe="M5", tick_value=0.01, tick_size=0.001,
    )
    surface_short = compute_profitability_surface(
        data["high"], data["low"], data["close"],
        horizon_bars=args.horizon, atr_period=args.atr_period,
        sl_range=[float(x) for x in args.sl_range.split(",")],
        tp_range=[float(x) for x in args.tp_range.split(",")],
        entry_stride=args.entry_stride, side="short",
        symbol="XAUUSDc", timeframe="M5", tick_value=0.01, tick_size=0.001,
    )

    # Build lookup: (sl, tp) -> EV for each direction
    ev_long: dict[tuple[float, float], float] = {}
    for p in surface_long.points:
        ev_long[(p.sl_atr_mult, p.tp_atr_mult)] = p.expected_pnl_r
    ev_short: dict[tuple[float, float], float] = {}
    for p in surface_short.points:
        ev_short[(p.sl_atr_mult, p.tp_atr_mult)] = p.expected_pnl_r

    # Analyze each combined point
    balanced: list[dict[str, Any]] = []
    rejected_weak: list[dict[str, Any]] = []
    rejected_skew: list[dict[str, Any]] = []

    for p in combined_surface.points:
        key = (p.sl_atr_mult, p.tp_atr_mult)
        ev_l = ev_long.get(key, -999.0)
        ev_s = ev_short.get(key, -999.0)
        if ev_l <= -998 or ev_s <= -998:
            continue

        skew = max(abs(ev_l), abs(ev_s)) / max(min(abs(ev_l), abs(ev_s)), 1e-8) if ev_l != 0 and ev_s != 0 else 999.0
        min_ev = min(ev_l, ev_s)
        entry = {
            "sl": p.sl_atr_mult, "tp": p.tp_atr_mult,
            "ev_combined": p.expected_pnl_r,
            "ev_long": round(ev_l, 6), "ev_short": round(ev_s, 6),
            "skew_ratio": round(skew, 2),
            "min_directional_ev": round(min_ev, 6),
        }

        if min_ev < args.min_directional_ev:
            rejected_weak.append(entry)
        elif skew > args.max_directional_skew:
            rejected_skew.append(entry)
        else:
            balanced.append(entry)

    n_total = len(balanced) + len(rejected_weak) + len(rejected_skew)
    print(f"          Total configs evaluated: {n_total}")
    print(f"          PASSED (balanced):       {len(balanced)}")
    print(f"          REJECTED (weak dir):     {len(rejected_weak)} (min EV < {args.min_directional_ev})")
    print(f"          REJECTED (skewed):       {len(rejected_skew)} (skew > {args.max_directional_skew})")

    # Build balanced-only surface
    balanced_keys = {(b["sl"], b["tp"]) for b in balanced}
    balanced_points = [p for p in combined_surface.points if (p.sl_atr_mult, p.tp_atr_mult) in balanced_keys]

    report = {
        "total_configs": n_total,
        "balanced": len(balanced),
        "rejected_weak_direction": len(rejected_weak),
        "rejected_skewed": len(rejected_skew),
        "constraints": {
            "min_directional_ev": args.min_directional_ev,
            "max_directional_skew": args.max_directional_skew,
        },
        "rejected_details": {
            "weak_direction": sorted(rejected_weak, key=lambda x: x["min_directional_ev"]),
            "skewed": sorted(rejected_skew, key=lambda x: -x["skew_ratio"]),
        },
        "balanced_configs": sorted(balanced, key=lambda x: -x["ev_combined"]),
    }

    return {
        "report": report,
        "balanced_points": balanced_points,
        "balanced_keys": balanced_keys,
        "surface_long": surface_long,
        "surface_short": surface_short,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ── Load data ───────────────────────────────────────────────────────
    print(f"[1/3] Loading OHLC data from {args.price_data}...")
    try:
        data = load_ohlc_csv(args.price_data)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        return 1

    print(f"       Loaded {data['n_bars']} bars")

    # Parse SL/TP ranges
    sl_range = [float(x.strip()) for x in args.sl_range.split(",")]
    tp_range = [float(x.strip()) for x in args.tp_range.split(",")]

    # ── Compute profitability surface ────────────────────────────────────
    print("[2/3] Computing profitability surface...")
    print(f"       SL grid: {sl_range}")
    print(f"       TP grid: {tp_range}")
    print(f"       Horizon: {args.horizon} bars, ATR: {args.atr_period}")
    print(
        f"       Simulating {data['n_bars'] - 100 - args.horizon} entries × {args.sides} sides..."
    )

    surface = compute_profitability_surface(
        data["high"],
        data["low"],
        data["close"],
        horizon_bars=args.horizon,
        atr_period=args.atr_period,
        sl_range=sl_range,
        tp_range=tp_range,
        entry_stride=args.entry_stride,
        side=args.sides,
        symbol="XAUUSDc",
        timeframe="M5",
        tick_value=0.01,
        tick_size=0.001,
    )

    # Display surface summary
    print(
        f"\n       {'SL':>6} {'TP':>6} {'RR':>6} {'TP%':>8} {'SL%':>8} {'TO%':>8} {'EV(R)':>8} {'Sharpe':>8}"
    )
    print(f"       {'-'*6} {'-'*6} {'-'*6} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
    for p in sorted(surface.points, key=lambda x: x.expected_pnl_r, reverse=True)[:20]:
        marker = " PROFITABLE" if p.is_profitable else ""
        print(
            f"       {p.sl_atr_mult:>6.1f} {p.tp_atr_mult:>6.1f} {p.reward_risk_ratio:>6.1f} "
            f"{p.tp_hit_rate:>8.4f} {p.sl_hit_rate:>8.4f} {p.timeout_rate:>8.4f} "
            f"{p.expected_pnl_r:>8.4f} {p.sharpe_estimate:>8.2f}{marker}"
        )

    n_profitable = len(surface.profitable_configs())
    print(f"\n       Profitable configs: {n_profitable}/{len(surface.points)}")

    if n_profitable == 0:
        print("\n[WARNING] No profitable (SL, TP) configurations found!")
        print("         Try widening the TP range or narrowing SL range.")
        if not args.surface_only:
            return 1

    # ── DQAF-20260616-003/P1: Directional balance check ──────────────────
    _bal_result = None
    if args.min_directional_ev > 0 and args.sides == "both":
        _bal_result = _compute_directional_balance(args, data, surface)

        if args.directional_balance_report:
            args.directional_balance_report.parent.mkdir(parents=True, exist_ok=True)
            args.directional_balance_report.write_text(
                json.dumps(_bal_result["report"], indent=2), encoding="utf-8"
            )
            print(f"       Balance report saved to {args.directional_balance_report}")

        _n_balanced = len(_bal_result["balanced_points"])
        if _n_balanced == 0:
            print("\n[ERROR] All (SL, TP) configurations rejected by directional balance filter!")
            print("        Try relaxing --min-directional-ev or --max-directional-skew.")
            return 1

        # Replace surface points with balanced subset for subsequent selection
        surface.points = _bal_result["balanced_points"]
        _n_after = len(surface.profitable_configs())
        print(f"       After directional filter: {_n_after} profitable configs (from {n_profitable})")

    # ── Select optimal configuration ─────────────────────────────────────
    if args.force_sl is not None and args.force_tp is not None:
        sl_sel, tp_sel = args.force_sl, args.force_tp
        print(f"\n       Using forced config: SL={sl_sel}, TP={tp_sel}")
    else:
        rec = recommend_label_contract(
            surface,
            min_expected_pnl=args.min_expected_pnl,
            min_reward_risk=args.min_rr,
        )
        if rec is None:
            print(
                f"\n[WARNING] No config meets criteria (EV>={args.min_expected_pnl}, RR>={args.min_rr})"
            )
            # Fall back to best available
            best = surface.best_config()
            if best is None:
                print("[ERROR] No valid configs at all.")
                return 1
            sl_sel, tp_sel = best.sl_atr_mult, best.tp_atr_mult
            print(f"         Falling back to best available: SL={sl_sel}, TP={tp_sel}")
        else:
            sl_sel = rec["sl_atr_mult"]
            tp_sel = rec["tp_atr_mult"]
            print(f"\n       Selected: SL={sl_sel}, TP={tp_sel}")
            print(f"       Expected PnL: {rec['expected_pnl_r']:.4f}R/trade")
            print(
                f"       TP hit rate: {rec['tp_hit_rate']:.4f}  SL hit rate: {rec['sl_hit_rate']:.4f}"
            )
            print(f"       Reward:Risk: {rec['reward_risk_ratio']:.1f}:1")

    # ── Save report ──────────────────────────────────────────────────────
    if args.report:
        report = surface_to_report(surface)
        report["selected_config"] = {"sl_atr_mult": sl_sel, "tp_atr_mult": tp_sel}
        report["generated_at"] = _utc_now_iso()
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"       Report: {report_path}")

    if args.surface_only:
        return 0

    # ── Generate calibrated labels ──────────────────────────────────────
    print(f"\n[3/3] Generating labels with SL={sl_sel}, TP={tp_sel}...")
    labels = build_calibrated_labels(
        data["high"],
        data["low"],
        data["close"],
        cast(list[str], data["timestamp"]),
        sl_atr_mult=sl_sel,
        tp_atr_mult=tp_sel,
        horizon_bars=args.horizon,
        atr_period=args.atr_period,
        sides=["long", "short"] if args.sides == "both" else [args.sides],
        entry_stride=args.entry_stride,
    )

    # Distribution summary
    from collections import Counter

    dist = Counter(l["label_class"] for l in labels)
    total = len(labels)
    print(f"       Generated {total} labels")
    for cls_name in ["tp_hit_first", "sl_hit_first", "timeout"]:
        count = dist.get(cls_name, 0)
        pct = count / total * 100 if total > 0 else 0
        print(f"         {cls_name}: {count} ({pct:.1f}%)")

    # Profitability check
    tp_hits = dist.get("tp_hit_first", 0)
    sl_hits = dist.get("sl_hit_first", 0)
    _to_count = dist.get("timeout", 0)
    ev_check = (tp_hits * tp_sel - sl_hits * sl_sel) / max(total, 1)
    print(
        f"       Expected PnL (from labels): {ev_check:.4f}R/trade"
        f"{' [OK]' if ev_check > 0 else ' [UNPROFITABLE]'}"
    )

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            for label in labels:
                f.write(json.dumps(label, ensure_ascii=False, default=str) + "\n")
        print(f"       Labels saved to: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
