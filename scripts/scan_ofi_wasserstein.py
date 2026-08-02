"""T21 Gate-1 — OFI 46-dim Wasserstein feature-discrimination scan.

DQAF-20260707-005 Phase 3 (adjudication for btc_macro_flow_46 forward-only
OFI accumulation).  Gate 1 (screening) is MET once ``inspect_ofi_history.py``
reports ≥2,000 raw settles with ≥7d span.  This script then answers the
GO/NO-GO question from ACTIVE_TASKS_REGISTRY T21:

    best OFI feature Wasserstein < 0.01  → STOP (OFI has no directional
        discrimination beyond the 41-dim baseline's 0.0084; pivot to
        liquidation/funding order-flow sources)
    best OFI feature Wasserstein >= 0.02 → PROCEED to Gate 2 (wait for
        ≥1,000 distinct H1 windows, then transfer-retrain freezing 41-dim)

Method (Iron Law #11 — script stdout is the only legal evidence source):
  1. Load all OFI settles from ``<data-dir>/reports/ofi_history.jsonl``.
  2. Label each settle with the **forward 1H return** derived from the
     golden_master ``inputs.mid_price`` series (linear interpolation at
     t and t+1h; a settle is labelled only if both endpoints are covered).
  3. z-score each OFI feature over the labelled sample (thresholds were
     calibrated on standardized features).
  4. Split labelled settles by forward-return sign (up vs down) and compute
     the 1-D empirical Wasserstein-1 distance per feature between the two
     conditional distributions.
  5. Report per-feature W1, the best (max) W1, and the T21 verdict.
     Robustness: also report a thinned (non-overlapping ~1h) subsample W1
     to guard against label autocorrelation from 30s settles.

Usage::

    python scripts/scan_ofi_wasserstein.py --data-dir data_btc [--json]

Exit codes: 0 = scan completed (verdict in stdout), 2 = insufficient data.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import wasserstein_distance

_FLOW_FEATURES = [
    "OFI_M5",
    "OFI_ZScore_20",
    "OFI_Cumulative_1H",
    "OFI_Cumulative_Delta",
    "OFI_Delta_Divergence",
    "OFI_Volume_Real_Ratio",
]

# T21 adjudication thresholds (ACTIVE_TASKS_REGISTRY).
_W1_STOP = 0.01
_W1_PROCEED = 0.02
# 41-dim baseline best-feature W1 from DQAF-20260707-005 diagnostics.
_BASELINE_41D = 0.0084

_FWD_HOURS = 1.0
# Minimum labelled settles + span for a valid screening (mirrors Gate 1).
_MIN_LABELED = 2_000
_MIN_SPAN_DAYS = 7.0
# Thinning stride: OFI settles ~30s; ~120 strides ≈ 1h non-overlapping.
_THIN_STRIDE = 120


def _iso_to_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _load_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
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
                out.append(rec)
    return out


def _build_price_series(gm_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Return (timestamps, mid_prices) sorted, from golden_master inputs."""
    ts: list[float] = []
    px: list[float] = []
    for r in _load_jsonl(gm_path):
        mp = (r.get("inputs") or {}).get("mid_price")
        t = r.get("timestamp_utc")
        if isinstance(mp, int | float) and mp > 0 and isinstance(t, str):
            try:
                ts.append(_iso_to_ts(t))
            except (ValueError, TypeError):
                continue
            px.append(float(mp))
    if not ts:
        return np.array([]), np.array([])
    order = np.argsort(ts)
    return np.array(ts)[order], np.array(px)[order]


