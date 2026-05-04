"""Pre-commit gate: auto-update architecture/ docs when core/ files change.

Strategy (mode: AUTO-UPDATE):
1. Detect if any watched files (core/ apps/ configs/ scripts/ main.py) changed.
2. If changed, run scanner + doc_generator to refresh:
   - MODULE_INVENTORY.md
   - DEPENDENCY_GRAPH.md
   - CHANGELOG.md (append)
3. Auto-stage the updated docs so the commit includes them.
4. If generation fails, refuse the commit.

Override: ARCHITECTURE_GATE_BYPASS=1
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

WATCHED_DIRS = [
    "core/",
    "apps/",
    "configs/",
    "scripts/",
    "main.py",
]

DOCS = [
    "roadmap/architecture/MODULE_INVENTORY.md",
    "roadmap/architecture/DEPENDENCY_GRAPH.md",
    "roadmap/changelog/CHANGELOG.md",
]


def _is_ci() -> bool:
    return any(
        var in os.environ
        for var in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_HOME", "TF_BUILD")
    )


def _bypassed() -> bool:
    return os.environ.get("ARCHITECTURE_GATE_BYPASS", "").strip() in ("1", "true", "yes")


def _staged_files() -> list[str]:
    """Return list of staged file paths relative to repo root."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def _any_watched_changed(staged: list[str]) -> bool:
    """Check if any staged file is under a watched directory."""
    for path in staged:
        for watched in WATCHED_DIRS:
            if path == watched or path.startswith(watched):
                return True
    return False


def _run_doc_generation() -> int:
    """Run scanner + doc_generator. Returns exit code."""
    try:
        from roadmap.scripts.doc_generator import write_all

        write_all()
        return 0
    except Exception as exc:
        print(f"[architecture-gate] ERROR: doc generation failed: {exc}", file=sys.stderr)
        return 1


def _stage_docs() -> int:
    """Git-add the generated docs."""
    try:
        result = subprocess.run(
            ["git", "add"] + DOCS,
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
        )
        if result.returncode != 0:
            print(f"[architecture-gate] WARNING: git add failed: {result.stderr}", file=sys.stderr)
            return 1
        return 0
    except Exception as exc:
        print(f"[architecture-gate] WARNING: git add error: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    if _is_ci():
        print("[architecture-gate] CI detected — skip auto-update.")
        return 0

    if _bypassed():
        print("[architecture-gate] ARCHITECTURE_GATE_BYPASS=1 — skipped.")
        return 0

    staged = _staged_files()

    if not _any_watched_changed(staged):
        # No core files changed — nothing to update
        return 0

    print("[architecture-gate] Detected core/ changes — auto-updating docs...")

    rc = _run_doc_generation()
    if rc != 0:
        print("[architecture-gate] GATE CLOSED — doc generation failed. Fix and retry.")
        print("[architecture-gate] Set ARCHITECTURE_GATE_BYPASS=1 to skip.")
        return 1

    rc = _stage_docs()
    if rc != 0:
        print("[architecture-gate] WARNING — docs generated but staging failed. Proceeding.")
        return 0  # Don't block commit just because staging failed

    print("[architecture-gate] Docs auto-updated and staged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
