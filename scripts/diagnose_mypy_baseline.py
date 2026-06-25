"""Diagnose mypy baseline regression: 66 errors vs roadmap target ≤15.

Iron Law #11 compliant: all statistics from script stdout, not context reading.

Usage:
    python scripts/diagnose_mypy_baseline.py
    python scripts/diagnose_mypy_baseline.py --json  # machine-readable output
"""

import json
import subprocess
import sys
from collections import defaultdict
from typing import Any
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "mypy_baseline.json"


def load_baseline() -> dict[str, int]:
    """Load mypy_baseline.json → {filepath: error_count}."""
    with open(BASELINE_PATH) as f:
        return json.load(f)


def categorize(baseline: dict[str, int]) -> dict[str, dict]:
    """Group errors by directory prefix."""
    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"files": 0, "errors": 0, "paths": []})
    for path, count in baseline.items():
        # Normalize separators
        path_norm = path.replace("\\", "/")
        if "/" in path_norm:
            prefix = path_norm.split("/")[0] + "/"
        else:
            prefix = "(root)"
        groups[prefix]["files"] += 1
        groups[prefix]["errors"] += count
        groups[prefix]["paths"].append((path_norm, count))
    return dict(groups)


def run_live_mypy() -> tuple[int, str]:
    """Run mypy against current codebase, return (exit_code, stdout)."""
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "scripts/", "--no-error-summary"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=120,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout


def check_baseline_history() -> list[dict]:
    """Git log of mypy_baseline.json to see when counts changed."""
    result = subprocess.run(
        ["git", "log", "--oneline", "--follow", "--", "mypy_baseline.json"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=10,
        encoding="utf-8",
        errors="replace",
    )
    commits = []
    for line in result.stdout.strip().split("\n"):
        if line:
            parts = line.split(" ", 1)
            commits.append({"hash": parts[0], "message": parts[1] if len(parts) > 1 else ""})
    return commits


def parse_mypy_output(stdout: str) -> dict[str, int]:
    """Parse mypy stdout into {filepath: error_count}."""
    counts: dict[str, int] = {}
    for line in stdout.strip().split("\n"):
        if ":" in line and ": error:" in line:
            # Format: file.py:line: error: ...
            filepath = line.split(":")[0]
            counts[filepath] = counts.get(filepath, 0) + 1
    return counts


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    baseline = load_baseline()
    groups = categorize(baseline)

    # --- Section 1: Baseline structure ---
    total_errors = sum(v for v in baseline.values())
    total_files = len(baseline)
    scripts_errors = groups.get("scripts/", {}).get("errors", 0)
    scripts_files = groups.get("scripts/", {}).get("files", 0)
    core_errors = groups.get("core/", {}).get("errors", 0)
    tests_errors = groups.get("tests/", {}).get("errors", 0)

    # --- Section 2: Top offenders ---
    top_scripts = sorted(
        [(p, c) for p, c in baseline.items() if p.replace("\\", "/").startswith("scripts/")],
        key=lambda x: -x[1],
    )

    # --- Section 3: Git history ---
    commits = check_baseline_history()

    # --- Section 4: Live mypy comparison ---
    live_code, live_stdout = run_live_mypy()
    live_counts = parse_mypy_output(live_stdout)
    live_total = sum(live_counts.values())
    live_scripts_total = sum(
        c for p, c in live_counts.items() if p.replace("\\", "/").startswith("scripts/")
    )

    # --- Output ---
    if args.json:
        output = {
            "baseline_total_errors": total_errors,
            "baseline_total_files": total_files,
            "scripts_errors": scripts_errors,
            "scripts_files": scripts_files,
            "core_errors": core_errors,
            "tests_errors": tests_errors,
            "top_scripts_offenders": top_scripts[:10],
            "baseline_commits": commits[:10],
            "live_mypy_total": live_total,
            "live_mypy_scripts": live_scripts_total,
            "live_mypy_exit_code": live_code,
        }
        print(json.dumps(output, indent=2))
    else:
        print("=" * 60)
        print("  Mypy Baseline Diagnostic Report")
        print("=" * 60)
        print()
        print("--- Baseline Snapshot ---")
        print(f"  Total errors:  {total_errors}")
        print(f"  Total files:   {total_files}")
        print(f"  scripts/:       {scripts_errors} errors in {scripts_files} files")
        print(f"  core/:          {core_errors} errors")
        print(f"  tests/:         {tests_errors} errors")
        print()

        print("--- Top 5 scripts/ Offenders ---")
        for path, count in top_scripts[:5]:
            print(f"  {count:3d}  {path}")
        print()

        print("--- Roadmap Target Gap ---")
        target = 15
        gap = scripts_errors - target
        print(f"  Target (6/24):  <={target}")
        print(f"  Actual:          {scripts_errors}")
        print(f"  Gap:             {gap} ({'+' if gap > 0 else ''}{gap})")
        print(f"  Status:          {'NOT MET' if gap > 0 else 'MET'}")
        print()

        print("--- Live Mypy vs Baseline Comparison ---")
        print(f"  Live mypy exit:  {live_code}")
        print(f"  Live total:      {live_total} errors")
        print(f"  Live scripts/:   {live_scripts_total} errors")
        print(f"  Baseline total:  {total_errors}")
        if live_scripts_total > scripts_errors:
            print(
                f"  !! REGRESSION: live scripts/ ({live_scripts_total}) > baseline ({scripts_errors})"
            )
        elif live_scripts_total < scripts_errors:
            print(
                f"  [OK] Baseline is conservative: live ({live_scripts_total}) < baseline ({scripts_errors})"
            )
            print(
                f"    {scripts_errors - live_scripts_total} errors in baseline are stale (already fixed)"
            )
        else:
            print("  [=] Baseline and live mypy match")
        print()

        print("--- Baseline Git History (last 10 commits) ---")
        for c in commits[:10]:
            print(f"  {c['hash']} {c['message'][:80]}")
        print()

        # --- Assessment ---
        print("--- Assessment ---")
        if live_scripts_total > scripts_errors:
            print("  VERDICT: REAL REGRESSION — live errors exceed baseline")
            print("  ACTION:  Fix new errors → update baseline → recommit")
        elif scripts_errors > 50:
            print("  VERDICT: BASELINE RESCAN — baseline inflated, not live regression")
            print("  ACTION:  Roadmap claim of 23 on 6/20 was likely pre-rescan.")
            print(
                f"           Current live scripts/ = {live_scripts_total} vs baseline = {scripts_errors}"
            )
            print(f"           Need to triage {scripts_errors - live_scripts_total} stale entries")
            print(f"           and reduce live {live_scripts_total} toward ≤15 target.")
        else:
            print("  VERDICT: Within expected range")

    return 0


if __name__ == "__main__":
    sys.exit(main())
