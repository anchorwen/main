"""OFI History Accumulation Monitor — readiness gate for 46-dim retrain.

DQAF-20260707-005 Phase 2: The bridge worker appends each settled OFI bar
to ``ofi_history.jsonl``.  This script reports accumulation progress so we
know when enough aligned OFI data exists to build the 46-dim training set.

Statistics reported (Iron Law #11 — script is the only legal evidence source):
  - Record count + wall-clock span
  - Per-feature coverage (non-zero rate) for the 5 flow features
  - Delta_Divergence firing rate (sparse binary — informs effective dim)
  - Volume_Real_Ratio availability (BTC often lacks real_volume)
  - Readiness verdict against evaluation (2,000) and retrain (5,000) thresholds

Usage::

    python scripts/inspect_ofi_history.py --data-dir data_btc
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

# ── 5 flow features carried into btc_macro_flow_46 (DQAF-20260707-004) ──
_FLOW_FEATURES = [
    "OFI_M5",
    "OFI_ZScore_20",
    "OFI_Cumulative_Delta",
    "OFI_Delta_Divergence",
    "OFI_Volume_Real_Ratio",
]

# ── Readiness thresholds (settled bars, ~2,880/day at 30s cadence) ──
_EVAL_THRESHOLD = 2_000  # Minimum for Wasserstein directional evaluation
_RETRAIN_THRESHOLD = 5_000  # Minimum for a competitive production retrain


def _load_history(path: Path) -> list[dict[str, Any]]:
    """Load all OFI history records; skip malformed lines (fail-open read)."""
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(rec, dict):
                records.append(rec)
    return records


def inspect(data_dir: Path) -> dict[str, Any]:
    """Compute accumulation statistics from ofi_history.jsonl."""
    hist_path = data_dir / "reports" / "ofi_history.jsonl"
    records = _load_history(hist_path)
    n = len(records)

    result: dict[str, Any] = {
        "history_path": str(hist_path),
        "n_records": n,
    }

    if n == 0:
        result["verdict"] = "NO_DATA — bridge has not settled any OFI bar yet"
        return result

    # ── Wall-clock span ──
    first_ts = records[0].get("time", "?")
    last_ts = records[-1].get("time", "?")
    result["first_ts"] = first_ts
    result["last_ts"] = last_ts

    # ── Per-feature non-zero coverage ──
    coverage: dict[str, float] = {}
    for feat in _FLOW_FEATURES:
        nonzero = sum(1 for r in records if abs(float(r.get(feat, 0.0) or 0.0)) > 1e-12)
        coverage[feat] = round(nonzero / n, 4)
    result["nonzero_coverage"] = coverage

    # ── Divergence firing rate (sparse binary informs effective dim) ──
    div_fires = sum(1 for r in records if float(r.get("OFI_Delta_Divergence", 0.0) or 0.0) > 0.5)
    result["divergence_fire_rate"] = round(div_fires / n, 4)

    # ── real_volume availability (BTC broker-dependent) ──
    real_vol_present = coverage.get("OFI_Volume_Real_Ratio", 0.0) > 0.0
    result["volume_real_available"] = real_vol_present

    # ── Effective dimensionality estimate ──
    # A flow feature is "live" if it carries signal in >5% of bars.
    live_feats = [f for f, cov in coverage.items() if cov > 0.05]
    result["live_flow_features"] = live_feats
    result["effective_flow_dim"] = len(live_feats)
    result["effective_schema_dim"] = 41 + len(live_feats)

    # ── Readiness verdict ──
    if n >= _RETRAIN_THRESHOLD:
        verdict = f"RETRAIN_READY — {n:,} bars ≥ {_RETRAIN_THRESHOLD:,} (production retrain viable)"
    elif n >= _EVAL_THRESHOLD:
        pct = 100 * n / _RETRAIN_THRESHOLD
        verdict = (
            f"EVAL_READY — {n:,} bars ≥ {_EVAL_THRESHOLD:,} (Wasserstein scan viable; "
            f"{pct:.0f}% to retrain threshold)"
        )
    else:
        pct = 100 * n / _EVAL_THRESHOLD
        verdict = f"ACCUMULATING — {n:,}/{_EVAL_THRESHOLD:,} bars ({pct:.0f}% to eval threshold)"
    result["verdict"] = verdict

    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="OFI history accumulation monitor")
    ap.add_argument("--data-dir", default="data_btc", help="Data directory root")
    ap.add_argument("--json", action="store_true", help="Emit raw JSON only")
    args = ap.parse_args()

    stats = inspect(Path(args.data_dir))

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return

    print("=" * 60)
    print("OFI History Accumulation Monitor (DQAF-20260707-005)")
    print("=" * 60)
    print(f"  File:    {stats['history_path']}")
    print(f"  Records: {stats['n_records']:,}")

    if stats["n_records"] == 0:
        print(f"\n  {stats['verdict']}")
        return

    print(f"  Span:    {stats['first_ts']} → {stats['last_ts']}")
    print("\n  Non-zero coverage per flow feature:")
    for feat, cov in stats["nonzero_coverage"].items():
        bar = "#" * int(cov * 20)
        print(f"    {feat:24s} {cov * 100:5.1f}% {bar}")
    print(f"\n  Divergence fire rate:  {stats['divergence_fire_rate'] * 100:.1f}%")
    print(f"  real_volume available: {stats['volume_real_available']}")
    print(
        f"  Live flow features:    {stats['effective_flow_dim']}/5 → "
        f"effective schema = {stats['effective_schema_dim']}-dim"
    )
    print(f"\n  VERDICT: {stats['verdict']}")


if __name__ == "__main__":
    main()
