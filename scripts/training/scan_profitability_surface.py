"""Profitability surface scanner with timeout penalty.

Scans all (SL, TP) grid points on XAUUSD M5 data and outputs the
profitability surface with two ranking criteria:

  1. Hard filter: timeout_rate > 30% → eliminated regardless of EV
  2. EV_penalized = P(TP)*tp - P(SL)*sl - TIMEOUT_COST*P(timeout)
     where TIMEOUT_COST = 0.15R (opportunity cost of tied-up capital)

Usage::

    python scripts/training/scan_profitability_surface.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np

from core.training.profitability_calibrator import (
    ProfitabilityPoint,
    compute_profitability_surface,
)

TIMEOUT_HARD_FILTER = 0.30  # ruthlessly eliminate >30% timeout
TIMEOUT_COST_R = 0.15  # opportunity cost per timeout (in R-units)

CSV_PATH = Path("data/raw/xauusdc_m5_1y.csv")
OUTPUT_PATH = Path("data/training/profitability_surface_m5_12bar.json")


def load_ohlc(csv_path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    highs, lows, closes = [], [], []
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            highs.append(float(row["high"]))
            lows.append(float(row["low"]))
            closes.append(float(row["close"]))
    return (
        np.array(highs, dtype=np.float64),
        np.array(lows, dtype=np.float64),
        np.array(closes, dtype=np.float64),
    )


def penalize_timeout(point: ProfitabilityPoint) -> float:
    """Compute EV with timeout opportunity-cost penalty."""
    raw_ev = point.expected_pnl_r
    penalty = TIMEOUT_COST_R * point.timeout_rate
    return raw_ev - penalty


def main() -> None:
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found", file=sys.stderr)
        sys.exit(1)

    print(json.dumps({"event": "loading_data", "path": str(CSV_PATH)}), flush=True)
    highs, lows, closes = load_ohlc(CSV_PATH)
    print(
        json.dumps({"event": "data_loaded", "bars": len(closes)}),
        flush=True,
    )

    # ── Grid tailored for M5 barrier_12bar ──
    # SL: tight defensive range (M5 noise is high, so SL < 0.5 ATR = suicide)
    # TP: 1.0-8.0 covers sniper (1.5-2.0) through runner (3.0-4.0)
    sl_range = [0.5, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0]
    tp_range = [1.0, 1.2, 1.5, 1.8, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0]

    print(
        json.dumps(
            {
                "event": "scanning",
                "sl_range": sl_range,
                "tp_range": tp_range,
                "horizon_bars": 12,
                "side": "both",
            }
        ),
        flush=True,
    )

    surface = compute_profitability_surface(
        highs=highs,
        lows=lows,
        closes=closes,
        horizon_bars=12,
        atr_period=14,
        sl_range=sl_range,
        tp_range=tp_range,
        entry_stride=3,  # every 3rd bar = 15 min spacing
        warmup_bars=100,
        side="both",
        symbol="XAUUSDc",
        timeframe="M5",
        min_profitability=0.0,
        spread_points=30,
        slippage_points=10,
        tick_value=0.01,
        tick_size=0.001,  # XAUUSDc 3-digit precision
    )

    # ── Classify each config ──
    eliminated: list[dict] = []
    survivors: list[dict] = []

    for p in surface.points:
        ev_penalized = penalize_timeout(p)
        record = {
            "sl_atr_mult": p.sl_atr_mult,
            "tp_atr_mult": p.tp_atr_mult,
            "rr_ratio": round(p.reward_risk_ratio, 2),
            "tp_hit_rate": round(p.tp_hit_rate, 4),
            "sl_hit_rate": round(p.sl_hit_rate, 4),
            "timeout_rate": round(p.timeout_rate, 4),
            "ev_raw": round(p.expected_pnl_r, 4),
            "ev_penalized": round(ev_penalized, 4),
            "sharpe_estimate": p.sharpe_estimate,
        }
        if p.timeout_rate > TIMEOUT_HARD_FILTER:
            eliminated.append(record)
        else:
            survivors.append(record)

    # Sort survivors by penalized EV desc
    survivors.sort(key=lambda r: r["ev_penalized"], reverse=True)
    eliminated.sort(key=lambda r: r["timeout_rate"], reverse=True)

    # ── Categorize for user's Plan A / Plan B ──
    plan_a = [
        r for r in survivors if 1.0 <= r["sl_atr_mult"] <= 1.5 and 1.5 <= r["tp_atr_mult"] <= 2.0
    ]
    plan_b = [
        r for r in survivors if 1.5 <= r["sl_atr_mult"] <= 2.0 and 3.0 <= r["tp_atr_mult"] <= 4.0
    ]
    plan_a.sort(key=lambda r: r["ev_penalized"], reverse=True)
    plan_b.sort(key=lambda r: r["ev_penalized"], reverse=True)

    # ── Output ──
    report = {
        "surface": {
            "symbol": surface.symbol,
            "timeframe": surface.timeframe,
            "total_bars": surface.total_bars,
            "entries_simulated": surface.entries_simulated,
            "horizon_bars": surface.horizon_bars,
            "mean_atr": surface.mean_atr,
        },
        "filters": {
            "timeout_hard_filter": TIMEOUT_HARD_FILTER,
            "timeout_cost_r": TIMEOUT_COST_R,
        },
        "summary": {
            "total_configs": len(surface.points),
            "eliminated_timeout": len(eliminated),
            "survivors": len(survivors),
            "plan_a_candidates": len(plan_a),
            "plan_b_candidates": len(plan_b),
        },
        "plan_a_sniper": plan_a[:5],
        "plan_b_runner": plan_b[:5],
        "top_10_survivors": survivors[:10],
        "top_10_eliminated": eliminated[:10],
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    print(f"\nDone → {OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
