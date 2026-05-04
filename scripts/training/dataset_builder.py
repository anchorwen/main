"""Build training datasets by joining P&L labels with feature vectors.

Reads labels from label_builder.py output, queries the LocalFeatureStore for
nearest feature vectors by time, joins them, splits into train/validation sets,
and exports as Parquet files.

Usage:
  python scripts/training/dataset_builder.py \
    --labels data/reports/live_labels.jsonl \
    --feature-store-dir data/feature_store \
    --output-dir data/training
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from core.features.local_feature_store import LocalFeatureStore
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES
from core.features.store_contracts import FeatureQuery

SCHEMA_VERSION = "training_dataset.v1"


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp, handling 'Z' suffix and None."""
    if not ts:
        return None
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        # Try parsing without timezone
        try:
            return datetime.fromisoformat(ts.split("+")[0].split("-00")[0])
        except ValueError:
            return None


def _normalize_symbol(symbol: str) -> str:
    """Strip trailing 'c' from symbols like 'XAUUSDc' -> 'XAUUSD'."""
    return symbol.rstrip("c") if symbol.endswith("c") else symbol


def _find_nearest_feature(
    label_time: datetime,
    store: LocalFeatureStore,
    symbol: str,
    max_delta_seconds: float,
) -> Any | None:
    """Find the feature record nearest to label_time within max_delta_seconds.

    Queries the store for records within [label_time - max_delta, label_time + max_delta]
    and returns the one with minimum absolute time difference.
    """
    from datetime import timedelta

    start = label_time - timedelta(seconds=max_delta_seconds)
    end = label_time + timedelta(seconds=max_delta_seconds)

    query = FeatureQuery(
        symbol=symbol,
        timeframe="M5",
        schema_name="v9_institutional_40",
        start=start,
        end=end,
    )
    records = store.query(query)

    if not records:
        return None

    best = None
    best_delta = float("inf")
    for r in records:
        delta = abs((r.event_time - label_time).total_seconds())
        if delta < best_delta:
            best_delta = delta
            best = r

    return best


def join_labels_to_features(
    labels: list[dict[str, Any]],
    feature_store: LocalFeatureStore,
    *,
    symbol: str = "XAUUSD",
    max_time_delta_seconds: float = 300.0,
) -> dict[str, Any]:
    """Join each labeled trade to its nearest feature vector.

    Args:
        labels: List of label dicts from label_builder.py (training_label.v1).
        feature_store: LocalFeatureStore instance with feature records.
        symbol: Trading symbol to match in the feature store.
        max_time_delta_seconds: Maximum allowed time gap between label and feature.

    Returns:
        {"joined": [...], "matched": N, "unmatched": N, "skipped_unlabeled": N}
    """
    joined: list[dict[str, Any]] = []
    matched = 0
    unmatched = 0
    skipped_unlabeled = 0

    for lab in labels:
        if lab.get("label") == "unlabeled":
            skipped_unlabeled += 1
            continue

        label_time = _parse_iso(lab.get("open_recorded_at"))
        if label_time is None:
            unmatched += 1
            continue

        record = _find_nearest_feature(label_time, feature_store, symbol, max_time_delta_seconds)
        if record is None:
            unmatched += 1
            continue

        time_delta = (record.event_time - label_time).total_seconds()

        row: dict[str, Any] = {
            "label_id": lab.get("label_id", ""),
            "symbol": _normalize_symbol(lab.get("symbol", symbol)),
            "side": lab.get("side", ""),
            "label": lab.get("label", "unlabeled"),
            "pnl": lab.get("pnl"),
            "entry_price": lab.get("entry_price"),
            "exit_price": lab.get("exit_price"),
            "volume": lab.get("volume"),
            "open_recorded_at": lab.get("open_recorded_at", ""),
            "feature_event_time": record.event_time.isoformat(),
            "feature_source": record.source,
            "time_delta_seconds": round(time_delta, 2),
        }

        # Add feature values as f_0..f_39
        for i, fname in enumerate(V9_INSTITUTIONAL_40_FEATURES):
            row[f"f_{i}"] = record.values.get(fname, 0.0)

        joined.append(row)
        matched += 1

    return {
        "joined": joined,
        "matched": matched,
        "unmatched": unmatched,
        "skipped_unlabeled": skipped_unlabeled,
    }


