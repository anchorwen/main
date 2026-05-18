#!/usr/bin/env python3
"""Unified verification script -- the iron law gate.

Usage:
    python scripts/verify.py --quick       # mypy + ruff on changed files (~10s)
    python scripts/verify.py --full        # mypy + ruff + pytest (~2min)
    python scripts/verify.py --stamp       # update verification stamp after passing
    python scripts/verify.py --check-stamp # exit 0 if stamp is current
    python scripts/verify.py --mypy-only --files core/runtime/live_cycle.py

The --mypy-only mode is designed for the Claude Code PostToolUse hook.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAMP_FILE = ROOT / ".verify_stamp.json"

# Force UTF-8 on Windows
if sys.platform == "win32":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def _changed_py_files() -> list[str]:
    """Return list of changed .py files (unstaged + staged vs HEAD)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
        )
        staged = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
        )
        files: set[str] = set()
        for r in [result, staged]:
            if r.returncode == 0:
                for line in r.stdout.strip().split("\n"):
                    line = line.strip()
                    if line.endswith(".py"):
                        files.add(line)
        return sorted(files)
    except Exception:
        return []


def _current_commit_hash() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def run_mypy(targets: list[str] | None = None) -> tuple[bool, str]:
    """Run mypy on specified targets. Returns (passed, output)."""
    if targets is None:
        targets = ["core/", "apps/", "scripts/"]
    existing = [t for t in targets if (ROOT / t).exists()]
    if not existing:
        return True, "No targets to check."
    try:
        result = subprocess.run(
            [sys.executable, "-m", "mypy", "--no-error-summary", *existing],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=120,
        )
        passed = result.returncode == 0
        output = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
        return passed, output
    except subprocess.TimeoutExpired:
        return False, "mypy timed out"
    except Exception as exc:
        return False, str(exc)


def run_ruff(targets: list[str] | None = None) -> tuple[bool, str]:
    """Run ruff check. Returns (passed, output)."""
    if targets is None:
        targets = ["core/", "apps/", "scripts/"]
    existing = [t for t in targets if (ROOT / t).exists()]
    if not existing:
        return True, "No targets to check."
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", *existing],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=60,
        )
        passed = result.returncode == 0
        output = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
        return passed, output
    except subprocess.TimeoutExpired:
        return False, "ruff timed out"
    except Exception as exc:
        return False, str(exc)


def run_pytest() -> tuple[bool, str]:
    """Run full test suite. Returns (passed, output summary)."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-x", "-q", "--tb=short"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=300,
        )
        passed = result.returncode == 0
        lines = result.stdout.strip().split("\n")
        summary = lines[-1] if lines else result.stdout.strip()
        return passed, summary
    except subprocess.TimeoutExpired:
        return False, "pytest timed out"
    except Exception as exc:
        return False, str(exc)


def _compute_file_hash() -> str:
    """Simple hash of tracked .py file sizes+mtimes for change detection."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"],
            capture_output=True,
            text=False,
            cwd=str(ROOT),
            timeout=5,
        )
        if result.returncode != 0:
            return ""
        raw = result.stdout.decode("utf-8", errors="replace")
        files = raw.split("\0")
        items: list[str] = []
        for f in files:
            fp = ROOT / f
            if fp.exists():
                st = fp.stat()
                items.append(f"{f}:{st.st_mtime}:{st.st_size}")
        return str(hash("\n".join(sorted(items))))
    except Exception:
        return ""


def update_stamp(passed: bool, details: str) -> str:
    """Write verification stamp. Returns status message."""
    stamp = {
        "passed": passed,
        "timestamp": time.time(),
        "commit": _current_commit_hash(),
        "file_hash": _compute_file_hash(),
        "details": details[:500],
    }
    STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STAMP_FILE, "w", encoding="utf-8") as f:
        json.dump(stamp, f, indent=2)
    if passed:
        return "Stamp updated -- verification PASSED"
    else:
        return "Stamp NOT updated -- verification FAILED"


