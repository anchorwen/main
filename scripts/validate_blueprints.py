#!/usr/bin/env python3
"""Validate blueprint consistency.

Checks:
  1. All expected module blueprint files exist
  2. Each module file has required sections
  3. FIX_REGISTRY entries match module fix histories (no orphans)
  4. DEPENDENCY_GRAPH entries exist for all modules

Usage:
    python scripts/validate_blueprints.py
    python scripts/validate_blueprints.py --verbose
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES_DIR = ROOT / "blueprints" / "modules"
SYSTEM_DIR = ROOT / "blueprints" / "system"
FIX_REGISTRY = SYSTEM_DIR / "FIX_REGISTRY.md"
DEPS_FILE = SYSTEM_DIR / "DEPENDENCY_GRAPH.md"
OVERVIEW_FILE = SYSTEM_DIR / "OVERVIEW.md"

REQUIRED_SECTIONS = [
    "## Purpose",
    "## Key Files",
    "## Data Flow",
    "## Inbound Dependencies",
    "## Outbound Dependents",
    "## Known Issues",
    "## Fix History",
    "## Cross-Module Contracts",
    "## Verification",
]

EXPECTED_MODULES = [
    "brains_adapters",
    "brains_services",
    "brains_schema",
    "brains_validation",
    "execution_guards",
    "execution_orders",
    "execution_reentry",
    "risk_policies",
    "risk_regime",
    "risk_portfolio",
    "feedback_performance",
    "feedback_pnl",
    "feedback_online",
    "protocol_governance",
    "protocol_parliament",
    "protocol_services",
    "contracts_domain",
    "contracts_ids",
    "contracts_training",
    "deployment_config",
    "deployment_lifecycle",
    "features_rolling",
    "features_service",
    "runtime_live",
    "runtime_state",
    "training",
    "market_mtf",
    "monitor_dashboard",
]

FIX_ID_RE = re.compile(r"FIX-\d{8}-\d{3}")


def check_modules_exist() -> list[str]:
    """Check all expected module files exist. Returns list of errors."""
    errors = []
    for mod in EXPECTED_MODULES:
        path = MODULES_DIR / f"{mod}.md"
        if not path.exists():
            errors.append(f"MISSING: {path.relative_to(ROOT)}")
    # Check for unexpected files
    for path in MODULES_DIR.glob("*.md"):
        name = path.stem
        if name not in EXPECTED_MODULES:
            errors.append(f"UNEXPECTED: {path.relative_to(ROOT)} (not in expected list)")
    return errors


def check_module_sections() -> list[str]:
    """Check each module file has all required sections. Returns list of errors."""
    errors = []
    for mod in EXPECTED_MODULES:
        path = MODULES_DIR / f"{mod}.md"
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        for section in REQUIRED_SECTIONS:
            if section not in content:
                errors.append(f"MISSING SECTION: {mod}.md — '{section}'")
    return errors


def check_fix_registry_consistency() -> list[str]:
    """Check FIX_REGISTRY entries match module fix histories. Returns list of errors."""
    errors = []
    if not FIX_REGISTRY.exists():
        errors.append(f"MISSING: {FIX_REGISTRY.relative_to(ROOT)}")
        return errors

    registry_text = FIX_REGISTRY.read_text(encoding="utf-8")

    # Extract fix IDs from registry index
    registry_ids: set[str] = set()
    for m in FIX_ID_RE.finditer(registry_text):
        registry_ids.add(m.group(0))

    # Extract fix IDs from all module files
    module_ids: set[str] = set()
    for mod in EXPECTED_MODULES:
        path = MODULES_DIR / f"{mod}.md"
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        for m in FIX_ID_RE.finditer(content):
            module_ids.add(m.group(0))

    # Orphans in registry (not in any module)
    orphans = registry_ids - module_ids
    for o in sorted(orphans):
        errors.append(f"ORPHAN: {o} in FIX_REGISTRY but not in any module Fix History")

    # Missing from registry (in module but not in registry)
    missing = module_ids - registry_ids
    for mid in sorted(missing):
        errors.append(f"MISSING FROM REGISTRY: {mid} in module Fix History but not in FIX_REGISTRY")

    return errors


def check_system_files_exist() -> list[str]:
    """Check all system-level files exist. Returns list of errors."""
    errors = []
    for path in [FIX_REGISTRY, DEPS_FILE, OVERVIEW_FILE]:
        if not path.exists():
            errors.append(f"MISSING: {path.relative_to(ROOT)}")
    return errors


def check_source_blueprint_freshness() -> list[str]:
    """Check that changed .py files have corresponding blueprint updates.

    Uses MODULE_SOURCE_MAP from check_blueprint_compliance to verify
    that any substantive .py change has a blueprint update in the same change set.
    Returns list of errors.
    """
    errors: list[str] = []

    # Import the compliance engine
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from check_blueprint_compliance import classify_diff, resolve_modules
    except ImportError as exc:
        errors.append(f"CANNOT IMPORT: check_blueprint_compliance — {exc}")
        return errors

    import subprocess as _sp

    # In pre-commit context only check staged files — unstaged changes are
    # stashed by the pre-commit framework and belong to other sessions.
    # Without this guard, cumulative unstaged changes from prior sessions
    # create an unresolvable deadlock: each session's FIX entries are written
    # to blueprints (Post-Fix Protocol) but the pre-commit stash reverts
    # those blueprints to HEAD → false STALE/ORPHAN violations → commit
    # blocked → changes stay unstaged → next session adds more.
    in_precommit = os.environ.get("PRE_COMMIT", "") == "1"

    # Get changed .py files AND all changed files (for blueprint matching)
    changed_py: set[str] = set()
    changed_all: set[str] = set()
    diff_args_list: list[list[str]] = [
        ["git", "diff", "--name-only", "--cached"],
    ]
    if not in_precommit:
        diff_args_list.append(["git", "diff", "--name-only", "HEAD"])
    for args in diff_args_list:
        try:
            result = _sp.run(args, capture_output=True, text=True, cwd=str(ROOT), timeout=10)
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    changed_all.add(line)
                    if line.endswith(".py") and not line.startswith("tests/"):
                        changed_py.add(line)
        except Exception:  # noqa: BLE001
            pass

    if not changed_py:
        return errors

    # Check each substantive .py change has blueprint update
    for fp in sorted(changed_py):
        modules = resolve_modules(fp)
        if not modules:
            errors.append(f"ORPHAN: {fp} changed but not mapped in MODULE_SOURCE_MAP")
            continue

        category = classify_diff(fp, cached_only=in_precommit)
        if category == "cosmetic":
            continue

        # Check if any owning blueprint is also in the change set
        bp_changed = False
        for m in modules:
            bp_path = f"blueprints/modules/{m}.md"
            if bp_path in changed_all:
                bp_changed = True
                break

        if not bp_changed:
            bp_list = "\n    ".join(f"blueprints/modules/{m}.md" for m in modules)
            errors.append(
                f"STALE: {fp} substantively changed but blueprint(s) not staged.\n"
                f"  Missing: {', '.join(modules)}\n"
                f"  Action: update Fix History in:\n"
                f"    {bp_list}\n"
                f"  Then: git add {' '.join(f'blueprints/modules/{m}.md' for m in modules)}\n"
                f"  Tip: use 'python scripts/register_fix.py --help' or stage both .py + blueprint together."
            )

    return errors


def check_dependency_graph_consistency() -> list[str]:
    """Check DEPENDENCY_GRAPH references all modules. Returns list of errors."""
    errors: list[str] = []
    if not DEPS_FILE.exists():
        return errors

    deps_text = DEPS_FILE.read_text(encoding="utf-8")

    # Simple check: each module name should appear somewhere in the graph
    for mod in EXPECTED_MODULES:
        # Try both underscore and slash forms
        name_parts = mod.split("_")
        slash_name = "/".join(name_parts)
        found = slash_name in deps_text or mod in deps_text
        # Also check if individual parts appear (e.g., "deployment" and "config")
        if not found and len(name_parts) >= 2:
            found = all(part in deps_text for part in name_parts)
        if not found:
            errors.append(f"MISSING FROM DEPS: {mod} not found in DEPENDENCY_GRAPH.md")
    return errors


def main() -> int:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    all_errors: list[str] = []

    checks = [
        ("Module files exist", check_modules_exist),
        ("Module sections complete", check_module_sections),
        ("System files exist", check_system_files_exist),
        ("Fix registry consistency", check_fix_registry_consistency),
        ("Source-blueprint freshness", check_source_blueprint_freshness),
        ("Dependency graph coverage", check_dependency_graph_consistency),
    ]

    for name, check_fn in checks:
        errors = check_fn()
        if errors:
            all_errors.extend(errors)
            print(f"[FAIL] {name}: {len(errors)} issue(s)")
            for e in errors:
                print(f"  {e}")
        else:
            if verbose:
                print(f"[PASS] {name}")

    if all_errors:
        print(f"\n{len(all_errors)} blueprint validation issue(s) found.", file=sys.stderr)
        return 1
    else:
        print("All blueprint consistency checks passed.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
