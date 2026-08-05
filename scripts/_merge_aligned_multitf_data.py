"""
Merge BTC + Cross-asset MT5 data with ffill alignment + timestamp assertions.

Guardrails:
  1. BTC timestamps as absolute backbone
  2. Cross-asset gaps: forward-fill with max limit=3 (15 min at M5)
  3. Timestamp alignment assertions — no look-ahead bias
  4. Zero-fill blackhole detection

Iron Law #11: stdout = only legal evidence source.

Usage:
  python scripts/_merge_aligned_multitf_data.py --output-dir data/training/aligned_btc_multitf
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = ROOT / "data" / "raw"


def load_csv(path: Path) -> pd.DataFrame:
    """Load MT5 CSV with proper datetime parsing."""
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time").sort_index()
    return df


def align_cross_asset_to_btc(
    btc_df: pd.DataFrame,
    cross_df: pd.DataFrame,
    symbol: str,
    max_ffill: int = 3,
) -> pd.DataFrame:
    """
    Align cross-asset data to BTC timestamps using forward-fill.

    Args:
        btc_df: BTC data with DatetimeIndex
        cross_df: Cross-asset data with DatetimeIndex
        symbol: Cross-asset symbol name (for reporting)
        max_ffill: Max consecutive forward-fill bars (limit=3 = 15 min at M5)

    Returns:
        DataFrame aligned to BTC index with cross-asset columns
    """
    btc_idx = btc_df.index

    # Reindex cross-asset to BTC timestamps
    aligned = cross_df.reindex(btc_idx, method=None)

    # Detect gaps
    null_mask = aligned["close"].isna()
    total_gaps = null_mask.sum()
    total_bars = len(aligned)

    # Forward-fill with limit
    aligned = aligned.ffill(limit=max_ffill)

    # After ffill, count remaining NaNs (gaps exceeding limit)
    remaining_null = aligned["close"].isna().sum()
    exceeded_gaps = remaining_null
    filled_gaps = total_gaps - exceeded_gaps

    if total_gaps > 0:
        print(f"  [{symbol}] Gaps: {total_gaps}/{total_bars} ({total_gaps/total_bars*100:.1f}%)")
        print(f"    Filled (ffill limit={max_ffill}): {filled_gaps}")
        print(f"    Exceeded limit (marked NaN):  {exceeded_gaps}")

    # Rename columns with symbol prefix
    col_map = {}
    for col in aligned.columns:
        if col in ("time",):
            continue
        if symbol.lower() == "btcusdc":
            col_map[col] = col  # BTC keeps original names
        else:
            col_map[col] = f"{symbol}_{col}"
    aligned = aligned.rename(columns=col_map)

    return aligned


def compute_cross_asset_returns(df: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    """Compute per-bar returns for each cross-asset symbol."""
    for sym in symbols:
        close_col = f"{sym}_close"
        if close_col in df.columns:
            df[f"{sym}_return"] = df[close_col].pct_change()
    return df


def detect_zero_fill_blackhole(
    df: pd.DataFrame, cross_cols: list[str], window_start: str, window_end: str
) -> dict:
    """
    Detect zero-fill blackhole: proportion of zero values in cross-asset
    return columns within the training window.

    Zero cross-asset returns = model trained on dead features.
    """
    mask = (df.index >= window_start) & (df.index <= window_end)
    window = df.loc[mask] if mask.any() else df

    report = {}
    for col in cross_cols:
        if col not in window.columns:
            report[col] = {"zero_pct": 1.0, "status": "MISSING"}
            continue
        series = window[col].dropna()
        if len(series) == 0:
            report[col] = {"zero_pct": 1.0, "status": "ALL_NAN"}
            continue
        zero_pct = (series == 0.0).sum() / len(series)
        status = "OK" if zero_pct < 0.10 else ("WARN" if zero_pct < 0.30 else "BLACKHOLE")
        report[col] = {"zero_pct": round(zero_pct, 4), "n": len(series), "status": status}

    return report


def main():
    parser = argparse.ArgumentParser(
        description="Merge BTC + Cross-asset data with ffill alignment"
    )
    parser.add_argument("--output-dir", default="data/training/aligned_btc_multitf")
    parser.add_argument(
        "--max-ffill", type=int, default=3, help="Max ffill bars (default: 3 = 15 min)"
    )
    parser.add_argument("--btc-tf", default="M5", help="BTC timeframe to use as backbone")
    args = parser.parse_args()

    output_dir = ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    btc_tf = args.btc_tf.lower()
    max_ffill = args.max_ffill

    print("=" * 80)
    print("Phase 0: BTC + Cross-Asset Data Alignment")
    print(f"  Backbone: BTC {btc_tf}")
    print(f"  Max ffill: {max_ffill} bars")
    print(f"  Time (UTC): {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    # ═══════════════════════════════════════════════════════════════
    # 1. Load BTC backbone
    # ═══════════════════════════════════════════════════════════════
    btc_path = DATA_RAW / f"btcusdc_{btc_tf}_merged.csv"
    if not btc_path.exists():
        print(f"[FATAL] BTC backbone not found: {btc_path}")
        sys.exit(1)

    print(f"\n[1] Loading BTC backbone: {btc_path}")
    btc_df = load_csv(btc_path)
    print(f"    {len(btc_df):,} bars, {btc_df.index[0]} → {btc_df.index[-1]}")

    # ═══════════════════════════════════════════════════════════════
    # 2. Load and align cross-asset data
    # ═══════════════════════════════════════════════════════════════
    cross_symbols = ["XAUUSDc", "XAGUSDc", "AUDJPYc", "EURUSDc", "USDJPYc"]
    cross_dfs = {}
    return_cols = []

    print("\n[2] Aligning cross-asset data to BTC timestamps...")

    all_aligned = [btc_df.add_prefix("BTC_")] if False else [btc_df]
    # Use raw BTC columns without prefix
    btc_cols = list(btc_df.columns)

    for sym in cross_symbols:
        cross_path = DATA_RAW / f"{sym.lower()}_{btc_tf}_merged.csv"
        if not cross_path.exists():
            print(f"    [{sym}] SKIP: file not found")
            continue

        cross_df = load_csv(cross_path)
        print(f"    [{sym}] {len(cross_df):,} bars, {cross_df.index[0]} → {cross_df.index[-1]}")

        aligned = align_cross_asset_to_btc(btc_df, cross_df, sym, max_ffill=max_ffill)
        cross_dfs[sym] = aligned

        if f"{sym}_return" in aligned.columns:
            return_cols.append(f"{sym}_return")

    # ═══════════════════════════════════════════════════════════════
    # 3. Merge all into one DataFrame
    # ═══════════════════════════════════════════════════════════════
    print("\n[3] Merging all aligned data...")
    merged = btc_df.copy()

    for _sym, adf in cross_dfs.items():
        # Only take columns not already in merged
        new_cols = [c for c in adf.columns if c not in merged.columns]
        merged = merged.join(adf[new_cols], how="left")

    # Compute cross-asset returns
    for sym in cross_symbols:
        close_col = f"{sym}_close" if sym in [c.split("_")[0] for c in merged.columns] else None
        # Find the close column
        for col in merged.columns:
            if col.startswith(f"{sym}_") and "close" in col.lower():
                merged[f"{sym}_return"] = merged[col].pct_change()
                return_cols.append(f"{sym}_return")
                break

    # ═══════════════════════════════════════════════════════════════
    # 4. Timestamp integrity assertions
    # ═══════════════════════════════════════════════════════════════
    print("\n[4] Timestamp integrity assertions...")

    # Assertion 1: No duplicate timestamps
    dupes = merged.index.duplicated().sum()
    if dupes > 0:
        print(f"    [FAIL] {dupes} duplicate timestamps found")
        merged = merged[~merged.index.duplicated(keep="first")]
        print(f"    [FIX] Dropped duplicates, {len(merged)} rows remain")
    else:
        print("    [PASS] No duplicate timestamps")

    # Assertion 2: Monotonic increasing
    if not merged.index.is_monotonic_increasing:
        print("    [FAIL] Index not monotonic — sorting...")
        merged = merged.sort_index()
    print("    [PASS] Index is monotonic increasing")

    # Assertion 3: No future timestamps
    now_utc = pd.Timestamp.now(tz="UTC").tz_localize(None)
    future_bars = (merged.index > now_utc).sum()
    if future_bars > 0:
        print(f"    [WARN] {future_bars} bars with future timestamps (clock skew?)")
    else:
        print("    [PASS] No future timestamps")

    # Assertion 4: Typical M5 gap detection (> 10 min = data gap)
    time_deltas = merged.index.to_series().diff()
    typical_gap = pd.Timedelta(minutes=5)
    large_gaps = time_deltas[time_deltas > typical_gap * 2]
    if len(large_gaps) > 0:
        print(f"    [WARN] {len(large_gaps)} gaps > 10 min detected:")
        for ts, delta in large_gaps.head(5).items():
            print(f"      {ts}: {delta}")
    else:
        print("    [PASS] No time gaps > 10 min")

    # ═══════════════════════════════════════════════════════════════
    # 5. Zero-fill blackhole detection
    # ═══════════════════════════════════════════════════════════════
    print("\n[5] Zero-fill blackhole detection...")

    # Find the intersection period (where BTC AND cross-asset data exist)
    btc_start = btc_df.index[0]
    btc_end = btc_df.index[-1]

    # Earliest cross-asset start, latest cross-asset end
    cross_starts = []
    cross_ends = []
    for sym, adf in cross_dfs.items():
        valid_mask = (
            adf[f"{sym}_close"].notna()
            if f"{sym}_close" in adf.columns
            else pd.Series(False, index=adf.index)
        )
        if valid_mask.any():
            cross_starts.append(adf.index[valid_mask].min())
            cross_ends.append(adf.index[valid_mask].max())

    if cross_starts and cross_ends:
        intersect_start = max(btc_start, max(cross_starts))
        intersect_end = min(btc_end, min(cross_ends))
        print(f"    BTC window:      {btc_start} → {btc_end}")
        print(f"    Cross-asset max: {max(cross_starts)} → {min(cross_ends)}")
        print(f"    Intersection:    {intersect_start} → {intersect_end}")
        intersect_bars = len(merged.loc[intersect_start:intersect_end])
        print(f"    Usable bars:     {intersect_bars:,}")

    # Zero-fill check
    cross_return_cols = [
        c for c in merged.columns if c.endswith("_return") and not c.startswith("BTC")
    ]
    if cross_return_cols:
        blackhole_report = detect_zero_fill_blackhole(
            merged, cross_return_cols, str(btc_start), str(btc_end)
        )
        print("\n    Cross-asset return zero-fill report:")
        all_ok = True
        for col, info in sorted(blackhole_report.items()):
            flag = (
                "  !! BLACKHOLE"
                if info["status"] == "BLACKHOLE"
                else "  WARN"
                if info["status"] == "WARN"
                else "  OK"
            )
            print(f"    {flag}: {col}: {info['zero_pct']*100:.1f}% zero (n={info.get('n','?')})")
            if info["status"] != "OK":
                all_ok = False

        if not all_ok:
            print("\n    [WARN] Cross-asset features have zero-fill blackhole.")
            print("    This means training data has dead features → train-serve skew.")
            print("    Fix: pull full cross-asset history from MT5.")

    # ═══════════════════════════════════════════════════════════════
    # 6. Save merged data
    # ═══════════════════════════════════════════════════════════════
    print("\n[6] Saving merged data...")

    # Drop rows with NaN in BTC close (shouldn't happen)
    merged = merged.dropna(subset=["close"])

    # Save CSV
    csv_path = output_dir / f"btc_{btc_tf}_aligned_multitf.csv"
    merged.to_csv(csv_path)
    print(f"    CSV: {csv_path} ({len(merged):,} rows × {len(merged.columns)} cols)")

    # Save metadata
    meta = {
        "schema_version": "aligned_multitf.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "backbone": f"BTCUSDc_{btc_tf}",
        "btc_window": [str(btc_start), str(btc_end)],
        "cross_assets": list(cross_dfs.keys()),
        "max_ffill": max_ffill,
        "total_bars": len(merged),
        "columns": list(merged.columns),
        "zero_fill_report": {col: info["zero_pct"] for col, info in blackhole_report.items()}
        if cross_return_cols
        else {},
    }
    meta_path = output_dir / "merge_meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2, default=str)
    print(f"    Meta: {meta_path}")

    print(f"\n[Phase 0 COMPLETE] Merged data ready at {output_dir}")
    print("  Next: python scripts/training/build_btc_dataset_from_ssot.py")

    return merged, meta


if __name__ == "__main__":
    main()
