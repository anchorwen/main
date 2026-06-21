"""Clean up old/ended Claude Code conversation transcripts.

Iron Law #11 compliant: all statistics from script stdout only.

Usage:
    python scripts/cleanup_claude_transcripts.py              # dry-run
    python scripts/cleanup_claude_transcripts.py --execute    # actually delete
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(os.path.expandvars(r"%USERPROFILE%")) / ".claude" / "projects" / "d--future"
SAFE_AGE_HOURS = 6  # only delete sessions idle for >6 hours
KEEP_RECENT = 2      # keep the 2 most recently modified transcripts (safety margin)


def _get_session_age_hours(jsonl_path: Path) -> float:
    """Return hours since the transcript was last modified."""
    mtime = jsonl_path.stat().st_mtime
    age_seconds = datetime.now().timestamp() - mtime
    return age_seconds / 3600


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up old Claude Code transcripts")
    parser.add_argument("--execute", action="store_true", help="Actually delete (default: dry-run)")
    parser.add_argument("--max-age-hours", type=float, default=SAFE_AGE_HOURS,
                        help=f"Only delete sessions idle longer than this (default: {SAFE_AGE_HOURS}h)")
    parser.add_argument("--keep", type=int, default=KEEP_RECENT,
                        help=f"Keep N most recent transcripts (default: {KEEP_RECENT})")
    args = parser.parse_args()

    if not PROJECT_DIR.exists():
        print(f"Project dir not found: {PROJECT_DIR}")
        sys.exit(1)

    # Discover transcript files
    transcripts: list[tuple[Path, float, int]] = []  # (path, age_hours, size_bytes)
    for f in PROJECT_DIR.iterdir():
        if f.suffix == ".jsonl":
            size = f.stat().st_size
            age = _get_session_age_hours(f)
            transcripts.append((f, age, size))

    if not transcripts:
        print("No transcript files found.")
        return

    # Sort by modification time (newest first)
    transcripts.sort(key=lambda x: x[1])  # ascending age

    total_saved = 0
    kept = 0
    deleted = 0

    print(f"{'AGE (h)':>8}  {'SIZE (MB)':>10}  {'ACTION':>8}  FILE")
    print("-" * 80)

    for i, (path, age, size) in enumerate(transcripts):
        size_mb = size / (1024 * 1024)
        # Keep the KEEP_RECENT most recent
        if i < args.keep:
            print(f"{age:8.1f}  {size_mb:10.1f}  {'KEEP':>8}  {path.name}")
            kept += 1
            continue

        # Delete if old enough
        if age > args.max_age_hours:
            action = "DELETE"
            if args.execute:
                # Also delete corresponding session directory
                session_dir = path.with_suffix("")
                if session_dir.exists() and session_dir.is_dir():
                    import shutil
                    shutil.rmtree(session_dir)
                    print(f"{age:8.1f}  {size_mb:10.1f}  {'DEL DIR':>8}  {session_dir.name}/")
                path.unlink()
            total_saved += size
            deleted += 1
            print(f"{age:8.1f}  {size_mb:10.1f}  {action:>8}  {path.name}")
        else:
            print(f"{age:8.1f}  {size_mb:10.1f}  {'KEEP':>8}  {path.name} (active, {age:.1f}h < {args.max_age_hours}h)")
            kept += 1

    print("-" * 80)
    if args.execute:
        print(f"DONE: deleted {deleted} transcripts, saved {total_saved / (1024*1024):.1f} MB, kept {kept}")
    else:
        print(f"DRY-RUN: would delete {deleted} transcripts, save {total_saved / (1024*1024):.1f} MB, keep {kept}")
        print("Run with --execute to actually delete.")


if __name__ == "__main__":
    main()
