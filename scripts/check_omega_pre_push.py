#!/usr/bin/env python3
"""Pre-push omega compliance scan — standalone hook for pre-commit framework.

Scans all unpushed commits for Omega-Routing signatures.
Extracted from ``scripts/hook_pre_push.py`` (UGR-A10 / P0-3).

Performance: < 2s — reads commit messages only, no file I/O beyond git log.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_SUBPROCESS_KWARGS: dict = {"encoding": "utf-8", "errors": "replace"}
SIGNATURE_RE = re.compile(r"\[[OΩ].*Routing:\s*Scene\s+[A-H]", re.IGNORECASE)


def check_omega() -> bool:
    """Verify all unpushed commits have Omega-Routing signatures."""
    print("\n" + "=" * 60)
    print("[pre-push] Omega compliance scan")
    print("=" * 60)

    try:
        result = subprocess.run(
            ["git", "log", "--format=%B", "@{u}..HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=30,
            **_SUBPROCESS_KWARGS,
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["git", "log", "--format=%B", "-1", "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                timeout=10,
                **_SUBPROCESS_KWARGS,
            )

        all_commits = (result.stdout or "").strip()
        if not all_commits:
            print("[pre-push] Omega: no unpushed commits — SKIPPED")
            return True

        raw = all_commits.strip()
        commits = (
            [raw]
            if "\n\n-- \n" not in raw
            else [c.strip() for c in raw.split("\n\n-- \n") if c.strip()]
        )
        missing = 0
        for i, commit in enumerate(commits):
            first_line = commit.strip().split("\n")[0] if commit else ""
            has_sig = bool(SIGNATURE_RE.search(commit))
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
    except Exception as exc:  # noqa: BLE001
        print(f"[pre-push] Omega: INTERNAL ERROR — {exc}")
        return False


def main() -> int:
    return 0 if check_omega() else 1


if __name__ == "__main__":
    sys.exit(main())
