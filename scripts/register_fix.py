#!/usr/bin/env python3
"""Register a fix in the blueprint system.

Usage:
    python scripts/register_fix.py --module brains-adapters \\
        --fix-id FIX-20260514-001 --type fix \\
        --description "Fixed null check in V9 adapter infer()" \\
        --root-cause RC-01 --files "core/brains/adapters/v9_onnx_brain_adapter.py"

The script appends an entry to:
  1. blueprints/modules/<module>.md Fix History table
  2. blueprints/system/FIX_REGISTRY.md Fix Index + Fix Details

It auto-increments the NNN counter per date if --fix-id is not specified.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES_DIR = ROOT / "blueprints" / "modules"
FIX_REGISTRY = ROOT / "blueprints" / "system" / "FIX_REGISTRY.md"
FIXES_DIR = ROOT / "blueprints" / "system" / "fixes"
FIXES_TEMPLATE = FIXES_DIR / "_TEMPLATE.md"

ROOT_CAUSE_MAP = {
    "RC-01": "missing-null-check",
    "RC-02": "type-confusion",
    "RC-03": "state-leak",
    "RC-04": "race-condition",
    "RC-05": "boundary-error",
    "RC-06": "contract-violation",
    "RC-07": "missing-validation",
    "RC-08": "incomplete-cleanup",
    "RC-09": "config-drift",
    "RC-10": "dependency-order",
}

FIX_ID_RE = re.compile(r"^FIX-\d{8}-\d{3}$")


def _get_git_user() -> str:
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        return "unknown"


def _get_git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        return "unknown"


def _next_fix_id(date_str: str) -> str:
    """Find the next available NNN for today's date."""
    existing = []
    if FIX_REGISTRY.exists():
        text = FIX_REGISTRY.read_text(encoding="utf-8")
        for m in re.finditer(rf"FIX-{date_str}-(\d{{3}})", text):
            existing.append(int(m.group(1)))
    nnn = max(existing) + 1 if existing else 1
    return f"FIX-{date_str}-{nnn:03d}"


def validate_fix_id(fix_id: str) -> bool:
    return bool(FIX_ID_RE.match(fix_id))


def _resolve_module_file(module: str) -> Path | None:
    """Resolve a module name (hyphens or underscores) to its blueprint file."""
    # Try exact match first
    exact = MODULES_DIR / f"{module}.md"
    if exact.exists():
        return exact
    # Try converting hyphens to underscores
    underscore = MODULES_DIR / f"{module.replace('-', '_')}.md"
    if underscore.exists():
        return underscore
    return None


def _quarter_for_date(date_str: str) -> tuple[str, str]:
    """Map a date string (YYYY-MM-DD) to quarter info.

    Returns (quarter_label, filename).
    Example: '2026-06-25' → ('2026 Q2', 'FIX_2026_Q2.md')
    """
    parts = date_str.split("-")
    year = int(parts[0])
    month = int(parts[1])
    quarter = (month - 1) // 3 + 1
    return (f"{year} Q{quarter}", f"FIX_{year}_Q{quarter}.md")


def _ensure_quarter_file(quarter_file: Path) -> bool:
    """Create a quarterly fix file from template if it doesn't exist."""
    if quarter_file.exists():
        return True
    if not FIXES_TEMPLATE.exists():
        print(f"WARNING: Template not found: {FIXES_TEMPLATE}", file=sys.stderr)
        return False
    template = FIXES_TEMPLATE.read_text(encoding="utf-8")
    # Replace placeholder fields
    quarter_name = quarter_file.stem.replace("FIX_", "").replace("_", " ")
    content = template.replace("YYYY QN", quarter_name)
    content = content.replace("Mon–Mon", "TBD–TBD")
    content = content.replace("YYYY-MM-DD to YYYY-MM-DD", "TBD to TBD")
    content = content.replace("FIX-YYYYMMDD-NNN", "(none yet)")
    quarter_file.write_text(content, encoding="utf-8")
    print(f"  [OK] Created new quarter file: {quarter_file.relative_to(ROOT)}")
    return True