def temporal_split(
    joined: list[dict[str, Any]],
    *,
    val_ratio: float = 0.2,
    time_column: str = "open_recorded_at",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split joined records by date (earliest 80% train, latest 20% val).

    This avoids time-series leakage by ensuring all training data precedes
    all validation data.
    """
    if not joined or val_ratio <= 0:
        return list(joined), []

    sorted_rows = sorted(joined, key=lambda r: r.get(time_column, ""))
    n = len(sorted_rows)
    split_idx = max(1, int(n * (1 - val_ratio)))
    if split_idx >= n:
        split_idx = n

    return sorted_rows[:split_idx], sorted_rows[split_idx:]


def export_parquet(
    joined: list[dict[str, Any]],
    output_path: Path,
) -> Path:
    """Export joined records as a Parquet file.

    Features are stored as columns f_0 through f_39. Metadata columns
    (label_id, side, label, pnl, prices, timestamps) are preserved alongside.
    """
    import pandas as pd

    df = pd.DataFrame(joined)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, index=False)
    return output_path


def export_npz(
    joined: list[dict[str, Any]],
    output_path: Path,
) -> Path:
    """Export joined records as a NumPy .npz file.

    Produces:
      X: (n_samples, 40) feature matrix
      y: (n_samples,) binary labels (1=win, 0=loss/breakeven)
      pnl: (n_samples,) P&L values
      feature_names: list of 40 feature names
    """
    n = len(joined)
    X = np.zeros((n, 40), dtype=np.float64)
    y = np.zeros(n, dtype=np.int32)
    pnl = np.zeros(n, dtype=np.float64)

    for i, row in enumerate(joined):
        for j in range(40):
            X[i, j] = float(row.get(f"f_{j}", 0.0))
        y[i] = 1 if row.get("label") == "win" else 0
        pnl[i] = float(row.get("pnl", 0.0) or 0.0)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        output_path,
        X=X,
        y=y,
        pnl=pnl,
        feature_names=np.array(V9_INSTITUTIONAL_40_FEATURES, dtype=str),
    )
    return output_path


def build_dataset(
    labels_path: Path,
    feature_store_dir: Path,
    output_dir: Path,
    *,
    symbol: str = "XAUUSD",
    max_time_delta_seconds: float = 300.0,
    val_ratio: float = 0.2,
    fmt: str = "parquet",
) -> dict[str, Any]:
    """Full pipeline: labels → join → split → export.

    Args:
        labels_path: Path to live_labels.jsonl from label_builder.py.
        feature_store_dir: Path to feature store base directory.
        output_dir: Directory for output files.
        symbol: Trading symbol for feature store (no 'c' suffix).
        max_time_delta_seconds: Max time window for feature matching.
        val_ratio: Fraction of data for validation (temporal split).
        fmt: Export format — "parquet" or "npz".

    Returns:
        Summary dict with schema_version, counts, and output paths.
    """
    # ── Load labels ──
    labels: list[dict[str, Any]] = []
    if labels_path.exists():
        for line in labels_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                labels.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not labels:
        return {
            "schema_version": SCHEMA_VERSION,
            "error": "no_labels_found",
            "labels_path": str(labels_path),
        }

    # ── Load feature store ──
    store = LocalFeatureStore(str(feature_store_dir))

    # ── Join ──
    join_result = join_labels_to_features(
        labels,
        store,
        symbol=symbol,
        max_time_delta_seconds=max_time_delta_seconds,
    )

    joined = join_result["joined"]
    if not joined:
        return {
            "schema_version": SCHEMA_VERSION,
            "labels_loaded": len(labels),
            **{k: v for k, v in join_result.items() if k != "joined"},
            "train_path": None,
            "val_path": None,
        }

    # ── Split ──
    train, val = temporal_split(joined, val_ratio=val_ratio)

    # ── Export ──
    output_dir.mkdir(parents=True, exist_ok=True)

    if fmt == "parquet":
        train_path = export_parquet(train, output_dir / "train.parquet")
        val_path = export_parquet(val, output_dir / "val.parquet")
    elif fmt == "npz":
        train_path = export_npz(train, output_dir / "train.npz")
        val_path = export_npz(val, output_dir / "val.npz")
    else:
        return {
            "schema_version": SCHEMA_VERSION,
            "error": f"unsupported_format: {fmt}",
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "labels_loaded": len(labels),
        "matched": join_result["matched"],
        "unmatched": join_result["unmatched"],
        "skipped_unlabeled": join_result["skipped_unlabeled"],
        "train_samples": len(train),
        "val_samples": len(val),
        "train_path": str(train_path),
        "val_path": str(val_path),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dataset_builder")
    p.add_argument(
        "--labels",
        type=Path,
        default=Path("data/reports/live_labels.jsonl"),
        help="Path to live_labels.jsonl (default: data/reports/live_labels.jsonl)",
    )
    p.add_argument(
        "--feature-store-dir",
        type=Path,
        default=Path("data/feature_store"),
        help="Feature store base directory (default: data/feature_store)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/training"),
        help="Output directory for Parquet/NPZ files (default: data/training)",
    )
    p.add_argument(
        "--symbol",
        default="XAUUSD",
        help="Trading symbol for feature store matching (default: XAUUSD)",
    )
    p.add_argument(
        "--max-time-delta",
        type=float,
        default=300.0,
        help="Max seconds between label time and feature time (default: 300)",
    )
    p.add_argument(
        "--val-ratio",
        type=float,
        default=0.2,
        help="Fraction of data reserved for validation (default: 0.2)",
    )
    p.add_argument(
        "--format",
        choices=["parquet", "npz"],
        default="parquet",
        help="Export format (default: parquet)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = build_dataset(
        labels_path=args.labels,
        feature_store_dir=args.feature_store_dir,
        output_dir=args.output_dir,
        symbol=args.symbol,
        max_time_delta_seconds=args.max_time_delta,
        val_ratio=args.val_ratio,
        fmt=args.format,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if result.get("error"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
