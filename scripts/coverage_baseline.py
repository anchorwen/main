#!/usr/bin/env python
"""Coverage baseline snapshot and regression detector.

Takes a per-file coverage snapshot from coverage.json (produced by pytest-cov),
groups files by institutional Tier, and supports --check mode to detect regression.

Usage:
    # Snapshot current coverage as baseline:
    python scripts/coverage_baseline.py --update

    # Check for regression against baseline:
    python scripts/coverage_baseline.py --check

    # Show current coverage grouped by Tier:
    python scripts/coverage_baseline.py --show

Tier definitions (from the Asymmetric Coverage Gates plan):
    Tier 1 (capital path):   execution, risk, strategies, contracts, runtime (extracted services)
    Tier 2 (features/signals): features, alpha, brains
    Tier 3 (infrastructure):  protocol, feedback, parliament, governance, observability,
                               market, state, deployment, ledger
    Tier 4 (offline):         training, backtest, simulation

Exit codes:
    0 — no regression (or --update/--show completed)
    1 — regression detected (--check mode only)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------
TIER_MAP: dict[int, list[str]] = {
    1: [
        "core/execution",
        "core/risk",
        "core/strategies",
        "core/contracts",
        "core/runtime",  # NOTE: includes live_cycle.py (monolith — Strangler Fig in Phase 1)
    ],
    2: [
        "core/features",
        "core/alpha",
        "core/brains",
    ],
    3: [
        "core/protocol",
        "core/feedback",
        "core/parliament",
        "core/governance",
        "core/observability",
        "core/market",
        "core/state",
        "core/deployment",
        "core/ledger",
    ],
    4: [
        "core/training",
        "core/backtest",
        "core/simulation",
    ],
}

# Tier thresholds (line%, branch%)
TIER_THRESHOLDS: dict[int, tuple[float, float]] = {
    1: (85.0, 75.0),
    2: (70.0, 50.0),
    3: (50.0, 30.0),
    4: (40.0, 20.0),
}

TIER_NAMES: dict[int, str] = {
    1: "Tier 1 - Capital Path",
    2: "Tier 2 - Features & Signals",
    3: "Tier 3 - Infrastructure",
    4: "Tier 4 - Offline Pipelines",
}

BASELINE_PATH = Path(".coverage_baseline.json")


# ---------------------------------------------------------------------------
# Coverage data loading
# ---------------------------------------------------------------------------
def load_coverage_json(path: str = "coverage.json") -> dict:
    """Load coverage.json produced by pytest-cov."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_per_file_coverage(cov_data: dict) -> dict[str, dict]:
    """Extract per-file line and branch coverage from coverage data.

    Returns: {filepath: {"line_pct": float, "branch_pct": float, "lines": int, "covered": int}}
    """
    result: dict[str, dict] = {}
    files = cov_data.get("files", {})
    for fpath, finfo in files.items():
        # Normalize Windows paths
        fpath = fpath.replace("\\", "/")
        summary = finfo.get("summary", {})
        num_statements = summary.get("num_statements", 0)
        covered_lines = summary.get("covered_lines", 0)
        num_branches = summary.get("num_branches", 0)
        covered_branches = summary.get("covered_branches", 0)

        line_pct = (covered_lines / num_statements * 100) if num_statements > 0 else 100.0
        branch_pct = (covered_branches / num_branches * 100) if num_branches > 0 else 100.0

        result[fpath] = {
            "line_pct": round(line_pct, 1),
            "branch_pct": round(branch_pct, 1),
            "lines": num_statements,
            "covered": covered_lines,
            "branches": num_branches,
            "branches_covered": covered_branches,
        }

    return result


