#!/usr/bin/env python3
"""Audit Claude memory files for staleness and completion status.

Detects three classes of maintenance debt:
  1. STALE — review date is past due (or no update in >N days)
  2. DONE_ACTIVE — file body contains completion markers but file is still in active memory
  3. UNTRACKED — file has no review_date or updated metadata

Usage:
    python scripts/audit_memory.py                  # audit only (report)
    python scripts/audit_memory.py --sweep           # move completed items to _archive/
    python scripts/audit_memory.py --stale 14        # flag items stale after 14 days (default: 30)
    python scripts/audit_memory.py --memory-dir ...  # override memory directory
    python scripts/audit_memory.py --verbose         # show per-file details
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# ── Windows encoding workaround ──────────────────────────────────────────────
# Ensure stdout can handle Unicode (emoji in memory file descriptions).
# On Windows, stdout defaults to the system code page (e.g. GBK) which
# cannot encode emoji. Reconfigure to UTF-8 with 'replace' fallback.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except OSError:
        pass

# ── Defaults ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = Path.home() / ".claude" / "projects" / "d--future" / "memory"
ARCHIVE_DIR_NAME = "_archive"

# Completion markers found in file body (not frontmatter)
COMPLETION_MARKERS = [
    r"✅.*COMPLETED",
    r"✅.*终审通过",
    r"✅.*已实施",
    r"✅.*CLOSED",
    r"status:\s*COMPLETED",
    r"🟢.*CLOSED",
]
COMPLETION_RE = re.compile("|".join(COMPLETION_MARKERS), re.IGNORECASE)

# Reference-only files (never stale)
REFERENCE_FILES = {
    "feedback_language.md",
    "feedback_mode_reminder.md",
    "mt5_reference.md",
    "MEMORY.md",
}
# Iron Law / protocol files (rarely change)
PROTOCOL_FILES = {
    "iron_law_13_auto_closing.md",
    "omega_protocol_iron_law.md",
}
# Tracking/roadmap files — active by nature, never archivable
TRACKING_FILES_PATTERNS = [
    r"(?i)roadmap",  # ROADMAP_2026_Q2, roadmap_*.md
    r"(?i)路线图",  # Chinese "roadmap"
]

# ── Parsing helpers ──────────────────────────────────────────────────────────


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Extract YAML frontmatter between --- delimiters. Returns empty dict on failure."""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    raw = m.group(1)
    result: dict[str, Any] = {}
    # Parse flat key: value pairs and nested metadata: block
    in_metadata = False
    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "metadata:":
            in_metadata = True
            continue
        # Nested metadata key: value
        if in_metadata:
            if ":" in stripped and not stripped.startswith(" "):
                # Top-level key after metadata block → exit metadata
                in_metadata = False
            else:
                kv = stripped.split(":", 1)
                if len(kv) == 2:
                    result[kv[0].strip()] = kv[1].strip().strip('"').strip("'")
                continue
        kv = stripped.split(":", 1)
        if len(kv) == 2:
            result[kv[0].strip()] = kv[1].strip().strip('"').strip("'")
    return result


def _parse_date(value: str | None) -> datetime | None:
    """Parse a date string in common formats. Returns None on failure."""
    if not value:
        return None
    for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M UTC"]:
        try:
            # strptime doesn't handle optional timezone well; strip trailing UTC
            clean = value.replace(" UTC", "").replace("Z", "")
            return datetime.strptime(clean, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    # Try date-only prefix (e.g., "2026-06-24" from "2026-06-24 12:52 UTC")
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except (ValueError, IndexError):
        return None


def _is_completed(text: str) -> bool:
    """Check if file body indicates task completion."""
    # Strip frontmatter before checking body
    body = re.sub(r"^---\s*\n.*?\n---", "", text, flags=re.DOTALL)
    return bool(COMPLETION_RE.search(body))


def _has_active_tasks(text: str) -> bool:
    """Heuristic: does the file contain sub-tasks that are NOT all completed?"""
    body = re.sub(r"^---\s*\n.*?\n---", "", text, flags=re.DOTALL)
    # If body has ✅ but also has 🟡 or 🔴, it has mixed completion
    has_done = "✅" in body
    has_pending = "🟡" in body or "🔴" in body
    return has_pending  # If anything is pending, don't archive


# ── Audit logic ──────────────────────────────────────────────────────────────


def audit(
    memory_dir: Path,
    stale_days: int = 30,
    verbose: bool = False,
) -> dict[str, list[dict]]:
    """Run audit. Returns dict with keys: stale, done_active, untracked, ok."""
    if not memory_dir.exists():
        print(f"ERROR: Memory directory not found: {memory_dir}", file=sys.stderr)
        return {"stale": [], "done_active": [], "untracked": [], "ok": []}

    results: dict[str, list[dict]] = {
        "stale": [],
        "done_active": [],
        "untracked": [],
        "ok": [],
    }
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(days=stale_days)

    for fp in sorted(memory_dir.glob("*.md")):
        fname = fp.name
        if fname == "MEMORY.md":
            continue

        try:
            text = fp.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"WARNING: Cannot read {fname}: {exc}", file=sys.stderr)
            continue

        fm = _parse_frontmatter(text)
        is_ref = fname in REFERENCE_FILES
        is_protocol = fname in PROTOCOL_FILES
        is_tracking = any(
            re.search(p, fname) or re.search(p, fm.get("description", ""))
            for p in TRACKING_FILES_PATTERNS
        )
        completed = _is_completed(text) and not is_tracking
        has_active = _has_active_tasks(text)

        # Determine best review date
        review_date: datetime | None = None
        review_source = ""
        for key in ("review_date", "completed", "updated", "started"):
            val = fm.get(key)
            if val:
                parsed = _parse_date(val)
                if parsed:
                    review_date = parsed
                    review_source = key
                    break

        # Classify
        entry = {
            "file": fname,
            "name": fm.get("name", fname.replace(".md", "")),
            "description": fm.get("description", ""),
            "review_date": review_date,
            "review_source": review_source,
            "completed": completed,
            "has_active_tasks": has_active,
            "is_reference": is_ref,
            "is_protocol": is_protocol,
        }

        if is_ref:
            results["ok"].append(entry)
            if verbose:
                print(f"  [REF ] {fname}")
        elif completed and not has_active:
            results["done_active"].append(entry)
            if verbose:
                print(f"  [DONE] {fname}")
        elif not is_protocol and review_date is None:
            results["untracked"].append(entry)
            if verbose:
                print(f"  [NO_DT] {fname}")
        elif not is_protocol and review_date and review_date < stale_cutoff:
            days_stale = (now - review_date).days
            results["stale"].append({**entry, "days_stale": days_stale})
            if verbose:
                print(
                    f"  [STALE] {fname} — {days_stale}d past review ({review_source}: {review_date.strftime('%Y-%m-%d')})"
                )
        else:
            results["ok"].append(entry)
            if verbose:
                print(f"  [OK  ] {fname}")

    return results


