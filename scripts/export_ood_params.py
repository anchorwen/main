"""Export OOD parameters from the live feature store.

Reads feature-store JSONL records, groups by schema, computes centroid +
covariance, and writes OOD config files to ``data_btc/models/ood_{schema}.json``.

Usage::

    python scripts/export_ood_params.py --data-dir data_btc
    python scripts/export_ood_params.py --data-dir data_btc --min-samples 500
    python scripts/export_ood_params.py --data-dir data_btc --max-age-days 30

Architecture
------------
- Reads ``data_btc/feature_store/records/symbol=BTCUSDc/timeframe=M5/features.jsonl``
- Groups records by ``schema_name`` field
- For each schema with >= min_samples records:
    - Builds (n_samples, n_features) feature matrix
    - Calls OODGateway.calibrate() to compute centroid + std + thresholds
    - Saves as ``data_btc/models/ood_{schema_name}.json``

The resulting OOD config file is consumed by ``OODGateway`` at inference time
for Mahalanobis distance regime-shift detection.

Rolling-window calibration
--------------------------
Pass ``--max-age-days N`` to use only records whose ``event_time`` is within
the last N days.  This keeps the OOD centroid and thresholds anchored to the
recent market regime so the gate adapts to secular shifts (e.g. a multi-week
low-volatility period) while still catching sudden anomalies relative to that
regime.

Without ``--max-age-days`` the entire feature store is used (legacy behaviour,
suitable for initial calibration from a representative historical sample).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np


def main() -> None:
    parser = argparse.ArgumentParser(description="Export OOD parameters from feature store")
    parser.add_argument(
        "--data-dir",
        default="data_btc",
        help="Data directory (default: data_btc)",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=200,
        help="Minimum samples per schema to calibrate (default: 200)",
    )
    parser.add_argument(
        "--symbol",
        default="BTCUSDc",
        help="Symbol to calibrate (default: BTCUSDc)",
    )
    parser.add_argument(
        "--max-age-days",
        type=float,
        default=None,
        help="Only use records with event_time within the last N days "
        "(rolling window). If not set, all records are used.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print statistics without writing files",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    feature_path = (
        data_dir
        / "feature_store"
        / "records"
        / f"symbol={args.symbol}"
        / "timeframe=M5"
        / "features.jsonl"
    )

    if not feature_path.exists():
        print(f"ERROR: Feature store not found at {feature_path}")
        print("Run the live system first to populate the feature store, or use --data-dir.")
        sys.exit(1)

    # ── Date filtering ──
    cutoff_utc: str | None = None
    if args.max_age_days is not None:
        cutoff_dt = datetime.now(UTC) - timedelta(days=args.max_age_days)
        cutoff_utc = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S")
        print(f"Date filter: max-age={args.max_age_days} days, cutoff={cutoff_utc}")

    print(f"Reading feature store: {feature_path}")

    # ── Read and group by schema ──
    schema_values: dict[str, list[list[float]]] = defaultdict(list)
    schema_feature_names: dict[str, list[str]] = {}
    line_count = 0
    parse_errors = 0
    skipped_age = 0

    with open(feature_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            line_count += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue

            # ── Date filter ──
            if cutoff_utc is not None:
                event_time = record.get("event_time", "")
                if event_time < cutoff_utc:
                    skipped_age += 1
                    continue

            schema_name = record.get("schema_name", "unknown")
            values = record.get("values")
            if not isinstance(values, dict) or not values:
                continue

            # Collect feature names from first record per schema
            if schema_name not in schema_feature_names:
                schema_feature_names[schema_name] = list(values.keys())

            # Build ordered feature vector
            feature_names = schema_feature_names[schema_name]
            try:
                vector = [float(values.get(name, 0.0)) for name in feature_names]
            except (TypeError, ValueError):
                continue

            schema_values[schema_name].append(vector)

    print(f"  Lines read: {line_count}")
    print(f"  Parse errors: {parse_errors}")
    if cutoff_utc is not None:
        print(f"  Skipped (age > {args.max_age_days}d): {skipped_age}")
    print(f"  Schemas found: {len(schema_values)}")
    for schema, vecs in sorted(schema_values.items()):
        print(f"    {schema}: {len(vecs)} samples, {len(schema_feature_names[schema])} features")

    # ── Calibrate per schema ──
    from core.execution.ood_gateway import OODGateway

    models_dir = data_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    for schema_name, vecs in sorted(schema_values.items()):
        n = len(vecs)
        if n < args.min_samples:
            print(f"\n  SKIP {schema_name}: {n} samples < min {args.min_samples}")
            continue

        n_features = len(vecs[0])
        X = np.array(vecs, dtype=np.float64)

        # Remove rows with NaN or Inf
        valid_mask = np.isfinite(X).all(axis=1)
        X_clean = X[valid_mask]
        n_removed = n - X_clean.shape[0]
        if n_removed > 0:
            print(f"  {schema_name}: removed {n_removed} rows with NaN/Inf")

        config = OODGateway.calibrate(
            X_clean,
            schema_name=schema_name,
            source="feature_store",
        )

        print(f"\n  CALIBRATED {schema_name}:")
        print(f"    samples: {config.num_samples} (after cleaning)")
        print(f"    features: {config.num_features}")
        print(f"    threshold_block (3σ): {config.threshold_block:.2f}")
        print(f"    threshold_cautious (2σ): {config.threshold_cautious:.2f}")
        print(f"    centroid range: [{config.centroid.min():.4f}, {config.centroid.max():.4f}]")
        print(f"    std range: [{config.std.min():.6f}, {config.std.max():.4f}]")
        if config.inv_covariance is not None:
            print(f"    covariance: full ({config.num_features}x{config.num_features})")
        else:
            print("    covariance: diagonal (insufficient samples for full matrix)")

        if not args.dry_run:
            output_path = models_dir / f"ood_{schema_name}.json"
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(config.to_dict(), f, indent=2, ensure_ascii=False)
            print(f"    saved: {output_path}")

    if args.dry_run:
        print("\n[Dry run — no files written. Remove --dry-run to write OOD configs.]")
    else:
        print(f"\n[DONE] OOD configs written to {models_dir}/")

    # ── Quick validation: compute distances for first 100 samples ──
    print("\n── Validation: Mahalanobis distances on training data ──")
    for schema_name in sorted(schema_values.keys()):
        config_path = models_dir / f"ood_{schema_name}.json"
        if not config_path.exists():
            continue
        with open(config_path, encoding="utf-8") as f:
            config_data = json.load(f)
        config = __import__(
            "core.execution.ood_gateway", fromlist=["OODConfig"]
        ).OODConfig.from_dict(config_data)

        vecs = schema_values[schema_name]
        X = np.array(vecs[: min(500, len(vecs))], dtype=np.float64)
        valid_mask = np.isfinite(X).all(axis=1)
        X = X[valid_mask]

        gateway = __import__("core.execution.ood_gateway", fromlist=["OODGateway"]).OODGateway()
        distances = []
        for row in X:
            verdict = gateway.check(row, schema_name=schema_name)
            distances.append(verdict.distance)

        if distances:
            dists = np.array(distances)
            pct_blocked = np.mean(dists >= config.threshold_block) * 100
            pct_cautious = (
                np.mean((dists >= config.threshold_cautious) & (dists < config.threshold_block))
                * 100
            )
            print(
                f"  {schema_name}: "
                f"mean_dist={np.mean(dists):.2f} "
                f"median={np.median(dists):.2f} "
                f"P95={np.percentile(dists, 95):.2f} "
                f"P99={np.percentile(dists, 99):.2f} "
                f"blocked={pct_blocked:.1f}% "
                f"cautious={pct_cautious:.1f}% "
                f"(expected: blocked≈1% cautious≈4%)"
            )


if __name__ == "__main__":
    main()
