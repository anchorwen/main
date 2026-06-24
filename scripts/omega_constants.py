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
#   comment corrections, formatting, trivial renames.
#   Must NOT involve logic changes, behavioral modifications,
#   or new code paths.
# Exempt from: FIX/DQAF ID, 4D Quality Gate, Pre-Edit Checklist,
#   Closing Checklist, Pattern Search, Root Cause Layer.
# Still REQUIRED: [Ω-Routing: Scene F → #N] signature.

SCENE_F_EXEMPTION_SCOPE = (
    "config adjustments, lint suppressions, comment corrections, "
    "formatting, trivial renames — no logic or behavioral changes"
)

# ---------------------------------------------------------------------------
# Exemption keywords (commit body contains any of these → is_exempt = True)
# ---------------------------------------------------------------------------

EXEMPTION_PATTERN = re.compile(
    r"(?i)(?:pure.?mechanical|formatting|docs?.only|config.value|exempt|豁免|no.dqaf.needed)"
)
