#!/usr/bin/env python
"""Omega Protocol commit-message gate — physically rejects commits without Ω routing.

FIX-20260612-002: Phase 1 of Systemic Operating System.
Scans commit message for [Ω-Routing: Scene X -> ...] signature.
If hot-path files are changed, requires #10 in the signature.
Exit 1 = commit blocked.

Usage (via pre-commit hook)::

    - repo: local
      hooks:
        - id: omega-routing
          name: "[Ω] Routing signature required"
          entry: python scripts/omega_gate.py
          language: system
          stages: [commit-msg]
          pass_filenames: false
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── Hot-path files (require #10 in signature) ──
HOT_PATH_FILES = {
    "core/runtime/live_cycle.py",
    "core/execution/strategy_line.py",
    "core/execution/execution_queue.py",
    "scripts/live_intent_loop.py",
}

# ── Signature patterns ──
SIGNATURE_RE = re.compile(
    r"\[[OΩ].*Routing:\s*Scene\s+[A-G]\s*(?:→|->)\s*.*\]",
    re.IGNORECASE,
)
# Scene A (Bug fix) requires DQAF: #9
# Scene B (Code) requires: #0 → #6 → #5
# Scene C (Config) requires: #0 → #8
# etc.

SCENE_REQUIRES_IRON_LAW: dict[str, list[str]] = {
    "A": ["#9", "#8", "#12"],
    "B": ["#0", "#6", "#5"],
    "C": ["#0", "#8"],
    "E": ["#6", "#0"],
}

HOT_PATH_IRON_LAW = "#10"


def get_commit_msg() -> str:
    """Read commit message from the file passed by pre-commit."""
    commit_msg_file = sys.argv[1] if len(sys.argv) > 1 else ".git/COMMIT_EDITMSG"
    path = Path(commit_msg_file)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def get_staged_files() -> set[str]:
    """Get set of staged Python files."""
    result = subprocess.run(
        ["git", "diff", "--name-only", "--cached"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=10,
    )
    if result.returncode != 0:
        return set()
    return {
        line.strip()
        for line in result.stdout.strip().split("\n")
        if line.strip().endswith(".py")
    }


def main() -> int:
    commit_msg = get_commit_msg()
    staged = get_staged_files()

    # ── Check 1: Signature required ──
    sig_match = SIGNATURE_RE.search(commit_msg)
    if not sig_match:
        print("=" * 60)
        print("[Ω] COMMIT REJECTED: No Ω-Routing signature found.")
        print("[Ω] Your commit message MUST include:")
        print("[Ω]   [Ω-Routing: Scene X -> #N -> #M -> ...]")
        print("[Ω] See CLAUDE.md Omega Protocol for scene codes.")
        print("=" * 60)
        return 1

    signature = sig_match.group(0)
    print(f"[Ω] Signature found: {signature}")

    # ── Check 2: Scene requires minimal iron law references ──
    scene_match = re.search(r"Scene\s+([A-G])", signature)
    if scene_match:
        scene = scene_match.group(1).upper()
        required = SCENE_REQUIRES_IRON_LAW.get(scene, [])
        missing = [law for law in required if law not in signature]
        if missing:
            print(f"[Ω] WARNING: Scene {scene} should include {missing} in signature.")
            print("[Ω] (Non-blocking — amend if this is a full routing chain.)")
    else:
        print("[Ω] WARNING: Could not parse scene from signature.")

    # ── Check 3: Hot-path files require #10 ──
    hot_path_staged = staged & HOT_PATH_FILES
    if hot_path_staged and HOT_PATH_IRON_LAW not in signature:
        print("=" * 60)
        print(f"[Ω] COMMIT REJECTED: Hot-path files modified without #10:")
        for f in sorted(hot_path_staged):
            print(f"[Ω]   {f}")
        print(f"[Ω] Signature MUST include {HOT_PATH_IRON_LAW} when touching hot-path files.")
        print(f"[Ω] Current signature: {signature}")
        print("=" * 60)
        return 1

    if hot_path_staged:
        print(f"[Ω] Hot-path check PASSED: {HOT_PATH_IRON_LAW} in signature for {len(hot_path_staged)} file(s).")

    print("[Ω] Gate PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
