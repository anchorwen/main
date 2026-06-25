"""Shared constants for Omega Protocol gate scripts.

Single source of truth for constants shared between:
  - ``scripts/omega_gate.py`` (commit-msg hook, 14 sequential checks)
  - ``scripts/validate_commit_msg.py`` (pre-flight validator, all-at-once)

Previously duplicated across both files, causing drift bugs
(e.g., Scene H regex parity fix needed 2 separate commits on 2026-06-22).
"""

import re

# ---------------------------------------------------------------------------
# Ω-Routing signature detection
# ---------------------------------------------------------------------------

# Matches: [Ω-Routing: Scene X → #N → ...] or [O-Routing: Scene X -> #N]
SIGNATURE_RE = re.compile(
    r"\[[OΩ].*Routing:\s*Scene\s+[A-H]\s*(?:→|->)\s*.*\]",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Scene → Required Iron Law references
# ---------------------------------------------------------------------------

SCENE_REQUIRES_IRON_LAW: dict[str, list[str]] = {
    "A": ["#9", "#8", "#12"],
    "B": ["#0", "#6", "#5"],
    "C": ["#0", "#8"],
    "E": ["#6", "#0"],
    "F": [],  # Scene F: pure mechanical — no iron law references required
}

# ---------------------------------------------------------------------------
# Hot-path files requiring #10 (Iron Law #10: fail-open audit)
# ---------------------------------------------------------------------------

HOT_PATH_FILES = {
    "core/runtime/live_cycle.py",
    "core/execution/strategy_line.py",
    "core/execution/execution_queue.py",
    "scripts/live_intent_loop.py",
}

HOT_PATH_IRON_LAW = "#10"

# ---------------------------------------------------------------------------
# Scene F: Pure Mechanical Operations
# ---------------------------------------------------------------------------
# Scope: Non-semantic changes — config adjustments, lint suppressions,
#   comment corrections, formatting, trivial renames, pure test modifications.
#   Must NOT involve logic changes, behavioral modifications,
#   or new code paths.
# Exempt from: FIX/DQAF ID, 4D Quality Gate, Pre-Edit Checklist,
#   Closing Checklist, Pattern Search, Root Cause Layer.
# Still REQUIRED: [Ω-Routing: Scene F → #N] signature.
#
# Plan A (2026-06-25): Pure test-only commits (all covered staged files
#   under tests/) are auto-detected as Scene F equivalent. All hard
#   quality gates (ruff, mypy, pytest, AST scanner) remain active.

SCENE_F_EXEMPTION_SCOPE = (
    "config adjustments, lint suppressions, comment corrections, "
    "formatting, trivial renames, pure test modifications — "
    "no logic or behavioral changes"
)

# ---------------------------------------------------------------------------
# Test-only commit detection (Plan A: test exemption)
# ---------------------------------------------------------------------------

# Root-level files that are test infrastructure (not production code).
# Changes to these files alone qualify as test-only → Scene F exempt.
TEST_INFRA_ROOT_FILES: set[str] = {
    "conftest.py",  # pytest root configuration (fixtures, markers, plugins)
}


def is_test_only_commit(staged_files: set[str]) -> bool:
    """Return True if ALL covered staged files are test infrastructure.

    Test-only means every covered file (.py/.yaml/.yml/.json, excluding
    data/ directories) is either under ``tests/`` or listed in
    ``TEST_INFRA_ROOT_FILES``.

    Path separators are normalized to forward slashes for cross-platform
    compatibility (Windows ``\\`` vs POSIX ``/``).

    Returns ``False`` for empty covered sets (no relevant files staged).
    """
    covered = {
        f.replace("\\", "/")
        for f in staged_files
        if f.endswith((".py", ".yaml", ".yml", ".json"))
        and not any(
            f.replace("\\", "/").startswith(d)
            for d in ("data/", "data_btc/", "__pycache__/", ".claude/")
        )
    }
    if not covered:
        return False
    return all(f.startswith("tests/") or f in TEST_INFRA_ROOT_FILES for f in covered)


# ---------------------------------------------------------------------------
# Exemption keywords (commit body contains any of these → is_exempt = True)
# ---------------------------------------------------------------------------

EXEMPTION_PATTERN = re.compile(
    r"(?i)(?:pure.?mechanical|formatting|docs?.only|config.value|exempt|豁免|no.dqaf.needed)"
)
