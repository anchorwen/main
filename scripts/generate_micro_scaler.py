#!/usr/bin/env python
"""DQAF-20260622-058: Generate per-symbol empirical micro scaler (multi-asset).

Supersedes the BTC-only ``generate_btc_empirical_scaler.py`` (DQAF-054).
Supports BTCUSDc (empirical from Feature Store) and XAUUSDc (cold-start
identity until Feature Store data accumulates).

Read-only ingestion.  Reads Feature Store records → derives 9 microstructure
features from available bar-level data → fits a StandardScaler → saves JSON
to ``data_{shorthand}/models/{shorthand}_micro_scaler.json``.

**Design constraint (投委会 VETO):** scalers MUST be fit exclusively on the
target symbol's data.  Cross-symbol reuse is prohibited — BTC and XAU have
fundamentally different market microstructure (tick density, order-book depth,
spread characteristics).

Usage::

    # BTC — empirical from Feature Store
    python scripts/generate_micro_scaler.py --symbol BTCUSDC          # dry-run
    python scripts/generate_micro_scaler.py --symbol BTCUSDC --write  # save

    # XAU — cold-start identity (no Feature Store data yet)
    python scripts/generate_micro_scaler.py --symbol XAUUSDc --cold-start --write

    # Override data directory
    python scripts/generate_micro_scaler.py --symbol BTCUSDC --data-dir data_btc
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

from core.features.adapters.microstructure_feature_adapter import (
    MicrostructureFeatureAdapter,
)
from core.features.schemas.microstructure_schema import MICROSTRUCTURE_9_FEATURES

# ── Path resolution ───────────────────────────────────────────────────


def resolve_paths(symbol: str, data_dir: str | None = None) -> tuple[Path, Path, str]:
    """Resolve feature-store path and output path for *symbol*.

    Returns (feature_store_path, output_path, shorthand).
    """
    shorthand = symbol.lower()[:3]
    base = Path(data_dir) if data_dir else Path(f"data_{shorthand}")

    feature_store_path = (
        base / "feature_store" / "records" / f"symbol={symbol}" / "timeframe=M5" / "features.jsonl"
    )
    output_path = base / "models" / f"{shorthand}_micro_scaler.json"
    return feature_store_path, output_path, shorthand


# ── Feature computation ────────────────────────────────────────────────


def load_feature_vectors(path: Path) -> np.ndarray:
    """Load the 40-dim feature matrix from the Feature Store.

    Returns (N, 40) float64 array in canonical v9_institutional_40 order.
    """
    from core.features.schemas.registry import get_schema_feature_names

    canonical_order = get_schema_feature_names("v9_institutional_40")
    records: list[list[float]] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        vals = entry.get("values")
        et = entry.get("event_time", "")
        if et in seen:
            continue
        seen.add(et)
        if isinstance(vals, dict) and len(vals) == 40:
            records.append([float(vals.get(name, 0.0)) for name in canonical_order])
        elif isinstance(vals, list) and len(vals) == 40:
            records.append([float(v) for v in vals])
    return np.asarray(records, dtype=np.float64)


def compute_micro_features(feature_matrix: np.ndarray) -> np.ndarray:
    """Derive 9 microstructure features from the v9_institutional_40 matrix.

    Mapping (v9_institutional_40 indices → micro feature index):
      [0] M5_Ret_1          → tick_return     (direct)
      [1] M5_Body_Ratio     → hl_ratio proxy  (|body|/range)
      [2] M5_ATR_14         → volatility scale
      [5] M5_Vol_ZScore     → tick_velocity proxy
      [6] M5_Macro1_Corr    → avg_spread proxy (correlation ≈ liquidity)
      [7] M5_Price_ZScore   → OIM proxy (price pressure)

    Features 6-8 (forex returns) are set to 0 — cross-pair returns are
    instrument-specific.  The scaler will learn ``mean≈0, scale≈small_constant``
    for these, which is the correct statistical representation.
    """
    n_samples = feature_matrix.shape[0]
    micro = np.zeros((n_samples, 9), dtype=np.float64)

    # [0] tick_return  ← M5_Ret_1
    micro[:, 0] = feature_matrix[:, 0]
    # [1] hl_ratio  ← |M5_Body_Ratio|
    micro[:, 1] = np.abs(feature_matrix[:, 1])
    # [2] co_ratio  ← M5_Body_Ratio
    micro[:, 2] = feature_matrix[:, 1]
    # [3] avg_spread  ← |M5_Macro1_Corr| * 0.01
    micro[:, 3] = np.abs(feature_matrix[:, 6]) * 0.01
    # [4] OIM  ← M5_Price_ZScore
    micro[:, 4] = feature_matrix[:, 7]
    # [5] tick_velocity  ← M5_ATR_14 / 100
    micro[:, 5] = feature_matrix[:, 2] / 100.0
    # [6-8] forex returns — zero (投委会: no cross-species leakage)
    micro[:, 6] = 0.0
    micro[:, 7] = 0.0
    micro[:, 8] = 0.0

    # ── Sanity filters ──
    micro = np.nan_to_num(micro, nan=0.0, posinf=0.0, neginf=0.0)
    for j in range(9):
        col = micro[:, j]
        std = np.std(col)
        if std > 0:
            med = np.median(col)
            col = np.clip(col, med - 10 * std, med + 10 * std)
            micro[:, j] = col

    return micro


# ── Empirical scaler fitting ───────────────────────────────────────────


def fit_empirical_scaler(
    feature_store_path: Path,
    output_path: Path,
    symbol: str,
    shorthand: str,
    dry_run: bool,
) -> None:
    """Load Feature Store data, fit StandardScaler, save JSON."""
    from sklearn.preprocessing import StandardScaler

    # 1. Load
    if not feature_store_path.exists():
        print(f"[FAIL] Feature Store not found: {feature_store_path}")
        print(f"  Run daily_ops --symbol {symbol} first to populate the feature store.")
        print("  Or use --cold-start for an identity scaler.")
        sys.exit(1)

    print(f"\n[1/4] Loading {symbol} feature vectors from {feature_store_path} ...")
    matrix = load_feature_vectors(feature_store_path)
    print(f"      Loaded {matrix.shape[0]:,} records × {matrix.shape[1]} features")

    if matrix.shape[0] < 100:
        print(
            f"[WARN] Only {matrix.shape[0]} records — scaler will be unreliable. "
            f"Recommend ≥5000 records for a stable fit."
        )

    # 2. Compute micro features
    print("\n[2/4] Computing 9 microstructure features ...")
    micro = compute_micro_features(matrix)
    print(f"      Shape: {micro.shape}")
    for j, name in enumerate(MICROSTRUCTURE_9_FEATURES):
        col = micro[:, j]
        print(
            f"      [{j}] {name:20s}: mean={np.mean(col):.6f}  std={np.std(col):.6f}  "
            f"min={np.min(col):.4f}  max={np.max(col):.4f}"
        )

    # 3. Fit scaler
    print(f"\n[3/4] Fitting StandardScaler on {micro.shape[0]:,} samples ...")
    scaler = StandardScaler()
    scaler.fit(micro)

    scaler_data: dict[str, Any] = {
        "mean_": scaler.mean_.tolist(),
        "scale_": scaler.scale_.tolist(),
        "var_": scaler.var_.tolist(),
        "n_features_in_": int(scaler.n_features_in_),
        "feature_names_in_": list(MICROSTRUCTURE_9_FEATURES),
        "_comment": (
            f"{symbol} empirical micro scaler — fit on {matrix.shape[0]:,} "
            f"Feature Store M5 records.  Generated by "
            f"scripts/generate_micro_scaler.py (DQAF-058)."
        ),
    }

    print(f"      mean_  = {[round(v, 6) for v in scaler_data['mean_']]}")
    print(f"      scale_ = {[round(v, 6) for v in scaler_data['scale_']]}")

    # 4. Save
    print(
        f"\n[4/4] {'[DRY-RUN] Would write' if dry_run else 'Writing'} scaler to {output_path} ..."
    )
    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(scaler_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"      Wrote {output_path.stat().st_size:,} bytes")
    else:
        print("      (dry-run — add --write to persist)")

    # Verify round-trip
    if not dry_run:
        adapter = MicrostructureFeatureAdapter(output_path, require_scaler=False)
        assert adapter._scaler is not None, "Round-trip failed: scaler did not load!"
        print(
            f"      Round-trip verified: scaler loads correctly "
            f"({adapter._scaler.n_features_in_} features)"
        )


# ── Main ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate per-symbol empirical micro scaler (DQAF-058)",
    )
    parser.add_argument(
        "--symbol",
        default="BTCUSDC",
        choices=["BTCUSDC", "XAUUSDc"],
        help="Trading symbol (default: BTCUSDC)",
    )
    parser.add_argument(
        "--cold-start",
        action="store_true",
        help="Generate identity scaler for instruments without Feature Store data",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Persist scaler to disk (default: dry-run)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Override data directory (default: data_{symbol_shorthand})",
    )
    args = parser.parse_args()

    dry_run = not args.write
    feature_store_path, output_path, shorthand = resolve_paths(args.symbol, args.data_dir)

    print("=" * 65)
    print(
        f"  DQAF-20260622-058: {args.symbol} Micro Scaler Generator"
        + (" [COLD-START]" if args.cold_start else "")
    )
    print("=" * 65)
    print(f"  Symbol:        {args.symbol}")
    print(f"  Shorthand:     {shorthand}")
    print(f"  Output:        {output_path}")
    if not args.cold_start:
        print(f"  Feature Store: {feature_store_path}")
    print(
        f"  Mode:          {'cold-start (identity)' if args.cold_start else 'empirical from Feature Store'}"
    )
    print(f"  Dry-run:       {dry_run}")

    # ── Cold-start path ──
    if args.cold_start:
        if dry_run:
            print(
                "\n[Cold-Start] Would generate identity scaler "
                f"(mean=0, scale=1, {len(MICROSTRUCTURE_9_FEATURES)} features)"
            )
            print(f"              Output: {output_path}")
            print("\n              Add --write to persist.")
        else:
            MicrostructureFeatureAdapter.generate_cold_start_scaler(output_path)
            # Verify round-trip
            adapter = MicrostructureFeatureAdapter(output_path, require_scaler=False)
            assert adapter._scaler is not None, "Round-trip failed!"
            print(
                f"      Round-trip verified: cold-start scaler loads correctly "
                f"({adapter._scaler.n_features_in_} features)"
            )
        print(f"\n{'=' * 65}")
        print(
            f"  DONE — {'cold-start scaler saved' if not dry_run else 'dry-run complete (add --write)'}"
        )
        print(f"{'=' * 65}")
        return

    # ── Empirical path ──
    if not feature_store_path.exists():
        print(f"\n[FAIL] Feature Store not found: {feature_store_path}")
        print("  Options:")
        print("    1. Run daily_ops first to populate the feature store.")
        print("    2. Use --cold-start for an identity scaler (no Feature Store needed).")
        sys.exit(1)

    fit_empirical_scaler(feature_store_path, output_path, args.symbol, shorthand, dry_run)

    print(f"\n{'=' * 65}")
    print(f"  DONE — {'scaler saved' if not dry_run else 'dry-run complete (add --write)'}")
    print(f"{'=' * 65}")


if __name__ == "__main__":
    main()
