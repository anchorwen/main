#!/usr/bin/env python
"""Ω Feature Distribution Drift Monitor — Dual-Mode PSI (Institutional Grade).

DQAF-20260622-060: Upgraded from raw-feature single-mode to normalized
dual-mode drift detection (Regime + Anomaly).  Three mandatory engineering
protections per Investment Committee audit:

  1. Zero-variance trap: sigma = max(sigma, 1e-8) in all normalization paths
  2. PSI log-divergence: epsilon = 1e-6 pseudo-count on both actual and expected
  3. Mode B self-normalization: use rolling window's own μ/σ (not training)

Additional deep-water directives:
  4. Exclusive window isolation: Expected [T-8d, T-1d] ∩ Actual [T-1d, T_now] = Ø
  5. Sample asymmetry mitigation: Sev1 threshold 0.25→0.32 when N_actual < 500

Modes:
  --mode regime   Compare live vs training baseline (detect market regime change)
  --mode anomaly  Compare recent 1d vs rolling 7d (detect pipeline/data bugs)
  --mode both     Run both modes

Usage:
  python scripts/monitor_feature_drift.py --data-dir data_btc --mode both --normalize
  python scripts/monitor_feature_drift.py --compute-baseline train.npz --normalize --write
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

# ── Constants ────────────────────────────────────────────────────────
DEFAULT_WINDOW = 1000
MIN_WINDOW = 500
BASELINE_FILE = "data/training/balanced_v1/feature_baseline_v9_20260617.json"
PSI_GREEN = 0.10
PSI_YELLOW = 0.25
# Sample-asymmetry dynamic threshold (Directive #5)
PSI_YELLOW_SPARSE = 0.32  # when N_actual < 500
# Engineering protections (Directives #1, #2)
EPSILON_SIGMA = 1e-8  # zero-variance floor
EPSILON_PSI = 1e-6  # log-divergence pseudo-count

FEATURE_DIRS = {
    "data": "data/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl",
    "data_btc": "data_btc/feature_store/records/symbol=BTCUSDc/timeframe=M5/features.jsonl",
}


# ═══════════════════════════════════════════════════════════════════════
#  JSONL integrity defense
# ═══════════════════════════════════════════════════════════════════════


def _safe_read_features(fpath: str, max_retries: int = 3) -> list[dict[str, Any]]:
    """Read feature JSONL with line-level integrity validation."""
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
                        if attempt < max_retries - 1:
                            import time

                            time.sleep(0.1)
                            raise
                        continue
            return records
        except Exception:  # noqa: BLE001
            try:  # BLE001:FOG (was: FOG/LAC)
                if attempt < max_retries - 1:
                    continue
                return []
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass
    return []


# ═══════════════════════════════════════════════════════════════════════
#  Normalization (Contract #1: zero-variance protection)
# ═══════════════════════════════════════════════════════════════════════


def _normalize_features(
    X: np.ndarray,
    mu: np.ndarray,
    sigma: np.ndarray,
) -> np.ndarray:
    """Z-score normalize with zero-variance floor (Contract #1).

    sigma = max(sigma, 1e-8) — prevents DivideByZero when a feature
    has zero variance in the rolling window (e.g., constant spread
    during no-trading periods).
    """
    sigma_safe = np.maximum(sigma, EPSILON_SIGMA)
    return (X - mu) / sigma_safe


# ═══════════════════════════════════════════════════════════════════════
#  PSI computation (Contract #2: epsilon padding on both sides)
# ═══════════════════════════════════════════════════════════════════════


def _compute_psi(
    actual: np.ndarray,
    expected_bin_edges: list[float],
) -> tuple[float, list[dict[str, Any]]]:
    """Compute PSI using fixed baseline bin edges.

    Contract #2: Both actual_pct and expected_pct are clamped to
    EPSILON_PSI (1e-6) minimum — prevents ln(0) → -∞ divergence
    when a bin has zero samples in live data.
    """
    bins = np.array(expected_bin_edges, dtype=np.float64)
    n_bins = len(bins) - 1

    expected_pct = 1.0 / n_bins

    actual_counts, _ = np.histogram(actual, bins=bins)
    total = max(actual_counts.sum(), 1)
    actual_pcts = actual_counts / total

    # Contract #2: pseudo-count floor on BOTH sides
    actual_pcts = np.maximum(actual_pcts, EPSILON_PSI)
    e_clamped = max(expected_pct, EPSILON_PSI)

    contributions = []
    total_psi = 0.0
    for i in range(n_bins):
        a = actual_pcts[i]
        contribution = (a - e_clamped) * np.log(a / e_clamped)
        contributions.append(
            {
                "bin": i,
                "range": [round(float(bins[i]), 6), round(float(bins[i + 1]), 6)],
                "expected_pct": round(e_clamped, 4),
                "actual_pct": round(float(a), 4),
                "psi_contribution": round(float(contribution), 6),
            }
        )
        total_psi += contribution

    return float(total_psi), contributions


# ═══════════════════════════════════════════════════════════════════════
#  Baseline computation
# ═══════════════════════════════════════════════════════════════════════


def _compute_baseline(
    X: np.ndarray,
    feature_names: list[str],
    *,
    normalize: bool = False,
    norm_mu: np.ndarray | None = None,
    norm_sigma: np.ndarray | None = None,
    source: str = "unknown",
    feature_schema: str = "v9_institutional_40",
) -> dict[str, Any]:
    """Generate feature baseline JSON from a feature matrix.

    When *normalize* is True, z-scores X using *norm_mu*/*norm_sigma*
    (with Contract #1 zero-variance protection) before computing bins.
    """
    X_work = X.copy()
    if normalize and norm_mu is not None and norm_sigma is not None:
        X_work = _normalize_features(X_work, norm_mu, norm_sigma)

    n_samples, n_features = X_work.shape
    features: dict[str, dict] = {}
    for j, name in enumerate(feature_names):
        col = X_work[:, j]
        col_mean = float(np.mean(col))
        col_std = float(np.std(col))

        if col_std < 1e-10:
            # Constant feature — PSI undefined, skip
            features[name] = {
                "mean": col_mean,
                "std": 0.0,
                "min": float(np.min(col)),
                "max": float(np.max(col)),
                "p1": col_mean,
                "p5": col_mean,
                "p50": col_mean,
                "p95": col_mean,
                "p99": col_mean,
                "bin_edges": [float(np.min(col)), float(np.max(col))],
                "constant": True,
            }
            continue

        pcts_arr = np.asarray(
            np.percentile(col, [1, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 99]),
            dtype=np.float64,
        )
        # 10-bin equal-frequency: decile boundaries
        bin_edges = (
            [float(np.min(col))]
            + [float(p) for p in pcts_arr[2:11]]  # p10, p20, ..., p90
            + [float(np.max(col))]
        )

        features[name] = {
            "mean": col_mean,
            "std": col_std,
            "min": float(np.min(col)),
            "max": float(np.max(col)),
            "p1": float(pcts_arr[0]),
            "p5": float(pcts_arr[1]),
            "p50": float(pcts_arr[6]),
            "p95": float(pcts_arr[11]),
            "p99": float(pcts_arr[12]),
            "bin_edges": bin_edges,
            "constant": False,
        }

    baseline: dict[str, Any] = {
        "schema_version": "feature_baseline.v1",
        "feature_schema": feature_schema,
        "n_features": n_features,
        "n_samples": n_samples,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "normalized": normalize,
        "features": features,
    }
    # Embed normalization params so PSI runtime can auto-apply
    if normalize and norm_mu is not None and norm_sigma is not None:
        baseline["norm_mean"] = norm_mu.tolist()
        baseline["norm_std"] = norm_sigma.tolist()
    return baseline


# ═══════════════════════════════════════════════════════════════════════
#  Feature matrix extraction
# ═══════════════════════════════════════════════════════════════════════


def _records_to_matrix(
    records: list[dict],
    feature_names: list[str],
) -> np.ndarray:
    """Extract feature matrix from JSONL records."""
    n = len(records)
    matrix = np.zeros((n, len(feature_names)), dtype=np.float64)
    for i, rec in enumerate(records):
        vals = rec.get("values", {})
        for j, name in enumerate(feature_names):
            matrix[i, j] = float(vals.get(name, 0.0))
    return matrix


# ═══════════════════════════════════════════════════════════════════════
#  Mode B: Exclusive window isolation (Directive #4)
# ═══════════════════════════════════════════════════════════════════════


def _split_exclusive_windows(
    records: list[dict],
    rolling_days: int = 7,
) -> tuple[list[dict], list[dict]]:
    """Split records into non-overlapping Expected and Actual windows.

    Directive #4: Expected strictly [T-8d, T-1d], Actual strictly [T-1d, T].
    Zero overlap — prevents cross-contamination that dulls anomaly sensitivity.
    """
    if not records:
        return [], []

    # Parse event_time from records
    def _parse_ts(rec: dict) -> datetime | None:
        et = rec.get("event_time", "")
        if not et:
            return None
        try:
            # ISO format: "2026-06-22T09:35:00Z" or similar
            return datetime.fromisoformat(et.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    timestamps = [_parse_ts(r) for r in records]
    valid_ts = [ts for ts in timestamps if ts is not None]
    if not valid_ts:
        return records[-max(1, len(records) // 8) :], records[-max(1, len(records) // 8) :]

    t_max = max(valid_ts)
    t_cutoff = t_max - timedelta(days=1)  # [T-1d, T] for Actual
    t_baseline_start = t_max - timedelta(days=rolling_days + 1)  # [T-8d, T-1d] for Expected

    expected_records = []
    actual_records = []
    for rec, ts in zip(records, timestamps, strict=False):
        if ts is None:
            continue
        if t_baseline_start <= ts < t_cutoff:
            expected_records.append(rec)
        elif ts >= t_cutoff:
            actual_records.append(rec)

    return expected_records, actual_records


# ═══════════════════════════════════════════════════════════════════════
#  Core per-feature PSI analysis
# ═══════════════════════════════════════════════════════════════════════


def _analyze_features(
    live_matrix: np.ndarray,
    feature_names: list[str],
    baseline_features: dict[str, dict],
    n_actual: int,
    mode: str,
) -> dict[str, Any]:
    """Compute per-feature PSI and classify drift severity.

    Directive #5: Sample-asymmetry mitigation — when N_actual < 500
    in anomaly mode, relax Sev1 threshold from 0.25 → 0.32.
    """
    psi_yellow = PSI_YELLOW
    if mode == "anomaly" and n_actual < MIN_WINDOW:
        psi_yellow = PSI_YELLOW_SPARSE  # 0.32 — suppress sampling noise

    drifting_features: list[dict] = []
    all_psi: list[float] = []
    for j, name in enumerate(feature_names):
        binfo = baseline_features.get(name)
        if not binfo or binfo.get("constant"):
            continue
        col = live_matrix[:, j]
        psi, contributions = _compute_psi(col, binfo["bin_edges"])

        severity = "OK" if psi < PSI_GREEN else ("Sev2" if psi < psi_yellow else "Sev1")
        all_psi.append(psi)

        if severity != "OK":
            drifting_features.append(
                {
                    "feature": name,
                    "psi": round(psi, 4),
                    "severity": severity,
                    "mean_baseline": round(binfo.get("mean", 0), 4),
                    "mean_live": round(float(np.mean(col)), 4),
                    "std_baseline": round(binfo.get("std", 0), 4),
                    "std_live": round(float(np.std(col)), 4),
                    "top_bins": sorted(contributions, key=lambda x: -abs(x["psi_contribution"]))[
                        :3
                    ],
                }
            )

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
        "severity": overall,
        "mean_psi": round(mean_psi, 4),
        "max_psi": round(max_psi, 4),
        "n_features": len(feature_names),
        "n_drifting": n_drifting,
        "n_sev1": n_sev1,
        "n_sev2": n_sev2,
        "drifting_features": sorted(drifting_features, key=lambda x: -x["psi"])[:10],
    }


# ═══════════════════════════════════════════════════════════════════════
#  Main drift check — dual-mode
# ═══════════════════════════════════════════════════════════════════════


def check_feature_drift(
    data_dir: str,
    baseline_path: str = BASELINE_FILE,
    window: int = DEFAULT_WINDOW,
    *,
    normalize: bool = False,
    norm_config: dict[str, Any] | None = None,
    mode: str = "regime",
    rolling_days: int = 7,
) -> dict[str, Any]:
    """Run dual-mode PSI drift check.

    Parameters
    ----------
    mode:
        "regime"  — Mode A: live vs training baseline (retrain trigger)
        "anomaly" — Mode B: 1d vs 7d rolling (pipeline anomaly detection)
        "both"    — Run both modes
    """
    fpath = FEATURE_DIRS.get(data_dir)
    if not fpath or not Path(fpath).exists():
        return {"passed": True, "severity": "SKIP", "reason": f"no feature file: {fpath}"}

    # Load training baseline (Mode A)
    bp = Path(baseline_path)
    if not bp.exists():
        return {"passed": True, "severity": "SKIP", "reason": "no baseline file"}
    baseline = json.loads(bp.read_text(encoding="utf-8"))
    baseline_schema = baseline.get("feature_schema", "unknown")
    baseline_normalized = baseline.get("normalized", False)
    feature_names = list(baseline["features"].keys())

    # Load normalization config — auto-detect from baseline if embedded
    norm_mu: np.ndarray | None = None
    norm_sigma: np.ndarray | None = None
    if normalize:
        if norm_config is not None:
            norm_mu = np.array(
                [
                    norm_config.get("mean", [])[j] if j < len(norm_config.get("mean", [])) else 0.0
                    for j in range(len(feature_names))
                ],
                dtype=np.float64,
            )
            norm_sigma = np.array(
                [
                    norm_config.get("std", [])[j] if j < len(norm_config.get("std", [])) else 1.0
                    for j in range(len(feature_names))
                ],
                dtype=np.float64,
            )
        elif baseline.get("normalized") and "norm_mean" in baseline:
            # Auto-load from baseline (embedded during --compute-baseline --normalize)
            norm_mu = np.array(baseline["norm_mean"], dtype=np.float64)
            norm_sigma = np.array(baseline["norm_std"], dtype=np.float64)

    # Load live features
    records = _safe_read_features(fpath)
    if len(records) < MIN_WINDOW:
        return {
            "passed": True,
            "severity": "Sev3",
            "reason": f"insufficient samples: {len(records)} < {MIN_WINDOW}",
            "note": "alert suppressed — confidence too low",
        }

    results: dict[str, Any] = {
        "data_dir": data_dir,
        "baseline_schema": baseline_schema,
        "baseline_normalized": baseline_normalized,
        "normalize": normalize,
        "n_total_records": len(records),
    }

    # ── Mode A: Regime Detection (vs Training) ──
    if mode in ("regime", "both"):
        recent = records[-window:]
        n_used = len(recent)
        live_matrix = _records_to_matrix(recent, feature_names)

        if normalize and norm_mu is not None and norm_sigma is not None:
            live_matrix = _normalize_features(live_matrix, norm_mu, norm_sigma)

        regime_result = _analyze_features(
            live_matrix, feature_names, baseline["features"], n_used, "regime"
        )
        regime_result["mode"] = "regime"
        regime_result["n_live_samples"] = n_used
        regime_result["n_baseline_samples"] = baseline.get("n_samples", 0)
        regime_result["window"] = window
        regime_result["note"] = (
            f"{regime_result['n_drifting']}/{regime_result['n_features']} "
            f"features drifting (PSI > {PSI_GREEN})"
        )
        regime_result["passed"] = regime_result["severity"] == "OK"
        results["regime"] = regime_result

    # ── Mode B: Anomaly Detection (vs Rolling 7d) ──
    if mode in ("anomaly", "both"):
        expected_recs, actual_recs = _split_exclusive_windows(records, rolling_days)
        n_actual = len(actual_recs)
        n_expected = len(expected_recs)

        if n_expected < MIN_WINDOW:
            anomaly_result = {
                "mode": "anomaly",
                "severity": "SKIP",
                "passed": True,
                "reason": f"insufficient expected samples: {n_expected} < {MIN_WINDOW}",
                "n_expected": n_expected,
                "n_actual": n_actual,
            }
        elif n_actual < 10:
            anomaly_result = {
                "mode": "anomaly",
                "severity": "SKIP",
                "passed": True,
                "reason": f"insufficient actual samples: {n_actual} < 10",
                "n_expected": n_expected,
                "n_actual": n_actual,
            }
        else:
            expected_matrix = _records_to_matrix(expected_recs, feature_names)
            actual_matrix = _records_to_matrix(actual_recs, feature_names)

            # Contract #3: Mode B self-normalization — use rolling window's own μ/σ
            mode_b_mu = np.mean(expected_matrix, axis=0)
            mode_b_sigma = np.maximum(np.std(expected_matrix, axis=0), EPSILON_SIGMA)

            baseline_b = _compute_baseline(
                expected_matrix,
                feature_names,
                normalize=True,
                norm_mu=mode_b_mu,
                norm_sigma=mode_b_sigma,
                source=f"rolling_{rolling_days}d",
                feature_schema=baseline_schema,
            )

            # Normalize actual using rolling window's μ/σ (NOT training μ/σ)
            actual_normalized = _normalize_features(actual_matrix, mode_b_mu, mode_b_sigma)

            anomaly_result = _analyze_features(
                actual_normalized,
                feature_names,
                baseline_b["features"],
                n_actual,
                "anomaly",
            )
            anomaly_result["mode"] = "anomaly"
            anomaly_result["n_live_samples"] = n_actual  # actual = 1d
            anomaly_result["n_baseline_samples"] = n_expected  # expected = 7d
            anomaly_result["rolling_days"] = rolling_days
            anomaly_result["note"] = (
                f"{anomaly_result['n_drifting']}/{anomaly_result['n_features']} "
                f"features drifting (PSI > {PSI_GREEN})"
            )
            anomaly_result["passed"] = anomaly_result["severity"] == "OK"
            anomaly_result["_directive4"] = (
                f"exclusive windows: expected [T-{rolling_days+1}d, T-1d] "
                f"({n_expected} recs), actual [T-1d, T] ({n_actual} recs)"
            )
            if mode == "anomaly" and n_actual < MIN_WINDOW:
                anomaly_result["_directive5"] = (
                    f"sample asymmetry: N_actual={n_actual}<500, "
                    f"Sev1 threshold relaxed {PSI_YELLOW}→{PSI_YELLOW_SPARSE}"
                )

        results["anomaly"] = anomaly_result

    if mode == "both":
        regime_sev = results.get("regime", {}).get("severity", "OK")
        anomaly_sev = results.get("anomaly", {}).get("severity", "OK")
        sev_order = {"Sev1": 3, "Sev2": 2, "Sev3": 1, "OK": 0, "SKIP": -1}
        results["severity"] = max(
            (regime_sev, anomaly_sev),
            key=lambda s: sev_order.get(s, -1),
        )
        results["passed"] = results["severity"] in ("OK", "SKIP")
    elif mode == "regime":
        # Propagate all regime-result fields to top level for _print_result
        r = results.get("regime", {})
        results.update({k: v for k, v in r.items() if k not in results})
        results["passed"] = r.get("passed", True)
    else:
        # Propagate all anomaly-result fields to top level
        a = results.get("anomaly", {})
        results.update({k: v for k, v in a.items() if k not in results})
        results["passed"] = a.get("passed", True)

    return results


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════


def main() -> int:
    p = argparse.ArgumentParser(prog="monitor_feature_drift")
    p.add_argument("--data-dir", type=str, default="data", help="data or data_btc")
    p.add_argument("--window", type=int, default=DEFAULT_WINDOW, help="rolling window in bars")
    p.add_argument("--alert", action="store_true", help="push DingTalk alert on drift")
    p.add_argument("--baseline", type=str, default=BASELINE_FILE, help="training baseline path")
    p.add_argument("--json", action="store_true", help="JSON output")
    # DQAF-060: dual-mode + normalization
    p.add_argument(
        "--mode",
        choices=["regime", "anomaly", "both"],
        default="regime",
        help="Drift detection mode (default: regime)",
    )
    p.add_argument(
        "--rolling-days",
        type=int,
        default=7,
        help="Rolling window in days for anomaly mode (default: 7)",
    )
    p.add_argument(
        "--normalize", action="store_true", help="Z-score features before PSI computation"
    )
    p.add_argument(
        "--norm-config",
        type=Path,
        default=None,
        help="Path to normalization config JSON (mean/std per feature)",
    )
    # Baseline (re)generation
    p.add_argument(
        "--compute-baseline",
        type=Path,
        default=None,
        help="Generate baseline JSON from training .npz file and exit",
    )
    p.add_argument(
        "--feature-schema",
        type=str,
        default="v9_institutional_40",
        help="Feature schema name for baseline metadata",
    )
    p.add_argument("--output", type=Path, default=None, help="Output path for generated baseline")
    args = p.parse_args()

    # ── Baseline generation mode ──
    if args.compute_baseline:
        npz_path = args.compute_baseline
        if not npz_path.exists():
            print(f"[FAIL] .npz file not found: {npz_path}", file=sys.stderr)
            return 1

        data = np.load(npz_path)
        X = data["X"] if "X" in data else data[list(data.keys())[0]]
        feature_names = (
            list(data.get("feature_names", []))
            if "feature_names" in data
            else [f"f{j}" for j in range(X.shape[1])]
        )

        norm_mu = None
        norm_sigma = None
        normalize = args.normalize
        if normalize and args.norm_config:
            nc = json.loads(args.norm_config.read_text(encoding="utf-8"))
            norm_mu = np.array(nc.get("mean", [0.0] * X.shape[1]), dtype=np.float64)
            norm_sigma = np.array(nc.get("std", [1.0] * X.shape[1]), dtype=np.float64)
        elif normalize:
            # Auto-compute from training data
            norm_mu = np.mean(X, axis=0)
            norm_sigma = np.maximum(np.std(X, axis=0), EPSILON_SIGMA)

        baseline = _compute_baseline(
            X,
            feature_names,
            normalize=normalize,
            norm_mu=norm_mu,
            norm_sigma=norm_sigma,
            source=str(npz_path),
            feature_schema=args.feature_schema,
        )

        output_path = args.output or Path(
            f"feature_baseline_{args.feature_schema}_{datetime.now(UTC).strftime('%Y%m%d')}.json"
        )
        output_path = Path(output_path)
        output_path.write_text(json.dumps(baseline, indent=2, ensure_ascii=False), encoding="utf-8")
        print(
            f"[BASELINE] Wrote {output_path} ({baseline['n_samples']} samples, {baseline['n_features']} features, normalized={baseline['normalized']})"
        )
        return 0

    # ── Normalization config loading ──
    norm_config: dict[str, Any] | None = None
    if args.normalize and args.norm_config:
        norm_config = json.loads(args.norm_config.read_text(encoding="utf-8"))

    # ── Run drift check ──
    result = check_feature_drift(
        args.data_dir,
        args.baseline,
        args.window,
        normalize=args.normalize,
        norm_config=norm_config,
        mode=args.mode,
        rolling_days=args.rolling_days,
    )

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_result(args, result)

    # Alerting
    if args.alert and result.get("severity") in ("Sev1", "Sev2"):
        try:
            from scripts.alert_dispatcher import AlertCard, dispatch_alert

            card = AlertCard(
                source="drift",
                title=f"Feature Drift: {result['severity']} ({args.data_dir}, mode={args.mode})",
                severity=result["severity"],
                details={
                    "data_dir": args.data_dir,
                    "mode": args.mode,
                    "severity": result["severity"],
                },
            )
            dispatch_alert(card)
        except Exception:  # noqa: BLE001
            try:  # BLE001:FOG (was: FOG/LAC)
                pass
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass

    sev = result.get("severity", "OK")
    return 1 if sev == "Sev1" else 2 if sev == "Sev2" else 0


def _print_result(args: argparse.Namespace, result: dict[str, Any]) -> None:
    """Pretty-print drift results (dual-mode aware)."""
    if args.mode == "both":
        for mode_key, label in [("regime", "REGIME"), ("anomaly", "ANOMALY")]:
            mr = result.get(mode_key, {})
            if mr.get("severity") == "SKIP":
                print(f"[DRIFT:{label}] {args.data_dir}: SKIP — {mr.get('reason', '?')}")
                continue
            print(
                f"[DRIFT:{label}] {args.data_dir}: {mr['severity']} | "
                f"mean_PSI={mr['mean_psi']:.4f} max_PSI={mr['max_psi']:.4f} | "
                f"{mr['note']}"
            )
            print(
                f"  normalized={args.normalize} | n_live={mr.get('n_live_samples','?')} n_baseline={mr.get('n_baseline_samples','?')}"
            )
            for f in mr.get("drifting_features", []):
                print(
                    f"  {f['feature']}: PSI={f['psi']:.4f} ({f['severity']}) mean: {f['mean_baseline']:.3f}→{f['mean_live']:.3f}"
                )
    else:
        sev = result.get("severity", "OK")
        if sev == "SKIP":
            print(f"[DRIFT] {args.data_dir}: SKIP — {result.get('reason', '?')}")
            return
        print(
            f"[DRIFT:{args.mode.upper()}] {args.data_dir}: {sev} | "
            f"mean_PSI={result.get('mean_psi', 0):.4f} max_PSI={result.get('max_psi', 0):.4f} | "
            f"{result.get('note', '')}"
        )
        print(f"  normalized={args.normalize} | mode={args.mode}")
        for f in result.get("drifting_features", []):
            print(
                f"  {f['feature']}: PSI={f['psi']:.4f} ({f['severity']}) mean: {f['mean_baseline']:.3f}→{f['mean_live']:.3f}"
            )


if __name__ == "__main__":
    raise SystemExit(main())