def _fwd_ret(ts: np.ndarray, px: np.ndarray, t0: float, fwd_h: float) -> float | None:
    """Forward return over fwd_h hours at time t0 via linear interpolation.

    Returns None if either endpoint falls outside coverage or crosses a
    data gap larger than 3h (interpolation across a gap is not trustworthy).
    """
    t1 = t0 + fwd_h * 3600.0
    lo_i = int(np.searchsorted(ts, t0, side="right"))
    hi_i = int(np.searchsorted(ts, t1, side="left"))
    if lo_i <= 0 or lo_i >= len(ts) or hi_i <= 0 or hi_i > len(ts):
        return None
    # Nearest price at or before t0 (interp anchor).
    p0 = np.interp(t0, ts, px)
    p1 = np.interp(t1, ts, px)
    if not (p0 > 0 and p1 > 0):
        return None
    # Gap guard: if t1 is beyond last sample, no label.
    if t1 > ts[-1]:
        return None
    # Guard against crossing a >3h hole: check both neighbourhoods.
    for _anchor, _idx, _t in ((0, lo_i, t0), (1, hi_i, t1)):
        if _idx >= len(ts):
            return None
        if abs(ts[_idx - 1] - _t) > 3 * 3600.0 and abs(ts[_idx] - _t) > 3 * 3600.0:
            return None
    return (p1 - p0) / p0


def _per_feature_w1(feats_up: np.ndarray, feats_dn: np.ndarray, labels: list[str]) -> list[dict]:
    rows: list[dict] = []
    for j, f in enumerate(_FLOW_FEATURES):
        u = feats_up[:, j]
        d = feats_dn[:, j]
        # Filter dead / single-valued columns (std==0 → no discrimination).
        if u.std() == 0.0 or d.std() == 0.0 or (u.var() == 0.0 and d.var() == 0.0):
            rows.append({"feature": f, "w1": 0.0, "dead": True})
            continue
        w = wasserstein_distance(u, d)
        rows.append({"feature": f, "w1": round(float(w), 6), "dead": False})
    rows.sort(key=lambda r: r["w1"], reverse=True)
    return rows


