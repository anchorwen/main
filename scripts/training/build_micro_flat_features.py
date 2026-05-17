"""Build flat (single-bar) microstructure features for V9 merge.

Unlike MicrostructureDatasetBuilder which produces (N, 32, 9) sequences,
this script computes per-bar 9-dim micro features and saves them as
(N, 9) with timestamps — ready for timestamp-aligned merge with V9 data.

Usage:
    python scripts/training/build_micro_flat_features.py \
        --output data/training/micro_features_flat.npz
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

FEATURE_NAMES = [
    "tick_return",
    "hl_ratio",
    "co_ratio",
    "avg_spread",
    "OIM",
    "tick_velocity",
    "XAGUSDc_return",
    "EURUSDc_return",
    "USDJPYc_return",
]


def _ingest_merge(csv_dir: Path) -> pd.DataFrame:
    """Read 4 M5 CSVs and merge_asof by timestamp (XAU as anchor)."""
    xau = pd.read_csv(csv_dir / "xauusdc_m5_merged.csv", parse_dates=["time"])
    eur = pd.read_csv(csv_dir / "eurusdc_m5_merged.csv", parse_dates=["time"])
    jpy = pd.read_csv(csv_dir / "usdjpyc_m5_merged.csv", parse_dates=["time"])
    xag = pd.read_csv(csv_dir / "xagusdc_m5_merged.csv", parse_dates=["time"])

    for df in [xau, eur, jpy, xag]:
        # Normalize timezone: XAU has naive datetime, EUR/JPY/XAG have UTC
        if pd.api.types.is_datetime64tz_dtype(df["time"]):
            df["time"] = df["time"].dt.tz_convert(None)
        else:
            df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(None)
        df.sort_values("time", inplace=True)
        df.reset_index(drop=True, inplace=True)

    df = pd.merge_asof(
        xau,
        eur[["time", "close"]],
        on="time",
        direction="backward",
        suffixes=("", "_eur"),
    )
    df = pd.merge_asof(
        df,
        jpy[["time", "close"]],
        on="time",
        direction="backward",
        suffixes=("", "_jpy"),
    )
    df = pd.merge_asof(
        df,
        xag[["time", "close"]],
        on="time",
        direction="backward",
        suffixes=("", "_xag"),
    )
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute 9 microstructure features per bar (same logic as builder)."""
    df = df.copy()
    close = df["close"].clip(lower=1e-9)
    df["tick_return"] = df["close"].pct_change() * 100.0
    df["hl_ratio"] = (df["high"] - df["low"]) / close
    df["co_ratio"] = df["close"] / df["open"].clip(lower=1e-9)
    df["avg_spread"] = df["spread"] / close
    hl_diff = df["high"] - df["low"]
    df["OIM"] = np.where(hl_diff > 1e-12, (df["close"] - df["open"]) / hl_diff, 0.0)
    df["tick_velocity"] = df["tick_volume"] / 1000.0
    df["XAGUSDc_return"] = df["close_xag"].pct_change() * 100.0
    df["EURUSDc_return"] = df["close_eur"].pct_change() * 100.0
    df["USDJPYc_return"] = df["close_jpy"].pct_change() * 100.0
    df.dropna(subset=FEATURE_NAMES, inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Build flat microstructure features NPZ")
    parser.add_argument("--csv-dir", default="data/raw", help="Directory with M5 CSVs")
    parser.add_argument(
        "--output", default="data/training/micro_features_flat.npz", help="Output NPZ"
    )
    args = parser.parse_args()

    csv_dir = Path(args.csv_dir)
    required = [
        "xauusdc_m5_merged.csv",
        "eurusdc_m5_merged.csv",
        "usdjpyc_m5_merged.csv",
        "xagusdc_m5_merged.csv",
    ]
    for f in required:
        if not (csv_dir / f).exists():
            print(f"ERROR: Missing {csv_dir / f}", file=sys.stderr)
            sys.exit(1)

    print("[micro_flat] Ingesting and merging 4 M5 CSVs...")
    df = _ingest_merge(csv_dir)
    print(f"[micro_flat] Merged: {len(df)} bars")

    print("[micro_flat] Computing 9 microstructure features...")
    df = compute_features(df)
    print(f"[micro_flat] After feature computation: {len(df)} bars")

    X_micro = df[FEATURE_NAMES].to_numpy(dtype=np.float32)
    timestamps = df["time"].apply(lambda t: t.timestamp()).to_numpy(dtype=np.float64)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        output_path,
        X=X_micro,
        timestamps=timestamps,
        feature_names=np.array(FEATURE_NAMES, dtype=str),
    )

    print(
        json.dumps(
            {
                "event": "micro_flat_built",
                "output": str(output_path),
                "samples": int(len(X_micro)),
                "features": len(FEATURE_NAMES),
                "feature_names": FEATURE_NAMES,
                "time_range": [float(timestamps[0]), float(timestamps[-1])],
            }
        )
    )


if __name__ == "__main__":
    main()
