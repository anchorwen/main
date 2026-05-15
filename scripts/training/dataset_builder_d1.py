"""D1 Dataset Builder — join daily features with single-direction barrier labels.

Aligns D1 features and labels by timestamp proximity, producing train/val/test
splits in NPZ format.  Supports walk-forward temporal split with Purge & Embargo
(Lopez de Prado) to prevent label-overlap leakage between splits.

Usage:
    python scripts/training/dataset_builder_d1.py \\
        --labels data/labels/d1_swing_5d.jsonl \\
        --features-csv data/raw/xauusdc_d1_merged.csv \\
        --h4-csv data/raw/xauusdc_h4_merged.csv \\
        --output data/training/d1_swing_5d.npz

Output NPZ contains:
    X       — feature array (float32, N x 22)
    y       — label array (int32, N)  [-1, 0, 1] = [short, neutral, long]
    pnl_r   — P&L in R multiples (float32, N)
    times   — entry timestamps (str array, N)
    meta    — dict with schema_version, contract_id, feature_names, split info
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from core.features.computers.daily_computer import DailyFeatureComputer
from core.features.schemas.daily_swing_schema import DAILY_SWING_22_FEATURES

SCHEMA_VERSION = "training_dataset.v2"


def _parse_date(ts: str) -> str:
    """Normalize timestamp to YYYY-MM-DD for matching."""
    ts_stripped = ts.strip()
    if len(ts_stripped) == 10 and ts_stripped[4] == "-":
        return ts_stripped
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y.%m.%d"):
        try:
            return datetime.strptime(
                ts_stripped[:19] if len(ts_stripped) >= 19 else ts_stripped, fmt
            ).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ts_stripped[:10]


def _extract_horizon(contract_id: str) -> int:
    """Extract horizon_bars from contract_id string.

    Handles both formats: 'd1_swing_10d' → 10, 'm15_swing_24bar' → 24.
    """
    import re

    # Match explicit bar count: "24bar", "18bar", etc.
    match = re.search(r"(\d+)bar", contract_id)
    if match:
        return int(match.group(1))
    # Match day-based: "5d", "10d", "20d"
    match = re.search(r"(\d+)d", contract_id)
    if match:
        return int(match.group(1))
    return 5  # safe default


def build_dataset(
    labels_path: Path,
    d1_csv: Path,
    h4_csv: Path | None = None,
    *,
    val_split: float = 0.15,
    test_split: float = 0.10,
    seed: int = 42,
    class_weights: bool = True,
) -> dict[str, Any]:
    """Join features and labels, split chronologically with Purge & Embargo.

    Returns dict with X, y, pnl_r, times, and also writes NPZ.
    """
    # ── Load labels ──
    labels: list[dict[str, Any]] = []
    with open(labels_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                labels.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    print(f"[dataset_builder_d1] Loaded {len(labels)} labels")
    if not labels:
        raise ValueError("No labels loaded")

    # Extract contract info
    contract_id = labels[0].get("contract_id", "unknown") if labels else "unknown"
    horizon_bars = (int(labels[0].get("horizon_bars", 0)) if labels else 0) or _extract_horizon(
        contract_id
    )
    print(f"[dataset_builder_d1] Contract: {contract_id}, horizon={horizon_bars} bars")

    # ── Compute features ──
    cross_assets: dict[str, str] = {}
    for sym, fname in [("XAGUSDc", "xagusdc_d1_merged.csv"), ("EURUSDc", "eurusdc_d1_merged.csv")]:
        p = d1_csv.parent / fname
        if p.exists():
            cross_assets[sym] = str(p)

    print(f"[dataset_builder_d1] Computing features from {d1_csv} ...")
    comp = DailyFeatureComputer(
        d1_csv=str(d1_csv),
        h4_csv=str(h4_csv) if h4_csv else None,
        cross_assets=cross_assets,
    )
    features_array, feature_timestamps = comp.compute_all()
    print(
        f"[dataset_builder_d1] Computed {features_array.shape} features "
        f"({feature_timestamps[0]} → {feature_timestamps[-1]})"
    )

    # Build feature lookup by date
    feat_by_date: dict[str, np.ndarray] = {}
    for i, ts in enumerate(feature_timestamps):
        date_key = _parse_date(ts)
        feat_by_date[date_key] = features_array[i]

    # ── Join features with labels ──
    X_list: list[np.ndarray] = []
    y_list: list[int] = []
    pnl_list: list[float] = []
    time_list: list[str] = []
    skipped_missing_feature = 0

    for lbl in labels:
        entry_date = _parse_date(lbl["entry_time"])
        if entry_date not in feat_by_date:
            skipped_missing_feature += 1
            continue

        feat_vec = feat_by_date[entry_date]
        # new format uses "label_int"; fall back to parsing "label" string
        if "label_int" in lbl:
            label_int = int(lbl["label_int"])
        else:
            label_int = int(lbl["label"])  # legacy "-1", "0", "1"

        X_list.append(feat_vec)
        y_list.append(label_int)
        pnl_list.append(float(lbl.get("pnl_r", 0.0)))
        time_list.append(entry_date)

    if not X_list:
        raise ValueError(
            "No features matched to labels. Check date alignment. "
            f"First label date: {_parse_date(labels[0]['entry_time'])}, "
            f"First feature date: {_parse_date(feature_timestamps[0])}"
        )

    X = np.array(X_list, dtype=np.float32)
    y = np.array(y_list, dtype=np.int32)
    pnl_r = np.array(pnl_list, dtype=np.float32)
    times_arr = np.array(time_list)

    print(
        f"[dataset_builder_d1] Joined: {len(X)} samples "
        f"({skipped_missing_feature} skipped — no matching feature)"
    )

    # ── Label distribution ──
    n_total = len(y)
    n_short = int(np.sum(y == -1))
    n_neutral = int(np.sum(y == 0))
    n_long = int(np.sum(y == 1))
    print(
        f"[dataset_builder_d1] Labels: short={n_short} ({n_short/max(n_total,1)*100:.1f}%)  "
        f"neutral={n_neutral} ({n_neutral/max(n_total,1)*100:.1f}%)  "
        f"long={n_long} ({n_long/max(n_total,1)*100:.1f}%)"
    )

    # ── Chronological sort ──
    sort_idx = np.argsort(time_list)
    X = X[sort_idx]
    y = y[sort_idx]
    pnl_r = pnl_r[sort_idx]
    times_arr = times_arr[sort_idx]

    n = len(X)
    n_test = int(n * test_split)
    n_val = int(n * val_split)
    n_train = n - n_val - n_test

    # ── Purge & Embargo (Lopez de Prado) ──
    # Remove training samples whose label horizon extends into validation period,
    # and validation samples whose label horizon extends into test period.
    # This prevents information leakage from overlapping label windows.
    purge_bars = horizon_bars

    train_end = max(0, n_train - purge_bars)
    val_start = n_train  # chronological — val begins where train's data ends
    val_end = n_train + max(0, n_val - purge_bars)
    test_start = n_train + n_val

    purged_train = n_train - train_end
    purged_val = n_val - (val_end - val_start) if val_end > val_start else n_val

    train_X = X[:train_end]
    train_y = y[:train_end]
    train_pnl = pnl_r[:train_end]
    train_times = times_arr[:train_end]

    val_X = X[val_start:val_end]
    val_y = y[val_start:val_end]
    val_pnl = pnl_r[val_start:val_end]
    val_times = times_arr[val_start:val_end]

    test_X = X[test_start:]
    test_y = y[test_start:]
    test_pnl = pnl_r[test_start:]
    test_times = times_arr[test_start:]

    n_train_final = len(train_y)
    n_val_final = len(val_y)
    n_test_final = len(test_y)

    if purge_bars > 0:
        print(
            f"[dataset_builder_d1] Purge & Embargo: removed {purged_train} train + {purged_val} val "
            f"samples (purge_zone={purge_bars} bars)"
        )
    print(
        f"[dataset_builder_d1] Split after purge: train={n_train_final}  val={n_val_final}  test={n_test_final}"
    )
    print(
        f"[dataset_builder_d1] Date ranges: "
        f"train=[{train_times[0] if len(train_times) > 0 else 'N/A'}, "
        f"{train_times[-1] if len(train_times) > 0 else 'N/A'}]  "
        f"val=[{val_times[0] if len(val_times) > 0 else 'N/A'}, "
        f"{val_times[-1] if len(val_times) > 0 else 'N/A'}]  "
        f"test=[{test_times[0] if len(test_times) > 0 else 'N/A'}, "
        f"{test_times[-1] if len(test_times) > 0 else 'N/A'}]"
    )

    # ── Class weights (balanced) ──
    weights: dict[str, float] = {}
    if class_weights:
        for label_val, label_name in [(-1, "short"), (0, "neutral"), (1, "long")]:
            count = int(np.sum(train_y == label_val))
            if count > 0:
                weight = n_train_final / (3.0 * count)
            else:
                weight = 1.0
            weights[f"class_{label_name}"] = round(weight, 4)

    return {
        "X": train_X,
        "y": train_y,
        "pnl_r": train_pnl,
        "X_val": val_X,
        "y_val": val_y,
        "pnl_r_val": val_pnl,
        "X_test": test_X,
        "y_test": test_y,
        "pnl_r_test": test_pnl,
        "times_train": train_times,
        "times_val": val_times,
        "times_test": test_times,
        "feature_names": DAILY_SWING_22_FEATURES,
        "contract_id": contract_id,
        "class_weights": weights,
        "n_train": n_train_final,
        "n_val": n_val_final,
        "n_test": n_test_final,
        "purge_bars": purge_bars,
        "purged_train": purged_train,
        "purged_val": purged_val,
    }


def _dates_to_unix(date_strings: np.ndarray) -> np.ndarray:
    """Convert YYYY-MM-DD date strings to Unix epoch seconds (float64).

    Uses noon UTC for each date so all intraday samples on the same
    date share the same timestamp for CPCV purging purposes.
    """
    result = np.zeros(len(date_strings), dtype=np.float64)
    for i, ds in enumerate(date_strings):
        try:
            dt = datetime.strptime(str(ds)[:10], "%Y-%m-%d").replace(hour=12, tzinfo=UTC)
            result[i] = dt.timestamp()
        except (ValueError, OSError):
            result[i] = 0.0
    return result


def save_npz(dataset: dict[str, Any], output_path: Path) -> Path:
    """Save dataset as NPZ with timestamps and metadata."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    np.savez_compressed(
        output_path,
        X=dataset["X"],
        y=dataset["y"],
        pnl_r=dataset["pnl_r"],
        X_val=dataset["X_val"],
        y_val=dataset["y_val"],
        pnl_r_val=dataset["pnl_r_val"],
        X_test=dataset["X_test"],
        y_test=dataset["y_test"],
        pnl_r_test=dataset["pnl_r_test"],
    )

    meta_path = output_path.with_suffix(".meta.json")
    total = dataset["n_train"] + dataset["n_val"] + dataset["n_test"]

    # Build timestamp arrays if time data is available
    train_times = dataset.get("times_train")
    val_times = dataset.get("times_val")
    test_times = dataset.get("times_test")

    min_time = ""
    max_time = ""
    time_range_days = 0.0
    has_timestamps = False

    if train_times is not None and len(train_times) > 0:
        np.savez_compressed(
            output_path,
            X=dataset["X"],
            y=dataset["y"],
            pnl_r=dataset["pnl_r"],
            X_val=dataset["X_val"],
            y_val=dataset["y_val"],
            pnl_r_val=dataset["pnl_r_val"],
            X_test=dataset["X_test"],
            y_test=dataset["y_test"],
            pnl_r_test=dataset["pnl_r_test"],
            timestamps_train=_dates_to_unix(train_times),
            timestamps_val=_dates_to_unix(val_times)
            if val_times is not None and len(val_times) > 0
            else np.zeros(0, dtype=np.float64),
            timestamps_test=_dates_to_unix(test_times)
            if test_times is not None and len(test_times) > 0
            else np.zeros(0, dtype=np.float64),
        )
        all_times = list(train_times)
        if val_times is not None:
            all_times.extend(val_times)
        if test_times is not None:
            all_times.extend(test_times)
        all_unix = _dates_to_unix(np.array(all_times))
        valid = all_unix[all_unix > 0]
        if len(valid) > 0:
            min_time = datetime.fromtimestamp(float(valid.min()), tz=UTC).isoformat()
            max_time = datetime.fromtimestamp(float(valid.max()), tz=UTC).isoformat()
            time_range_days = float((valid.max() - valid.min()) / 86400.0)
            has_timestamps = True

    meta = {
        "schema_version": SCHEMA_VERSION,
        "contract_id": dataset["contract_id"],
        "feature_names": dataset["feature_names"],
        "n_features": len(dataset["feature_names"]),
        "n_train": dataset["n_train"],
        "n_val": dataset["n_val"],
        "n_test": dataset["n_test"],
        "class_weights": dataset["class_weights"],
        "split_strategy": "chronological_with_purge",
        "purge_bars": dataset.get("purge_bars", 0),
        "purged_train": dataset.get("purged_train", 0),
        "purged_val": dataset.get("purged_val", 0),
        "val_split_pct": round(dataset["n_val"] / max(total, 1) * 100, 1),
        "test_split_pct": round(dataset["n_test"] / max(total, 1) * 100, 1),
        "min_time": min_time,
        "max_time": max_time,
        "time_range_days": round(time_range_days, 2),
        "has_timestamps": has_timestamps,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"[dataset_builder_d1] Saved {output_path} ({output_path.stat().st_size / 1024:.0f} KB)")
    print(f"[dataset_builder_d1] Saved {meta_path}")
    return output_path


