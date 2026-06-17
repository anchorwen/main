#!/usr/bin/env python3
"""Claude Code PostToolUse hook — runs mypy on edited files.

Configure in .claude/settings.local.json:
  "hooks": {
    "PostToolUse": {
      "command": "python scripts/hook_mypy_check.py",
      "timeout_ms": 30000
    }
  }

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

    failed = False  # IRON_LAW-13-S1: track failure state

    # Run mypy on the file
    try:
        result = subprocess.run(
            [sys.executable, "-m", "mypy", "--no-error-summary", str(full_path)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=30,
        )
        output = (result.stdout.strip() + result.stderr.strip()).strip()
        if output:
            errors = [l for l in output.split("\n") if ": error:" in l]
            if errors:
                print(f"[mypy] {file_path}: {len(errors)} error(s)")
                # Print just the error summary, not full output
                for e in errors[:5]:
                    print(f"  {e.strip()}")
                if len(errors) > 5:
                    print(f"  ... and {len(errors) - 5} more")
            else:
                print(f"[mypy] {file_path}: clean")
        # Load baseline to check if NEW errors were introduced
        baseline_path = ROOT / "mypy_baseline.json"
        if baseline_path.exists() and output:
            with open(baseline_path, encoding="utf-8") as f:
                baseline = json.load(f)
            current_errors = len([l for l in output.split("\n") if ": error:" in l])
            prev_errors = baseline.get(file_path, 0)
            if current_errors > prev_errors:
                new_count = current_errors - prev_errors
                print(
                    f"[mypy] BLOCKED: +{new_count} NEW error(s) in {file_path} "
                    f"(was {prev_errors}, now {current_errors})"
                )
                print("[mypy] Fix the new type errors before continuing.")
                failed = True
    except subprocess.TimeoutExpired:
        print(f"[mypy] {file_path}: timed out")
        failed = True
    except Exception as exc:  # noqa: BLE001
        print(f"[mypy] {file_path}: check failed ({exc})")

    return 1 if failed else 0  # IRON_LAW-13-S1: blocking on new errors


if __name__ == "__main__":
    sys.exit(main())
