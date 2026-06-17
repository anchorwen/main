#!/usr/bin/env python
"""Pre-push hook — mirrors CI checks locally before git push.

Runs the EXACT same checks as .github/workflows/ci-windows.yml:
  1. Ruff full lint (core/ apps/ scripts/) — same scope as CI
  2. Mypy baseline check — no new type errors vs mypy_baseline.json

This hook does NOT use git stash — it only reads files, never modifies them.
Safe to run alongside live trading processes.

Exit codes:
  0 — all checks passed, push proceeds
  1 — checks failed, push BLOCKED (fix errors locally first)

Installation (one-time):
    cd .git/hooks
    cp ../../scripts/hook_pre_push.py pre-push
    # or create a symlink

Override (emergency only):
    git push --no-verify   # bypasses the pre-push hook
    # MUST document reason in commit message: --no-verify: <reason>
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Check 1: Ruff full-codebase lint (mirrors CI)
# ---------------------------------------------------------------------------
def check_ruff() -> bool:
    """Run ruff check on core/ apps/ scripts/ — same as CI."""
    print("\n" + "=" * 60)
    print("[pre-push] Ruff check (core/ apps/ scripts/)")
    print("=" * 60)

    result = subprocess.run(
        [
            sys.executable, "-m", "ruff", "check",
            "core/", "apps/", "scripts/",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode == 0:
        print("[pre-push] Ruff: PASSED")
        return True

    print("[pre-push] Ruff: FAILED")
    print(result.stdout)
    if result.stderr:
        print(result.stderr)
    return False


# ---------------------------------------------------------------------------
# Check 2: Mypy baseline (mirrors CI)
# ---------------------------------------------------------------------------
def check_mypy() -> bool:
    """Run mypy baseline check — same as CI."""
    print("\n" + "=" * 60)
    print("[pre-push] Mypy baseline check")
    print("=" * 60)

    result = subprocess.run(
        [sys.executable, "scripts/pre_commit_mypy.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode == 0:
        print("[pre-push] Mypy: PASSED")
        return True

    print("[pre-push] Mypy: FAILED (new type errors detected)")
    print(result.stdout)
    if result.stderr:
        print(result.stderr)
    return False


# ---------------------------------------------------------------------------
# Check 3: Omega protocol compliance scan
# ---------------------------------------------------------------------------
def check_omega() -> bool:
    """Verify all commits being pushed have Ω-Routing signatures."""
    print("\n" + "=" * 60)
    print("[pre-push] Omega compliance scan")
    print("=" * 60)

    try:
        # Try upstream range first; fall back to last push
        result = subprocess.run(
            ["git", "log", "--format=%B", "@{u}..HEAD"],
            cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0 or not (result.stdout or "").strip():
            # Fallback: check only the latest commit
            result = subprocess.run(
                ["git", "log", "--format=%B", "-1", "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True, text=True, timeout=10,
            )

        all_commits = result.stdout or ""
        if not all_commits.strip():
            print("[pre-push] Omega: no commits to check — SKIPPED")
            return True

        # Check each commit for omega signature (split by commit delimiter or use whole)
        raw = all_commits.strip()
        commits = [raw] if "\n\n-- \n" not in raw else [
            c.strip() for c in raw.split("\n\n-- \n") if c.strip()
        ]
        missing = 0
        for i, commit in enumerate(commits):
            first_line = commit.strip().split("\n")[0] if commit else ""
            has_sig = bool(re.search(
                r"\[[OΩ].*Routing:\s*Scene\s+[A-G]",
                commit, re.IGNORECASE,
            ))
            if not has_sig:
                print(f"[pre-push] Commit {i+1} missing Ω signature: {first_line[:80]}")
                missing += 1

        if missing > 0:
            print(f"[pre-push] Omega: {missing} commit(s) missing Ω-Routing signature.")
            print("[pre-push] Add [Ω-Routing: Scene X → ...] to commit message.")
            return False

        print(f"[pre-push] Omega: PASSED ({len(commits)} commits valid)")
        return True

    except subprocess.TimeoutExpired:
        print("[pre-push] Omega: TIMEOUT (>30s)")
        return False
    except Exception as exc:
        print(f"[pre-push] Omega: INTERNAL ERROR — {exc}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    """Run all pre-push checks. Block push on any failure."""
    print("[pre-push] Running CI-equivalent checks before push...")

    checks = [
        ("ruff", check_ruff),
        ("mypy", check_mypy),
        ("omega", check_omega),
    ]

    failures: list[str] = []
    for name, check_fn in checks:
        try:
            if not check_fn():
                failures.append(name)
        except subprocess.TimeoutExpired:
            print(f"[pre-push] {name}: TIMEOUT (>120s)")
            failures.append(name)
        except Exception as exc:
            print(f"[pre-push] {name}: INTERNAL ERROR — {exc}")
            failures.append(name)

    if failures:
        print(f"\n[pre-push] BLOCKED — {len(failures)} check(s) failed: {', '.join(failures)}")
        print("[pre-push] Fix the errors above locally, then re-commit and push.")
        print("[pre-push] Emergency override: git push --no-verify")
        return 1

    print("\n[pre-push] All checks PASSED. Push proceeding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
