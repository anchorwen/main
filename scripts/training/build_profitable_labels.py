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
from datetime import UTC, datetime
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


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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
