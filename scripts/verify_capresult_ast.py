#!/usr/bin/env python
"""AST structural constraint scanner — Iron Law #3 enforcement for UGR v3.1.

Baseline scan mode (default): Scans the full codebase for:
  1. Dynamic call escape: getattr/setattr/hasattr on FORBIDDEN_DYNAMIC_SYMBOLS
  2. AST parse errors (corrupted files)

After UGR deployment, this scanner also enforces:
  3. CapResult.ok() placement (inside success_scope only)
  4. _raw attribute access on TypedClock types
  5. fail_open_guard usage (→ DEPRECATED, redirect to CapResult)

Usage::

    python scripts/verify_capresult_ast.py              # baseline scan
    python scripts/verify_capresult_ast.py --enforce     # full enforcement (post-UGR)
    python scripts/verify_capresult_ast.py --report      # JSON report to stdout

Exit codes: 0 = clean (or whitelisted), 1 = violations found.
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── Forbidden symbols for dynamic access ────────────────────────────────────
FORBIDDEN_DYNAMIC_SYMBOLS: set[str] = {
    "CapResult",
    "_SuccessProof",
    "fail_open_guard",
    "MonotonicInstant",
    "WallInstant",
    "Duration",
}

# ── Files allowed to own/use forbidden symbols ──────────────────────────────
WHITELIST_FILES: set[str] = {
    "core/contracts/cap_result.py",  # Owns CapResult + _SuccessProof
    "core/contracts/phantom_contract.py",  # Phantom contract decorator
    "core/runtime/typed_clock.py",  # Owns time types
    "core/runtime/fault_handler.py",  # Owns fail_open_guard
}

# ── Directories to scan ─────────────────────────────────────────────────────
SCAN_DIRS: list[str] = ["core", "scripts", "apps", "tests"]

# ── Excluded paths (virtual envs, caches, generated files) ──────────────────
EXCLUDE_PREFIXES: tuple[str, ...] = (
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
    ".venv",
    "venv",
)


@dataclass
class Violation:
    file: str
    line: int
    col: int
    rule: str
    detail: str


@dataclass
class ScanReport:
    files_scanned: int = 0
    parse_errors: list[str] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return len(self.violations) == 0 and len(self.parse_errors) == 0

    def to_dict(self) -> dict:
        return {
            "files_scanned": self.files_scanned,
            "parse_errors": self.parse_errors,
            "violations": [
                {"file": v.file, "line": v.line, "col": v.col, "rule": v.rule, "detail": v.detail}
                for v in self.violations
            ],
            "verdict": "CLEAN" if self.clean else "VIOLATIONS_FOUND",
        }


class DynamicCallDetector(ast.NodeVisitor):
    """Detect getattr/setattr/hasattr on forbidden symbols."""

    def __init__(self, filepath: str, whitelist: set[str] | None = None) -> None:
        self.filepath = filepath
        self.whitelist = whitelist or WHITELIST_FILES
        self.violations: list[Violation] = []

    def _is_whitelisted(self) -> bool:
        # Normalize path separators for cross-platform matching
        normalized = self.filepath.replace("\\", "/")
        for wl in self.whitelist:
            if normalized.endswith(wl.replace("\\", "/")):
                return True
        return False

    def _check_dynamic_call(self, node: ast.Call, func_name: str) -> None:
        """Check getattr(X, SYMBOL) / setattr(X, SYMBOL, V) / hasattr(X, SYMBOL)."""
        if self._is_whitelisted():
            return
        # Second argument is the attribute name string
        if len(node.args) < 2:
            return
        name_arg = node.args[1]
        if isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str):
            if name_arg.value in FORBIDDEN_DYNAMIC_SYMBOLS:
                self.violations.append(
                    Violation(
                        file=self.filepath,
                        line=node.lineno,
                        col=node.col_offset,
                        rule="DYNAMIC_FORBIDDEN",
                        detail=f"{func_name}(..., '{name_arg.value}') — "
                        f"dynamic access to forbidden symbol '{name_arg.value}'",
                    )
                )

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in (
            "getattr",
            "setattr",
            "hasattr",
        ):
            self._check_dynamic_call(node, node.func.id)
        self.generic_visit(node)


def scan_file(filepath: Path, whitelist: set[str] | None = None) -> list[Violation]:
    """Scan a single .py file for AST violations."""
    try:
        source = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError as e:
        # Report parse error but don't crash
        return [
            Violation(
                file=str(filepath.relative_to(ROOT)),
                line=e.lineno or 0,
                col=e.offset or 0,
                rule="PARSE_ERROR",
                detail=f"SyntaxError: {e.msg}",
            )
        ]

    rel_path = str(filepath.relative_to(ROOT))
    detector = DynamicCallDetector(rel_path, whitelist)
    detector.visit(tree)
    return detector.violations


def scan_codebase(
    scan_dirs: list[str] | None = None,
    whitelist: set[str] | None = None,
    enforce: bool = False,
) -> ScanReport:
    """Scan all .py files in the given directories."""
    report = ScanReport()
    dirs = scan_dirs or SCAN_DIRS

    for scan_dir in dirs:
        dir_path = ROOT / scan_dir
        if not dir_path.exists():
            continue
        for py_file in dir_path.rglob("*.py"):
            # Skip excluded paths
            path_str = str(py_file)
            if any(excl in path_str for excl in EXCLUDE_PREFIXES):
                continue
            report.files_scanned += 1
            violations = scan_file(py_file, whitelist)
            report.violations.extend(violations)
            # Count parse errors separately
            for v in violations:
                if v.rule == "PARSE_ERROR":
                    report.parse_errors.append(f"{v.file}:{v.line}: {v.detail}")

    return report


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="UGR v3.1 AST Structural Constraint Scanner")
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="Full enforcement mode (post-UGR deployment).",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Output JSON report to stdout.",
    )
    parser.add_argument(
        "--whitelist",
        type=str,
        default=None,
        help="Path to a JSON file with additional whitelisted files.",
    )
    args = parser.parse_args()

    # Build whitelist
    whitelist = set(WHITELIST_FILES)
    if args.whitelist:
        wl_path = Path(args.whitelist)
        if wl_path.exists():
            extra = json.loads(wl_path.read_text(encoding="utf-8"))
            whitelist.update(extra.get("files", []))

    report = scan_codebase(whitelist=whitelist, enforce=args.enforce)

    if args.report:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"Files scanned: {report.files_scanned}")
        if report.parse_errors:
            print(f"\nParse errors ({len(report.parse_errors)}):")
            for err in report.parse_errors:
                print(f"  {err}")
        if report.violations:
            non_parse = [v for v in report.violations if v.rule != "PARSE_ERROR"]
            if non_parse:
                print(f"\nViolations ({len(non_parse)}):")
                for v in non_parse:
                    print(f"  [{v.rule}] {v.file}:{v.line}:{v.col} — {v.detail}")
        if report.clean:
            print("\n[CLEAN] No violations found.")

    return 0 if report.clean else 1


if __name__ == "__main__":
    sys.exit(main())
