"""Micro Barrier Training Dataset Builder v2 — Institutional-grade data engine.

Architecture:
  1. Multi-source merge_asof (XAU + EUR + JPY + XAG) — backward-looking, no peeking
  2. Vectorized feature computation — no for-loops
  3. Rolling-window standardization (1000-bar lookback) — zero future-data leakage
  4. sliding_window_view — zero-copy sequence tensor generation
  5. 288-dim flatten for XGBoost — full temporal information, not just last bar

Usage:
  python scripts/training/build_micro_barrier_dataset.py \
    --xau-data data/raw/xauusdc_m5_merged.csv \
    --eur-data data/raw/eurusdc_m5_merged.csv \
    --jpy-data data/raw/usdjpyc_m5_merged.csv \
    --xag-data data/raw/xagusdc_m5_merged.csv \
    --labels data/labels/micro_barrier_labels.jsonl \
    --output-dir data/training/micro_barrier_v2
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

SEQ_LEN = 32
NUM_FEATURES = 9
ROLLING_WINDOW = 1000

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


def _parse_iso(ts: str) -> float:
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).timestamp()


def load_barrier_labels(labels_path: Path) -> dict[float, list[dict[str, Any]]]:
    """Load barrier labels, indexed by entry_time Unix timestamp."""
    index: dict[float, list[dict[str, Any]]] = {}
    with labels_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            entry_time = rec.get("entry_time", "")
            if not entry_time:
                continue
            ts = _parse_iso(entry_time)
            index.setdefault(ts, []).append(rec)
    print(
        f"[v2] Loaded {sum(len(v) for v in index.values())} barrier labels across {len(index)} entry times"
    )
    return index


def build_production_dataset(
    xau_path: Path,
    eur_path: Path,
    jpy_path: Path,
    xag_path: Path,
    labels_path: Path,
    val_ratio: float = 0.2,
) -> dict[str, Any]:
    """Full production pipeline."""

    # ═══════════════════════════════════════════════════════════════════
    # Step 1: Ingest & merge_asof (backward-looking, no peeking)
    # ═══════════════════════════════════════════════════════════════════
    print("[v2] Step 1: Ingest & merge_asof multi-source alignment...")

    df_xau = pd.read_csv(xau_path, parse_dates=["time"]).sort_values("time")
    df_eur = pd.read_csv(eur_path, parse_dates=["time"]).sort_values("time")
    df_jpy = pd.read_csv(jpy_path, parse_dates=["time"]).sort_values("time")
    df_xag = pd.read_csv(xag_path, parse_dates=["time"]).sort_values("time")

    # merge_asof: direction='backward' — never peek into future bars
    df = pd.merge_asof(
        df_xau, df_eur[["time", "close"]], on="time", direction="backward", suffixes=("", "_eur")
    )
    df = pd.merge_asof(
        df, df_jpy[["time", "close"]], on="time", direction="backward", suffixes=("", "_jpy")
    )
    df = pd.merge_asof(
        df, df_xag[["time", "close"]], on="time", direction="backward", suffixes=("", "_xag")
    )
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    print(
        f"  Merged {len(df)} aligned bars from {len(df_xau)} XAU + {len(df_eur)} EUR + {len(df_jpy)} JPY + {len(df_xag)} XAG"
    )

    # ═══════════════════════════════════════════════════════════════════
    # Step 2: Vectorized feature computation
    # ═══════════════════════════════════════════════════════════════════
    print("[v2] Step 2: Vectorized feature computation...")

    # XAUUSD micro-market features
    df["tick_return"] = df["close"].pct_change() * 100.0
    df["hl_ratio"] = (df["high"] - df["low"]) / (df["close"].clip(lower=1e-9))
    df["co_ratio"] = df["close"] / (df["open"].clip(lower=1e-9))
    df["avg_spread"] = df["spread"] / (df["close"].clip(lower=1e-9))

    # OIM: order imbalance proxy (avoid div by zero)
    hl_diff = df["high"] - df["low"]
    df["OIM"] = np.where(hl_diff > 1e-12, (df["close"] - df["open"]) / hl_diff, 0.0)

    # tick_velocity: normalized tick volume
    df["tick_velocity"] = df["tick_volume"] / 1000.0

    # Cross-asset returns (real data, not zeros)
    df["XAGUSDc_return"] = df["close_xag"].pct_change() * 100.0
    df["EURUSDc_return"] = df["close_eur"].pct_change() * 100.0
    df["USDJPYc_return"] = df["close_jpy"].pct_change() * 100.0

    # Drop rows where feature computation produced NaN (first row pct_change)
    df.dropna(subset=FEATURE_NAMES, inplace=True)
    df.reset_index(drop=True, inplace=True)
    print(f"  {len(df)} bars after NaN drop")

    # ═══════════════════════════════════════════════════════════════════
    # Step 3: Rolling-window standardization (no future leakage)
    # ═══════════════════════════════════════════════════════════════════
    print(
        f"[v2] Step 3: Rolling standardization (window={ROLLING_WINDOW}, min_periods={SEQ_LEN})..."
    )

    rolling_mean = df[FEATURE_NAMES].rolling(window=ROLLING_WINDOW, min_periods=SEQ_LEN).mean()
    rolling_std = df[FEATURE_NAMES].rolling(window=ROLLING_WINDOW, min_periods=SEQ_LEN).std()
    rolling_std = rolling_std.replace(0.0, 1.0)

    df_scaled = (df[FEATURE_NAMES] - rolling_mean) / rolling_std
    df_scaled.bfill(inplace=True)  # fill initial NaN period
    scaled_feats = df_scaled.values.astype(np.float32)

    # Store mean/std of the last rolling window for inference-time standardization
    last_mean = rolling_mean.iloc[-1].values.astype(np.float32)
    last_std = rolling_std.iloc[-1].values.astype(np.float32)
    print(f"  Standardized. Last window mean range: [{last_mean.min():.4f}, {last_mean.max():.4f}]")
    print(f"  Last window std range: [{last_std.min():.4f}, {last_std.max():.4f}]")

    # ═══════════════════════════════════════════════════════════════════
    # Step 4: sliding_window_view — zero-copy sequence tensor
    # ═══════════════════════════════════════════════════════════════════
    print("[v2] Step 4: sliding_window_view zero-copy sequence generation...")

    X_all_seq = sliding_window_view(scaled_feats, window_shape=(SEQ_LEN, NUM_FEATURES))
    X_all_seq = X_all_seq.squeeze(axis=1)  # (N-SEQ_LEN+1, SEQ_LEN, NUM_FEATURES)

    # Valid timestamps: each window anchored to its last bar
    valid_timestamps = df["time"].iloc[SEQ_LEN - 1 :].values
    print(f"  Generated {X_all_seq.shape[0]} sequences ({X_all_seq.shape[1]}x{X_all_seq.shape[2]})")

    # ═══════════════════════════════════════════════════════════════════
    # Step 5: Label anchoring + XGBoost 288-dim flatten
    # ═══════════════════════════════════════════════════════════════════
    print("[v2] Step 5: Label anchoring + XGBoost flatten...")

    label_dict = load_barrier_labels(labels_path)

    X_seq_list: list[np.ndarray] = []
    X_flat_list: list[np.ndarray] = []
    y_list: list[int] = []

    for idx in range(len(valid_timestamps)):
        ts = pd.Timestamp(valid_timestamps[idx]).timestamp()
        if ts not in label_dict:
            continue
        labels = label_dict[ts]
        for lab in labels:
            y_list.append(int(lab.get("label_int", 0)))

            seq = X_all_seq[idx]  # (SEQ_LEN, NUM_FEATURES)
            X_seq_list.append(seq)

            # Flatten 32x9=288 dims — XGBoost gets full temporal picture
            X_flat_list.append(seq.flatten())

    X_seq = np.stack(X_seq_list, axis=0).astype(np.float32)
    X_flat = np.stack(X_flat_list, axis=0).astype(np.float32)
    y = np.array(y_list, dtype=np.int32)

    total = len(y)
    tp_count = int((y == 1).sum())
    sl_count = int((y == -1).sum())
    timeout_count = int((y == 0).sum())

    print(f"  Matched {total} samples:")
    print(f"    tp_hit: {tp_count} ({100*tp_count/total:.1f}%)")
    print(f"    sl_hit: {sl_count} ({100*sl_count/total:.1f}%)")
    print(f"    timeout: {timeout_count} ({100*timeout_count/total:.1f}%)")
    print(f"  X_seq shape: {X_seq.shape} (Transformer)")
    print(f"  X_flat shape: {X_flat.shape} (XGBoost 288-dim)")

    # ═══════════════════════════════════════════════════════════════════
    # Step 6: Temporal split + export
    # ═══════════════════════════════════════════════════════════════════
    n = len(X_seq)
    n_val = int(n * val_ratio)
    n_train = n - n_val

    X_seq_train, X_seq_val = X_seq[:n_train], X_seq[n_train:]
    X_flat_train, X_flat_val = X_flat[:n_train], X_flat[n_train:]
    y_train, y_val = y[:n_train], y[n_train:]

    print(f"\n[v2] Step 6: Temporal split — train={n_train}, val={n_val}")

    return {
        "X_seq_train": X_seq_train,
        "X_seq_val": X_seq_val,
        "X_flat_train": X_flat_train,
        "X_flat_val": X_flat_val,
        "y_train": y_train,
        "y_val": y_val,
        "feat_mean": last_mean,
        "feat_std": last_std,
        "train_samples": n_train,
        "val_samples": n_val,
    }


def export_npz(
    X_seq: np.ndarray,
    X_flat: np.ndarray,
    y: np.ndarray,
    output_path: Path,
    *,
    feat_mean: np.ndarray | None = None,
    feat_std: np.ndarray | None = None,
) -> Path:
    """Export combined NPZ with both sequence (Transformer) and flat (XGBoost) arrays."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        output_path,
        X=X_seq.astype(np.float32),
        X_flat=X_flat.astype(np.float32),
        y=y.astype(np.int32),
        seq_len=SEQ_LEN,
        num_features=NUM_FEATURES,
        feature_names=np.array(FEATURE_NAMES, dtype=str),
        feat_mean=feat_mean if feat_mean is not None else np.zeros(NUM_FEATURES, dtype=np.float32),
        feat_std=feat_std if feat_std is not None else np.ones(NUM_FEATURES, dtype=np.float32),
    )
    size_kb = output_path.stat().st_size / 1024
    print(f"[v2] Exported {output_path} ({size_kb:.1f} KB)")
    return output_path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="build_micro_barrier_dataset")
    p.add_argument("--xau-data", type=Path, required=True, help="XAUUSDc M5 OHLC CSV")
    p.add_argument("--eur-data", type=Path, required=True, help="EURUSDc M5 OHLC CSV")
    p.add_argument("--jpy-data", type=Path, required=True, help="USDJPYc M5 OHLC CSV")
    p.add_argument("--xag-data", type=Path, required=True, help="XAGUSDc M5 OHLC CSV")
    p.add_argument("--labels", type=Path, required=True, help="micro_barrier_labels.jsonl")
    p.add_argument("--output-dir", type=Path, default=Path("data/training/micro_barrier_v2"))
    p.add_argument("--val-ratio", type=float, default=0.2)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    for p in [args.xau_data, args.eur_data, args.jpy_data, args.xag_data, args.labels]:
        if not p.exists():
            print(f"[v2] ERROR: file not found: {p}")
            return 2

    result = build_production_dataset(
        xau_path=args.xau_data,
        eur_path=args.eur_data,
        jpy_path=args.jpy_data,
        xag_path=args.xag_data,
        labels_path=args.labels,
        val_ratio=args.val_ratio,
    )

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    export_npz(
        result["X_seq_train"],
        result["X_flat_train"],
        result["y_train"],
        out / "train.npz",
        feat_mean=result["feat_mean"],
        feat_std=result["feat_std"],
    )
    export_npz(
        result["X_seq_val"],
        result["X_flat_val"],
        result["y_val"],
        out / "val.npz",
        feat_mean=result["feat_mean"],
        feat_std=result["feat_std"],
    )

    print(
        json.dumps(
            {
                "dataset": "micro_barrier_v2",
                "train_samples": result["train_samples"],
                "val_samples": result["val_samples"],
                "seq_len": SEQ_LEN,
                "num_features": NUM_FEATURES,
                "xgb_dim": SEQ_LEN * NUM_FEATURES,
                "train_dist": {
                    "tp": int((result["y_train"] == 1).sum()),
                    "timeout": int((result["y_train"] == 0).sum()),
                    "sl": int((result["y_train"] == -1).sum()),
                },
                "val_dist": {
                    "tp": int((result["y_val"] == 1).sum()),
                    "timeout": int((result["y_val"] == 0).sum()),
                    "sl": int((result["y_val"] == -1).sum()),
                },
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
