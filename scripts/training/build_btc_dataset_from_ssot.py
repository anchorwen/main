"""
BTC SSOT Dataset Builder — feature half via the SHARED pure assembly.

Phase 1 / M1 (FIX-20260803-XXX, BTC 机构级训练管线重建 — 战役一):
    Historical features are now produced by ``core/training/feature_replay.py``
    flowing through the SAME pure 41-dim assembly as live inference
    (``core.features.computers.btc_feature_augmenter.assemble_41_series``).

    This REPLACES the feature half of build_btc_expected_r_dataset.py's
    hand-rolled 41-dim implementation (which had its own slot mapping, OU/Hurst
    computation, and regime-delta logic — a third divergent implementation).

    Labels (two-tower expected R / barrier) are wired in Phase 2 via
    ``core/contracts/training/label_contract.py`` (label_from_live_yaml.py SSOT).

Iron Law #11: stdout is the only legal evidence source.  All dataset statistics
printed here come from computation, never eyeballing.

Usage:
  python scripts/training/build_btc_dataset_from_ssot.py \
    --input data/training/aligned_btc_multitf/btc_m5_aligned_multitf.csv \
    --output-dir data_btc/training/btc_ssot_v2 \
    --schema btc_macro_enhanced_41_v2 --tf-minutes 5
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.training.feature_replay import MIN_WARMUP, replay_features  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Build BTC SSOT feature dataset")
    parser.add_argument("--input", required=True, help="Aligned multi-TF CSV path")
    parser.add_argument("--output-dir", required=True, help="Output NPZ directory")
    parser.add_argument(
        "--schema",
        default="btc_macro_enhanced_41_v2",
        help="Feature schema (default: btc_macro_enhanced_41_v2 — no legacy shim)",
    )
    parser.add_argument("--tf-minutes", type=float, default=5.0, help="Bar timeframe in minutes")
    parser.add_argument(
        "--warmup-bars",
        type=int,
        default=MIN_WARMUP,
        help="Drop first N bars (unconverged features)",
    )
    parser.add_argument("--train-ratio", type=float, default=0.70)
    parser.add_argument("--val-ratio", type=float, default=0.15)
    parser.add_argument("--purge-bars", type=int, default=0, help="Purge gap at split boundaries")
    args = parser.parse_args()

    input_path = PROJECT_ROOT / args.input
    output_dir = PROJECT_ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("BTC SSOT Dataset Builder (M1 / FIX-20260803-XXX)")
    print(f"  Input:    {input_path}")
    print(f"  Output:   {output_dir}")
    print(f"  Schema:   {args.schema}")
    print(f"  TF:       {args.tf_minutes:.0f} min")
    print(f"  Time:     {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 80)

    # ── Load aligned data ──
    print("\n[1] Loading aligned data...")
    df = pd.read_csv(input_path)
    df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time").sort_index()
    print(f"    {len(df):,} bars: {df.index[0]} → {df.index[-1]}")

    # ── Replay features via SHARED assembly (SSOT) ──
    print(f"\n[2] Replay features (shared pure assembly, schema={args.schema})...")
    X, replay_meta = replay_features(df, tf_minutes=args.tf_minutes, schema_name=args.schema)
    timestamps = df.index.astype(np.int64).values // 10**9  # ns → unix seconds
    timestamps = timestamps.astype(np.float64)
    print(f"    Feature matrix: {X.shape}  (NaN: {int(np.isnan(X).sum())})")
    print(f"    micro_zeros_frac: {replay_meta['micro_zeros_frac']}")

    # ── Warmup drop ──
    print(f"\n[3] Warmup drop (first {args.warmup_bars} bars)...")
    n_before = len(X)
    X = X[args.warmup_bars :]
    timestamps = timestamps[args.warmup_bars :]
    print(f"    {n_before:,} → {len(X):,}")

    # ── Time-based split (no look-ahead) + purge gap ──
    print("\n[4] Creating time-based splits...")
    n_total = len(X)
    train_end = int(n_total * args.train_ratio)
    val_end = int(n_total * (args.train_ratio + args.val_ratio))
    purge = args.purge_bars

    splits = {
        "train": (0, train_end - purge),
        "val": (train_end, val_end - purge),
        "test": (val_end, n_total),
    }
    for name, (lo, hi) in splits.items():
        if hi <= lo:
            print(f"    WARNING: split '{name}' empty ({lo}:{hi}) — check ratios")
    print(f"    Train: {splits['train'][1] - splits['train'][0]:,}")
    print(f"    Val:   {splits['val'][1] - splits['val'][0]:,}")
    print(f"    Test:  {splits['test'][1] - splits['test'][0]:,}")

    # ── Save ──
    print("\n[5] Saving NPZ datasets...")
    for name, (lo, hi) in splits.items():
        _X = X[lo:hi]
        _ts = timestamps[lo:hi]
        np.savez_compressed(
            output_dir / f"{name}.npz",
            X=_X,
            timestamps=_ts,
            feature_names=np.array(replay_meta["feature_names"], dtype=object),
            schema_id=np.array([args.schema], dtype=object),
        )
        print(f"    {name}.npz: {_X.shape}")

    meta = {
        "builder": "build_btc_dataset_from_ssot.py",
        "fix_id": "FIX-20260803-XXX",
        "schema_version": "ssot.v2",
        "created_at": datetime.now(UTC).isoformat(),
        "input_file": str(input_path),
        "schema_id": args.schema,
        "feature_names": replay_meta["feature_names"],
        "n_features": X.shape[1],
        "tf_minutes": args.tf_minutes,
        "warmup_bars": args.warmup_bars,
        "purge_bars": args.purge_bars,
        "train_ratio": args.train_ratio,
        "val_ratio": args.val_ratio,
        "split_boundaries": {k: list(v) for k, v in splits.items()},
        "n_train": splits["train"][1] - splits["train"][0],
        "n_val": splits["val"][1] - splits["val"][0],
        "n_test": splits["test"][1] - splits["test"][0],
        "micro_zeros_frac": replay_meta["micro_zeros_frac"],
        "min_warmup": replay_meta["min_warmup"],
        "assembly": replay_meta["assembly"],
        "labels": "Phase 2 — LabelContract (label_from_live_yaml.py SSOT)",
    }
    with open(output_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"    {output_dir}/meta.json")

    print("\n[M1 COMPLETE] SSOT feature dataset ready.")
    print("  Next (Phase 2): labels via LabelContract + validate_label_vs_live.py")


if __name__ == "__main__":
    main()
