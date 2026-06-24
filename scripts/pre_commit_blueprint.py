#!/usr/bin/env python3
"""Git pre-commit blueprint hook — baseline-gated.

Mirrors the architecture of ``scripts/pre_commit_mypy.py``:

  - ``blueprint_baseline.json`` records the CURRENT count of pre-existing
    blueprint validation issues per check category.
  - On each commit, ``validate_blueprints.py`` is run and its per-category
    error counts are compared against the baseline.
  - Only INCREASES from baseline are blocked.  Pre-existing debt does not
    block new commits.

**Critical design rule**: ``source_blueprint_freshness`` is ALWAYS set to 0
in the baseline and is unaffected by ``--update-baseline``.  This check
detects when a .py file is modified without a corresponding blueprint
update — it has ZERO TOLERANCE, always.  The baseline only exempts
pre-existing debt in static consistency checks (module existence, required
sections, registry cross-references).

Usage::

    # Run as pre-commit hook (compares against baseline)
    python scripts/pre_commit_blueprint.py

    # Rebuild baseline (capture current state as acceptable)
    python scripts/pre_commit_blueprint.py --update-baseline

    # Dry-run: show what --update-baseline would write
    python scripts/pre_commit_blueprint.py --dry-run
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "blueprint_baseline.json"
VALIDATOR_SCRIPT = ROOT / "scripts" / "validate_blueprints.py"

# Categories in validate_blueprints.py (must match print output exactly)
CATEGORIES = [
    ("modules_exist", "Module files exist"),
    ("sections_complete", "Module sections complete"),
    ("fix_registry_consistency", "Fix registry consistency"),
    ("source_blueprint_freshness", "Source-blueprint freshness"),
    ("dep_graph_coverage", "Dependency graph coverage"),
]

# Regex to parse "[FAIL] Category Name: N issue(s)" lines
_FAIL_LINE_RE = re.compile(r"\[FAIL\]\s+(.+?):\s+(\d+)\s+issue\(s\)")


def load_baseline() -> dict[str, int]:
    """Read the stored baseline from ``blueprint_baseline.json``."""
    if BASELINE_PATH.exists():
        with open(BASELINE_PATH, encoding="utf-8") as f:
            raw: object = json.load(f)
            if isinstance(raw, dict):
                return {str(k): int(v) for k, v in raw.items()}
    return {}


def save_baseline(baseline: dict[str, int]) -> None:
    """Write the baseline to ``blueprint_baseline.json``."""
    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, sort_keys=True)
        f.write("\n")


def run_validate() -> dict[str, int]:
    """Run ``validate_blueprints.py`` and return per-category error counts.

    Returns a dict mapping category key → error count.  Returns an empty
    dict (mypy crashed / script not found) on failure.
    """
    try:
        result = subprocess.run(
            [sys.executable, str(VALIDATOR_SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=60,
        )
    except FileNotFoundError:
        print("pre_commit_blueprint: validate_blueprints.py not found", file=sys.stderr)
        return {}
    except subprocess.TimeoutExpired:
        print("pre_commit_blueprint: validate_blueprints.py timed out", file=sys.stderr)
        return {}

    # Parse stderr (where the [FAIL] lines are printed by validate_blueprints.py)
    output = (result.stderr or "") + "\n" + (result.stdout or "")

    counts: dict[str, int] = {}
    for line in output.split("\n"):
        m = _FAIL_LINE_RE.search(line)
        if m:
            category_display = m.group(1).strip()
            count = int(m.group(2))
            # Map display name to key
            for key, display in CATEGORIES:
                if display == category_display:
                    counts[key] = count
                    break
    return counts


def get_baseline_for_update(dry_run: bool = False) -> dict[str, int]:
    """Capture current error counts for baseline update.

    Runs validate_blueprints.py WITHOUT PRE_COMMIT=1 so all files are
    checked (not just staged).  ``source_blueprint_freshness`` is always
    set to 0 — zero tolerance enforcement.
    """
    # Remove PRE_COMMIT from env for baseline capture (check all files)
    import os

    saved = os.environ.pop("PRE_COMMIT", None)
    try:
        counts = run_validate()
    finally:
        if saved is not None:
            os.environ["PRE_COMMIT"] = saved

    # ALWAYS zero out freshness — never baselined
    counts["source_blueprint_freshness"] = 0

    if dry_run:
        print("[dry-run] Would write baseline:")
        for key, display in CATEGORIES:
            count = counts.get(key, 0)
            print(f"  {display}: {count}")
        print("  (source_blueprint_freshness always 0 — zero tolerance)")
    return counts


def main() -> int:
    if "--update-baseline" in sys.argv:
        print("Rebuilding blueprint baseline from current state...")
        baseline = get_baseline_for_update(dry_run=False)
        save_baseline(baseline)
        print(
            f"Baseline updated: {len(baseline)} categories, "
            f"{sum(baseline.values())} total pre-existing issues"
        )
        for key, display in CATEGORIES:
            count = baseline.get(key, 0)
            note = " (zero tolerance)" if key == "source_blueprint_freshness" else ""
            print(f"  {display}: {count}{note}")
        return 0

    if "--dry-run" in sys.argv:
        print("[dry-run] Previewing baseline update...")
        get_baseline_for_update(dry_run=True)
        return 0

    # ── Pre-commit check: compare current counts against baseline ────────

    current = run_validate()
    if not current:
        # validator crashed / not found — fail open (don't block commit)
        print("pre_commit_blueprint: could not run validator, skipping", file=sys.stderr)
        return 0

    baseline = load_baseline()
    new_issues: list[str] = []

    for key, display in CATEGORIES:
        cur_count = current.get(key, 0)
        base_count = baseline.get(key, 0)

        if key == "source_blueprint_freshness":
            # Zero tolerance: any freshness issue blocks (baseline always 0)
            if cur_count > 0:
                new_issues.append(
                    f"  {display}: {cur_count} issue(s) — ZERO TOLERANCE (baseline always 0)"
                )
        elif cur_count > base_count:
            delta = cur_count - base_count
            new_issues.append(f"  {display}: {base_count} → {cur_count} issue(s) (+{delta} NEW)")

    if new_issues:
        print("=" * 72)
        print("IRON LAW: Blueprint validation issues INCREASED!")
        print("Fix the new issues before committing, or if they are")
        print("intentional / pre-existing, run:")
        print("  python scripts/pre_commit_blueprint.py --update-baseline")
        print("=" * 72)
        for issue in new_issues:
            print(issue)
        return 1

    # Print passing summary
    total = sum(current.values())
    print(f"blueprint: {total} pre-existing issue(s) in baseline, no new issues.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
