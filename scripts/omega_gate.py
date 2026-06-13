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

    # ── Check 2: Scene requires minimal iron law references (BLOCKING) ──
    # FIX-20260613-061: Upgraded from WARNING to hard fail.
    # The Ω chain is not optional — missing steps = incomplete diagnosis.
    scene_match = re.search(r"Scene\s+([A-G])", signature)
    if scene_match:
        scene = scene_match.group(1).upper()
        required = SCENE_REQUIRES_IRON_LAW.get(scene, [])
        missing = [law for law in required if law not in signature]
        if missing:
            print("=" * 60)
            print(f"[Ω] COMMIT REJECTED: Scene {scene} requires {missing} in signature.")
            print(f"[Ω] Current signature: {signature}")
            print(f"[Ω] Required chain for Scene {scene}: {' -> '.join(required)}")
            print("[Ω] See CLAUDE.md for the full execution protocol.")
            print("=" * 60)
            return 1
    else:
        print("[Ω] WARNING: Could not parse scene from signature.")

    # ── Check 3: Hot-path files require #10 ──
    hot_path_staged = staged & HOT_PATH_FILES
    if hot_path_staged and HOT_PATH_IRON_LAW not in signature:
        print("=" * 60)
        print("[Ω] COMMIT REJECTED: Hot-path files modified without #10:")
        for f in sorted(hot_path_staged):
            print(f"[Ω]   {f}")
        print(f"[Ω] Signature MUST include {HOT_PATH_IRON_LAW} when touching hot-path files.")
        print(f"[Ω] Current signature: {signature}")
        print("=" * 60)
        return 1

    if hot_path_staged:
        print(f"[Ω] Hot-path check PASSED: {HOT_PATH_IRON_LAW} in signature for {len(hot_path_staged)} file(s).")

    # ── Check 4: FIX/DQAF ID required for .py/.yaml/.json changes ──
    # Iron Law #0: every non-exempt change to covered files must carry a docket ID.
    covered_staged = {
        f for f in staged
        if f.endswith((".py", ".yaml", ".yml", ".json"))
        and not any(f.startswith(d) for d in ("data/", "data_btc/", "__pycache__/", ".claude/"))
    }
    has_fix = bool(re.search(r"FIX-\d{8}-\d{3}", commit_msg))
    has_dqaf = bool(re.search(r"DQAF-\d{8}-\d{3}", commit_msg))
    is_exempt = bool(re.search(
        r"(?i)(?:pure.?mechanical|formatting|docs?.only|config.value|exempt|豁免|no.dqaf.needed)",
        commit_msg,
    ))

    if covered_staged and not has_fix and not has_dqaf and not is_exempt:
        print("=" * 60)
        print("[Ω] COMMIT REJECTED: Covered files changed without FIX/DQAF ID.")
        print("[Ω] Changed files requiring docket ID:")
        for f in sorted(covered_staged):
            print(f"[Ω]   {f}")
        print("[Ω] Commit message MUST include FIX-YYYYMMDD-NNN or DQAF-YYYYMMDD-NNN.")
        print("[Ω] Exemptions: add 'pure mechanical'/'formatting'/'config value' to msg.")
        print("=" * 60)
        return 1

    if covered_staged:
        docket_type = "FIX" if has_fix else ("DQAF" if has_dqaf else "exempt")
        print(f"[Ω] Docket check PASSED: {docket_type} ID for {len(covered_staged)} covered file(s).")

    # ── Check 5: FIX_REGISTRY cross-reference (FIX-20260613-061) ────────
    # When a FIX ID is claimed in the commit, it MUST exist in the registry.
    # This closes the loop: diagnosis → fix → registration → commit.
    if has_fix:
        fix_ids = set(re.findall(r"FIX-\d{8}-\d{3}", commit_msg))
        registry_path = ROOT / "blueprints" / "system" / "FIX_REGISTRY.md"
        if registry_path.exists():
            registry_text = registry_path.read_text(encoding="utf-8")
            missing_in_registry = [
                fid for fid in fix_ids if fid not in registry_text
            ]
            if missing_in_registry:
                print("=" * 60)
                print("[Ω] COMMIT REJECTED: FIX ID(s) not registered in FIX_REGISTRY.md:")
                for fid in missing_in_registry:
                    print(f"[Ω]   {fid}")
                print(f"[Ω] Update {registry_path} before committing.")
                print("[Ω] Run: python scripts/register_fix.py --help")
                print("=" * 60)
                return 1
            print(f"[Ω] Registry check PASSED: {len(fix_ids)} FIX ID(s) found in registry.")
        else:
            print("[Ω] WARNING: FIX_REGISTRY.md not found — skipping cross-reference.")

    print("[Ω] Gate PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
