"""One-shot migration: replace fail_open_guard/log_and_continue with try/except in live_cycle.py.

Reads live_cycle.py, replaces every `with fail_open_guard(...):` and
`with log_and_continue(...):` context manager with equivalent try/except
using the UGR-A09 specific exception tuple.

Usage:
    python scripts/migrate_fog_live_cycle.py
    python -m ruff check core/runtime/live_cycle.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TARGET = Path("core/runtime/live_cycle.py")
EXCEPT_TUPLE = "RuntimeError, ValueError, KeyError, TypeError, OSError"

# Patterns to match:
#   with fail_open_guard("label"):
#   with fail_open_guard(\n    "label"\n):
#   with log_and_continue(component="label"):
#   with log_and_continue("OUBrainExit"):  (for compat with old call sites)
FOG_RE = re.compile(
    r'(\s*)(with\s+(?:fail_open_guard|log_and_continue)\s*\(\s*(?:component\s*=\s*)?["\'][^"\']+["\']\s*\)\s*:\s*\n)',
)


def find_block_end(lines: list[str], start_idx: int) -> int:
    """Find the last line of an indented block starting at start_idx.

    The block starts with a `with` line at some indentation level N.
    All subsequent lines indented > N belong to the block.
    Empty lines and comment-only lines are included if they follow block
    indentation rules (comments at same or deeper level).

    Returns the index of the last line that is part of the block.
    """
    indent = len(lines[start_idx]) - len(lines[start_idx].lstrip())
    end = start_idx
    for i in range(start_idx + 1, len(lines)):
        stripped = lines[i].rstrip("\n\r")
        if stripped == "" or stripped.lstrip().startswith("#"):
            # Empty/comment line: could be a separator; check if next
            # line continues at proper indent
            continue
        line_indent = len(lines[i]) - len(lines[i].lstrip())
        if line_indent <= indent:
            break
        end = i
    return end


def transform_file(path: Path) -> tuple[int, list[str]]:
    """Transform the file, returning (change_count, new_lines)."""
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    changes = 0
    i = 0
    while i < len(lines):
        m = FOG_RE.match(lines[i])
        if m is None:
            i += 1
            continue

        indent = m.group(1)
        # Find the end of the with-block
        block_end = find_block_end(lines, i)

        # Collect the body lines (all lines after the with statement)
        body_start = i + 1
        body_lines = lines[body_start : block_end + 1]

        # Build replacement
        replacement = [f"{indent}try:\n"]
        # Body stays at same indentation — 'with' and 'try' are at the same level
        for bl in body_lines:
            replacement.append(bl)
        replacement.append(f"{indent}except ({EXCEPT_TUPLE}):\n")
        replacement.append(f"{indent}    pass\n")

        # Replace: remove original lines and insert replacement
        lines[i : block_end + 1] = replacement
        changes += 1
        # Move past the replacement to avoid re-matching
        i += len(replacement)

    return changes, lines


def main() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found", file=sys.stderr)
        return 1

    changes, new_lines = transform_file(TARGET)

    if changes == 0:
        print("No changes made — file may already be migrated.")
        return 0

    # Write back
    TARGET.write_text("".join(new_lines), encoding="utf-8")
    print(f"Transformed {changes} sites in {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