# ---------------------------------------------------------------------------
# Tier assignment
# ---------------------------------------------------------------------------
def classify_tier(filepath: str) -> int | None:
    """Return tier number (1-4) for a file, or None if unclassified."""
    # Handle top-level core files
    if filepath.startswith("core/") and "/" not in filepath[5:]:
        return None  # e.g. core/__init__.py, core/constants.py

    for tier, prefixes in TIER_MAP.items():
        for prefix in prefixes:
            if filepath.startswith(prefix + "/") or filepath == prefix + ".py":
                return tier
    return None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def aggregate_by_tier(per_file: dict[str, dict]) -> dict[int, dict]:
    """Aggregate per-file coverage into per-tier summaries."""
    tiers: dict[int, dict] = {}
    for tier in [1, 2, 3, 4]:
        tiers[tier] = {
            "files": {},
            "total_lines": 0,
            "covered_lines": 0,
            "total_branches": 0,
            "covered_branches": 0,
            "line_pct": 0.0,
            "branch_pct": 0.0,
            "threshold_line": TIER_THRESHOLDS[tier][0],
            "threshold_branch": TIER_THRESHOLDS[tier][1],
            "pass_line": False,
            "pass_branch": False,
            "file_count": 0,
            "zero_coverage_files": [],
        }

    unclassified_lines = 0
    unclassified_covered = 0

    for fpath, finfo in per_file.items():
        file_tier = classify_tier(fpath)
        if file_tier is None:
            unclassified_lines += finfo["lines"]
            unclassified_covered += finfo["covered"]
            continue

        t = tiers[file_tier]
        t["files"][fpath] = finfo
        t["total_lines"] += finfo["lines"]
        t["covered_lines"] += finfo["covered"]
        t["total_branches"] += finfo["branches"]
        t["covered_branches"] += finfo["branches_covered"]
        t["file_count"] += 1

        if finfo["line_pct"] == 0.0:
            t["zero_coverage_files"].append(fpath)

    # Compute percentages
    for tier in [1, 2, 3, 4]:
        t = tiers[tier]
        t["line_pct"] = round(
            t["covered_lines"] / t["total_lines"] * 100, 1
        ) if t["total_lines"] > 0 else 100.0
        t["branch_pct"] = round(
            t["covered_branches"] / t["total_branches"] * 100, 1
        ) if t["total_branches"] > 0 else 100.0
        t["pass_line"] = t["line_pct"] >= t["threshold_line"]
        t["pass_branch"] = t["branch_pct"] >= t["threshold_branch"]

    tiers[0] = {
        "unclassified_lines": unclassified_lines,
        "unclassified_covered": unclassified_covered,
    }
    return tiers


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
def show_tiers(tiers: dict[int, dict]) -> None:
    """Display per-tier coverage summary."""
    total_all_lines = sum(t["total_lines"] for t in tiers.values() if isinstance(t, dict) and "total_lines" in t)
    total_all_covered = sum(t["covered_lines"] for t in tiers.values() if isinstance(t, dict) and "covered_lines" in t)
    overall_line = round(total_all_covered / total_all_lines * 100, 1) if total_all_lines > 0 else 0.0

    print(f"\n{'='*70}")
    print(f"  Coverage by Institutional Tier  (overall line: {overall_line}%)")
    print(f"{'='*70}")

    for tier in [1, 2, 3, 4]:
        t = tiers[tier]
        name = TIER_NAMES[tier]
        line_status = "PASS" if t["pass_line"] else "FAIL"
        branch_status = "PASS" if t["pass_branch"] else "FAIL"

        print(f"\n  {name}")
        print(f"    Line:   {t['line_pct']:5.1f}%  (threshold: {t['threshold_line']}%)  [{line_status}]")
        print(f"    Branch: {t['branch_pct']:5.1f}%  (threshold: {t['threshold_branch']}%)  [{branch_status}]")
        print(f"    Files:  {t['file_count']}  ({t['covered_lines']}/{t['total_lines']} lines)")

        zero_files = t["zero_coverage_files"]
        if zero_files:
            print(f"    !! Zero-coverage files: {len(zero_files)}")
            for zf in sorted(zero_files)[:5]:
                print(f"       - {zf}")
            if len(zero_files) > 5:
                print(f"       ... and {len(zero_files) - 5} more")

    print(f"\n{'='*70}")
    print(f"  Tier 1 line  ≥{TIER_THRESHOLDS[1][0]}%  Tier 2 line  ≥{TIER_THRESHOLDS[2][0]}%")
    print(f"  Tier 3 line  ≥{TIER_THRESHOLDS[3][0]}%  Tier 4 line  ≥{TIER_THRESHOLDS[4][0]}%")
    print(f"{'='*70}\n")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_update() -> int:
    """Snapshot current coverage as baseline."""
    print("Updating coverage baseline...")
    cov = load_coverage_json()
    per_file = get_per_file_coverage(cov)
    tiers = aggregate_by_tier(per_file)

    baseline = {
        "timestamp": __import__("time").time(),
        "per_file": per_file,
        "tiers": {
            str(k): {
                "line_pct": v["line_pct"],
                "branch_pct": v["branch_pct"],
                "total_lines": v["total_lines"],
                "covered_lines": v["covered_lines"],
                "file_count": v["file_count"],
            }
            for k, v in tiers.items()
            if k in [1, 2, 3, 4]
        },
    }

    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2)
    print(f"Baseline saved to {BASELINE_PATH}")
    show_tiers(tiers)
    return 0


