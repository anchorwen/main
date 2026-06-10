#!/usr/bin/env python3
"""Build BTC MetaFilter V2 training dataset with PIT-aligned features.

MLOps Iron Law #1 (Point-in-Time / ASOF Join):
  For each trade open time T, find the LAST feature record with
  event_time <= T.  Never use features from AFTER the trade opened.
  This prevents lookahead bias (data leakage).

MLOps Iron Law #2 (Label Engineering with Damping):
  y=1 only when PnL > spread_cost * 1.5.  Breakeven and micro-loss
  trades are labeled y=0 — the model learns to avoid friction noise.

Usage:
  python scripts/build_btc_metafilter_v2_dataset.py --data-dir data_btc
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build BTC MetaFilter V2 PIT-aligned dataset")
    p.add_argument("--data-dir", default="data_btc", help="BTC data directory")
    p.add_argument("--output", default=None, help="Output NPZ path (default: data_btc/training/meta_features_btc_v2.npz)")
    p.add_argument("--spread-cost-usd", type=float, default=2.50, help="Estimated round-trip spread+slippage cost in USD")
    p.add_argument("--pnl-threshold-mult", type=float, default=1.5, help="Damping multiplier on spread cost for y=1")
    return p.parse_args()


def load_journal_opens(data_dir: str) -> list[dict[str, Any]]:
    """Extract open entries with close PnL from live_trade_journal."""
    jl_path = os.path.join(data_dir, "live_trade_journal.jsonl")
    if not os.path.exists(jl_path):
        print(f"ERROR: {jl_path} not found")
        return []

    # First pass: collect all entries grouped by position_ticket
    by_ticket: dict[int, list[dict]] = {}
    with open(jl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            tkt = rec.get("position_ticket")
            if tkt:
                by_ticket.setdefault(int(tkt), []).append(rec)

    # Build trade records: open + final close
    trades = []
    for tkt, recs in by_ticket.items():
        opens = [r for r in recs if r.get("action") == "open"]
        if not opens:
            continue
        open_rec = opens[0]
        open_ts = open_rec.get("recorded_at", "")
        entry_price = open_rec.get("detail", {}).get("entry_price") or open_rec.get("entry_price", 0)
        side = open_rec.get("side", "")
        volume = open_rec.get("volume", 0)

        # Find final close
        FINAL = {"close", "loss", "win", "close_accepted", "sl_hit_first", "tp_hit_first", "breakeven"}
        closes = [r for r in recs if r.get("action") in FINAL]
        if not closes:
            continue
        # Use last close (in case of duplicates)
        close_rec = max(closes, key=lambda r: r.get("recorded_at", "z"))
        pnl = close_rec.get("pnl")
        if pnl is None:
            # Try detail.pnl
            pnl = close_rec.get("detail", {}).get("pnl")
        if pnl is None:
            continue  # skip trades with no PnL

        trades.append({
            "ticket": tkt,
            "open_time": open_ts,
            "entry_price": float(entry_price) if entry_price else 0,
            "side": side,
            "volume": float(volume) if volume else 0,
            "pnl": float(pnl),
            "close_label": close_rec.get("label", ""),
        })

    print(f"Journal: {len(by_ticket)} tickets, {len(trades)} with PnL")
    return trades


def load_feature_store(data_dir: str) -> list[dict[str, Any]]:
    """Load all feature records from the M5 feature store."""
    fs_path = os.path.join(
        data_dir, "feature_store", "records",
        "symbol=BTCUSDc", "timeframe=M5", "features.jsonl",
    )
    if not os.path.exists(fs_path):
        print(f"ERROR: {fs_path} not found")
        return []

    records = []
    with open(fs_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                records.append(rec)
            except json.JSONDecodeError:
                continue

    print(f"Feature store: {len(records)} records")
    return records


def load_v9_feature_names(data_dir: str) -> list[str]:
    """Load V9 institutional feature names from the feature store schema."""
    fs_schema_path = os.path.join(data_dir, "feature_store", "schemas.json")
    if not os.path.exists(fs_schema_path):
        print("WARNING: schemas.json not found, using positional indexing")
        return []
    with open(fs_schema_path, encoding="utf-8") as f:
        schemas = json.load(f)
    for sc in schemas.values():
        if isinstance(sc, dict) and "v9_institutional" in sc.get("name", "") and "BTCUSDc" in sc.get("symbol", ""):
            return sc.get("fields", [])[:40]
    return []


def asof_join(
    trades: list[dict[str, Any]],
    features: list[dict[str, Any]],
    v9_feature_names: list[str],
) -> tuple[np.ndarray, np.ndarray, list[dict[str, Any]]]:
    """PIT ASOF join: for each trade, find last feature BEFORE open time.

    MLOps Iron Law #1: backward-looking join only.  Never use future data.
    """
    # Sort features by event_time for binary search
    feat_times = []
    for i, f in enumerate(features):
        ts = f.get("event_time", "")
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts)[:26])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                feat_times.append((dt, i))
            except (ValueError, TypeError):
                continue

    feat_times.sort(key=lambda x: x[0])

    X_rows = []
    y_rows = []
    meta_rows = []
    matched = 0
    skipped_future = 0
    skipped_missing = 0

    for trade in trades:
        open_ts = trade["open_time"]
        if not open_ts:
            skipped_missing += 1
            continue
        try:
            open_dt = datetime.fromisoformat(str(open_ts)[:26])
            if open_dt.tzinfo is None:
                open_dt = open_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            skipped_missing += 1
            continue

        # Binary search: find last feature with event_time <= open_dt
        lo, hi = 0, len(feat_times) - 1
        best_idx = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if feat_times[mid][0] <= open_dt:
                best_idx = mid
                lo = mid + 1
            else:
                hi = mid - 1

        if best_idx < 0:
            skipped_future += 1
            continue

        feat_idx = feat_times[best_idx][1]
        feat = features[feat_idx]
        values = feat.get("values", {})
        if not values or (isinstance(values, dict) and len(values) < 40):
            skipped_missing += 1
            continue

        # values is a dict {feature_name: value} — extract in schema order
        if isinstance(values, dict):
            feat_vec = [float(values.get(fn, 0.0)) for fn in v9_feature_names]
        else:
            feat_vec = list(values)[:40]
        if len(feat_vec) < 40:
            skipped_missing += 1
            continue

        X_rows.append(feat_vec)
        y_rows.append(trade["pnl"])
        meta_rows.append({
            "ticket": trade["ticket"],
            "open_time": open_ts,
            "feature_time": feat.get("event_time", ""),
            "pnl": trade["pnl"],
            "side": trade["side"],
            "entry_price": trade["entry_price"],
            "volume": trade["volume"],
            "close_label": trade["close_label"],
        })
        matched += 1

    print(f"ASOF join: {matched} matched, {skipped_future} no prior feature, {skipped_missing} missing data")
    if matched == 0:
        return np.array([]), np.array([]), []

    return np.array(X_rows), np.array(y_rows), meta_rows


def apply_labels(
    y_pnl: np.ndarray,
    spread_cost: float,
    threshold_mult: float,
) -> np.ndarray:
    """MLOps Iron Law #2: Damping threshold for binary labels.

    y=1: PnL > spread_cost * threshold_mult  (clear winner)
    y=0: PnL <= spread_cost * threshold_mult  (loser or friction noise)
    """
    threshold = spread_cost * threshold_mult
    y_binary = (y_pnl > threshold).astype(np.int32)

    n_win = int(y_binary.sum())
    n_loss = len(y_binary) - n_win
    print(f"Labels: {n_win} wins (PnL > ${threshold:.2f}), {n_loss} non-wins "
          f"(WR={n_win/max(len(y_binary),1)*100:.1f}%)")
    return y_binary


def main() -> None:
    args = parse_args()
    data_dir = args.data_dir

    trades = load_journal_opens(data_dir)
    if not trades:
        return

    features = load_feature_store(data_dir)
    if not features:
        return

    v9_names = load_v9_feature_names(data_dir)
    print(f"V9 schema: {len(v9_names)} features: {v9_names[:3]}...")

    X, y_pnl, meta = asof_join(trades, features, v9_names)
    if len(X) == 0:
        return

    y = apply_labels(y_pnl, args.spread_cost_usd, args.pnl_threshold_mult)

    # ── Save ──
    output = args.output or os.path.join(data_dir, "training", "meta_features_btc_v2.npz")
    os.makedirs(os.path.dirname(output), exist_ok=True)

    np.savez_compressed(
        output,
        X=X,
        y=y,
        y_pnl=y_pnl,
        feature_names=np.array(v9_names, dtype=str),
        meta_tickets=np.array([m["ticket"] for m in meta]),
        spread_cost=args.spread_cost_usd,
        pnl_threshold=args.spread_cost_usd * args.pnl_threshold_mult,
    )
    print(f"\nDataset saved: {output}")
    print(f"  X shape: {X.shape}")
    print(f"  y distribution: {dict(zip(*np.unique(y, return_counts=True)))}")
    print(f"  Features: {v9_names[:5]}... ({len(v9_names)} total)")

    # Print PnL distribution for diagnostics
    pnl_sorted = sorted(y_pnl)
    print(f"\nPnL distribution: min={pnl_sorted[0]:+.2f}, "
          f"median={pnl_sorted[len(pnl_sorted)//2]:+.2f}, max={pnl_sorted[-1]:+.2f}")
    print(f"  Wins above threshold: {int((y_pnl > args.spread_cost_usd * args.pnl_threshold_mult).sum())}")
    print(f"  Breakeven zone (0 to threshold): {int(((y_pnl > 0) & (y_pnl <= args.spread_cost_usd * args.pnl_threshold_mult)).sum())}")
    print(f"  Losses: {int((y_pnl <= 0).sum())}")


if __name__ == "__main__":
    main()
