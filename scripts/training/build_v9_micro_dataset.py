"""Build merged V9+Microstructure 49-dim training dataset.

Loads a pre-built V9 institutional NPZ (from dataset_builder.py) and a
microstructure NPZ (from builders/microstructure.py or
MicrostructureFeatureComputer), aligns them by timestamp, and outputs a
single merged NPZ with 49 feature columns.

CRITICAL: Missing micro data rows are DROPPED, not zero-imputed.
Zero has physical meaning for micro features (avg_spread=0 = impossible,
OIM=0 = perfect balance, tick_velocity=0 = no ticks).  Filling with zero
would teach models that "no data" = "calm market" — an Imputation Ghost.

Usage::

    python scripts/training/build_v9_micro_dataset.py \\
        --v9-npz data/training/barrier_labels.npz \\
        --micro-npz data/training/micro_features.npz \\
        --output data/training/v9_micro_49.npz
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build merged V9+Micro 49-dim training dataset")
    p.add_argument(
        "--v9-npz", required=True, help="Path to V9 institutional NPZ (from dataset_builder)"
    )
    p.add_argument("--micro-npz", required=True, help="Path to microstructure NPZ")
    p.add_argument("--output", required=True, help="Output NPZ path")
    p.add_argument(
        "--max-time-gap",
        type=float,
        default=5.0,
        help="Max seconds between V9 and micro timestamps for a row to be kept (default: 5s)",
    )
    p.add_argument(
        "--fit-scaler",
        action="store_true",
        help="Fit a StandardScaler on micro features and save as .micro_scaler.json",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    v9_path = Path(args.v9_npz)
    micro_path = Path(args.micro_npz)
    output_path = Path(args.output)

    if not v9_path.exists():
        print(f"ERROR: V9 NPZ not found: {v9_path}", file=sys.stderr)
        sys.exit(1)
    if not micro_path.exists():
        print(f"ERROR: Micro NPZ not found: {micro_path}", file=sys.stderr)
        sys.exit(1)

    # Load V9 data
    v9_data = np.load(v9_path, allow_pickle=True)
    X_v9 = v9_data["X"]  # (N, 40)
    v9_feature_names = list(v9_data.get("feature_names", []))
    v9_timestamps = v9_data.get("timestamps", None)
    y = v9_data.get("y", None)
    y_reg = v9_data.get("y_reg", None)
    pnl = v9_data.get("pnl", None)

    # Load micro data
    micro_data = np.load(micro_path, allow_pickle=True)
    X_micro = micro_data["X"]  # (M, 9)
    micro_timestamps = micro_data.get("timestamps", None)
    micro_feature_names = list(micro_data.get("feature_names", []))

    if X_v9.ndim != 2 or X_v9.shape[1] != 40:
        print(f"ERROR: Expected V9 X shape (N, 40), got {X_v9.shape}", file=sys.stderr)
        sys.exit(1)
    if X_micro.ndim != 2 or X_micro.shape[1] != 9:
        print(f"ERROR: Expected micro X shape (M, 9), got {X_micro.shape}", file=sys.stderr)
        sys.exit(1)

    # ── Align by timestamp ──
    n_v9 = len(X_v9)
    n_micro = len(X_micro)

    if v9_timestamps is not None and micro_timestamps is not None and n_micro > 1:
        v9_ts = np.asarray(v9_timestamps, dtype=np.float64)
        micro_ts = np.asarray(micro_timestamps, dtype=np.float64)

        # For each V9 row, find the closest PAST micro row (backward-only).
        # Using np.abs() would allow matching future micro data to past bars,
        # leaking future information into training (look-ahead bias).
        kept_v9_indices: list[int] = []
        kept_micro_indices: list[int] = []
        dropped_missing = 0
        future_leak_prevented = 0

        for i, ts in enumerate(v9_ts):
            valid_mask = micro_ts <= ts
            if not np.any(valid_mask):
                dropped_missing += 1
                continue
            diffs = ts - micro_ts[valid_mask]
            best_valid_idx = int(np.argmin(diffs))
            if diffs[best_valid_idx] <= args.max_time_gap:
                actual_j = int(np.where(valid_mask)[0][best_valid_idx])
                kept_v9_indices.append(i)
                kept_micro_indices.append(actual_j)
                # Detect whether the old np.abs() would have picked a future row
                abs_diffs = np.abs(micro_ts - ts)
                abs_best_j = int(np.argmin(abs_diffs))
                if micro_ts[abs_best_j] > ts:
                    future_leak_prevented += 1
            else:
                dropped_missing += 1

        if dropped_missing > 0 or future_leak_prevented > 0:
            print(
                json.dumps(
                    {
                        "event": "micro_alignment",
                        "v9_rows": n_v9,
                        "micro_rows": n_micro,
                        "kept": len(kept_v9_indices),
                        "dropped_missing_micro": dropped_missing,
                        "future_leak_prevented": future_leak_prevented,
                    }
                ),
                flush=True,
            )

        X_v9 = X_v9[kept_v9_indices]
        X_micro = X_micro[kept_micro_indices]
        if y is not None:
            y = y[kept_v9_indices]
        if y_reg is not None:
            y_reg = y_reg[kept_v9_indices]
        if pnl is not None:
            pnl = pnl[kept_v9_indices]

    elif n_v9 != n_micro:
        print(
            json.dumps(
                {
                    "event": "micro_alignment_warning",
                    "message": "No timestamps available, shapes differ — truncating to min",
                    "v9_rows": n_v9,
                    "micro_rows": n_micro,
                }
            ),
            flush=True,
        )
        min_rows = min(n_v9, n_micro)
        X_v9 = X_v9[:min_rows]
        X_micro = X_micro[:min_rows]
        if y is not None:
            y = y[:min_rows]
        if y_reg is not None:
            y_reg = y_reg[:min_rows]
        if pnl is not None:
            pnl = pnl[:min_rows]

    # ── Check for NaN in micro features (Imputation Ghosts Fix) ──
    nan_mask = np.any(np.isnan(X_micro), axis=1)
    n_nan = int(np.sum(nan_mask))
    if n_nan > 0:
        print(
            json.dumps(
                {
                    "event": "micro_nan_dropped",
                    "rows_with_nan": n_nan,
                    "action": "dropped_not_imputed",
                }
            ),
            flush=True,
        )
        keep_mask = ~nan_mask
        X_v9 = X_v9[keep_mask]
        X_micro = X_micro[keep_mask]
        if y is not None:
            y = y[keep_mask]
        if y_reg is not None:
            y_reg = y_reg[keep_mask]
        if pnl is not None:
            pnl = pnl[keep_mask]

    # ── Merge feature matrices ──
    X_merged = np.column_stack([X_v9, X_micro])  # (N_kept, 49)
    merged_feature_names = v9_feature_names + micro_feature_names

    # ── Fit StandardScaler on micro columns (Scaling Toxicity Fix) ──
    micro_scaler = None
    if args.fit_scaler:
        from sklearn.preprocessing import StandardScaler

        micro_scaler = StandardScaler()
        micro_scaler.fit(X_micro)

        scaler_path = output_path.parent / f"{output_path.stem}.micro_scaler.json"
        scaler_params = {
            "mean_": micro_scaler.mean_.tolist(),
            "scale_": micro_scaler.scale_.tolist(),
            "var_": micro_scaler.var_.tolist(),
            "n_features_in_": int(micro_scaler.n_features_in_),
            "feature_names_in_": list(micro_feature_names) if micro_feature_names else None,
        }
        scaler_path.write_text(json.dumps(scaler_params, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "event": "micro_scaler_saved",
                    "path": str(scaler_path),
                    "n_features": len(micro_feature_names),
                }
            ),
            flush=True,
        )

    # ── Save merged NPZ ──
    save_kwargs: dict = {
        "X": X_merged.astype(np.float32),
        "feature_names": np.array(merged_feature_names, dtype=str),
    }
    if y is not None:
        save_kwargs["y"] = y
    if y_reg is not None:
        save_kwargs["y_reg"] = y_reg
    if pnl is not None:
        save_kwargs["pnl"] = pnl
    if v9_timestamps is not None:
        save_kwargs["timestamps"] = v9_timestamps

    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output_path, **save_kwargs)

    print(
        json.dumps(
            {
                "event": "v9_micro_dataset_built",
                "output": str(output_path),
                "samples": int(len(X_merged)),
                "features": 49,
                "feature_names": merged_feature_names,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