# ── Sweep logic ──────────────────────────────────────────────────────────────


def sweep(memory_dir: Path, dry_run: bool = True) -> int:
    """Move completed files to _archive/ and update MEMORY.md index."""
    archive_dir = memory_dir / ARCHIVE_DIR_NAME
    index_path = memory_dir / "MEMORY.md"
    results = audit(memory_dir)

    to_archive = results["done_active"]
    if not to_archive:
        print("No completed files to archive.")
        return 0

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Archiving {len(to_archive)} completed file(s):")
    for entry in to_archive:
        print(f"  → {entry['file']}  ({entry['description'][:80]})")

    if dry_run:
        print("\nRun with --sweep --no-dry-run to execute.")
        return 0

    # Create archive dir
    archive_dir.mkdir(exist_ok=True)

    # Move files
    moved = 0
    for entry in to_archive:
        src = memory_dir / entry["file"]
        dst = archive_dir / entry["file"]
        if dst.exists():
            print(f"  SKIP {entry['file']}: already in archive")
            continue
        src.rename(dst)
        moved += 1
        print(f"  [OK] {entry['file']} → _archive/")

    # Update MEMORY.md index
    if moved > 0 and index_path.exists():
        index_text = index_path.read_text(encoding="utf-8")
        index_lines = index_text.split("\n")
        new_lines = []
        for line in index_lines:
            removed = False
            for entry in to_archive:
                if entry["file"] in line:
                    removed = True
                    break
            if not removed:
                new_lines.append(line)
        index_path.write_text("\n".join(new_lines), encoding="utf-8")
        print(f"  [OK] MEMORY.md index updated (−{moved} entries)")

    print(f"\nArchived {moved} file(s).")
    return 0


# ── Report formatting ────────────────────────────────────────────────────────


def print_report(results: dict, stale_days: int) -> None:
    """Print the audit report."""
    stale = results["stale"]
    done_active = results["done_active"]
    untracked = results["untracked"]
    ok = results["ok"]

    print()
    print("=" * 60)
    print("  Memory Audit Report")
    print(
        f"  {len(ok)} OK | {len(stale)} stale | {len(done_active)} done | {len(untracked)} untracked"
    )
    print("=" * 60)

    if stale:
        print(f"\n── STALE (> {stale_days}d past review date) ──")
        for e in sorted(stale, key=lambda x: x.get("days_stale", 0), reverse=True):
            src = e.get("review_source", "?")
            ds = e.get("days_stale", "?")
            print(f"  {e['file']}")
            print(f"    {ds}d since {src}: {e.get('review_date')}")

    if done_active:
        print("\n── DONE BUT STILL ACTIVE (archivable) ──")
        for e in done_active:
            print(f"  {e['file']}")
            if e["description"]:
                print(f"    {e['description'][:100]}")

    if untracked:
        print("\n── UNTRACKED (no review date) ──")
        for e in untracked:
            print(f"  {e['file']}")
            if e["description"]:
                print(f"    {e['description'][:100]}")

    if not stale and not done_active and not untracked:
        print("\n  All memory files are up to date.")

    print()
    if done_active:
        print(
            f"  → {len(done_active)} file(s) ready for archival: python scripts/audit_memory.py --sweep --no-dry-run"
        )
    if stale:
        print(f"  → {len(stale)} file(s) past review date — update review_date or mark completed")
    if untracked:
        print(f"  → {len(untracked)} file(s) without review_date — consider adding one")


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit Claude memory files for staleness and completion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--memory-dir",
        type=Path,
        default=MEMORY_DIR,
        help=f"Memory directory (default: {MEMORY_DIR})",
    )
    parser.add_argument(
        "--stale",
        type=int,
        default=30,
        metavar="DAYS",
        help="Days after which a file is considered stale (default: 30)",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Archive completed files to _archive/",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        dest="no_dry_run",
        help="Actually execute sweep (default: dry run only)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show per-file status",
    )
    args = parser.parse_args(argv)

    if args.sweep:
        dry_run = not args.no_dry_run
        return sweep(args.memory_dir, dry_run=dry_run)

    results = audit(
        args.memory_dir,
        stale_days=args.stale,
        verbose=args.verbose,
    )
    print_report(results, args.stale)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
