#!/usr/bin/env python
"""Ω Feature Distribution Drift Monitor — PSI-based early warning for model decay.

Compares live feature distributions against training baseline using
Population Stability Index (PSI) with 10-bin equal-frequency binning.
Alerts via alert_dispatcher when drift exceeds thresholds.

DQAF-20260616-005/GAP3: institutional feature drift detection.

Usage:
  python scripts/monitor_feature_drift.py --data-dir data
  python scripts/monitor_feature_drift.py --data-dir data_btc
  python scripts/monitor_feature_drift.py --data-dir data --alert
  python scripts/monitor_feature_drift.py --compute-baseline  # regenerate

Thresholds:
  PSI < 0.10:  No drift (green)
  PSI 0.10-0.25: Moderate drift (yellow)
  PSI > 0.25:  Significant drift (red)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from core.runtime.fault_handler import fail_open_guard

# ── Constants ──
DEFAULT_WINDOW = 1000  # bars for rolling window
MIN_WINDOW = 500  # below this, alert confidence is insufficient
BASELINE_FILE = "data/training/balanced_v1/feature_baseline_v9_20260617.json"
PSI_GREEN = 0.10
PSI_YELLOW = 0.25
# ── live feature dirs ──
FEATURE_DIRS = {
    "data": "data/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl",
    "data_btc": "data_btc/feature_store/records/symbol=BTCUSDc/timeframe=M5/features.jsonl",
}


# ═══════════════════════════════════════════════════════════════════════
#  JSONL integrity defense (GAP 3 risk mitigation #3)
# ═══════════════════════════════════════════════════════════════════════


def _safe_read_features(fpath: str, max_retries: int = 3) -> list[dict[str, Any]]:
    """Read feature JSONL with line-level integrity validation.

    If a line is truncated (half-written by live_cycle), retry up to
    max_retries times with 0.1s delay.  This defends against the known
    lack of atomic writes in the feature store.
    """
    for attempt in range(max_retries):
        try:
            records: list[dict] = []
            with open(fpath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        # Truncated line — retry
                        if attempt < max_retries - 1:
                            import time
                            time.sleep(0.1)
                            raise  # trigger retry
                        # Last attempt: skip the bad line
                        continue
            return records
        except Exception:  # BLE001:FOG
            with fail_open_guard("monitor_feature_drift:_safe_read_features"):
                if attempt < max_retries - 1:
                    continue
                return []
    return []


# ═══════════════════════════════════════════════════════════════════════
#  PSI computation
# ═══════════════════════════════════════════════════════════════════════


def _compute_psi(
    actual: np.ndarray,
    expected_bin_edges: list[float],
    epsilon: float = 1e-4,
) -> tuple[float, list[dict[str, Any]]]:
    """Compute PSI for one feature using fixed baseline bin edges.

    Uses equal-frequency binning: bin boundaries are quantiles from
    the training baseline.  Each bin's contribution is returned for
    troubleshooting.

    Returns (total_psi, per_bin_contributions).
    """
    bins = np.array(expected_bin_edges, dtype=np.float64)
    n_bins = len(bins) - 1

    # Expected: uniform (10% per bin for 10-bin equal-frequency)
    expected_pct = 1.0 / n_bins

    # Actual: count samples in each bin
    actual_counts, _ = np.histogram(actual, bins=bins)
    total = max(actual_counts.sum(), 1)
    actual_pcts = actual_counts / total

    contributions = []
    total_psi = 0.0
    for i in range(n_bins):
        e = expected_pct
        a = max(actual_pcts[i], epsilon)  # avoid log(0)
        e_clamped = max(e, epsilon)
        contribution = (a - e) * np.log(a / e_clamped)
        contributions.append({
            "bin": i,
            "range": [round(float(bins[i]), 6), round(float(bins[i + 1]), 6)],
            "expected_pct": round(e, 4),
            "actual_pct": round(float(a), 4),
            "psi_contribution": round(float(contribution), 6),
        })
        total_psi += contribution

    return float(total_psi), contributions


# ═══════════════════════════════════════════════════════════════════════
#  Main drift check
# ═══════════════════════════════════════════════════════════════════════


def check_feature_drift(
    data_dir: str,
    baseline_path: str = BASELINE_FILE,
    window: int = DEFAULT_WINDOW,
) -> dict[str, Any]:
    """Run PSI drift check for one data directory. Returns structured result."""
    fpath = FEATURE_DIRS.get(data_dir)
    if not fpath or not Path(fpath).exists():
        return {"passed": True, "severity": "SKIP", "reason": f"no feature file: {fpath}"}

    # Load baseline
    bp = Path(baseline_path)
    if not bp.exists():
        return {"passed": True, "severity": "SKIP", "reason": "no baseline file"}
    baseline = json.loads(bp.read_text(encoding="utf-8"))

    # Schema version check
    baseline_schema = baseline.get("feature_schema", "unknown")
    print(f"[DRIFT] Baseline schema: {baseline_schema}, samples: {baseline.get('n_samples', '?')}")

    # Load live features
    records = _safe_read_features(fpath)
    if len(records) < MIN_WINDOW:
        return {
            "passed": True,
            "severity": "Sev3",
            "reason": f"insufficient samples: {len(records)} < {MIN_WINDOW}",
            "note": "alert suppressed — confidence too low",
        }

    # Use last N records
    recent = records[-window:]
    n_used = len(recent)
    confidence_flag = "" if n_used >= MIN_WINDOW else " [LOW CONFIDENCE]"

    # Extract feature matrix
    feature_names = list(baseline["features"].keys())
    live_matrix = np.zeros((n_used, len(feature_names)), dtype=np.float64)
    for i, rec in enumerate(recent):
        vals = rec.get("values", {})
        for j, name in enumerate(feature_names):
            live_matrix[i, j] = float(vals.get(name, 0.0))

    # Compute PSI per feature
    drifting_features: list[dict] = []
    all_psi: list[float] = []
    for j, name in enumerate(feature_names):
        binfo = baseline["features"].get(name)
        if not binfo or binfo.get("constant"):
            continue
        col = live_matrix[:, j]
        psi, contributions = _compute_psi(col, binfo["bin_edges"])

        severity = "OK" if psi < PSI_GREEN else ("Sev2" if psi < PSI_YELLOW else "Sev1")
        all_psi.append(psi)

        if severity != "OK":
            drifting_features.append({
                "feature": name,
                "psi": round(psi, 4),
                "severity": severity,
                "mean_baseline": round(binfo["mean"], 4),
                "mean_live": round(float(np.mean(col)), 4),
                "std_baseline": round(binfo["std"], 4),
                "std_live": round(float(np.std(col)), 4),
                "top_bins": sorted(contributions, key=lambda x: -abs(x["psi_contribution"]))[:3],
            })

    n_drifting = len(drifting_features)
    n_sev1 = sum(1 for f in drifting_features if f["severity"] == "Sev1")
    n_sev2 = sum(1 for f in drifting_features if f["severity"] == "Sev2")

    if n_sev1 > 0:
        overall = "Sev1"
    elif n_sev2 > 2:
        overall = "Sev2"
    elif n_sev2 > 0:
        overall = "Sev3"
    else:
        overall = "OK"

    mean_psi = float(np.mean(all_psi)) if all_psi else 0.0
    max_psi = float(np.max(all_psi)) if all_psi else 0.0

    return {
        "passed": overall == "OK",
        "severity": overall,
        "n_features": len(feature_names),
        "n_live_samples": n_used,
        "window": window,
        "mean_psi": round(mean_psi, 4),
        "max_psi": round(max_psi, 4),
        "n_drifting": n_drifting,
        "n_sev1": n_sev1,
        "n_sev2": n_sev2,
        "drifting_features": sorted(drifting_features, key=lambda x: -x["psi"])[:10],
        "confidence": "LOW" if n_used < MIN_WINDOW else "OK",
        "note": f"{n_drifting}/{len(feature_names)} features drifting (PSI > {PSI_GREEN}){confidence_flag}",
    }


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════


def main() -> int:
    p = argparse.ArgumentParser(prog="monitor_feature_drift")
    p.add_argument("--data-dir", type=str, default="data", help="data or data_btc")
    p.add_argument("--window", type=int, default=DEFAULT_WINDOW, help="rolling window size")
    p.add_argument("--alert", action="store_true", help="push DingTalk alert on drift")
    p.add_argument("--baseline", type=str, default=BASELINE_FILE, help="baseline file path")
    p.add_argument("--json", action="store_true", help="JSON output")
    args = p.parse_args()

    result = check_feature_drift(args.data_dir, args.baseline, args.window)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"[DRIFT] {args.data_dir}: {result['severity']} | mean_PSI={result['mean_psi']:.4f} max_PSI={result['max_psi']:.4f} | {result['note']}")
        for f in result.get("drifting_features", []):
            print(f"  {f['feature']}: PSI={f['psi']:.4f} ({f['severity']}) mean: {f['mean_baseline']:.3f}→{f['mean_live']:.3f}")

    # Alerting
    if args.alert and result["severity"] in ("Sev1", "Sev2"):
        try:
            from scripts.alert_dispatcher import AlertCard, dispatch_alert

            card = AlertCard(
                source="drift",
                title=f"Feature Drift: {result['severity']} ({args.data_dir})",
                severity=result["severity"],
                details={
                    "data_dir": args.data_dir,
                    "mean_psi": result["mean_psi"],
                    "max_psi": result["max_psi"],
                    "n_drifting": result["n_drifting"],
                    "top_feature": result["drifting_features"][0]["feature"] if result["drifting_features"] else "none",
                },
            )
            dispatch_alert(card)
        except Exception:  # BLE001:FOG
            with fail_open_guard("monitor_feature_drift:main"):
                pass
    return 1 if result["severity"] == "Sev1" else 2 if result["severity"] == "Sev2" else 0


if __name__ == "__main__":
    raise SystemExit(main())
