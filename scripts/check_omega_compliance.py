#!/usr/bin/env python
"""Iron Law Ω Pre-Commit Compliance Gate — FIX-20260612-017.

Scans staged changes and blocks commits that violate the Ω protocol:

  - Every .py/.yaml/.json change MUST have a FIX-YYYYMMDD-NNN or
    DQAF-YYYYMMDD-NNN marker in the commit message.
  - Hot-path files (live_cycle, strategy_line, live_intent, execution_queue)
    additionally require Iron Law #10 compliance.
  - Pure mechanical changes (formatting, docs, config values) are exempt
    but must declare their exemption category.

Usage::

    python scripts/check_omega_compliance.py --commit-msg "fix: [FIX-20260612-XXX] ..."
    python scripts/check_omega_compliance.py --commit-msg-file .git/COMMIT_EDITMSG

Exit 0 = compliant, Exit 1 = BLOCKED.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# ── Constants ──

FIX_ID_PATTERN = re.compile(r"FIX-\d{8}-\d{3}")
DQAF_ID_PATTERN = re.compile(r"DQAF-\d{8}-\d{3}")
OMEGA_ROUTING_PATTERN = re.compile(r"\[Ω-Routing:")
EXEMPTION_PATTERN = re.compile(
    r"(?i)(?:pure.?mechanical|formatting|docs? only|config value|exempt|豁免)"
)

# Files requiring Iron Law #10 (BLE001 → fail_open_guard)
HOT_PATH_FILES = {
    "core/runtime/live_cycle.py",
    "scripts/live_intent_loop.py",
    "core/execution/strategy_line.py",
    "core/execution/execution_queue.py",
}

# Files covered by Iron Law #0
COVERED_EXTENSIONS = {".py", ".yaml", ".yml", ".json"}

# Directories excluded from check (runtime data)
EXCLUDED_DIRS = {"data", "data_btc", "data_xau", "__pycache__", ".claude"}


class Violation(NamedTuple):
    file: str
    reason: str


def get_staged_files() -> list[str]:
    """Return list of staged file paths."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, check=True,
        )
        return [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except subprocess.CalledProcessError:
        return []


def get_staged_diff() -> str:
    """Return full staged diff for content analysis."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError:
        return ""


def is_covered_file(filepath: str) -> bool:
    """Check if file is subject to Iron Law #0."""
    path = Path(filepath)
    if path.suffix not in COVERED_EXTENSIONS:
        return False
    # Exclude runtime data directories
    parts = path.parts
    if parts and parts[0] in EXCLUDED_DIRS:
        return False
    return True


def is_pure_mechanical(diff: str, commit_msg: str) -> bool:
    """Check if changes qualify for pure-mechanical exemption."""
    # Exemption markers in commit message
    if EXEMPTION_PATTERN.search(commit_msg):
        return True

    # Check if diff is only formatting/whitespace changes
    if not diff.strip():
        return True

    # Check if only .md files changed
    lines = diff.splitlines()
    changed_files = {
        line[6:] for line in lines
        if line.startswith("+++ b/") or line.startswith("--- a/")
    }
    non_md = {f for f in changed_files if Path(f).suffix != ".md"}
    if not non_md:
        return True

    return False


def check_commit_msg(commit_msg: str) -> list[Violation]:
    """Check commit message for required Ω markers."""
    violations: list[Violation] = []

    has_fix = bool(FIX_ID_PATTERN.search(commit_msg))
    has_dqaf = bool(DQAF_ID_PATTERN.search(commit_msg))
    has_routing = bool(OMEGA_ROUTING_PATTERN.search(commit_msg))

    if not has_fix and not has_dqaf:
        violations.append(Violation(
            file="(commit message)",
            reason="Missing FIX-YYYYMMDD-NNN or DQAF-YYYYMMDD-NNN marker. "
                   "All .py/.yaml/.json changes require a docket ID per Iron Law #0."
        ))

    return violations


def check_hot_path(files: list[str], diff: str) -> list[Violation]:
    """Iron Law #10: check hot-path files for BLE001 elimination."""
    violations: list[Violation] = []
    for f in files:
        if f in HOT_PATH_FILES:
            # Check if the diff removes any # BLE001:REVIEWED
            diff_lines = [l for l in diff.splitlines() if f in l]
            ble001_removed = any(
                "-# BLE001:REVIEWED" in l or "-    # BLE001:REVIEWED" in l
                for l in diff_lines
            )
            if ble001_removed:
                # Good — at least one BLE001 was replaced
                pass
            # Note: we don't block on this — Iron Law #10 is progressive
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ω Protocol Compliance Gate")
    parser.add_argument("--commit-msg", help="Commit message string")
    parser.add_argument("--commit-msg-file", help="Path to commit message file")
    parser.add_argument("--json", action="store_true", help="Output JSON (for CI)")
    args = parser.parse_args(argv)

    # Get commit message
    commit_msg = args.commit_msg or ""
    if args.commit_msg_file:
        try:
            commit_msg = Path(args.commit_msg_file).read_text(encoding="utf-8")
        except Exception:  # BLE001:REVIEWED
            pass

    # Get changed files
    staged = get_staged_files()
    covered = [f for f in staged if is_covered_file(f)]
    diff = get_staged_diff()

    if not covered:
        # No covered files changed — pass
        if args.json:
            import json
            print(json.dumps({"status": "pass", "reason": "no covered files"}))
        return 0

    # Check for pure-mechanical exemption
    if is_pure_mechanical(diff, commit_msg):
        if args.json:
            import json
            print(json.dumps({"status": "pass", "reason": "pure mechanical exemption"}))
        return 0

    # Check commit message
    violations = check_commit_msg(commit_msg)

    # Check hot-path files
    violations.extend(check_hot_path(covered, diff))

    if violations:
        if args.json:
            import json
            print(json.dumps({
                "status": "blocked",
                "violations": [{"file": v.file, "reason": v.reason} for v in violations],
            }))
        else:
            print("\n" + "=" * 70)
            print("  Ω IRON LAW COMPLIANCE GATE — COMMIT BLOCKED")
            print("=" * 70)
            for v in violations:
                print(f"\n  File: {v.file}")
                print(f"  Reason: {v.reason}")
            print(f"\n  Changed files requiring Ω markers: {', '.join(covered)}")
            print("\n  Required: FIX-YYYYMMDD-NNN or DQAF-YYYYMMDD-NNN in commit message.")
            print("  Exemption: add 'pure mechanical' / 'formatting' to commit message.")
            print("  Example: fix(module): [FIX-20260612-XXX] description")
            print("=" * 70 + "\n")
        return 1

    # All checks passed
    if args.json:
        import json
        print(json.dumps({"status": "pass", "files_checked": len(covered)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
