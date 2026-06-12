"""Shared training utilities — time helpers, git metadata, and other small
functions that were previously duplicated across 17+ training scripts.

These are extracted here as the single source of truth. Every training script
should import from this module instead of defining its own copies.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime


def utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix.

    Compact variant used consistently across the training codebase.
    Formerly duplicated as ``_utc_now_iso()`` in 17 separate scripts.
    """
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def get_git_commit_hash(repo_root: str = "") -> str:
    """Return the current HEAD commit hash (short SHA-8), or 'unknown'.

    Re-exported here for convenience so training scripts have a single import.
    The canonical implementation lives in ``core.training.brain_config``.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            capture_output=True,
            text=True,
            cwd=repo_root or ".",
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"
