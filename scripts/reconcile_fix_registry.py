#!/usr/bin/env python
"""Blueprint FIX reconciliation — one-shot backlog clearance.
# type: ignore  # FIX-20260620-076: Sev 4 audit script, suppressed

Iron Law #7 enforcement: synchronizes FIX_REGISTRY.md ↔ module blueprint
Fix History tables.  FIX-060 gate prevents *future* drift; this script
clears the *historical* backlog.

Usage:
    python scripts/reconcile_fix_registry.py --dry-run   # audit only
    python scripts/reconcile_fix_registry.py             # apply fixes
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "blueprints" / "system" / "FIX_REGISTRY.md"
MODULES_DIR = ROOT / "blueprints" / "modules"

# ── Parsing helpers ──────────────────────────────────────────────────────────


def _parse_registry_fix_index(content: str) -> list[dict]:
    """Extract rows from the Fix Index table in FIX_REGISTRY.md."""
    rows = []
    in_table = False
    for line in content.split("\n"):
        if line.startswith("| Fix ID |"):
            in_table = True
            continue
        if in_table:
            if line.startswith("|---"):
                continue
            if not line.startswith("| FIX-"):
                break
            # | FIX-YYYYMMDD-NNN | date | module | summary | root_cause |
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) >= 5:
                rows.append(
                    {
                        "fix_id": cells[0],
                        "date": cells[1],
                        "module": cells[2],
                        "summary": cells[3],
                        "root_cause": cells[4],
                        "raw_line": line,
                    }
                )
    return rows


def _parse_module_fix_history(content: str) -> list[dict]:
    """Extract rows from a module blueprint's Fix History table."""
    rows = []
    in_table = False
    for line in content.split("\n"):
        if "Fix History" in line:
            in_table = False  # Reset — looking for the NEXT table
        if line.startswith("| FIX-") and "|" in line:
            in_table = True
        if in_table and line.startswith("| FIX-"):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) >= 3:
                rows.append(
                    {
                        "fix_id": cells[0],
                        "date": cells[1] if len(cells) > 1 else "",
                        "summary": cells[2] if len(cells) > 2 else "",
                        "raw_line": line,
                    }
                )
    return rows


def _find_fix_history_insertion_point(lines: list[str]) -> int | None:
    """Find the line number where a new row should be inserted in Fix History.

    Returns the line index AFTER the last Fix History row, or after
    the table header if the table is empty.
    """
    header_idx = None
    last_row_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("| FIX-") and "|" in stripped:
            if header_idx is None:
                # Found first row — header must be 2 lines above
                header_idx = i - 2
            last_row_idx = i
    if last_row_idx is not None:
        return last_row_idx + 1
    # No existing rows — find the Fix History header
    for i, line in enumerate(lines):
        if line.strip().startswith("## Fix History"):
            # Table header should be 2 lines below
            if i + 3 < len(lines):
                return i + 3  # after |---|---|
            return None
    return None


