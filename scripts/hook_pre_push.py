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

from core.runtime.fault_handler import fail_open_guard

REPO_ROOT = Path(__file__).resolve().parent.parent

# On Windows, subprocess defaults to the system ANSI code-page (e.g. GBK).
# Ruff / mypy output may contain Unicode characters that the ANSI code-page
# cannot represent, causing UnicodeDecodeError in the reader thread.
# Use UTF-8 everywhere — this hook mirrors CI, and CI runs with PYTHONUTF8=1.
_SUBPROCESS_KWARGS: dict = {"encoding": "utf-8", "errors": "replace"}


# ---------------------------------------------------------------------------
# Check 1: Ruff full-codebase lint (mirrors CI)
# ---------------------------------------------------------------------------
def _git_tracked_py_files() -> list[str] | None:
    """Return list of git-tracked .py files under core/ apps/ scripts/.

    Returns None if git ls-files fails (e.g. not a git repo).
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "core/", "apps/", "scripts/"],
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout.replace(b"\r\n", b"\n")  # normalize CRLF on Windows
        files = [f.decode("utf-8") for f in raw.split(b"\0") if f.endswith(b".py")]
        return files or None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _run_ruff_on_paths(paths: list[str]) -> subprocess.CompletedProcess:
    """Run ruff on a list of file paths, respecting Windows cmdline limits."""
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", *paths],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=120,
        **_SUBPROCESS_KWARGS,
    )


def check_ruff() -> bool:
    """Run ruff check on git-tracked Python files — same surface as CI.

    CI runs on a clean checkout (only tracked files exist), so we mirror
    that by running ruff only against ``git ls-files`` output.  Untracked
    ad-hoc scripts are skipped — they don't land on CI and must not block
    the pre-push gate.
    """
    tracked = _git_tracked_py_files()

    if tracked is None:
        # Fallback: git ls-files failed — run on directories like CI does
        print("\n" + "=" * 60)
        print("[pre-push] Ruff check (core/ apps/ scripts/ — directory fallback)")
        print("=" * 60)
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "core/", "apps/", "scripts/"],
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=120,
            **_SUBPROCESS_KWARGS,
        )
    else:
        print(f"\n{'='*60}")
        print(
            f"[pre-push] Ruff check ({len(tracked)} git-tracked .py files in core/ apps/ scripts/)"
        )
        print("=" * 60)
        if not tracked:
            print("[pre-push] Ruff: SKIPPED (no tracked .py files)")
            return True

        # Batch to fit within Windows ~32K cmdline limit
        BATCH_SIZE = 400
        all_stdout: list[str] = []
        all_stderr: list[str] = []
        failed = False

        for i in range(0, len(tracked), BATCH_SIZE):
            batch = tracked[i : i + BATCH_SIZE]
            # ruff-check each batch; aggregate output on failure
            result = _run_ruff_on_paths(batch)
            if result.returncode != 0:
                failed = True
                if result.stdout:
                    all_stdout.append(result.stdout)
                if result.stderr:
                    all_stderr.append(result.stderr)

        if not failed:
            print("[pre-push] Ruff: PASSED")
            return True

        print("[pre-push] Ruff: FAILED")
        for out in all_stdout:
            print(out)
        for err in all_stderr:
            print(err)
        return False

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
        timeout=120,
        **_SUBPROCESS_KWARGS,
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
            capture_output=True,
            timeout=30,
            **_SUBPROCESS_KWARGS,
        )
        if result.returncode != 0:
            # No upstream configured — check only the latest commit
            result = subprocess.run(
                ["git", "log", "--format=%B", "-1", "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                timeout=10,
                **_SUBPROCESS_KWARGS,
            )

        all_commits = (result.stdout or "").strip()
        if not all_commits:
            # No un-pushed commits — nothing to verify
            print("[pre-push] Omega: no unpushed commits — SKIPPED")
            return True

        # Check each commit for omega signature (split by commit delimiter or use whole)
        raw = all_commits.strip()
        commits = (
            [raw]
            if "\n\n-- \n" not in raw
            else [c.strip() for c in raw.split("\n\n-- \n") if c.strip()]
        )
        missing = 0
        for i, commit in enumerate(commits):
            first_line = commit.strip().split("\n")[0] if commit else ""
            has_sig = bool(
                re.search(
                    r"\[[OΩ].*Routing:\s*Scene\s+[A-H]",
                    commit,
                    re.IGNORECASE,
                )
            )
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
    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        with fail_open_guard("hook_pre_push:check_omega"):
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
        except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
            with fail_open_guard("hook_pre_push:main"):
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
