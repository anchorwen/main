#!/usr/bin/env python
"""Journal freeze gate — blocks commits to core journal interfaces until
test coverage reaches 80% on the four core write paths.

Per the 2026-06-04 architecture amendment, the journal module is under a
code freeze.  This gate is installed as a pre-commit hook in
``.pre-commit-config.yaml``.

Coverage is read from ``coverage.json`` (pytest-cov output).  Coverage report
paths are platform-native (backslashes on Windows), so ``_is_protected``
normalizes every path to forward slashes before matching the canonical
``core/ledger/`` prefixes (FIX-20260819-007: Windows path-separator
false-block).  There is no env-var bypass; documented emergency exceptions use
``--no-verify`` with a stated reason (Iron Law #0-bis).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# ── Project root (for coverage.json lookup) ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Protected paths (the four core write interfaces) ──
_PROTECTED_PREFIXES: list[str] = [
    "core/contracts/label_contract.py",
    "core/ledger/",
]


def _get_staged_files() -> list[str]:
    """Return list of staged file paths from git diff --cached."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _is_protected(filepath: str) -> bool:
    """Check whether *filepath* falls under any protected prefix.

    Coverage reports use platform-native separators (``core\\ledger\\...`` on
    Windows); git staged paths are always forward-slash.  Normalize to forward
    slashes so both inputs match the canonical ``core/ledger/`` prefixes
    (FIX-20260819-007 — backslash paths previously never matched → 0.0%).
    """
    normalized = filepath.replace("\\", "/")
    for prefix in _PROTECTED_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix):
            return True
    return False


def _read_coverage_pct() -> float:
    """Read test coverage from ``coverage.json`` for the protected paths.

    Parses the JSON report produced by ``pytest --cov --cov-report=json``
    (configured in ``pyproject.toml``).  Computes a weighted average of
    line + branch coverage across all files under the protected prefixes.

    Returns 0.0 if ``coverage.json`` does not exist or contains no data
    for the protected paths.
    """
    import json as _json

    coverage_path = _PROJECT_ROOT / "coverage.json"
    if not coverage_path.exists():
        return 0.0

    try:
        data = _json.loads(coverage_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return 0.0

    files = data.get("files", {})
    if not files:
        return 0.0

    covered_lines = 0
    total_lines = 0
    covered_branches = 0
    total_branches = 0

    for fpath, finfo in files.items():
        if not _is_protected(fpath):
            continue
        summary = finfo.get("summary", {})
        covered_lines += summary.get("covered_lines", 0)
        total_lines += summary.get("num_statements", 0)
        # Branch coverage (available when pytest --cov-branch is used)
        bc = summary.get("covered_branches")
        tb = summary.get("num_branches")
        if bc is not None and tb is not None and tb > 0:
            covered_branches += bc
            total_branches += tb

    if total_lines == 0:
        return 0.0

    line_pct = (covered_lines / total_lines) * 100.0
    branch_pct = (covered_branches / total_branches) * 100.0 if total_branches > 0 else line_pct
    # Weighted: 50% line + 50% branch
    return round((line_pct + branch_pct) / 2.0, 1)


def main() -> int:
    staged = _get_staged_files()
    violations = [f for f in staged if _is_protected(f)]

    if not violations:
        return 0

    # ── Coverage-based gate (real coverage read) ──
    coverage_pct = _read_coverage_pct()
    if coverage_pct >= 80.0:
        print(f"✅ [铁律] 账本测试覆盖率已达标 ({coverage_pct:.1f}%), " "允许修改核心账本接口。")
        return 0

    # ── Block ──
    print("=" * 72)
    print("[IRON LAW] Journal interface is under architecture freeze!")
    print()
    print(f"   Core write-path test coverage: {coverage_pct:.1f}% (target: >= 80.0%)")
    print()
    print("   Protected files in this commit:")
    for vf in violations:
        print(f"     -> {vf}")
    print()
    print("   Modification of core journal code is BLOCKED until coverage >= 80%.")
    print("   Raise coverage on the protected paths; documented emergency")
    print("   exceptions use --no-verify (Iron Law #0-bis) with a stated reason.")
    print("=" * 72)
    return 1


if __name__ == "__main__":
    sys.exit(main())