def _resolve_module_name(registry_module_field: str) -> str | None:
    """Map registry module name to blueprint filename.

    Registry uses hyphenated names (e.g. 'runtime-live', 'execution-orders').
    Blueprint filenames use underscores (e.g. 'runtime_live.md', 'execution_orders.md').
    """
    candidate = registry_module_field.replace("-", "_")
    bp_path = MODULES_DIR / f"{candidate}.md"
    if bp_path.exists():
        return candidate
    # Try common aliases
    aliases = {
        "deployment-config": "deployment_config",
        "deployment-lifecycle": "deployment_lifecycle",
        "monitor-dashboard": "monitor_dashboard",
        "features-service": "features_service",
        "feedback-online": "feedback_online",
        "feedback-performance": "feedback_performance",
        "feedback-pnl": "feedback_pnl",
        "market-mtf": "market_mtf",
        "protocol-governance": "protocol_governance",
        "protocol-parliament": "protocol_parliament",
        "protocol-services": "protocol_services",
        "risk-policies": "risk_policies",
        "risk-portfolio": "risk_portfolio",
        "risk-regime": "risk_regime",
        "execution-orders": "execution_orders",
        "execution-guards": "execution_guards",
        "execution-reentry": "execution_reentry",
        "execution-state": "execution_state",
        "execution-exit-watchdog": "execution_exit_watchdog",
        "execution-managed-close": "execution_managed_close",
        "execution-position-manager": "execution_position_manager",
        "execution-strategy-line": "execution_strategy_line",
        "execution-trail-stop": "execution_trail_stop",
        "runtime-live": "runtime_live",
        "runtime": "runtime_live",  # "runtime" alone → runtime_live
        "brains-services": "brains_services",
        "brains-schema": "brains_schema",
        "brains-adapters": "brains_adapters",
        "brains-validation": "brains_validation",
        "contracts-domain": "contracts_domain",
        "contracts-ids": "contracts_ids",
        "contracts-training": "contracts_training",
        "data-infrastructure": "data_infrastructure",
        "features-rolling": "features_rolling",
        "ledger": "data_infrastructure",
        "observability": "monitor_dashboard",
        "config": "deployment_config",
        "scripts": "deployment_lifecycle",
        "configs": "deployment_config",
        # Unassignable entries from backlog
        "parliament": "protocol_parliament",
        "testing": "deployment_lifecycle",
        "features": "features_service",
        "ledger-services": "data_infrastructure",
        "governance": "protocol_governance",
        "daily-ops": "deployment_lifecycle",
        "alpha-registry": "deployment_config",
        "system-health": "monitor_dashboard",
        "data-health": "monitor_dashboard",
        "journal-cleanup": "data_infrastructure",
    }
    mapped = aliases.get(registry_module_field)
    if mapped and (MODULES_DIR / f"{mapped}.md").exists():
        return mapped
    return None


# ── Reconciliation logic ─────────────────────────────────────────────────────