def cmd_check() -> int:
    """Check for coverage regression against baseline."""
    if not BASELINE_PATH.exists():
        print(f"ERROR: No baseline found. Run --update first.")
        return 1

    with open(BASELINE_PATH, encoding="utf-8") as f:
        baseline = json.load(f)

    cov = load_coverage_json()
    per_file = get_per_file_coverage(cov)
    tiers = aggregate_by_tier(per_file)
    baseline_tiers = baseline.get("tiers", {})

    regressions: list[str] = []
    improvements: list[str] = []

    for tier_str, bt in baseline_tiers.items():
        tier = int(tier_str)
        ct = tiers.get(tier, {})
        name = TIER_NAMES.get(tier, f"Tier {tier}")

        if ct:
            delta_line = ct["line_pct"] - bt["line_pct"]
            delta_branch = ct["branch_pct"] - bt["branch_pct"]
            th_line = TIER_THRESHOLDS[tier][0]
            th_branch = TIER_THRESHOLDS[tier][1]

            if delta_line < -1.0:  # 1% tolerance
                regressions.append(
                    f"{name}: line {bt['line_pct']:.1f}% → {ct['line_pct']:.1f}% ({delta_line:+.1f}%)"
                )
            elif delta_line > 1.0:
                improvements.append(
                    f"{name}: line {bt['line_pct']:.1f}% → {ct['line_pct']:.1f}% ({delta_line:+.1f}%)"
                )

            # Always warn if below threshold
            if ct["line_pct"] < th_line:
                regressions.append(
                    f"{name}: line {ct['line_pct']:.1f}% BELOW threshold {th_line}%"
                )

    if regressions:
        print(f"\n[WARN] COVERAGE REGRESSION DETECTED:")
        for r in regressions:
            print(f"   {r}")
        return 1

    if improvements:
        print(f"\n[OK] Coverage improvements:")
        for imp in improvements:
            print(f"   {imp}")

    print(f"\n[OK] No coverage regression detected.")
    show_tiers(tiers)
    return 0


def cmd_show() -> int:
    """Display current coverage by tier."""
    cov = load_coverage_json()
    per_file = get_per_file_coverage(cov)
    tiers = aggregate_by_tier(per_file)
    show_tiers(tiers)

    # Count zero-coverage files
    zero_files = [
        f for f, info in per_file.items()
        if info["line_pct"] == 0.0 and not f.endswith("__init__.py")
    ]
    print(f"Zero-coverage files (non-__init__): {len(zero_files)}")
    for zf in sorted(zero_files):
        info = per_file[zf]
        print(f"  {zf}  ({info['lines']} lines)")

    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Coverage baseline snapshot and regression detector"
    )
    parser.add_argument(
        "--update", action="store_true", help="Snapshot current coverage as baseline"
    )
    parser.add_argument(
        "--check", action="store_true", help="Check for regression against baseline"
    )
    parser.add_argument(
        "--show", action="store_true", help="Show current coverage by tier"
    )
    args = parser.parse_args()

    if args.update:
        return cmd_update()
    elif args.check:
        return cmd_check()
    elif args.show:
        return cmd_show()
    else:
        # Default: show
        return cmd_show()


if __name__ == "__main__":
    sys.exit(main())
