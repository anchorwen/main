#!/usr/bin/env python3
"""Git pre-commit mypy hook -- the iron law.

Only flags NEW mypy errors beyond the stored baseline.
Reads staged .py files, runs mypy on each, compares error count
with mypy_baseline.json.  If any file has MORE errors than baseline,
the commit is BLOCKED.

To update the baseline (when you intentionally fix/change types):
    python scripts/pre_commit_mypy.py --update-baseline
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from core.runtime.fault_handler import fail_open_guard

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "mypy_baseline.json"


def load_baseline() -> dict[str, int]:
    if BASELINE_PATH.exists():
        with open(BASELINE_PATH, encoding="utf-8") as f:
            raw: object = json.load(f)
            if isinstance(raw, dict):
                return {str(k): int(v) for k, v in raw.items()}
    return {}


def save_baseline(baseline: dict[str, int]) -> None:
    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, sort_keys=True)


def staged_py_files() -> list[str]:
    """Return list of staged .py files (relative paths)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
        )
        if result.returncode != 0:
            return []
        return [
            line.strip()
            for line in result.stdout.strip().split("\n")
            if line.strip().endswith(".py")
        ]
    except Exception:  # BLE001:FOG
        with fail_open_guard("pre_commit_mypy:staged_py_files"):
            return []
def run_mypy(filepath: str) -> tuple[int, str]:
    """Run mypy on a single file. Returns (error_count, output)."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "mypy", "--no-error-summary", filepath],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=30,
        )
        output = result.stdout.strip()
        errors = [l for l in output.split("\n") if l.strip() and ": error:" in l]
        # If mypy exited non-zero but produced no recognizable errors, it crashed
        if result.returncode != 0 and not errors:
            crash_msg = result.stderr.strip() or output or f"mypy exit {result.returncode}"
            return -1, f"mypy crash: {crash_msg}"
        return len(errors), output
    except subprocess.TimeoutExpired:
        return -1, "mypy timed out"
    except FileNotFoundError:
        return -1, "mypy not installed — run: pip install mypy"
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("pre_commit_mypy:run_mypy"):
            return -1, str(exc)
def main() -> int:
    if "--update-baseline" in sys.argv:
        # Rebuild baseline from ALL tracked files
        print("Rebuilding mypy baseline from all tracked files...")
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"],
            capture_output=True,
            text=False,
            cwd=str(ROOT),
            timeout=10,
        )
        files = result.stdout.decode("utf-8", errors="replace").split("\0")
        new_baseline: dict[str, int] = {}
        for f in files:
            f = f.strip()
            if not f:
                continue
            count, _ = run_mypy(f)
            if count > 0:
                new_baseline[f] = count
        save_baseline(new_baseline)
        print(f"Baseline updated: {len(new_baseline)} files, {sum(new_baseline.values())} errors")
        return 0

    staged = staged_py_files()
    if not staged:
        return 0

    baseline = load_baseline()
    new_errors: list[str] = []
    crashes: list[str] = []
    total_checked = 0

    for rel_path in staged:
        abs_path = ROOT / rel_path
        if not abs_path.exists():
            continue
        total_checked += 1
        count, output = run_mypy(str(abs_path))
        if count < 0:
            crashes.append(f"  {rel_path}: {output}")
            continue
        prev = baseline.get(rel_path, 0)
        if count > prev:
            new_lines = count - prev
            new_errors.append(f"  {rel_path}: {prev} -> {count} errors (+{new_lines} NEW)")
            # Show the full mypy output for this file
            if output:
                for line in output.split("\n"):
                    if ": error:" in line:
                        new_errors.append(f"    {line.strip()}")

    if crashes:
        print("=" * 72)
        print("IRON LAW: mypy FAILED TO RUN on staged files!")
        print("Install mypy in the pre-commit environment or system Python.")
        print("=" * 72)
        for c in crashes:
            print(c)
        return 1

    if new_errors:
        print("=" * 72)
        print("IRON LAW: mypy errors INCREASED on staged files!")
        print("Fix the new errors before committing, or run:")
        print("  python scripts/pre_commit_mypy.py --update-baseline")
        print("  (only if the new errors are intentional type additions)")
        print("=" * 72)
        for e in new_errors:
            print(e)
        print(f"\nChecked {total_checked} file(s).")
        return 1

    print(f"mypy: {total_checked} file(s) checked, no new errors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