def reconcile(dry_run: bool = True) -> dict:
    """Run reconciliation. Returns summary dict."""
    registry_text = REGISTRY_PATH.read_text(encoding="utf-8")
    registry_rows = _parse_registry_fix_index(registry_text)
    registry_ids = {r["fix_id"]: r for r in registry_rows}

    # Parse all module blueprints
    module_fix_ids: dict[str, set[str]] = defaultdict(set)
    module_fix_details: dict[str, list[dict]] = defaultdict(list)
    module_paths: dict[str, Path] = {}
    for bp_path in sorted(MODULES_DIR.glob("*.md")):
        mod_name = bp_path.stem
        module_paths[mod_name] = bp_path
        content = bp_path.read_text(encoding="utf-8")
        for row in _parse_module_fix_history(content):
            module_fix_ids[mod_name].add(row["fix_id"])
            module_fix_details[mod_name].append(row)

    all_module_ids = set()
    for ids in module_fix_ids.values():
        all_module_ids |= ids

    # ── ORPHAN: in registry but not in any module──
    orphan_ids = registry_ids.keys() - all_module_ids

    # ── MISSING: in module but not in registry ──
    missing_ids = all_module_ids - registry_ids.keys()

    # ── Build module → FIX mapping for orphans ──
    orphan_by_module: dict[str, list[dict]] = defaultdict(list)
    unassignable: list[dict] = []
    for fid in sorted(orphan_ids):
        reg = registry_ids[fid]
        modules_str = reg["module"]
        # Registry module field can list multiple modules separated by comma
        primary = modules_str.split(",")[0].strip()
        bp_name = _resolve_module_name(primary)
        if bp_name:
            orphan_by_module[bp_name].append(reg)
        else:
            unassignable.append(reg)

    # ── Apply fixes (if not dry-run) ──
    orphan_added = 0
    missing_added = 0

    if not dry_run:
        # --- Backfill ORPHAN entries into module blueprints ---
        for bp_name, fixes in sorted(orphan_by_module.items()):
            if bp_name not in module_paths:
                print(f"  SKIP {bp_name}: blueprint not found", file=sys.stderr)
                continue
            bp_path = module_paths[bp_name]
            lines = bp_path.read_text(encoding="utf-8").split("\n")
            insert_at = _find_fix_history_insertion_point(lines)
            if insert_at is None:
                print(f"  SKIP {bp_name}: cannot find Fix History table", file=sys.stderr)
                continue

            new_rows = []
            for fix in sorted(fixes, key=lambda r: r["fix_id"]):
                # Format: | FIX-ID | date | summary | root_cause |
                row = (
                    f"| {fix['fix_id']} | {fix['date']} | cursor-agent | — | "
                    f"{fix['summary']} | {fix['root_cause']} |"
                )
                new_rows.append(row)
                orphan_added += 1

            # Insert new rows before the insertion point (reverse chronological)
            for row in reversed(new_rows):
                lines.insert(insert_at, row)
            bp_path.write_text("\n".join(lines), encoding="utf-8")
            print(f"  +{len(new_rows)} rows → {bp_name}.md")

        # --- Backfill MISSING entries into FIX_REGISTRY.md ---
        # Find missing entries from module blueprints
        missing_details: list[dict] = []
        for bp_name, details in module_fix_details.items():
            for d in details:
                if d["fix_id"] in missing_ids:
                    # Map module name back to registry module format
                    reg_module = bp_name.replace("_", "-")
                    missing_details.append(
                        {
                            "fix_id": d["fix_id"],
                            "date": d.get("date", "?date?"),
                            "module": reg_module,
                            "summary": d.get("summary", "?summary?"),
                            "root_cause": "RC-06",
                            "source_module": bp_name,
                        }
                    )
                    missing_added += 1

        if missing_details:
            reg_lines = REGISTRY_PATH.read_text(encoding="utf-8").split("\n")
            # Find the Fix Index table insertion point
            insert_at = None
            for i, line in enumerate(reg_lines):
                if line.startswith("| FIX-") and "|" in line:
                    insert_at = i + 1  # after this row
            if insert_at:
                # Sort by FIX ID for consistent ordering
                new_rows = []
                for fix in sorted(missing_details, key=lambda r: r["fix_id"]):
                    row = (
                        f"| {fix['fix_id']} | {fix['date']} | {fix['module']} | "
                        f"{fix['summary']} | {fix['root_cause']} |"
                    )
                    new_rows.append(row)

                for row in reversed(new_rows):
                    reg_lines.insert(insert_at, row)
                REGISTRY_PATH.write_text("\n".join(reg_lines), encoding="utf-8")
                print(f"  +{len(new_rows)} rows → FIX_REGISTRY.md")

    return {
        "orphan_total": len(orphan_ids),
        "orphan_assignable": sum(len(v) for v in orphan_by_module.values()),
        "orphan_unassignable": len(unassignable),
        "missing_total": len(missing_ids),
        "orphan_added": orphan_added,
        "missing_added": missing_added,
        "unassignable_list": [u["fix_id"] for u in unassignable],
    }


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reconcile_fix_registry")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Audit only, don't modify files (default)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        dest="apply",
        help="Actually apply the reconciliation",
    )
    args = parser.parse_args(argv)

    dry_run = not args.apply

    print("=" * 60)
    print("  FIX Registry <-> Module Blueprint Reconciliation")
    print("  Iron Law #7 backlog clearance")
    print("=" * 60)
    if dry_run:
        print("\n  *** DRY RUN — no files will be modified ***\n")

    result = reconcile(dry_run=dry_run)

    print(f"\n── ORPHAN (registry only): {result['orphan_total']}")
    print(f"   Assignable: {result['orphan_assignable']}")
    if result["orphan_unassignable"]:
        print(f"   Unassignable: {result['orphan_unassignable']}")
        for fid in result["unassignable_list"]:
            print(f"     - {fid}")

    print(f"\n── MISSING (module only): {result['missing_total']}")

    if not dry_run:
        print("\n── APPLIED:")
        print(f"   +{result['orphan_added']} rows to module blueprints")
        print(f"   +{result['missing_added']} rows to FIX_REGISTRY.md")
        print("\n[OK] Reconciliation applied. Run validate_blueprints.py to verify.")
    else:
        print("\n── WOULD APPLY:")
        print(f"   +{result['orphan_assignable']} rows to module blueprints")
        print(f"   +{result['missing_total']} rows to FIX_REGISTRY.md")
        print("\n  Run with --apply to execute.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