def append_to_module(
    module: str,
    fix_id: str,
    date_str: str,
    author: str,
    commit: str,
    description: str,
    root_cause: str,
) -> bool:
    """Append fix entry to the module's Fix History table."""
    module_file = _resolve_module_file(module)
    if module_file is None:
        print(f"ERROR: Module blueprint not found: {module_file}", file=sys.stderr)
        return False

    content = module_file.read_text(encoding="utf-8")
    rc_name = ROOT_CAUSE_MAP.get(root_cause, root_cause)
    entry = f"| {fix_id} | {date_str} | {author} | {commit} | {description} | {rc_name} |\n"

    # Find the Fix History table (after the header row)
    fix_history_marker = "## Fix History"
    if fix_history_marker not in content:
        print(f"ERROR: '## Fix History' section not found in {module_file}", file=sys.stderr)
        return False

    # Insert after the header+separator+comment lines, before any existing entries
    lines = content.split("\n")
    insert_idx = None
    in_fix_history = False
    for i, line in enumerate(lines):
        if line.strip() == fix_history_marker:
            in_fix_history = True
            continue
        if in_fix_history:
            # Skip the comment block and header/separator rows
            if line.strip().startswith("<!--") or line.strip().startswith("Format:"):
                continue
            if line.strip().startswith("| Fix ID"):
                # Table header found; insert after separator
                for j in range(i + 1, len(lines)):
                    if lines[j].strip().startswith("|---"):
                        continue
                    if lines[j].strip().startswith("|"):
                        # Found first entry line; insert before it
                        insert_idx = j
                        break
                    insert_idx = j
                    break
                break
            if line.strip().startswith("|"):
                insert_idx = i
                break

    if insert_idx is None:
        # Append at end of file
        lines.append(entry.rstrip())
    else:
        lines.insert(insert_idx, entry.rstrip())

    module_file.write_text("\n".join(lines), encoding="utf-8")
    print(f"  [OK] Appended to {module_file.relative_to(ROOT)}")
    return True


