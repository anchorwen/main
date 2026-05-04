"""Feature quality report: completeness, distribution shift, and missing-rate checks.

Reads feature vectors from LocalFeatureStore JSONL, validates quality, computes
per-feature statistics, and compares against normalization baseline for drift.

Usage:
  python scripts/live_feature_quality_report.py --store-dir data/feature_store --norm-config configs/brains/v9_institutional_01.normalization.json
  python scripts/live_feature_quality_report.py --store-dir data/feature_store --output data/reports/feature_quality.json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from scripts.validators.feature_quality_validator import (
    FEATURE_NAMES,
    compute_distribution_shift,
    compute_per_feature_stats,
    validate_sample_quality,
)

SCHEMA_VERSION = "feature_quality_report.v1"


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _load_norm_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_feature_vectors(
    store_dir: Path,
    *,
    symbol: str = "XAUUSD",
    timeframe: str = "M5",
    max_samples: int = 500,
    date_filter: str | None = None,
) -> list[np.ndarray]:
    """Read feature vectors from LocalFeatureStore JSONL."""
    vectors: list[np.ndarray] = []
    records_path = (
        store_dir / "records" / f"symbol={symbol}" / f"timeframe={timeframe}" / "features.jsonl"
    )
    if not records_path.exists():
        return vectors

    for line in records_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue

        if date_filter:
            event_time = str(rec.get("event_time", ""))
            if not event_time.startswith(date_filter):
                continue

        values = rec.get("values", {})
        if not values:
            continue
        arr = np.array([float(values.get(name, 0.0)) for name in FEATURE_NAMES], dtype=np.float64)
        vectors.append(arr)

        if len(vectors) >= max_samples:
            break

    return vectors


def build_report(
    store_dir: Path,
    *,
    norm_config_path: Path | None = None,
    symbol: str = "XAUUSD",
    timeframe: str = "M5",
    max_samples: int = 500,
    date_filter: str | None = None,
    shift_threshold: float = 2.0,
) -> dict[str, Any]:
    vectors = _read_feature_vectors(
        store_dir,
        symbol=symbol,
        timeframe=timeframe,
        max_samples=max_samples,
        date_filter=date_filter,
    )

    if not vectors:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_now_iso(),
            "store_dir": str(store_dir),
            "sample_size": 0,
            "error": "no_feature_vectors_found",
        }

    quality = validate_sample_quality(vectors)
    stats = compute_per_feature_stats(vectors)

    norm_config: dict[str, Any] = {}
    if norm_config_path:
        norm_config = _load_norm_config(norm_config_path)

    shift = {}
    if norm_config:
        shift = compute_distribution_shift(stats, norm_config, shift_threshold=shift_threshold)

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "store_dir": str(store_dir),
        "symbol": symbol,
        "timeframe": timeframe,
        "sample_size": stats["sample_size"],
        "quality": quality,
        "per_feature_stats": stats["features"],
        "distribution_shift": shift,
    }

    if norm_config_path:
        report["norm_config_path"] = str(norm_config_path)

    # Severity assessment
    issues: list[str] = []
    if quality["valid_rate"] < 0.95:
        issues.append(f"valid_rate={quality['valid_rate']:.2%}_below_threshold")
    if quality["zero_vectors"] > 0:
        issues.append(f"zero_vectors={quality['zero_vectors']}_detected")
    if quality["nan_vectors"] > 0:
        issues.append(f"nan_vectors={quality['nan_vectors']}_detected")
    if shift.get("shift_detected"):
        issues.append(f"distribution_shift={shift.get('shifted_count', 0)}_features")

    report["severity"] = (
        "critical" if quality["valid_rate"] < 0.80 else ("warning" if issues else "ok")
    )
    report["issues"] = issues

    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="live_feature_quality_report")
    p.add_argument(
        "--store-dir",
        type=Path,
        default=Path("data/feature_store"),
        help="LocalFeatureStore base directory",
    )
    p.add_argument(
        "--norm-config",
        type=Path,
        default=Path("configs/brains/v9_institutional_01.normalization.json"),
        help="Normalization config with mean/std baseline",
    )
    p.add_argument("--symbol", default="XAUUSD", help="Feature symbol partition")
    p.add_argument("--timeframe", default="M5", help="Feature timeframe partition")
    p.add_argument("--max-samples", type=int, default=500, help="Max vectors to read")
    p.add_argument("--date", default=None, help="ISO date filter, e.g. 2026-05-04")
    p.add_argument(
        "--shift-threshold", type=float, default=2.0, help="Z-score threshold for shift flagging"
    )
    p.add_argument("--output", default=None, help="Write JSON report to file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(
        store_dir=args.store_dir,
        norm_config_path=args.norm_config,
        symbol=args.symbol,
        timeframe=args.timeframe,
        max_samples=args.max_samples,
        date_filter=args.date,
        shift_threshold=args.shift_threshold,
    )
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"[feature_quality_report] Report written to {out}")

    if report.get("severity") == "critical":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
