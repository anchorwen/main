"""build_btc_flow46_dataset.py — M5-cadence OFI→46-dim aligned dataset builder.

M4 Phase 6.1 (IC 终局裁决, 2026-08-03): the OFI residual transfer's dataset
battle.  It aligns high-frequency OFI settles to the institutional base M5
bars and emits the 46-dim training set + a ``flow_alignment.json`` audit that
PROVES no look-ahead leak.

Alignment rule (leak-free by construction):
    For each base M5 bar at wall-clock W, the aligned flow vector is the LAST
    settle with ``settle_wall <= W`` (state known AT the bar open — never
    future data).  Wall-clock comparison uses the SAME basis as the base
    dataset (base epoch == CSV wall-clock-as-if-UTC, pandas naive semantics;
    OFI ``time`` strings are the same machine wall-clock).  Explicit epoch
    timezone conversion is FORBIDDEN here — it silently shifts alignment by
    the UTC offset (verified empirically: a UTC-naive parse misaligns by 8h).

Leak audit (flow_alignment.json):
    - settle_lag_seconds  = W - settle_wall  per aligned row
        lag < 0       → LOOK-AHEAD LEAK (FAIL)
        lag > 3600s   → timezone-mismatch warning (the bridge basis is not
                        the base dataset basis — the audit catches the 8h
                        shift before it ever trains a model)
    - per-feature non-zero coverage → effective_flow_dim (dead dims are
      zero-padded downstream, never dropped)

Output:
    <output-dir>/btc_flow46_aligned.npz      — X(n,46)=[X_41 base, flow_5],
                                                y_long, y_short, timestamps,
                                                feature_names(46), schema_id
    <output-dir>/flow_alignment.json         — the audit report (Iron Law #11:
                                                every number a script's output)

Usage:
    python scripts/training/build_btc_flow46_dataset.py \
        --base data_btc/training/btc_ssot_v2 \
        --ofi data_btc/reports/ofi_history.jsonl \
        --output-dir data_btc/training/btc_flow46_v1 \
        --tf-minutes 5
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Single-source of truth for the 5 flow features + the OFI history loader —
# reused from the Gate 2 monitor so the feature list can never drift.
from scripts.inspect_ofi_history import _FLOW_FEATURES, _load_history  # noqa: E402

SCHEMA_ID = "btc_macro_flow_46"
_LEAK_SLACK_SECONDS = 3600.0  # lag > 1h → timezone-basis warning (8h shift detected)
_BAR_SLACK_SECONDS = 600.0  # worst-case legit stall tolerance for the p99 note


def _parse_wall_clock(iso: str) -> datetime:
    """Parse an OFI ``time`` string as naive wall-clock (same basis as CSV)."""
    return datetime.fromisoformat(iso)


def _wall_seconds(dt: datetime) -> float:
    """Wall-clock datetime → seconds-as-if-UTC (matches base NPZ epoch basis)."""
    return dt.replace(tzinfo=UTC).timestamp()


def _base_wall_seconds(epoch: float) -> float:
    """Base NPZ epoch IS wall-clock-as-if-UTC seconds — no conversion needed."""
    return float(epoch)


def build_flow46(
    base_dir: Path,
    ofi_path: Path,
    output_dir: Path,
    tf_minutes: int = 5,
) -> dict[str, Any]:
    """Align OFI settles to base M5 bars; write NPZ + audit; return the audit."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Load base dataset (train split carries the full feature row order) ──
    train_path = base_dir / "train.npz"
    test_path = base_dir / "test.npz"
    for p in (train_path, test_path):
        if not p.exists():
            raise FileNotFoundError(f"base split missing: {p}")

    from core.training.model_hashing import hash_file

    base_hash = hash_file(train_path)
    base = np.load(train_path, allow_pickle=True)
    base_names = base["feature_names"].tolist()
    schema_names = _flow46_feature_names()

    if base_names != schema_names[:41]:
        raise RuntimeError(
            "base NPZ feature order != btc_macro_flow_46 base half — "
            "runtime slicing would misalign (train row order mismatch)."
        )

    # ── Load base TEST rows (the only split overlapping the OFI window) ──
    bt = np.load(test_path, allow_pickle=True)
    X41 = np.asarray(bt["X"], dtype=np.float64)
    y_long = np.asarray(bt["y_long"], dtype=np.float64)
    y_short = np.asarray(bt["y_short"], dtype=np.float64)
    ts = np.asarray(bt["timestamps"], dtype=np.float64)

    # ── Load + pre-parse OFI settles (chronological, one linear merge) ──
    records = _load_history(ofi_path)
    n_ofi = len(records)
    ofi_wall = np.array(
        [_wall_seconds(_parse_wall_clock(r["time"])) for r in records],
        dtype=np.float64,
    )
    flow_matrix = np.array(
        [[float(r.get(f, 0.0) or 0.0) for f in _FLOW_FEATURES] for r in records],
        dtype=np.float64,
    )
    flow_matrix = np.nan_to_num(flow_matrix, nan=0.0, posinf=0.0, neginf=0.0, copy=False)

    # ── Linear merge: for each base row (asc), last settle with wall <= W ──
    order = np.argsort(ts, kind="stable")
    sorted_ts = ts[order]
    X41_s = X41[order]
    y_long_s = y_long[order]
    y_short_s = y_short[order]

    aligned_X: list[np.ndarray] = []
    aligned_y_long: list[float] = []
    aligned_y_short: list[float] = []
    aligned_ts: list[float] = []
    lag_list: list[float] = []
    n_skipped_no_flow = 0

    j = 0  # index of the last OFI record consumed (wall <= current base row)
    n_ofi_used = 0
    for i, w in enumerate(sorted_ts):
        while j < n_ofi and ofi_wall[j] <= w:
            j += 1
        if j == 0:
            # No settle at-or-before this base row (before the bridge started).
            n_skipped_no_flow += 1
            continue
        last = j - 1
        n_ofi_used = max(n_ofi_used, last + 1)
        lag = w - ofi_wall[last]
        lag_list.append(float(lag))
        flow_vec = flow_matrix[last]
        aligned_X.append(np.concatenate([X41_s[i], flow_vec]))
        aligned_y_long.append(float(y_long_s[i]))
        aligned_y_short.append(float(y_short_s[i]))
        aligned_ts.append(float(w))

    if not aligned_X:
        raise RuntimeError("zero aligned rows — OFI window does not overlap base test split")

    X46 = np.stack(aligned_X)
    yl = np.array(aligned_y_long, dtype=np.float64)
    ys = np.array(aligned_y_short, dtype=np.float64)
    timestamps = np.array(aligned_ts, dtype=np.float64)
    lags = np.array(lag_list, dtype=np.float64)

    # ── Leak audit ──
    min_lag = float(lags.min())
    max_lag = float(lags.max())
    mean_lag = float(lags.mean())
    p99_lag = float(np.percentile(lags, 99))
    leak_fail = min_lag < 0.0
    tz_warning = p99_lag > _LEAK_SLACK_SECONDS
    leak_verdict = "PASS" if not leak_fail else "LEAK"
    if tz_warning:
        leak_verdict += f" + TZ_WARNING (p99 lag {p99_lag:.0f}s > {_LEAK_SLACK_SECONDS:.0f}s)"

    # ── Per-feature coverage → effective flow dim ──
    flow_all = X46[:, 41:46]
    coverage: dict[str, float] = {}
    for k, f in enumerate(_FLOW_FEATURES):
        coverage[f] = round(float(np.mean(np.abs(flow_all[:, k]) > 1e-12)), 4)
    live_feats = [f for f, cov in coverage.items() if cov > 0.05]
    effective_flow_dim = len(live_feats)

    # ── Persist ──
    out_path = output_dir / "btc_flow46_aligned.npz"
    np.savez_compressed(
        out_path,
        X=X46,
        y_long=yl,
        y_short=ys,
        timestamps=timestamps,
        feature_names=np.array(schema_names, dtype=object),
        schema_id=np.array([SCHEMA_ID], dtype=object),
        base_dataset_hash=np.array([base_hash], dtype=object),
        base_split=np.array(["test"] * len(timestamps), dtype=object),
    )

    audit: dict[str, Any] = {
        "schema_id": SCHEMA_ID,
        "tf_minutes": tf_minutes,
        "alignment_mode": "last_settle_le_bar_open (leak-free, wall-clock same basis)",
        "base_dataset": {
            "dir": str(base_dir),
            "train_npz_hash": base_hash,
            "feature_order_matches_schema": True,
        },
        "ofi": {
            "path": str(ofi_path),
            "n_records": n_ofi,
            "first_time": records[0].get("time"),
            "last_time": records[-1].get("time"),
        },
        "alignment": {
            "n_base_test_rows": int(len(ts)),
            "n_aligned_rows": int(len(timestamps)),
            "n_skipped_no_flow": n_skipped_no_flow,
            "n_ofi_records_consumed": n_ofi_used,
        },
        "leak_audit": {
            "verdict": leak_verdict,
            "settle_lag_seconds": {
                "min": round(min_lag, 1),
                "max": round(max_lag, 1),
                "mean": round(mean_lag, 1),
                "p99": round(p99_lag, 1),
            },
            "rule": "settle_wall <= bar_wall; lag >= 0 required; p99 > 1h => tz mismatch",
        },
        "flow_coverage": coverage,
        "live_flow_features": live_feats,
        "effective_flow_dim": effective_flow_dim,
        "effective_schema_dim": 41 + effective_flow_dim,
        "output_npz": str(out_path),
    }

    (output_dir / "flow_alignment.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return audit


def _flow46_feature_names() -> list[str]:
    from core.features.schemas.registry import get_schema_feature_names

    return list(get_schema_feature_names(SCHEMA_ID))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="build_btc_flow46_dataset",
        description="Align OFI settles to base M5 bars -> 46-dim NPZ + leak audit",
    )
    ap.add_argument("--base", type=Path, default=PROJECT_ROOT / "data_btc/training/btc_ssot_v2")
    ap.add_argument("--ofi", type=Path, default=PROJECT_ROOT / "data_btc/reports/ofi_history.jsonl")
    ap.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "data_btc/training/btc_flow46_v1"
    )
    ap.add_argument("--tf-minutes", type=int, default=5, help="Base bar cadence (M5 default)")
    args = ap.parse_args(argv)

    for p in (args.base, args.ofi):
        if not p.exists():
            print(f"[flow46] ERROR: missing input: {p}", file=sys.stderr)
            return 2

    print("[flow46] Building M5-aligned 46-dim dataset...")
    audit = build_flow46(args.base, args.ofi, args.output_dir, args.tf_minutes)

    print("=" * 70)
    print(
        f"[flow46] schema        : {audit['schema_id']} ({audit['effective_schema_dim']}-dim effective)"
    )
    print(
        f"[flow46] aligned rows  : {audit['alignment']['n_aligned_rows']:,} "
        f"(of {audit['alignment']['n_base_test_rows']:,} base test rows; "
        f"{audit['alignment']['n_skipped_no_flow']} skipped pre-bridge)"
    )
    la = audit["leak_audit"]["settle_lag_seconds"]
    print(
        f"[flow46] leak audit    : {audit['leak_audit']['verdict']} "
        f"(lag min={la['min']:.0f}s max={la['max']:.0f}s p99={la['p99']:.0f}s)"
    )
    print("[flow46] flow coverage :")
    for f, cov in audit["flow_coverage"].items():
        print(f"    {f:24s} {cov * 100:5.1f}%")
    print(f"[flow46] live flow dim : {audit['effective_flow_dim']}/5")
    print(f"[flow46] output NPZ    : {audit['output_npz']}")
    print("[flow46] audit JSON    : flow_alignment.json")
    print("=" * 70)
    return 0 if not audit["leak_audit"]["verdict"].startswith("LEAK") else 1


if __name__ == "__main__":
    raise SystemExit(main())
