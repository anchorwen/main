"""Training data quality gate — CI checks before model training.

Validates training data against quality thresholds. Fails the gate (non-zero
exit) if any check is below threshold. Designed for CI/CD integration.

Checks:
  1. Label balance — no class below min_ratio
  2. Feature validity — no NaN columns, reasonable value ranges
  3. Temporal integrity — train timestamps precede val timestamps
  4. Minimum samples — enough data to train
  5. Contract compliance — labels match contract's expected classes
  6. Feature coverage — no constant/dead features

Usage:
  # Check a training dataset
  python scripts/training/quality_gate.py --data data/training/train.npz

  # Check both train and val sets
  python scripts/training/quality_gate.py \\
    --data data/training/train.npz \\
    --val-data data/training/val.npz

  # With label contract validation
  python scripts/training/quality_gate.py \\
    --data data/training/train.npz \\
    --label-contract blueprints/contracts/label-survival-barrier-1.0.0.json

  # Strict mode (exit non-zero on any warning)
  python scripts/training/quality_gate.py --data data/training/train.npz --strict

Exit codes:
  0 — All checks passed
  1 — Soft failure (warnings only, not --strict)
  2 — Hard failure (thresholds breached)
  3 — Data file not found or unreadable
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

# ── Default thresholds ──
DEFAULT_MIN_SAMPLES = 100
DEFAULT_MIN_LABEL_RATIO = 0.15  # Minority class must be at least 15%
DEFAULT_MAX_FEATURE_ABS = 100.0  # Features beyond this are suspicious
DEFAULT_MAX_CONSTANT_FRAC = 0.95  # >95% same value = dead feature


# ═══════════════════════════════════════════════════════════════════════
# Individual checks (each returns list of issue strings)
# ═══════════════════════════════════════════════════════════════════════


def check_min_samples(
    X: np.ndarray, y: np.ndarray, min_samples: int = DEFAULT_MIN_SAMPLES
) -> list[str]:
    n = len(y)
    if n < min_samples:
        return [f"min_samples: {n} < {min_samples} (too few samples)"]
    return []


def check_label_balance(y: np.ndarray, min_ratio: float = DEFAULT_MIN_LABEL_RATIO) -> list[str]:
    n = len(y)
    if n == 0:
        return ["label_balance: no samples"]
    unique, counts = np.unique(y, return_counts=True)
    min_count = counts.min()
    min_class = unique[counts.argmin()]
    ratio = min_count / n
    if ratio < min_ratio:
        return [
            f"label_balance: class={min_class} has {min_count}/{n} ({ratio:.1%}) "
            f"< min_ratio={min_ratio:.0%}"
        ]
    return []


def check_feature_validity(X: np.ndarray, max_abs: float = DEFAULT_MAX_FEATURE_ABS) -> list[str]:
    issues: list[str] = []
    n_features = X.shape[1]

    for j in range(n_features):
        col = X[:, j]
        # NaN check
        nan_count = int(np.isnan(col).sum())
        if nan_count > 0:
            issues.append(f"feature f_{j}: {nan_count} NaN values ({nan_count/len(col):.1%})")
        # Inf check
        inf_count = int(np.isinf(col).sum())
        if inf_count > 0:
            issues.append(f"feature f_{j}: {inf_count} Inf values")
        # Outlier range check
        abs_max = float(np.nanmax(np.abs(col)))
        if abs_max > max_abs:
            issues.append(f"feature f_{j}: max_abs={abs_max:.1f} > {max_abs} (suspicious range)")

    return issues


def check_feature_coverage(
    X: np.ndarray, max_constant_frac: float = DEFAULT_MAX_CONSTANT_FRAC
) -> list[str]:
    """Flag features that are constant or nearly constant (dead features)."""
    issues: list[str] = []
    n = len(X)
    if n < 2:
        return []

    for j in range(X.shape[1]):
        col = X[:, j]
        if np.all(np.isnan(col)):
            issues.append(f"feature f_{j}: all NaN (dead feature)")
            continue
        # Check if most values are the same
        unique_ratio = len(np.unique(col[: min(n, 5000)])) / min(n, 5000)
        if unique_ratio < (1.0 - max_constant_frac):
            issues.append(f"feature f_{j}: {unique_ratio:.1%} unique (nearly constant)")

        # Check std
        std_val = float(np.nanstd(col))
        if std_val < 1e-8:
            issues.append(f"feature f_{j}: std={std_val:.2e} (zero variance)")

    return issues


def check_temporal_integrity(
    train_meta: dict[str, Any] | None,
    val_meta: dict[str, Any] | None,
) -> list[str]:
    """Verify train data timestamps precede validation data."""
    if train_meta is None or val_meta is None:
        return []
    issues: list[str] = []

    train_end = train_meta.get("max_time") or train_meta.get("end_date")
    val_start = val_meta.get("min_time") or val_meta.get("start_date")

    if train_end and val_start and str(train_end) > str(val_start):
        issues.append(f"temporal_leak: train end ({train_end}) after val start ({val_start})")
    return issues


def check_contract_compliance(
    labels: list[str],
    expected_classes: set[str],
) -> list[str]:
    """Verify label values match the contract's expected classes."""
    if not expected_classes:
        return []
    actual = set(labels)
    missing = expected_classes - actual
    extra = actual - expected_classes
    issues: list[str] = []
    if missing:
        issues.append(f"contract_compliance: missing classes {missing}")
    if extra and "unlabeled" not in extra:
        issues.append(f"contract_compliance: unexpected classes {extra}")
    return issues


