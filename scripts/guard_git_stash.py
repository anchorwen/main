#!/usr/bin/env python3
"""Pre-stash safety guard — prevents git stash corruption from live process file locks.

FIX-20260619-028: Git stash fails when ``data_btc/ledger_events.jsonl`` (or any
runtime JSONL/log file) is held open by a live trading process.  The stash
creates a temporary checkout that needs to unlink the file, but Windows refuses
to unlink files with active handles.

Usage:
    python scripts/guard_git_stash.py          # check only (exit 0=clean, 1=risk)
    python scripts/guard_git_stash.py --fix    # also print workaround commands
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Directories containing runtime files that may be locked by live processes
RUNTIME_DIRS: list[str] = [
    "data_btc",
    "data",
]

# File patterns likely to be held open
LOCKABLE_PATTERNS: list[str] = [
    "*.jsonl",
    "*.lock",
    "*.json",
]


def find_lockable_files(base_dir: Path) -> list[Path]:
    """Find runtime files that might be locked by live processes."""
    found: list[Path] = []
    for d in RUNTIME_DIRS:
        runtime_dir = base_dir / d
        if not runtime_dir.exists():
            continue
        for pattern in LOCKABLE_PATTERNS:
            for fpath in runtime_dir.rglob(pattern):
                if fpath.is_file():
                    found.append(fpath)
    return sorted(found)


def check_file_locked(fpath: Path) -> bool:
    """Check if a file is likely held open by another process.

    On Windows, we try to open the file with exclusive access.
    If OSError is raised, the file is locked.
    """
    try:
        # Attempt exclusive read — fails if another process has it open
        with open(fpath, "rb", buffering=0) as f:
            # Try to acquire an exclusive lock (non-blocking test)
            try:
                msvcrt = __import__("msvcrt")
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except (ImportError, OSError):
                pass
        return False
    except OSError:
        return True


def main() -> int:
    base = Path.cwd()
    lockable = find_lockable_files(base)

    if not lockable:
        print("[guard_git_stash] No runtime files found — safe to stash.")
        return 0

    locked: list[Path] = []
    for fpath in lockable:
        try:
            if check_file_locked(fpath):
                locked.append(fpath)
        except Exception:  # BLE001:REVIEWED — diagnostic tool, never crash
            pass

    if not locked:
        # Try a quick write test to catch files that bypass the lock check
        for fpath in lockable[:5]:  # Sample check
            try:
                # Check modification time — if recent (<60s), likely in use
                mtime = os.path.getmtime(str(fpath))
                import time
                if time.time() - mtime < 60:
                    locked.append(fpath)
            except OSError:
                locked.append(fpath)

    if locked:
        print(f"[guard_git_stash] ⚠️  {len(locked)} file(s) may be locked by live processes:")
        for fpath in locked:
            print(f"    {fpath.relative_to(base)}")
        print()
        print("[guard_git_stash] Workaround: commit before stash, or use --no-verify.")
        print("[guard_git_stash] Safe command: git commit --no-verify -m '...'")
        return 1

    print("[guard_git_stash] All runtime files accessible — safe to stash.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
