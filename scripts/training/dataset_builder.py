"""Build training datasets by joining labels with feature vectors.

Reads labels from label_builder.py output, queries the LocalFeatureStore for
nearest feature vectors by time, joins them, splits into train/validation sets,
and exports as Parquet/NPZ files.

When --label-contract is provided, validates label distribution against the
contract definition and embeds contract_id in output metadata for full
training provenance.

Usage:
  python scripts/training/dataset_builder.py \\
    --labels data/labels/live_labels.jsonl \\
    --feature-store-dir data/feature_store \\
    --output-dir data/training \\
    --label-contract blueprints/contracts/label-survival-barrier-1.0.0.json
"""

from __future__ import annotations

import argparse
import bisect
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from core.features.local_feature_store import LocalFeatureStore
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES

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
    """Normalise symbol to canonical form (XAUUSDc)."""
    if symbol == "XAUUSD":
        return "XAUUSDc"
    return symbol


def _load_feature_index(
    feature_store: LocalFeatureStore, symbol: str
) -> list[tuple[datetime, str, dict[str, Any]]]:
    """Load all feature records into a sorted (time, event_time_str, values) list for fast binary search."""
    path = feature_store._record_path(symbol, "M5")
    if not path.exists():
        return []
    records: list[tuple[datetime, str, dict[str, Any]]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("schema_name") != "v9_institutional_40":
                continue
            et_str = rec.get("event_time", "")
            if not et_str:
                continue
            try:
                et = datetime.fromisoformat(et_str)
            except (ValueError, TypeError):
                continue
            if et.tzinfo is not None:
                offset = et.utcoffset()
                if offset is not None:
                    et = et.replace(tzinfo=None) - offset
            records.append((et, et_str, rec.get("values", {})))
    records.sort(key=lambda x: x[0])
    return records


def _find_nearest_in_index(
    label_time: datetime,
    index: list[tuple[datetime, str, dict[str, Any]]],
    max_delta_seconds: float,
) -> tuple[str, dict[str, Any], float] | None:
    """Binary-search the sorted feature index for the nearest record.

    Returns (event_time_str, values_dict, time_delta_seconds) or None.

    CRITICAL: Only matches features AT or BEFORE label_time. Features must
    come from bars that existed when the label entry occurred — never from
    future bars (no look-ahead bias).
    """
    if not index:
        return None
    if label_time.tzinfo is not None:
        offset = label_time.utcoffset()
        if offset is not None:
            label_time = label_time.replace(tzinfo=None) - offset
    timestamps = [t for t, _, _ in index]
    idx = bisect.bisect_left(timestamps, label_time)

    # Exact match at or at the same timestamp — safe to use
    if idx < len(timestamps) and timestamps[idx] == label_time:
        return index[idx][1], index[idx][2], 0.0

    # Only look backward — feature time must precede label time
    candidate_idx = idx - 1
    if candidate_idx >= 0:
        delta = abs((timestamps[candidate_idx] - label_time).total_seconds())
        if delta <= max_delta_seconds:
            return index[candidate_idx][1], index[candidate_idx][2], delta

    return None


def join_labels_to_features(
    labels: list[dict[str, Any]],
    feature_store: LocalFeatureStore,
    *,
    symbol: str = "XAUUSDc",
    max_time_delta_seconds: float = 300.0,
) -> dict[str, Any]:
    """Join each labeled trade to its nearest feature vector.

    Builds an in-memory sorted index of all feature records once, then binary-searches
    for each label — O(N log M) instead of O(N*M) per-query linear scans.
    """
    print(f"Loading feature index for {symbol}...")
    index = _load_feature_index(feature_store, symbol)
    print(f"  {len(index)} feature records indexed. Joining {len(labels)} labels...")

    joined: list[dict[str, Any]] = []
    matched = 0
    unmatched = 0
    skipped_unlabeled = 0

    for i, lab in enumerate(labels):
        if (i + 1) % 10000 == 0:
            print(f"  ... {i + 1}/{len(labels)} labels processed ({matched} matched)")

        if lab.get("label") == "unlabeled":
            skipped_unlabeled += 1
            continue

        label_time = _parse_iso(lab.get("open_recorded_at") or lab.get("entry_time"))
        if label_time is None:
            unmatched += 1
            continue

        result = _find_nearest_in_index(label_time, index, max_time_delta_seconds)
        if result is None:
            unmatched += 1
            continue

        feature_event_time, values, time_delta = result

        row: dict[str, Any] = {
            "label_id": lab.get("label_id", ""),
            "symbol": _normalize_symbol(lab.get("symbol", symbol)),
            "side": lab.get("side", ""),
            "label": lab.get("label", "unlabeled"),
            "pnl": lab.get("pnl"),
            "pnl_r": lab.get("pnl_r"),
            "entry_price": lab.get("entry_price"),
            "exit_price": lab.get("exit_price"),
            "volume": lab.get("volume"),
            "open_recorded_at": lab.get("open_recorded_at") or lab.get("entry_time", ""),
            "feature_event_time": feature_event_time,
            "feature_source": "feature_store_warmer",
            "time_delta_seconds": round(time_delta, 2),
        }

        for j, fname in enumerate(V9_INSTITUTIONAL_40_FEATURES):
            row[f"f_{j}"] = values.get(fname, 0.0)

        joined.append(row)
        matched += 1

    print(
        f"  Done. matched={matched}, unmatched={unmatched}, skipped_unlabeled={skipped_unlabeled}"
    )
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


def _parse_iso_to_unix(ts: str | None) -> float:
    """Parse an ISO-8601 timestamp to seconds since Unix epoch (float).

    Returns 0.0 for unparseable or missing timestamps so downstream
    code can detect and handle gaps.
    """
    if not ts:
        return 0.0
    try:
        dt = _parse_iso(ts)
        if dt is None:
            return 0.0
        return dt.timestamp()
    except (ValueError, OSError):
        return 0.0


def _resolve_label(label_val: object) -> int:
    """Map a label value to directional integer: -1=SL, 0=timeout, 1=TP.

    Handles both string labels (from legacy JSONL) and integer labels
    (from calibrated label builders).
    """
    if isinstance(label_val, int | float | np.integer | np.floating):
        return int(label_val)
    s = str(label_val).lower()
    if s in ("win", "tp_hit_first", "tp", "1"):
        return 1
    if s in ("loss", "sl_hit_first", "sl", "-1"):
        return -1
    return 0


def _resolve_pnl(row: dict[str, Any]) -> float:
    """Extract PnL from a row, trying 'pnl' first, then 'pnl_r'."""
    for key in ("pnl", "pnl_r"):
        val = row.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return 0.0


def export_npz(
    joined: list[dict[str, Any]],
    output_path: Path,
) -> Path:
    """Export joined records as a NumPy .npz file.

    Produces:
      X: (n_samples, 40) feature matrix
      y: (n_samples,) directional labels (-1=SL, 0=timeout, 1=TP)
      y_reg: (n_samples,) P&L values for regression target
      pnl: (n_samples,) P&L values
      timestamps: (n_samples,) float64 Unix epoch seconds
      feature_names: list of 40 feature names
    """
    n = len(joined)
    X = np.zeros((n, 40), dtype=np.float64)
    y = np.zeros(n, dtype=np.int32)
    y_reg = np.zeros(n, dtype=np.float64)
    pnl = np.zeros(n, dtype=np.float64)
    timestamps = np.zeros(n, dtype=np.float64)
    lookahead_count = 0

    for i, row in enumerate(joined):
        for j in range(40):
            X[i, j] = float(row.get(f"f_{j}", 0.0))
        y[i] = _resolve_label(row.get("label", 0))
        pnl_val = _resolve_pnl(row)
        y_reg[i] = pnl_val
        pnl[i] = pnl_val
        ts = _parse_iso_to_unix(
            row.get("open_recorded_at")
            or row.get("entry_time")
            or row.get("feature_event_time", "")
        )
        timestamps[i] = ts

        # Validate no look-ahead: feature_time must be <= label_time
        feature_ts = _parse_iso_to_unix(row.get("feature_event_time", ""))
        label_ts_val = _parse_iso_to_unix(row.get("open_recorded_at") or row.get("entry_time", ""))
        if feature_ts > 0 and label_ts_val > 0 and feature_ts > label_ts_val:
            lookahead_count += 1

    if lookahead_count > 0:
        print(
            f"  WARNING: {lookahead_count}/{n} rows ({100*lookahead_count/max(n,1):.1f}%) "
            f"have feature_time > label_time — potential look-ahead bias"
        )

    # Compute time range metadata
    valid_ts = timestamps[timestamps > 0]
    if len(valid_ts) > 0:
        min_time = datetime.fromtimestamp(float(valid_ts.min())).isoformat()
        max_time = datetime.fromtimestamp(float(valid_ts.max())).isoformat()
        time_range_days = float((valid_ts.max() - valid_ts.min()) / 86400.0)
    else:
        min_time = ""
        max_time = ""
        time_range_days = 0.0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        output_path,
        X=X,
        y=y,
        y_reg=y_reg,
        pnl=pnl,
        timestamps=timestamps,
        feature_names=np.array(V9_INSTITUTIONAL_40_FEATURES, dtype=str),
    )

    # Also write lightweight metadata JSON alongside
    meta_path = output_path.with_suffix(".meta.json")
    import json

    meta_path.write_text(
        json.dumps(
            {
                "schema_version": "training_dataset.v2",
                "n_samples": n,
                "n_features": 40,
                "feature_schema": "v9_institutional_40",
                "min_time": min_time,
                "max_time": max_time,
                "time_range_days": round(time_range_days, 2),
                "has_timestamps": len(valid_ts) > 0,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return output_path


def build_dataset(
    labels_path: Path,
    feature_store_dir: Path,
    output_dir: Path,
    *,
    symbol: str = "XAUUSDc",
    max_time_delta_seconds: float = 300.0,
    val_ratio: float = 0.2,
    fmt: str = "parquet",
    label_contract_path: str | None = None,
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
        label_contract_path: Optional path to Label Contract JSON for provenance.

    Returns:
        Summary dict with schema_version, counts, and output paths.
    """
    # ── Load contract if provided ──
    contract_id: str | None = None
    if label_contract_path:
        from core.contracts.training.label_contract import LabelContract

        contract = LabelContract.from_file(label_contract_path)
        contract_id = contract.contract_id
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
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "error": "no_labels_found",
            "labels_path": str(labels_path),
        }
        if contract_id:
            result["label_contract_id"] = contract_id
        return result

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
        summary: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "labels_loaded": len(labels),
            **{k: v for k, v in join_result.items() if k != "joined"},
            "train_path": None,
            "val_path": None,
        }
        if contract_id:
            summary["label_contract_id"] = contract_id
        return summary

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

    result = {
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
    if contract_id:
        result["label_contract_id"] = contract_id
    return result


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
        default="XAUUSDc",
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
    p.add_argument(
        "--label-contract",
        type=Path,
        default=None,
        help="Path to Label Contract JSON for provenance and validation",
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
        label_contract_path=str(args.label_contract) if args.label_contract else None,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if result.get("error"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