# ── CLI ──


def main():
    parser = argparse.ArgumentParser(prog="dataset_builder_d1")
    parser.add_argument("--labels", type=Path, required=True, help="Path to label JSONL file")
    parser.add_argument(
        "--features-csv",
        type=Path,
        default=Path("data/raw/xauusdc_d1_merged.csv"),
        help="Path to D1 OHLC CSV for feature computation",
    )
    parser.add_argument(
        "--h4-csv",
        type=Path,
        default=Path("data/raw/xauusdc_h4_merged.csv"),
        help="Path to H4 OHLC CSV for macro features",
    )
    parser.add_argument("--output", type=Path, required=True, help="Output NPZ path")
    parser.add_argument("--val-split", type=float, default=0.15)
    parser.add_argument("--test-split", type=float, default=0.10)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    if not args.labels.exists():
        print(f"Labels file not found: {args.labels}", file=sys.stderr)
        return 2

    np.random.seed(args.seed)

    dataset = build_dataset(
        labels_path=args.labels,
        d1_csv=args.features_csv,
        h4_csv=args.h4_csv,
        val_split=args.val_split,
        test_split=args.test_split,
        seed=args.seed,
    )

    save_npz(dataset, args.output)

    for split_name, split_y in [
        ("train", dataset["y"]),
        ("val", dataset["y_val"]),
        ("test", dataset["y_test"]),
    ]:
        total = len(split_y)
        s = int(np.sum(split_y == -1))
        n = int(np.sum(split_y == 0))
        l = int(np.sum(split_y == 1))
        print(
            f"[dataset_builder_d1]   {split_name:5s}: short={s:4d} ({s/max(total,1)*100:5.1f}%)  "
            f"neutral={n:4d} ({n/max(total,1)*100:5.1f}%)  "
            f"long={l:4d} ({l/max(total,1)*100:5.1f}%)"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
