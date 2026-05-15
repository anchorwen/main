#!/usr/bin/env python
"""Build Stage 1 regression dataset for two-stage meta-labeling.

Target: (Close[t+horizon] - Close[t]) / ATR[t] — forward return in ATR units.
Features: 40 v9_institutional features computed at bar t.

Stage 1 predicts continuous future returns. Direction = sign(pred), confidence = abs(pred).

Usage:
  python scripts/training/build_s1_regression_dataset.py \
    --price-data data/raw/xauusdc_m5_merged.csv \
    --output data/training/s1_regression_12bar_v1 \
    --horizon 12
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from scripts.training.build_calibrated_dataset import (
    compute_features_at_bar,
    load_ohlc_arrays,
)


def build_s1_dataset(
    ohlc: dict[str, np.ndarray],
    *,
    warmup_bars: int = 500,
    horizon: int = 12,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Build Stage 1 regression dataset from OHLC data.

    For each bar i in [warmup, n_bars - horizon), compute features at bar i
    and target = (Close[i+horizon] - Close[i]) / ATR[i].

    Returns (X, y, feature_names).
    """
    o = ohlc["open"]
    h = ohlc["high"]
    l = ohlc["low"]
    c = ohlc["close"]
    v = ohlc["volume"]
    n_bars = ohlc["n_bars"]

    feature_names: list[str] = []
    X_rows: list[list[float]] = []
    y_rows: list[float] = []

    end_idx = n_bars - horizon
    total_bars = end_idx - warmup_bars

    for i in range(warmup_bars, end_idx):
        if (i - warmup_bars) % 2000 == 0:
            pct = (i - warmup_bars) / max(total_bars, 1) * 100
            print(f"  ... {i - warmup_bars}/{total_bars} bars ({pct:.0f}%)")

        # Compute 40 features at bar i
        feat_dict = compute_features_at_bar(o, h, l, c, v, i)
        if not feature_names:
            feature_names = sorted(feat_dict.keys())

        feats = [float(feat_dict.get(fn, 0.0)) for fn in feature_names]
        X_rows.append(feats)

        # Target: forward return in ATR units
        atr_i = feat_dict.get("M5_ATR_14", 0.0)
        if atr_i < 1e-10:
            atr_i = 1e-6
        fwd_return = (c[i + horizon] - c[i]) / atr_i
        y_rows.append(fwd_return)

    X = np.array(X_rows, dtype=np.float64)
    y = np.array(y_rows, dtype=np.float64)
    return X, y, feature_names


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="build_s1_regression_dataset")
    p.add_argument("--price-data", type=Path, required=True, help="Path to M5 OHLC CSV")
    p.add_argument(
        "--output", type=Path, required=True, help="Output directory for train.npz / val.npz"
    )
    p.add_argument("--horizon", type=int, default=12, help="Forward horizon in bars (default: 12)")
    p.add_argument(
        "--warmup-bars", type=int, default=500, help="Skip first N bars for feature computation"
    )
    p.add_argument(
        "--val-split", type=float, default=0.2, help="Fraction for validation (chronological split)"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    print("[1/3] Loading OHLC data...")
    ohlc = load_ohlc_arrays(args.price_data)
    n_bars = ohlc["n_bars"]
    print(f"       {n_bars} bars loaded")

    available = n_bars - args.warmup_bars - args.horizon
    if available < 500:
        print(f"[ERROR] Only {available} usable bars after warmup and horizon — need >= 500")
        return 1
    print(f"       Usable bars: {available} (warmup={args.warmup_bars}, horizon={args.horizon})")

    print(f"[2/3] Building regression dataset (horizon={args.horizon})...")
    X, y, feature_names = build_s1_dataset(
        ohlc,
        warmup_bars=args.warmup_bars,
        horizon=args.horizon,
    )

    n_samples = len(X)
    print(f"       Samples: {n_samples}, Features: {len(feature_names)}")
    print(
        f"       Target: mean={y.mean():.4f}, std={y.std():.4f}, "
        f"min={y.min():.4f}, max={y.max():.4f}"
    )
    long_pct = (y > 0).mean()
    print(
        f"       Long: {long_pct:.1%}, Short: {(y < 0).mean():.1%}, "
        f"Flat: {((y == 0).mean()):.1%}"
    )

    print("[3/3] Splitting and saving...")
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    split_idx = int(n_samples * (1 - args.val_split))
    X_train, X_val = X[:split_idx], X[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]

    np.savez_compressed(
        out_dir / "train.npz",
        X=X_train,
        y=y_train,
        feature_names=np.array(feature_names),
        schema="s1_regression_v1",
    )
    np.savez_compressed(
        out_dir / "val.npz",
        X=X_val,
        y=y_val,
        feature_names=np.array(feature_names),
        schema="s1_regression_v1",
    )

    train_mean, train_std = y_train.mean(), y_train.std()
    val_mean, val_std = y_val.mean(), y_val.std()
    print(f"       Train: {len(X_train)} samples (mean={train_mean:.4f}, std={train_std:.4f})")
    print(f"       Val:   {len(X_val)} samples (mean={val_mean:.4f}, val_std={val_std:.4f})")
    print(f"       Saved to: {out_dir}")

    meta = {
        "schema_version": "s1_regression_dataset.v1",
        "n_samples": n_samples,
        "n_features": len(feature_names),
        "feature_names": feature_names,
        "train_samples": int(len(X_train)),
        "val_samples": int(len(X_val)),
        "target_mean": round(float(y.mean()), 6),
        "target_std": round(float(y.std()), 6),
        "horizon": args.horizon,
        "price_data": str(args.price_data),
        "n_bars": n_bars,
        "warmup_bars": args.warmup_bars,
    }
    with open(out_dir / "dataset_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
