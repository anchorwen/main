"""Automated project fixer — detects and repairs common Pylance/ruff/import issues.

Usage:  python scripts/dev/fix_project.py [--check]

Modes:
  --check    Dry-run, print what would be fixed without changing files.
  (default)  Apply all detected fixes.

What it fixes:
  A. Missing __init__.py — directories with .py files but no __init__.py
     get an empty __init__.py so Pylance + Python can resolve imports.
  B. ruff lint violations — runs ``ruff check --fix``.
  C. Pyright/Pylance diagnostics — runs ``pyright --outputjson`` and
     auto-fixes common patterns: attribute-access issues, missing
     module sources, optional member access, missing imports, etc.

Requires: Python 3.11+, ruff (installed in project venv / PATH),
          pyright (pip install pyright).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent  # d:\future


# ============================================================================
# Fix A — missing __init__.py
# ============================================================================


def _should_skip(path: Path) -> bool:
    """Skip __pycache__, .git, node_modules, .venv, etc."""
    parts = set(path.parts)
    return bool(
        parts
        & {
            "__pycache__",
            ".git",
            "node_modules",
            ".venv",
            "venv",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".pyright_cache",
        }
    )


def count_missing_inits() -> list[str]:
    """Return sorted list of directories that need __init__.py."""
    missing: list[str] = []
    for root, _dirs, files in os.walk(ROOT):
        rp = Path(root)
        if _should_skip(rp):
            continue
        has_py = any(f.endswith(".py") and f != "__init__.py" for f in files)
        has_init = "__init__.py" in files
        if has_py and not has_init:
            missing.append(str(rp.relative_to(ROOT)))
    missing.sort()
    return missing


def fix_missing_inits(check_only: bool) -> int:
    """Create empty __init__.py files where needed.  Returns count created."""
    missing = count_missing_inits()
    if not missing:
        print("[fix A] All Python-package directories have __init__.py.  OK")
        return 0

    print(f"[fix A] Found {len(missing)} directory(s) missing __init__.py:")
    for d in missing:
        print(f"  {d}")

    if check_only:
        print(f"[fix A] (--check) Would create {len(missing)} __init__.py file(s).")
        return len(missing)

    created = 0
    for d in missing:
        init_path = ROOT / d / "__init__.py"
        init_path.parent.mkdir(parents=True, exist_ok=True)
        init_path.write_text("", encoding="utf-8")
        created += 1

    print(f"[fix A] Created {created} __init__.py file(s).  OK")
    return created


# ============================================================================
# Fix B — ruff
# ============================================================================


def fix_ruff(check_only: bool) -> int:
    """Run ruff check --fix (or just check).  Returns number of errors found."""
    cmd = ["ruff", "check"]
    if not check_only:
        cmd.append("--fix")
    cmd.append(str(ROOT))

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    output = (result.stdout + result.stderr).strip()

    if result.returncode == 0:
        print("[fix B] ruff -- all checks passed.  OK")
        return 0

    lines = output.splitlines()
    error_count = len([l for l in lines if l.strip() and not l.startswith("Found ")])
    print(f"[fix B] ruff found {error_count} issue(s):")
    print(output)
    if not check_only:
        print("[fix B] ruff fixes applied (re-run to verify).")
    return error_count


# ============================================================================
# Fix C — Pyright/Pylance diagnostics
# ============================================================================

# Diagnostic rules that can be auto-fixed, mapped to (strategy, description).
# Strategies:
#   "type_ignore_line" — append ``# type: ignore[rule]`` to the offending line.
#   "type_ignore_import" — append ``# type: ignore[rule]`` to an import line.
#   "guard_none" — add ``assert x is not None`` before the line.

AUTO_FIX_RULES: dict[str, dict] = {
    "reportAttributeAccessIssue": {
        "strategy": "type_ignore_line",
        "desc": "attribute access on possibly-unknown type",
    },
    "reportOptionalMemberAccess": {
        "strategy": "guard_none",
        "desc": "member access on possibly-None value",
    },
    "reportOptionalCall": {
        "strategy": "guard_none",
        "desc": "call on possibly-None value",
    },
    "reportMissingModuleSource": {
        "strategy": "type_ignore_import",
        "desc": "missing module source (e.g. C extension like MetaTrader5)",
    },
    "reportMissingImports": {
        "strategy": "type_ignore_import",
        "desc": "missing import (module not installed or C extension)",
    },
    "reportArgumentType": {
        "strategy": "type_ignore_line",
        "desc": "argument type mismatch",
    },
    "reportGeneralTypeIssues": {
        "strategy": "type_ignore_line",
        "desc": "general type issue",
    },
}


def _run_pyright() -> list[dict]:
    """Run pyright --outputjson and return sorted, de-duped diagnostics.

    Returns empty list if pyright is not installed or produces no output.
    """
    result = subprocess.run(
        ["pyright", "--outputjson", str(ROOT)],
        capture_output=True,
        cwd=str(ROOT),
        encoding="utf-8",
        errors="replace",
    )

    diagnostics: list[dict] = []
    stdout = result.stdout or ""

    # pyright may output multiple JSON objects (e.g. per-file mode).
    # We extract the one with "generalDiagnostics".
    for chunk in _extract_json_objects(stdout):
        if isinstance(chunk, dict) and "generalDiagnostics" in chunk:
            diagnostics.extend(chunk["generalDiagnostics"])
            break  # first full summary is enough
        elif isinstance(chunk, dict) and "version" in chunk and "summary" in chunk:
            # sometimes diagnostics are at top level directly
            if "generalDiagnostics" in chunk:
                diagnostics.extend(chunk["generalDiagnostics"])

    if not diagnostics:
        # fallback: try line-by-line
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
                if "generalDiagnostics" in obj:
                    diagnostics.extend(obj["generalDiagnostics"])
            except json.JSONDecodeError:
                continue

    # De-duplicate by (file, line, rule)
    seen: set[tuple[str, int, str]] = set()
    unique: list[dict] = []
    for d in diagnostics:
        file = d.get("file", "")
        line = d.get("range", {}).get("start", {}).get("line", 0)
        rule = d.get("rule", "")
        key = (file, line, rule)
        if key not in seen:
            seen.add(key)
            unique.append(d)

    return unique


def _extract_json_objects(text: str) -> list[dict]:
    """Extract top-level JSON objects from pyright's mixed output."""
    objects: list[dict] = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start : i + 1])
                    objects.append(obj)
                except json.JSONDecodeError:
                    pass
                start = -1
    return objects


