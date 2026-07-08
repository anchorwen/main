"""Augment existing 41-dim BTC H1 NPZ with 7 H1 directional features → 48-dim.

DQAF-20260707-003 Phase 2 (Training Side): The serving-side H1 feature computer
(``core/runtime/h1_features.py``) operates on M5 mid-price buffers with 12/24/48
bar lookbacks to capture 1h/2h/4h momentum.  For training data built from H1 bars
directly, we adapt the lookback indices to 1/2/4 bars (same time horizons, same
feature semantics).

Usage::

    python scripts/training/augment_h1_directional_features.py

Output: ``data_btc/training/btc_swing_h1_retrain_48/train.npz``
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

# ── H1 feature names (must match core/runtime/h1_features.py) ──────────────
H1_DIRECTIONAL_FEATURE_NAMES: list[str] = [
    "H1_Ret_1",
    "H1_Ret_2",
    "H1_Ret_4",
    "H1_Realized_Vol",
    "H1_Ret_Accel",
    "H1_MeanRev",
    "H1_M5_Div",
]

# ── 41-dim base feature names (must match training schema) ─────────────────
# Order B (FIX-20260625-137): BTC_MACRO_24 + BTC_MICRO_9 + TF_SPECIFIC_2
#                           + REGIME_DERIVED_4 + BTC_CROSS_2

# ── All 48 features ─────────────────────────────────────────────────────────
ALL_48_FEATURE_NAMES: list[str] = []


def _load_base_feature_names(meta_path: str) -> list[str]:
    """Load the 41 base feature names from the existing meta.json."""
    with open(meta_path) as f:
        meta = json.load(f)
    return list(meta["feature_names"])


def compute_h1_features_from_h1_bars(
    close_prices: np.ndarray,
    bar_idx: int,
    min_lookback: int = 49,
) -> dict[str, float]:
    """Compute 7 H1 directional features from H1 close prices.

    Adapted from ``core/runtime/h1_features.compute_h1_directional_features()``
    which operates on M5 bars (12/24/48 bar lookbacks ≈ 1h/2h/4h).
    For H1 bars directly, we use 1/2/4 bar lookbacks — same time horizons.

    Args:
        close_prices: 1-D array of H1 close prices, oldest first.
        bar_idx: Current bar index (inclusive).
        min_lookback: Minimum bars needed before *bar_idx*.

    Returns:
        Dict of 7 feature values.  Zeros when insufficient lookback.
    """
    if bar_idx < min_lookback:
        return {k: 0.0 for k in H1_DIRECTIONAL_FEATURE_NAMES}

    current = float(close_prices[bar_idx])

    # ── H1 horizons: 1 bar = 1 hour ──
    # Indices: -1 = 1 bar ago (1h), -2 = 2 bars ago (2h), -4 = 4 bars ago (4h)
    p_1 = float(close_prices[bar_idx - 1])  # 1 hour ago
    p_2 = float(close_prices[bar_idx - 2])  # 2 hours ago
    p_4 = float(close_prices[bar_idx - 4])  # 4 hours ago

    h1_ret_1 = float((current - p_1) / p_1) if p_1 > 0 else 0.0
    h1_ret_2 = float((current - p_2) / p_2) if p_2 > 0 else 0.0
    h1_ret_4 = float((current - p_4) / p_4) if p_4 > 0 else 0.0

    # ── Realized volatility: std of last 12 H1 bar returns (12h vol) ──
    # In M5: std of 12 M5 returns = 1h vol.  H1 can't resolve 1h intra-bar
    # vol, so we use 12 H1 returns = 12h vol — same concept, coarser scale.
    if bar_idx >= 13:
        _slice = close_prices[bar_idx - 12 : bar_idx + 1]
        rets_12 = np.diff(_slice) / _slice[:-1]
        h1_realized_vol = float(np.std(rets_12)) if len(rets_12) > 1 else 0.0
    else:
        h1_realized_vol = 0.0

    # ── Return acceleration ──
    h1_ret_accel = h1_ret_1 - h1_ret_2

    # ── Mean reversion: z-score from 12-bar MA (12h) ──
    # M5 uses 24-bar MA = 2h. H1 uses 12-bar MA = 12h — coarser.
    if bar_idx >= 13:
        ma_12 = float(np.mean(close_prices[bar_idx - 12 : bar_idx + 1]))
        std_12 = float(np.std(close_prices[bar_idx - 12 : bar_idx + 1]))
        if std_12 > 0 and ma_12 > 0:
            h1_mean_rev = float((current - ma_12) / std_12)
        else:
            h1_mean_rev = 0.0
    else:
        h1_mean_rev = 0.0

    # ── Multi-scale divergence: short (1h) vs longer (4h) momentum ──
    # Matches serving-side h1_features.py (DQAF-20260707-003v2).
    denom = abs(h1_ret_1) + abs(h1_ret_4) + 1e-10
    h1_m5_div = float(abs(h1_ret_1 - h1_ret_4) / denom)

    # ── NaN guard ──
    result = {
        "H1_Ret_1": h1_ret_1,
        "H1_Ret_2": h1_ret_2,
        "H1_Ret_4": h1_ret_4,
        "H1_Realized_Vol": h1_realized_vol,
        "H1_Ret_Accel": h1_ret_accel,
        "H1_MeanRev": h1_mean_rev,
        "H1_M5_Div": h1_m5_div,
    }
    for k in result:
        v = result[k]
        if v is None or not np.isfinite(float(v)):
            result[k] = 0.0
        else:
            result[k] = round(float(v), 8)
    return result


def augment_npz(
    npz_path: str,
    csv_path: str,
    output_dir: str,
    meta_path: str | None = None,
) -> dict[str, Any]:
    """Load 41-dim NPZ + H1 CSV → compute 7 H1 features → save 48-dim NPZ.

    Timestamp alignment: NPZ timestamps are matched to CSV timestamps
    via ``np.searchsorted``.  CSV bars beyond the NPZ range are used only
    for lookback (feature computation), never as samples.
    """
    # ── Load source NPZ ──────────────────────────────────────────────────
    print(f"[H1-Augment] Loading 41-dim NPZ from {npz_path}...")
    src = np.load(npz_path, allow_pickle=False)
    X_41 = src["X"]  # (N, 41)
    y = src["y"]  # (N,)
    ts_npz = src["timestamps"]  # (N,) float64
    pnl_r = src.get("pnl_r", np.zeros(len(y)))
    sample_weight = src.get("sample_weight", np.ones(len(y)))
    n_samples = len(ts_npz)
    print(f"  {n_samples:,} samples, X={X_41.shape}")

    # ── Load H1 CSV for close prices ─────────────────────────────────────
    print(f"[H1-Augment] Loading H1 close prices from {csv_path}...")
    import pandas as pd

    df = pd.read_csv(csv_path)
    csv_ts = pd.to_datetime(df["time"], format="mixed").astype("int64").values // 10**9
    csv_ts_f = csv_ts.astype(np.float64)
    csv_closes = df["close"].values.astype(np.float64)
    n_csv = len(csv_closes)
    print(f"  {n_csv:,} H1 bars")

    # ── Map NPZ timestamps → CSV indices ─────────────────────────────────
    # Each NPZ bar maps to its CSV bar index.  If NPZ timestamp is beyond
    # CSV range, we can't compute H1 features (need lookback).
    npz_to_csv = np.searchsorted(csv_ts_f, ts_npz)
    # Clamp: if npz timestamp doesn't exactly match, searchsorted gives
    # the insertion point.  For exact matches, we get the correct index+1.
    # We adjust: if csv_ts_f[npz_to_csv-1] == ts_npz[i], use npz_to_csv-1.

    # More robust: find exact matches
    csv_idx_for_npz = np.zeros(n_samples, dtype=np.int32)
    n_matched = 0
    n_beyond = 0
    for i in range(n_samples):
        t = ts_npz[i]
        idx = int(np.searchsorted(csv_ts_f, t))
        if idx >= n_csv:
            n_beyond += 1
            csv_idx_for_npz[i] = -1
        elif idx > 0 and abs(csv_ts_f[idx - 1] - t) < 1.0:  # within 1s
            csv_idx_for_npz[i] = idx - 1
            n_matched += 1
        elif idx < n_csv and abs(csv_ts_f[idx] - t) < 1.0:
            csv_idx_for_npz[i] = idx
            n_matched += 1
        else:
            csv_idx_for_npz[i] = -1  # no match
    print(f"  Timestamps: {n_matched:,} matched, {n_beyond:,} beyond CSV range")

    # ── Compute 7 H1 features for each NPZ row ───────────────────────────
    print("[H1-Augment] Computing 7 H1 directional features...")
    X_h1 = np.zeros((n_samples, 7), dtype=np.float32)
    n_computed = 0
    n_missing = 0

    for i in range(n_samples):
        csv_idx = csv_idx_for_npz[i]
        if csv_idx < 49:  # insufficient lookback
            n_missing += 1
            continue
        feats = compute_h1_features_from_h1_bars(csv_closes, csv_idx)
        for j, name in enumerate(H1_DIRECTIONAL_FEATURE_NAMES):
            X_h1[i, j] = float(feats.get(name, 0.0))
        n_computed += 1

        if (i + 1) % 10000 == 0:
            print(f"  ... {i + 1}/{n_samples} ({100 * (i + 1) / n_samples:.0f}%)")

    print(f"  Computed: {n_computed:,}, missing lookback: {n_missing:,}")

    # ── Concatenate → 48-dim ─────────────────────────────────────────────
    X_48 = np.concatenate([X_41, X_h1], axis=1)
    print(f"[H1-Augment] Final: X={X_48.shape} (41 + 7 = 48)")

    # ── Build 48-dim feature names ───────────────────────────────────────
    base_names = _load_base_feature_names(
        meta_path or os.path.join(os.path.dirname(npz_path), "meta.json")
    )
    all_48 = base_names + H1_DIRECTIONAL_FEATURE_NAMES

    # ── Save augmented NPZ ───────────────────────────────────────────────
    os.makedirs(output_dir, exist_ok=True)
    np.savez_compressed(
        os.path.join(output_dir, "train.npz"),
        X=X_48,
        y=y,
        pnl_r=pnl_r,
        sample_weight=sample_weight,
        timestamps=ts_npz,
    )
    print(f"  Saved: {os.path.join(output_dir, 'train.npz')}")

    # ── Save updated meta ────────────────────────────────────────────────
    meta_out: dict[str, Any] = {
        "schema_version": "btc_swing_v9.v2",
        "feature_names": all_48,
        "n_features": 48,
        "n_samples": int(n_samples),
        "built_at": datetime.now(UTC).isoformat(),
        "_augmented_from": str(npz_path),
        "_augmented_features": H1_DIRECTIONAL_FEATURE_NAMES,
        "_note": "DQAF-20260707-003: 41 base + 7 H1 directional momentum features",
    }
    # Carry forward key fields from original meta
    if meta_path:
        with open(meta_path) as f:
            orig_meta = json.load(f)
        for key in (
            "n_short",
            "n_neutral",
            "n_long",
            "horizon",
            "sl_atr_mult",
            "tp_atr_mult",
            "spread_points",
            "slippage_points",
            "tick_value",
            "decay_half_life_days",
            "cv_folds",
            "purge_bars",
            "ev_r",
            "objective",
            "num_class",
        ):
            if key in orig_meta:
                meta_out[key] = orig_meta[key]

    with open(os.path.join(output_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta_out, f, indent=2, ensure_ascii=False)
    print(f"  Saved: {os.path.join(output_dir, 'meta.json')}")

    # ── Copy CV splits (unchanged) ───────────────────────────────────────
    cv_path_src = os.path.join(os.path.dirname(npz_path), "cv_splits.json")
    if os.path.exists(cv_path_src):
        with open(cv_path_src) as f:
            cv_data = json.load(f)
        with open(os.path.join(output_dir, "cv_splits.json"), "w", encoding="utf-8") as f:
            json.dump(cv_data, f, indent=2)
        print("  Copied: cv_splits.json")

    return meta_out


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Augment 41-dim BTC H1 NPZ → 48-dim")
    ap.add_argument(
        "--npz-dir",
        default="data_btc/training/btc_swing_h1_retrain",
        help="Directory containing train.npz and meta.json (41-dim)",
    )
    ap.add_argument(
        "--csv",
        default="data/raw/btcusdc_h1_merged.csv",
        help="BTC H1 OHLC CSV path",
    )
    ap.add_argument(
        "--output-dir",
        default="data_btc/training/btc_swing_h1_retrain_48",
        help="Output directory for 48-dim NPZ",
    )
    ap.add_argument(
        "--from-mt5",
        action="store_true",
        help="Fetch H1 data from MT5 instead of CSV (covers full NPZ range)",
    )
    args = ap.parse_args()

    if args.from_mt5:
        # ── MT5 path: fetch fresh H1 bars ────────────────────────────────
        import MetaTrader5 as mt5
        import pandas as pd

        print("[H1-Augment] Connecting to MT5...")
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

        sym = "BTCUSDc"
        # Fetch from well before NPZ start to cover lookback
        from datetime import timezone

        start_dt = datetime(2020, 9, 1, tzinfo=UTC)
        end_dt = datetime(2026, 7, 7, tzinfo=UTC)
        print(f"[H1-Augment] Fetching {sym} H1 from {start_dt} to {end_dt}...")
        rates = mt5.copy_rates_range(sym, mt5.TIMEFRAME_H1, start_dt, end_dt)
        mt5.shutdown()

        if rates is None or len(rates) == 0:
            raise RuntimeError("MT5 returned 0 bars")

        print(f"  {len(rates):,} bars fetched")

        # Build arrays from MT5 rates
        # rate columns: time, open, high, low, close, tick_volume, spread, real_volume
        mt5_ts = np.array([r[0] for r in rates], dtype=np.float64)
        mt5_closes = np.array([r[4] for r in rates], dtype=np.float64)

        # Use the same augmentation logic but with MT5 arrays
        npz_path = os.path.join(args.npz_dir, "train.npz")
        meta_path = os.path.join(args.npz_dir, "meta.json")

        src = np.load(npz_path, allow_pickle=False)
        X_41 = src["X"]
        y = src["y"]
        ts_npz = src["timestamps"]
        pnl_r = src.get("pnl_r", np.zeros(len(y)))
        sample_weight = src.get("sample_weight", np.ones(len(y)))
        n_samples = len(ts_npz)
        print(f"  NPZ: {n_samples:,} samples")

        # Map timestamps
        csv_idx_for_npz = np.zeros(n_samples, dtype=np.int32)
        n_matched = 0
        for i in range(n_samples):
            t = ts_npz[i]
            idx = int(np.searchsorted(mt5_ts, t))
            if idx >= len(mt5_ts):
                csv_idx_for_npz[i] = -1
            elif idx > 0 and abs(mt5_ts[idx - 1] - t) < 1.0:
                csv_idx_for_npz[i] = idx - 1
                n_matched += 1
            elif idx < len(mt5_ts) and abs(mt5_ts[idx] - t) < 1.0:
                csv_idx_for_npz[i] = idx
                n_matched += 1
            else:
                csv_idx_for_npz[i] = -1
        print(f"  Timestamps: {n_matched:,} matched out of {n_samples:,}")

        # Compute H1 features
        X_h1 = np.zeros((n_samples, 7), dtype=np.float32)
        n_computed = 0
        n_missing = 0
        for i in range(n_samples):
            csv_idx = csv_idx_for_npz[i]
            if csv_idx < 49:
                n_missing += 1
                continue
            feats = compute_h1_features_from_h1_bars(mt5_closes, csv_idx)
            for j, name in enumerate(H1_DIRECTIONAL_FEATURE_NAMES):
                X_h1[i, j] = float(feats.get(name, 0.0))
            n_computed += 1
            if (i + 1) % 10000 == 0:
                print(f"  ... {i + 1}/{n_samples}")
        print(f"  Computed: {n_computed:,}, missing: {n_missing:,}")

        # Concatenate
        X_48 = np.concatenate([X_41, X_h1], axis=1)
        print(f"  Final: X={X_48.shape}")

        # Save
        os.makedirs(args.output_dir, exist_ok=True)
        np.savez_compressed(
            os.path.join(args.output_dir, "train.npz"),
            X=X_48,
            y=y,
            pnl_r=pnl_r,
            sample_weight=sample_weight,
            timestamps=ts_npz,
        )
        print(f"  Saved: {os.path.join(args.output_dir, 'train.npz')}")

        # Meta
        base_names = _load_base_feature_names(meta_path)
        all_48 = base_names + H1_DIRECTIONAL_FEATURE_NAMES
        with open(meta_path) as f:
            orig_meta = json.load(f)
        meta_out: dict[str, Any] = {
            "schema_version": "btc_swing_v9.v2",
            "feature_names": all_48,
            "n_features": 48,
            "n_samples": int(n_samples),
            "built_at": datetime.now(UTC).isoformat(),
            "_augmented_from": str(npz_path),
            "_augmented_features": H1_DIRECTIONAL_FEATURE_NAMES,
            "_data_source": "MT5 BTCUSDc H1",
            "_note": "DQAF-20260707-003: 41 base + 7 H1 directional momentum features (MT5-sourced)",
        }
        for key in (
            "n_short",
            "n_neutral",
            "n_long",
            "horizon",
            "sl_atr_mult",
            "tp_atr_mult",
            "spread_points",
            "slippage_points",
            "tick_value",
            "decay_half_life_days",
            "cv_folds",
            "purge_bars",
            "ev_r",
            "objective",
            "num_class",
        ):
            if key in orig_meta:
                meta_out[key] = orig_meta[key]
        with open(os.path.join(args.output_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta_out, f, indent=2, ensure_ascii=False)

        # Copy CV splits
        cv_src = os.path.join(args.npz_dir, "cv_splits.json")
        if os.path.exists(cv_src):
            with open(cv_src) as f:
                cv_data = json.load(f)
            with open(os.path.join(args.output_dir, "cv_splits.json"), "w", encoding="utf-8") as f:
                json.dump(cv_data, f, indent=2)

        print("[H1-Augment] Done.")

    else:
        # ── CSV path ──────────────────────────────────────────────────────
        meta = augment_npz(
            npz_path=os.path.join(args.npz_dir, "train.npz"),
            csv_path=args.csv,
            output_dir=args.output_dir,
            meta_path=os.path.join(args.npz_dir, "meta.json"),
        )
        print(f"[H1-Augment] Done. {meta['n_samples']:,} samples, {meta['n_features']}-dim")
