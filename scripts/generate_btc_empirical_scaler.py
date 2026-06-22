#!/usr/bin/env python
"""DQAF-20260622-054: Generate BTC-dedicated empirical micro scaler.

Read-only ingestion.  Reads BTC Feature Store records → derives
9 microstructure features from available bar-level data → fits
a StandardScaler → saves JSON to ``data_btc/models/btc_micro_scaler.json``.

**Design constraint (投委会 VETO):** the scaler MUST be fit exclusively
on BTCUSDc data.  Cross-symbol reuse of XAU scaler is prohibited because
BTC and XAU have fundamentally different market microstructure (tick
density, order-book depth, spread characteristics).

**Data source:** the Feature Store's 7,175 M5 records carry bar-level
features (M5_Ret_1, M5_Body_Ratio, M5_ATR_14, etc.) from which we
approximate the 9 microstructure inputs.  For tick-level-only features
(avg_spread, OIM, tick_velocity) that aren't directly available in the
40-dim vector we use BTC-relevant proxies.

Usage::

    python scripts/generate_btc_empirical_scaler.py          # dry-run
    python scripts/generate_btc_empirical_scaler.py --write  # save scaler
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.preprocessing import StandardScaler

from core.features.schemas.microstructure_schema import MICROSTRUCTURE_9_FEATURES

# ── Constants ────────────────────────────────────────────────────────

FEATURE_STORE_PATH = Path(
    "data_btc/feature_store/records/symbol=BTCUSDc/timeframe=M5/features.jsonl"
)
OUTPUT_PATH = Path("data_btc/models/btc_micro_scaler.json")


# ── Feature computation ──────────────────────────────────────────────


def load_btc_feature_vectors(path: Path) -> np.ndarray:
    """Load the 40-dim feature matrix from the BTC Feature Store.

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
            # Extract in canonical order (not alphabetical)
            records.append([float(vals.get(name, 0.0)) for name in canonical_order])
        elif isinstance(vals, list) and len(vals) == 40:
            records.append([float(v) for v in vals])
    return np.asarray(records, dtype=np.float64)


def compute_micro_features(feature_matrix: np.ndarray) -> np.ndarray:
    """Derive 9 microstructure features from the v9_institutional_40 matrix.

    Mapping (v9_institutional_40 indices → micro feature index):
      [0] M5_Ret_1          → tick_return     (direct)
      [1] M5_Body_Ratio     → hl_ratio proxy  (|body| / range)
      [2] M5_ATR_14         → volatility scale
      [5] M5_Vol_ZScore     → tick_velocity proxy
      [6] M5_Macro1_Corr    → avg_spread proxy (correlation ≈ liquidity)
      [7] M5_Price_ZScore   → OIM proxy (price pressure)

    Features 6-8 (forex returns) are set to 0 for BTC — BTC does not
    co-vary with XAGUSDc, EURUSDc, or USDJPYc in the same way XAU does.
    The scaler will learn ``mean≈0, scale≈small_constant`` for these,
    which is the correct statistical representation.
    """
    n_samples = feature_matrix.shape[0]
    micro = np.zeros((n_samples, 9), dtype=np.float64)

    # [0] tick_return  ← M5_Ret_1
    micro[:, 0] = feature_matrix[:, 0]

    # [1] hl_ratio  ← |M5_Body_Ratio| (bar body as fraction of range)
    micro[:, 1] = np.abs(feature_matrix[:, 1])

    # [2] co_ratio  ← M5_Body_Ratio (close-open relative to range proxy)
    micro[:, 2] = feature_matrix[:, 1]

    # [3] avg_spread  ← |M5_Macro1_Corr| scaled (correlation strength → liquidity)
    micro[:, 3] = np.abs(feature_matrix[:, 6]) * 0.01

    # [4] OIM  ← M5_Price_ZScore (price pressure ≈ order imbalance)
    micro[:, 4] = feature_matrix[:, 7]

    # [5] tick_velocity  ← M5_ATR_14 / 100 (ATR in USD → vol-velocity proxy)
    micro[:, 5] = feature_matrix[:, 2] / 100.0

    # [6-8] forex returns — zero for BTC (投委会: no cross-species leakage)
    micro[:, 6] = 0.0
    micro[:, 7] = 0.0
    micro[:, 8] = 0.0

    # ── Sanity filters ──
    # Replace infinities (from division by zero in raw features)
    micro = np.nan_to_num(micro, nan=0.0, posinf=0.0, neginf=0.0)
    # Clip extreme outliers (>10σ equivalent for stability)
    for j in range(9):
        col = micro[:, j]
        std = np.std(col)
        if std > 0:
            med = np.median(col)
            col = np.clip(col, med - 10 * std, med + 10 * std)
            micro[:, j] = col

    return micro


# ── Main ─────────────────────────────────────────────────────────────


def main() -> None:
    dry_run = "--write" not in sys.argv

    print("=" * 65)
    print("  DQAF-20260622-054: BTC Empirical Micro Scaler Generator")
    print("=" * 65)

    # 1. Load
    if not FEATURE_STORE_PATH.exists():
        print(f"[FAIL] Feature Store not found: {FEATURE_STORE_PATH}")
        print("  Run daily_ops first to populate the feature store.")
        sys.exit(1)

    print(f"\n[1/4] Loading BTC feature vectors from {FEATURE_STORE_PATH} ...")
    matrix = load_btc_feature_vectors(FEATURE_STORE_PATH)
    print(f"      Loaded {matrix.shape[0]:,} records × {matrix.shape[1]} features")

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
            "BTCUSDc empirical micro scaler — fit on Feature Store M5 data. "
            "Features 6-8 (forex returns) are zero-mean because BTC does not "
            "co-vary with XAG/USD, EUR/USD, JPY/USD the way XAU does. "
            "Generated by scripts/generate_btc_empirical_scaler.py (DQAF-054)."
        ),
    }

    print(f"      mean_  = {[round(v, 6) for v in scaler_data['mean_']]}")
    print(f"      scale_ = {[round(v, 6) for v in scaler_data['scale_']]}")

    # 4. Save
    print(
        f"\n[4/4] {'[DRY-RUN] Would write' if dry_run else 'Writing'} scaler to {OUTPUT_PATH} ..."
    )
    if not dry_run:
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(
            json.dumps(scaler_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"      Wrote {OUTPUT_PATH.stat().st_size:,} bytes")
    else:
        print("      (dry-run — add --write to persist)")

    # Verify round-trip
    if not dry_run:
        from core.features.adapters.microstructure_feature_adapter import (
            MicrostructureFeatureAdapter,
        )

        adapter = MicrostructureFeatureAdapter(OUTPUT_PATH, require_scaler=False)
        assert adapter._scaler is not None, "Round-trip failed: scaler did not load!"
        print(
            f"      Round-trip verified: scaler loads correctly ({adapter._scaler.n_features_in_} features)"
        )

    print(f"\n{'=' * 65}")
    print(f"  DONE — {'scaler saved' if not dry_run else 'dry-run complete (add --write)'}")
    print(f"{'=' * 65}")


if __name__ == "__main__":
    main()