def scan(data_dir: Path) -> dict:
    hist_path = data_dir / "reports" / "ofi_history.jsonl"
    gm_path = data_dir / "golden_master.jsonl"

    recs = _load_jsonl(hist_path)
    if not recs:
        return {"verdict": "NO_DATA — no OFI history records", "ok": False}

    # ── 1. OFI feature matrix (chronological) ──
    times = [_iso_to_ts(r.get("time", "")) for r in recs if r.get("time")]
    recs = [r for r, t in zip(recs, times, strict=False) if t is not None]
    times = [t for t in times if t is not None]
    if not times:
        return {"verdict": "NO_DATA — no parseable OFI timestamps", "ok": False}
    order = np.argsort(times)
    times = np.array(times)[order]
    feats = np.array(
        [
            [float(r.get(f, 0.0) or 0.0) for f in _FLOW_FEATURES]
            for r in np.array(recs, dtype=object)[order]
        ],
        dtype=float,
    )

    # ── 2. Labels from golden_master mid-price ──
    p_ts, p_px = _build_price_series(gm_path)
    fwd: list[float] = []
    kept_idx: list[int] = []
    for i, t0 in enumerate(times):
        r = _fwd_ret(p_ts, p_px, float(t0), _FWD_HOURS)
        if r is not None:
            fwd.append(float(r))
            kept_idx.append(i)

    if len(fwd) < _MIN_LABELED:
        return {
            "verdict": (
                f"INSUFFICIENT_LABELS — only {len(fwd):,}/{_MIN_LABELED:,} "
                f"settles have a valid forward-1H return in GM price coverage"
            ),
            "ok": False,
            "n_labeled": len(fwd),
        }

    # ── 3. z-score each feature over the labelled sample ──
    X = feats[kept_idx].astype(float)
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd
    fwd_arr = np.array(fwd)

    span_days = (times[kept_idx][-1] - times[kept_idx][0]) / 86_400.0
    up = fwd_arr > 0
    dn = ~up

    rows = _per_feature_w1(Z[up], Z[dn], _FLOW_FEATURES)
    best = rows[0]["w1"] if rows else 0.0

    # ── Robustness: thinned ~1h non-overlapping subsample ──
    thin_idx = kept_idx[::_THIN_STRIDE]
    thin = np.array(
        [fwd_arr[k] for k in range(len(fwd_arr)) if k in set(range(0, len(fwd_arr), _THIN_STRIDE))]
    )
    Xt = X[::_THIN_STRIDE]
    Zt = (Xt - Xt.mean(axis=0)) / (Xt.std(axis=0) + 1e-12)
    up_t, dn_t = thin > 0, ~(thin > 0)
    thin_rows = _per_feature_w1(Zt[up_t], Zt[dn_t], _FLOW_FEATURES)
    best_thin = thin_rows[0]["w1"] if thin_rows else 0.0

    # ── Verdict ──
    if best < _W1_STOP:
        verdict = (
            f"STOP — best OFI feature W1={best:.4f} < {_W1_STOP:.2f} "
            f"(≤ 41-dim baseline {_BASELINE_41D:.4f}). OFI adds no directional "
            f"discrimination → pivot to liquidation/funding order-flow sources."
        )
        outcome = "STOP"
    elif best >= _W1_PROCEED:
        verdict = (
            f"PROCEED — best OFI feature W1={best:.4f} ≥ {_W1_PROCEED:.2f} "
            f"(≫ baseline {_BASELINE_41D:.4f}). Strong discrimination → wait for "
            f"Gate 2 (≥1,000 H1 windows) → transfer-retrain freezing 41-dim."
        )
        outcome = "PROCEED"
    else:
        verdict = (
            f"ACCUMULATE — best OFI feature W1={best:.4f} in "
            f"[{_W1_STOP:.2f}, {_W1_PROCEED:.2f}). Above baseline but below "
            f"proceed bar → keep accumulating, re-scan at Gate 2."
        )
        outcome = "ACCUMULATE"

    return {
        "ok": True,
        "n_records": len(recs),
        "n_labeled": len(fwd),
        "span_days": round(span_days, 2),
        "up_n": int(up.sum()),
        "down_n": int((~up).sum()),
        "thinned_n": int(thin.size),
        "per_feature_w1": rows,
        "best_w1": round(best, 6),
        "thinned_best_w1": round(best_thin, 6),
        "baseline_41d": _BASELINE_41D,
        "thresholds": {"stop_below": _W1_STOP, "proceed_at": _W1_PROCEED},
        "outcome": outcome,
        "verdict": verdict,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="data_btc", help="Data directory root")
    ap.add_argument("--json", action="store_true", help="Emit raw JSON only")
    args = ap.parse_args()

    result = scan(Path(args.data_dir))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if not result.get("ok"):
        print(result["verdict"])
        raise SystemExit(2)

    print("=" * 64)
    print("T21 Gate-1 OFI Wasserstein Feature-Discrimination Scan")
    print("=" * 64)
    print(f"  OFI settles loaded:    {result['n_records']:,}")
    print(f"  Labelled (fwd 1H ret): {result['n_labeled']:,}  span={result['span_days']}d")
    print(f"  Up/Down split:         {result['up_n']:,} / {result['down_n']:,}")
    print(f"  Thinned (~1h) sample:  {result['thinned_n']:,}")
    print(f"  41-dim baseline:       {result['baseline_41d']:.4f}")
    print("  Per-feature Wasserstein-1 (up vs down, z-scored):")
    for r in result["per_feature_w1"]:
        tag = " (dead)" if r.get("dead") else ""
        print(f"    {r['feature']:24s} {r['w1']:.4f}{tag}")
    print(f"  BEST W1 (full):         {result['best_w1']:.4f}")
    print(f"  BEST W1 (thinned):      {result['thinned_best_w1']:.4f}")
    print(f"\n  OUTCOME: {result['outcome']}")
    print(f"  {result['verdict']}")


if __name__ == "__main__":
    main()