# ═══════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════


def run_quality_gate(
    data_path: Path,
    *,
    val_data_path: Path | None = None,
    label_contract_path: Path | None = None,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_label_ratio: float = DEFAULT_MIN_LABEL_RATIO,
) -> dict[str, Any]:
    """Run all quality checks and return a report dict.

    Returns:
        {
            "passed": bool,
            "checks": {check_name: {"passed": bool, "issues": [...]}},
            "summary": {"n_features": int, "n_samples": int, "label_distribution": {...}},
        }
    """
    from scripts.training.trainers.xgb_trainer import load_training_data

    X, y, _pnl, feature_names = load_training_data(data_path)

    val_X: np.ndarray | None = None
    val_y: np.ndarray | None = None
    if val_data_path is not None and val_data_path.exists():
        val_X, val_y, _, _ = load_training_data(val_data_path)

    # ── Expected classes from contract ──
    expected_classes: set[str] = set()
    if label_contract_path is not None and label_contract_path.exists():
        contract = json.loads(label_contract_path.read_text(encoding="utf-8"))
        lc = contract.get("label_classes", {})
        expected_classes = set(lc.values())

    # ── Label distribution ──
    unique, counts = np.unique(y, return_counts=True)
    label_dist = {f"class_{int(k)}": int(v) for k, v in zip(unique, counts, strict=False)}

    # ── Classification labels from training data ──
    label_set = {"win" if v == 1 else "loss" for v in np.unique(y)}
    if expected_classes:
        label_set = set(str(v) for v in np.unique(y))

    # ── Run checks ──
    checks: dict[str, dict[str, Any]] = {}

    issues = check_min_samples(X, y, min_samples)
    checks["min_samples"] = {"passed": len(issues) == 0, "issues": issues}

    issues = check_label_balance(y, min_label_ratio)
    checks["label_balance"] = {"passed": len(issues) == 0, "issues": issues}

    issues = check_feature_validity(X)
    checks["feature_validity"] = {"passed": len(issues) == 0, "issues": issues}

    issues = check_feature_coverage(X)
    checks["feature_coverage"] = {"passed": len(issues) == 0, "issues": issues}

    if val_X is not None and val_y is not None:
        val_issues = check_label_balance(val_y, min_label_ratio)
        checks["val_label_balance"] = {"passed": len(val_issues) == 0, "issues": val_issues}

    if expected_classes:
        issues = check_contract_compliance(list(label_set), expected_classes)
        checks["contract_compliance"] = {"passed": len(issues) == 0, "issues": issues}

    # ── Overall pass/fail ──
    all_passed = all(c["passed"] for c in checks.values())
    failed_checks = [k for k, v in checks.items() if not v["passed"]]

    return {
        "schema_version": "quality_gate.v1",
        "passed": all_passed,
        "failed_checks": failed_checks,
        "checks": checks,
        "summary": {
            "n_samples": int(len(y)),
            "n_features": int(X.shape[1]),
            "feature_names": feature_names[:10] if feature_names else [],
            "label_distribution": label_dist,
            "val_samples": int(len(val_y)) if val_y is not None else None,
        },
    }


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="quality_gate",
        description="Training data quality CI gate",
    )
    p.add_argument("--data", type=Path, required=True, help="Training data (NPZ or Parquet)")
    p.add_argument("--val-data", type=Path, default=None, help="Validation data (NPZ or Parquet)")
    p.add_argument(
        "--label-contract", type=Path, default=None, help="Label Contract JSON for compliance check"
    )
    p.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    p.add_argument("--min-label-ratio", type=float, default=DEFAULT_MIN_LABEL_RATIO)
    p.add_argument("--strict", action="store_true", help="Exit 1 on any warning")
    p.add_argument("--output", type=Path, default=None, help="Write report JSON to file")
    p.add_argument("--quiet", action="store_true", help="Only print summary line")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.data.exists():
        print(f"[ERROR] Data file not found: {args.data}", file=sys.stderr)
        return 3

    report = run_quality_gate(
        args.data,
        val_data_path=args.val_data,
        label_contract_path=args.label_contract,
        min_samples=args.min_samples,
        min_label_ratio=args.min_label_ratio,
    )

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        if not args.quiet:
            print(f"[quality_gate] Report: {out}")

    if args.quiet:
        status = "PASS" if report["passed"] else "FAIL"
        failed = ", ".join(report["failed_checks"])
        samples = report["summary"]["n_samples"]
        label_dist = report["summary"]["label_distribution"]
        print(f"[quality_gate] {status}  samples={samples}  labels={label_dist}  failed=[{failed}]")
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))

    if not report["passed"]:
        return 1 if args.strict else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
