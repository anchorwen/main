"""Shared training utilities — time helpers, git metadata, and other small
functions that were previously duplicated across 17+ training scripts.

These are extracted here as the single source of truth. Every training script
should import from this module instead of defining its own copies.
"""

from __future__ import annotations

import math
import subprocess
from datetime import UTC, datetime


def spearman_rho(x, y) -> float:
    """Spearman rank correlation (numpy-only, with scipy fast path).

    Phase 3 / M2 (FIX-20260803-004): single shared implementation used by
    ``compute_financial_metrics`` (train.py), ``oos_blind_test.py``, and any
    Expected-R two-tower evaluation.  Returns 0.0 when the sample is too small
    or degenerate (constant input) — a degenerate model cannot pass a rho gate.
    """
    import numpy as np

    x = np.asarray(x, dtype=np.float64).ravel()
    y = np.asarray(y, dtype=np.float64).ravel()
    n = len(x)
    if n < 3 or np.all(x == x[0]) or np.all(y == y[0]):
        return 0.0
    try:
        from scipy.stats import spearmanr

        rho, _ = spearmanr(x, y)
        return float(rho) if math.isfinite(float(rho)) else 0.0
    except ImportError:
        pass

    # Manual rank correlation (average-rank for ties).
    def _rank(a) -> np.ndarray:
        order = a.argsort(kind="stable")
        ranks = np.empty(n, dtype=np.float64)
        ranks[order] = np.arange(n, dtype=np.float64)
        i = 0
        while i < n:
            j = i
            while j < n and a[order[j]] == a[order[i]]:
                j += 1
            if j > i + 1:
                avg = float((i + j - 1) / 2.0)
                for k in range(i, j):
                    ranks[order[k]] = avg
            i = j
        return ranks

    rx = _rank(x) - (n - 1) / 2.0
    ry = _rank(y) - (n - 1) / 2.0
    denom = math.sqrt(float(np.sum(rx * rx)) * float(np.sum(ry * ry)))
    if denom == 0.0:
        return 0.0
    return float(np.sum(rx * ry) / denom)


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
