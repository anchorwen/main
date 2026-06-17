#!/usr/bin/env python3
"""Claude Code PostToolUse hook — Iron Law #6 reminder for blueprint awareness.

Configure in .claude/settings.local.json as a second hook alongside hook_mypy_check.py.
This hook NEVER blocks — it only prints advisory reminders (always exit 0).

Environment variables provided by Claude Code:
  CLAUDE_TOOL_NAME  — e.g. "Edit", "Write"
  CLAUDE_TOOL_INPUT — JSON string with tool parameters
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    tool_name = os.environ.get("CLAUDE_TOOL_NAME", "")
    tool_input_raw = os.environ.get("CLAUDE_TOOL_INPUT", "")

    # Only run on Edit/Write tools
    if tool_name not in ("Edit", "Write"):
        return 0

    # Extract file path
    file_path = ""
    try:
        params = json.loads(tool_input_raw)
        file_path = params.get("file_path", "")
    except (json.JSONDecodeError, TypeError):
        return 0

    if not file_path:
        return 0

    full_path = ROOT / file_path
    if not full_path.exists() or not file_path.endswith(".py"):
        return 0

    # ── IRON_LAW-13-S1: propagate actual exit code ──
    # Advisory-only for --pre-check mode, but surface failures so Agent
    # sees the warning immediately after Edit/Write.
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "check_blueprint_compliance.py"),
                "--pre-check",
                file_path,
            ],
            cwd=str(ROOT),
            timeout=15,
        )
        if result.returncode != 0:
            print(
                f"[blueprint] WARNING: consistency check failed for {file_path} "
                f"(exit {result.returncode})"
            )
            print("[blueprint] Review and update blueprints/modules/<module>.md before commit.")
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"[blueprint] {file_path}: check timed out")
        return 1
    except Exception:  # noqa: BLE001
        return 0  # silent on infrastructure failures


if __name__ == "__main__":
    sys.exit(main())