def append_to_registry(
    fix_id: str,
    date_str: str,
    author: str,
    commit: str,
    fix_type: str,
    module: str,
    files: str,
    description: str,
    root_cause: str,
    depends_on: str = "",
) -> bool:
    """Append fix detail to the quarterly file and update the index in FIX_REGISTRY.md.

    Quarterly split (2026-06-25): detail entries go to fixes/FIX_YYYY_QN.md.
    FIX_REGISTRY.md retains only the Fix Index table (central lookup).
    """
    if not FIX_REGISTRY.exists():
        print(f"ERROR: FIX_REGISTRY not found: {FIX_REGISTRY}", file=sys.stderr)
        return False

    rc_name = ROOT_CAUSE_MAP.get(root_cause, root_cause)

    # Build detail block
    detail = f"""
### {fix_id}
- **Date**: {date_str}
- **Author**: {author}
- **Commit**: {commit}
- **Type**: {fix_type}
- **Module**: {module}
- **Files**: {files}
- **Description**: {description}
- **Root Cause**: {root_cause} — {rc_name}
- **Prevention**: (to be filled)
- **Dependents Checked**: {depends_on if depends_on else "(none)"}
"""

    # ── Step 1: Write detail to the quarterly file ──
    _quarter_label, quarter_filename = _quarter_for_date(date_str)
    quarter_file = FIXES_DIR / quarter_filename
    if not _ensure_quarter_file(quarter_file):
        print(f"ERROR: Cannot create quarter file: {quarter_file}", file=sys.stderr)
        return False

    quarter_content = quarter_file.read_text(encoding="utf-8")
    quarter_lines = quarter_content.split("\n")

    # Append detail after the "## Fix Details" section header + template comment
    detail_marker = "## Fix Details"
    if detail_marker in quarter_content:
        insert_idx = len(quarter_lines)
        for i, line in enumerate(quarter_lines):
            if line.strip() == detail_marker:
                for j in range(i + 1, len(quarter_lines)):
                    if quarter_lines[j].strip().startswith("<!--"):
                        continue
                    insert_idx = j
                    break
                break
        quarter_lines.insert(insert_idx, detail.rstrip())
    else:
        quarter_lines.append(detail.rstrip())

    quarter_file.write_text("\n".join(quarter_lines), encoding="utf-8")
    print(f"  [OK] Detail → {quarter_file.relative_to(ROOT)}")

    # ── Step 2: Update Fix Index table in FIX_REGISTRY.md ──
    registry_content = FIX_REGISTRY.read_text(encoding="utf-8")
    registry_lines = registry_content.split("\n")

    index_entry = f"| {fix_id} | {date_str} | {module} | {description} | {root_cause} |"
    inserted = False
    for i, line in enumerate(registry_lines):
        if line.strip().startswith("| Fix ID") and "Module" in line:
            for j in range(i + 1, min(i + 5, len(registry_lines))):
                if registry_lines[j].strip().startswith("|---"):
                    for k in range(j + 1, len(registry_lines)):
                        if registry_lines[k].strip().startswith("| —"):
                            registry_lines[k] = index_entry
                            inserted = True
                            break
                        if not registry_lines[k].strip().startswith("|"):
                            registry_lines.insert(k, index_entry)
                            inserted = True
                            break
                    break
            break

    if not inserted:
        print("WARNING: Could not find index insertion point in FIX_REGISTRY.md", file=sys.stderr)

    FIX_REGISTRY.write_text("\n".join(registry_lines), encoding="utf-8")
    print(f"  [OK] Index → {FIX_REGISTRY.relative_to(ROOT)}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register a fix in the blueprint system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Root cause categories:
  RC-01  missing-null-check   RC-06  contract-violation
  RC-02  type-confusion        RC-07  missing-validation
  RC-03  state-leak            RC-08  incomplete-cleanup
  RC-04  race-condition        RC-09  config-drift
  RC-05  boundary-error        RC-10  dependency-order

Module names match blueprint filenames (without .md):
  brains-adapters, brains-services, brains-schema, execution-guards,
  execution-orders, execution-reentry, risk-policies, risk-regime,
  risk-portfolio, feedback-performance, feedback-pnl, feedback-online,
  protocol-governance, protocol-parliament, protocol-services,
  contracts-domain, contracts-ids, deployment-config,
  deployment-lifecycle, features-rolling, features-service,
  runtime-live, runtime-state
        """,
    )
    parser.add_argument("--module", required=True, help="Module name (e.g., brains-adapters)")
    parser.add_argument("--fix-id", help="Fix ID (FIX-YYYYMMDD-NNN). Auto-generated if omitted.")
    parser.add_argument(
        "--type",
        dest="fix_type",
        default="fix",
        choices=["fix", "feat", "refactor", "perf", "security"],
    )
    parser.add_argument("--description", required=True, help="What was fixed")
    parser.add_argument(
        "--root-cause",
        required=True,
        choices=list(ROOT_CAUSE_MAP.keys()),
        help="Root cause category",
    )
    parser.add_argument("--files", required=True, help="Comma-separated file paths")
    parser.add_argument("--depends-on", default="", help="Comma-separated dependent modules")
    args = parser.parse_args()

    # Determine fix ID
    today = datetime.now(UTC).strftime("%Y%m%d")
    if args.fix_id:
        fix_id = args.fix_id
        if not validate_fix_id(fix_id):
            print(
                f"ERROR: Invalid fix ID format: {fix_id}. Expected: FIX-YYYYMMDD-NNN",
                file=sys.stderr,
            )
            return 1
    else:
        fix_id = _next_fix_id(today)

    author = _get_git_user()
    commit = _get_git_commit()
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")

    print(f"Registering {fix_id}: {args.description[:60]}...")

    ok = True
    if not append_to_module(
        args.module, fix_id, date_str, author, commit, args.description, args.root_cause
    ):
        ok = False
    if not append_to_registry(
        fix_id,
        date_str,
        author,
        commit,
        args.fix_type,
        args.module,
        args.files,
        args.description,
        args.root_cause,
        args.depends_on,
    ):
        ok = False

    if ok:
        print(f"\nFix {fix_id} registered successfully.")
        print(f"Commit message: {args.fix_type}({args.module}): [{fix_id}] {args.description}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