def check_stamp() -> tuple[bool, str]:
    """Check if stamp is current. Returns (valid, reason)."""
    if not STAMP_FILE.exists():
        return False, "No verification stamp. Run: python scripts/verify.py --full --stamp"
    try:
        with open(STAMP_FILE, encoding="utf-8") as f:
            stamp = json.load(f)
    except Exception:
        return False, "Stamp corrupt. Re-run: python scripts/verify.py --full --stamp"

    if not stamp.get("passed"):
        return False, "Last verification FAILED. Fix errors and re-run with --stamp."
    if stamp.get("file_hash") != _compute_file_hash():
        return False, "Files changed since last verification. Re-run with --stamp."
    if stamp.get("commit") != _current_commit_hash():
        return False, "HEAD moved. Re-run verification with --stamp."
    age = time.time() - stamp.get("timestamp", 0)
    if age > 1800:  # 30 minutes
        return False, f"Stamp expired ({int(age)}s ago). Re-run with --stamp."
    return True, "Stamp valid"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Iron law verification gate")
    parser.add_argument("--quick", action="store_true", help="mypy + ruff on changed files only")
    parser.add_argument("--full", action="store_true", help="mypy + ruff + pytest on full codebase")
    parser.add_argument(
        "--stamp", action="store_true", help="Update verification stamp after checks pass"
    )
    parser.add_argument(
        "--check-stamp", action="store_true", help="Check if stamp is current (exit 0 = clean)"
    )
    parser.add_argument(
        "--mypy-only", action="store_true", help="Run only mypy (for PostToolUse hook)"
    )
    parser.add_argument(
        "--blueprints", action="store_true", help="Run blueprint consistency validation"
    )
    parser.add_argument("--files", nargs="*", help="Specific file(s) to check")
    args = parser.parse_args()

    # --blueprints: blueprint consistency check
    if args.blueprints:
        try:
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "validate_blueprints.py")],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                timeout=30,
            )
            print(result.stdout.strip())
            if result.stderr.strip():
                print(result.stderr.strip())
            return result.returncode
        except Exception as exc:
            print(f"Blueprint validation error: {exc}")
            return 1

    # --check-stamp: lightweight, no heavy checks
    if args.check_stamp or (
        not args.quick and not args.full and not args.mypy_only and not args.files
    ):
        valid, reason = check_stamp()
        print(reason)
        return 0 if valid else 1

    # --mypy-only: for PostToolUse hook
    if args.mypy_only:
        targets = args.files if args.files else None
        passed, output = run_mypy(targets)
        if output:
            print(output)
        return 0 if passed else 1

    all_passed = True

    if args.quick:
        changed = _changed_py_files()
        if not changed:
            print("No changed Python files.")
        else:
            print(f"Checking {len(changed)} changed file(s)...")
            passed, output = run_mypy(changed)
            if not passed:
                print(f"[FAIL] mypy:\n{output}")
                all_passed = False
            else:
                print("[PASS] mypy")

            passed, output = run_ruff(changed)
            if not passed:
                print(f"[FAIL] ruff:\n{output}")
                all_passed = False
            else:
                print("[PASS] ruff")

            print(">>> blueprint compliance (Iron Law #7)...")
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "check_blueprint_compliance.py"),
                        "--check",
                    ],
                    capture_output=True,
                    text=True,
                    cwd=str(ROOT),
                    timeout=30,
                )
                print(result.stdout.strip())
                if result.stderr.strip():
                    print(result.stderr.strip())
                if result.returncode != 0:
                    all_passed = False
            except Exception as exc:
                print(f"[FAIL] blueprint compliance check error: {exc}")
                all_passed = False

    elif args.full:
        print(">>> mypy...")
        passed, output = run_mypy()
        if not passed:
            print(f"[FAIL] mypy:\n{output}")
            all_passed = False
        else:
            print("[PASS] mypy")

        print(">>> ruff...")
        passed, output = run_ruff()
        if not passed:
            print(f"[FAIL] ruff:\n{output}")
            all_passed = False
        else:
            print("[PASS] ruff")

        print(">>> blueprint compliance (Iron Law #7)...")
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "check_blueprint_compliance.py"),
                    "--check",
                    "--all",
                ],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                timeout=30,
            )
            print(result.stdout.strip())
            if result.stderr.strip():
                print(result.stderr.strip())
            if result.returncode != 0:
                all_passed = False
        except Exception as exc:
            print(f"[FAIL] blueprint compliance check error: {exc}")
            all_passed = False

        print(">>> pytest...")
        passed, output = run_pytest()
        if not passed:
            print(f"[FAIL] pytest:\n{output}")
            all_passed = False
        else:
            print(f"[PASS] pytest: {output}")

    if args.stamp:
        msg = update_stamp(all_passed, "full" if args.full else "quick")
        print(msg)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
