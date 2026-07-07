"""OFI History Accumulation Monitor — readiness gate for 46-dim retrain.

DQAF-20260707-005 Phase 2: The bridge worker appends each settled OFI bar
to ``ofi_history.jsonl``.  This script reports accumulation progress so we
know when enough aligned OFI data exists to build the 46-dim training set.

Statistics reported (Iron Law #11 — script is the only legal evidence source):
  - Record count + wall-clock span + distinct H1 windows
  - Per-feature coverage (non-zero rate) for the 5 flow features
  - Delta_Divergence firing rate (sparse binary — informs effective dim)
  - Volume_Real_Ratio availability (BTC often lacks real_volume)
  - Dual-count readiness gates (raw settle ≠ H1 training sample):
      Gate 1 (screening): raw settles + regime span → Wasserstein feature scan
      Gate 2 (retrain):   distinct H1 windows → H1-cadence training samples

Usage::

    python scripts/inspect_ofi_history.py --data-dir data_btc
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
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

# ── Dual-count readiness gates (raw settle ≠ H1 training sample) ──
# Gate 1 (screening): raw 30s settles + regime span → Wasserstein feature scan.
#   raw settles accrue ~2,880/day, but a valid scan needs ≥1 week of varied
#   regimes so forward-return labels are not drawn from a single trend.
# Gate 2 (retrain): distinct H1 windows → H1-cadence training samples.
#   The H1 direction model trains on 1 sample/hour (verified: btc_swing_v12_h1
#   NPZ timestamps are spaced exactly 3600s). 5,000 raw settles ≈ only ~42 H1
#   windows, so raw count MUST NOT gate a retrain. 1,000 H1 windows is the
#   minimum for a transfer/adapter retrain (freeze 41-dim, learn OFI delta).
_EVAL_RAW_THRESHOLD = 2_000  # Gate 1: min raw settles for Wasserstein scan
_EVAL_SPAN_DAYS = 7.0  # Gate 1: min wall-clock span for regime coverage
_RETRAIN_H1_THRESHOLD = 1_000  # Gate 2: min distinct H1 windows (adapter-viable)
_H1_BUCKET_LEN = 13  # "YYYY-MM-DDTHH" prefix identifies one H1 window


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


def _distinct_h1_windows(records: list[dict[str, Any]]) -> int:
    """Count unique calendar-hour buckets — the true H1 training-sample count."""
    buckets: set[str] = set()
    for r in records:
        t = r.get("time")
        if isinstance(t, str) and len(t) >= _H1_BUCKET_LEN:
            buckets.add(t[:_H1_BUCKET_LEN])
    return len(buckets)


def _span_days(records: list[dict[str, Any]]) -> float:
    """Wall-clock span between first and last record, in days (regime coverage)."""
    times = [t for r in records if isinstance((t := r.get("time")), str)]
    if len(times) < 2:
        return 0.0
    try:
        delta = datetime.fromisoformat(times[-1]) - datetime.fromisoformat(times[0])
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, delta.total_seconds() / 86_400.0)


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
    span_days = _span_days(records)
    h1_windows = _distinct_h1_windows(records)
    result["first_ts"] = first_ts
    result["last_ts"] = last_ts
    result["span_days"] = round(span_days, 2)
    result["distinct_h1_windows"] = h1_windows

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

    # ── Gate 1: Wasserstein screening (raw settles + regime span) ──
    eval_ready = n >= _EVAL_RAW_THRESHOLD and span_days >= _EVAL_SPAN_DAYS
    result["gate1_screening"] = {
        "ready": eval_ready,
        "detail": (
            f"{n:,}/{_EVAL_RAW_THRESHOLD:,} raw settles, "
            f"{span_days:.1f}/{_EVAL_SPAN_DAYS:.0f}d span"
        ),
    }

    # ── Gate 2: H1-cadence retrain (distinct H1 windows) ──
    retrain_ready = h1_windows >= _RETRAIN_H1_THRESHOLD
    result["gate2_retrain"] = {
        "ready": retrain_ready,
        "detail": f"{h1_windows:,}/{_RETRAIN_H1_THRESHOLD:,} distinct H1 windows",
    }

    # ── Overall verdict ──
    if retrain_ready:
        verdict = f"RETRAIN_READY — {h1_windows:,} H1 windows ≥ {_RETRAIN_H1_THRESHOLD:,}"
    elif eval_ready:
        verdict = (
            f"EVAL_READY — Gate 1 met ({n:,} settles / {span_days:.1f}d); "
            f"Gate 2 at {h1_windows:,}/{_RETRAIN_H1_THRESHOLD:,} H1 windows"
        )
    else:
        pct = 100 * n / _EVAL_RAW_THRESHOLD
        verdict = (
            f"ACCUMULATING — {n:,}/{_EVAL_RAW_THRESHOLD:,} settles ({pct:.0f}%), "
            f"{span_days:.1f}/{_EVAL_SPAN_DAYS:.0f}d span, {h1_windows:,} H1 windows"
        )
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
    print(
        f"           ({stats['span_days']:.2f} days, {stats['distinct_h1_windows']:,} distinct H1 windows)"
    )
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
    g1, g2 = stats["gate1_screening"], stats["gate2_retrain"]
    print("\n  Gates:")
    print(f"    [{'✓' if g1['ready'] else ' '}] Gate 1 (Wasserstein scan):  {g1['detail']}")
    print(f"    [{'✓' if g2['ready'] else ' '}] Gate 2 (H1 retrain):        {g2['detail']}")
    print(f"\n  VERDICT: {stats['verdict']}")


if __name__ == "__main__":
    main()