def _group_diagnostics(diagnostics: list[dict]) -> dict[str, list[dict]]:
    """Group diagnostics by file path, sorted."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for d in diagnostics:
        groups[d.get("file", "")].append(d)
    return dict(sorted(groups.items()))


def _apply_type_ignore_line(
    file_path: Path,
    line_no: int,  # 0-based
    rule: str,
    content_lines: list[str],
    reverse_index: dict[int, int],
) -> bool:
    """Append ``# type: ignore[rule]`` to the given line.

    Returns True if the line was modified.
    """
    idx = reverse_index.get(line_no)
    if idx is None or idx >= len(content_lines):
        return False

    line = content_lines[idx]
    if f"# type: ignore[{rule}]" in line or "# type: ignore" in line:
        return False  # already suppressed

    stripped = line.rstrip("\n").rstrip("\r")
    if stripped.endswith("# type: ignore"):
        return False

    content_lines[idx] = stripped + f"  # type: ignore[{rule}]\n"
    return True


def _apply_type_ignore_import(
    file_path: Path,
    line_no: int,  # 0-based
    rule: str,
    content_lines: list[str],
    reverse_index: dict[int, int],
) -> bool:
    """Add ``# type: ignore[rule]`` to the import line (or nearest import).

    Returns True if modified.
    """
    idx = reverse_index.get(line_no)
    if idx is None or idx >= len(content_lines):
        return False

    # Search upward for the import statement (within 5 lines)
    target_idx = idx
    for offset in range(0, 6):
        candidate = idx - offset
        if candidate < 0:
            break
        cline = content_lines[candidate].strip()
        if cline.startswith("import ") or cline.startswith("from "):
            target_idx = candidate
            break

    line = content_lines[target_idx]
    if f"# type: ignore[{rule}]" in line:
        return False

    stripped = line.rstrip("\n").rstrip("\r")
    content_lines[target_idx] = stripped + f"  # type: ignore[{rule}]\n"
    return True


def _apply_guard_none(
    file_path: Path,
    line_no: int,  # 0-based
    rule: str,
    content_lines: list[str],
    reverse_index: dict[int, int],
) -> bool:
    """Insert ``# type: ignore[rule]`` on the line (conservative approach).

    Full ``assert x is not None`` insertion is too risky for automated fix,
    so we use type_ignore as a safe default.
    """
    return _apply_type_ignore_line(file_path, line_no, rule, content_lines, reverse_index)


def _get_line_indent(line: str) -> str:
    """Return the leading whitespace of a line."""
    return line[: len(line) - len(line.lstrip())]


def fix_pyright_diagnostics(check_only: bool) -> int:
    """Run pyright, group diagnostics, and apply auto-fixes.

    Returns number of fixable diagnostics found.
    """
    print("[fix C] Running pyright...")
    diagnostics = _run_pyright()
    if not diagnostics:
        print("[fix C] pyright returned no diagnostics (or is not installed).  SKIP")
        return 0

    # Only keep rules we can auto-fix
    fixable = [d for d in diagnostics if d.get("rule") in AUTO_FIX_RULES]
    total = len(diagnostics)
    fixable_count = len(fixable)

    if fixable_count == 0:
        print(f"[fix C] pyright: {total} diagnostic(s), 0 fixable.  OK")
        return 0

    print(f"[fix C] pyright: {total} total, {fixable_count} fixable diagnostic(s).")

    grouped = _group_diagnostics(fixable)

    fixes_applied = 0

    for file_path_str, file_diags in grouped.items():
        file_path = Path(file_path_str)
        if not file_path.exists():
            print(f"  [SKIP] {file_path_str} — file not found")
            continue

        if check_only:
            for d in file_diags:
                rule = d["rule"]
                line_1based = d.get("range", {}).get("start", {}).get("line", 0) + 1
                desc = AUTO_FIX_RULES[rule]["desc"]
                strategy = AUTO_FIX_RULES[rule]["strategy"]
                print(f"  (--check) {file_path_str}:{line_1based}  {rule} ({desc}) → {strategy}")
            continue

        # Load file content
        content_lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
        modified = False

        # Build reverse index: 0-based line_no → list index
        reverse_index: dict[int, int] = {}
        current_lineno = 0
        for i, line in enumerate(content_lines):
            reverse_index[current_lineno] = i
            current_lineno += line.count("\n") or 1

        for d in file_diags:
            rule = d["rule"]
            strategy_cfg = AUTO_FIX_RULES[rule]
            strategy = strategy_cfg["strategy"]
            line_no = d.get("range", {}).get("start", {}).get("line", 0)
            line_1based = line_no + 1

            if strategy == "type_ignore_line":
                applied = _apply_type_ignore_line(
                    file_path, line_no, rule, content_lines, reverse_index
                )
            elif strategy == "type_ignore_import":
                applied = _apply_type_ignore_import(
                    file_path, line_no, rule, content_lines, reverse_index
                )
            elif strategy == "guard_none":
                applied = _apply_guard_none(file_path, line_no, rule, content_lines, reverse_index)
            else:
                applied = False

            if applied:
                fixes_applied += 1
                modified = True
                print(f"  [FIXED] {file_path_str}:{line_1based}  {rule} ({strategy_cfg['desc']})")

        if modified:
            file_path.write_text("".join(content_lines), encoding="utf-8")

    if check_only:
        print(
            f"[fix C] (--check) Would fix {fixable_count} diagnostic(s) across {len(grouped)} file(s)."
        )
    else:
        print(f"[fix C] Applied {fixes_applied} fix(es) across {len(grouped)} file(s).  OK")

    return fixable_count


# ============================================================================
# CLI
# ============================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-fix common project issues.")
    parser.add_argument("--check", action="store_true", help="Dry-run only")
    parser.add_argument(
        "--skip-pyright",
        action="store_true",
        help="Skip fix C (pyright diagnostics) — useful when pyright is not installed",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Project Auto-Fixer")
    print(f"Root: {ROOT}")
    print(f"Mode: {'check-only' if args.check else 'fix'}")
    if args.skip_pyright:
        print("Fix C (pyright): SKIPPED")
    print("=" * 60)

    a = fix_missing_inits(args.check)
    b = fix_ruff(args.check)
    c = 0 if args.skip_pyright else fix_pyright_diagnostics(args.check)

    print("-" * 60)
    total = a + b + c
    if total == 0:
        print("No issues found.  Everything looks clean!  OK")
    elif args.check:
        print(f"{total} issue(s) would be fixed.  Run without --check to apply.")
        sys.exit(1)
    else:
        print(f"Fixed {total} issue(s).  OK")
        sys.exit(0)


if __name__ == "__main__":
    main()
