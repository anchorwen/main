#!/usr/bin/env python
"""AST structural constraint scanner — Iron Law #3 enforcement for UGR v3.1.

UGR-B03: Full enforcement mode with 5 detectors (upgraded from baseline).

Detectors:
  1. DynamicCallDetector     — getattr/setattr/hasattr on FORBIDDEN_DYNAMIC_SYMBOLS
  2. CapResultOkPlacement    — CapResult.ok() must be inside Kernel.success_scope()
  3. RawAccessDetector       — ._raw access on TypedClock types (whitelist-gated)
  4. FailOpenGuardDetector   — fail_open_guard / log_and_continue (DEPRECATED)
  5. ProofLeakDetector       — _SuccessProof assignment outside success_scope()

Usage::

    python scripts/verify_capresult_ast.py              # baseline scan (detector 1 only)
    python scripts/verify_capresult_ast.py --enforce     # full enforcement (all 5 detectors)
    python scripts/verify_capresult_ast.py --report      # JSON report to stdout
    python scripts/verify_capresult_ast.py --enforce --report  # both

Exit codes: 0 = clean (or whitelisted), 1 = violations found.
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

# ── Whitelist: files allowed to own/use forbidden symbols ───────────────────
WHITELIST_FILES: set[str] = {
    "core/contracts/cap_result.py",  # Owns CapResult + _SuccessProof + Kernel
    "core/contracts/phantom_contract.py",  # Phantom contract decorator
    "core/runtime/typed_clock.py",  # Owns time types
    "core/runtime/fault_handler.py",  # Owns fail_open_guard + log_and_continue
}

# ── Additional whitelists per detector ──────────────────────────────────────
# Files allowed to access ._raw on TypedClock types
# (owner module + documented bridge + test that verifies CI enforcement layer)
RAW_ACCESS_WHITELIST: set[str] = {
    "core/runtime/typed_clock.py",  # Owns the types
    "core/contracts/adapters.py",  # bridge_mono_to_float — documented bridge
    "tests/runtime/test_typed_clock.py",  # Tests object.__setattr__ bypass (CI-enforced)
}

# Files allowed to use CapResult.ok() outside success_scope()
# (defining module for internal map/flat_map/try_operation calls;
#  test file that intentionally calls ok() with expired proof to verify rejection)
CAPRESULT_OK_WHITELIST: set[str] = {
    "core/contracts/cap_result.py",
    "tests/contracts/test_cap_result.py",
}

# Files allowed to call fail_open_guard / log_and_continue
# (only the defining module — DEPRECATED elsewhere)
FAIL_OPEN_WHITELIST: set[str] = {
    "core/runtime/fault_handler.py",
}

# Files allowed to handle _SuccessProof lifecycle
PROOF_LEAK_WHITELIST: set[str] = {
    "core/contracts/cap_result.py",
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_scanned": self.files_scanned,
            "parse_errors": self.parse_errors,
            "violations": [
                {
                    "file": v.file,
                    "line": v.line,
                    "col": v.col,
                    "rule": v.rule,
                    "detail": v.detail,
                }
                for v in self.violations
            ],
            "verdict": "CLEAN" if self.clean else "VIOLATIONS_FOUND",
        }


# ═══════════════════════════════════════════════════════════════════════════
# Helper: normalise path for cross-platform whitelist matching
# ═══════════════════════════════════════════════════════════════════════════


def _path_in_whitelist(filepath: str, whitelist: set[str]) -> bool:
    """Check whether *filepath* (relative to ROOT) matches any whitelist entry."""
    normalized = filepath.replace("\\", "/")
    for wl in whitelist:
        if normalized.endswith(wl.replace("\\", "/")):
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# Detector 1: DynamicCallDetector (baseline — always runs)
# ═══════════════════════════════════════════════════════════════════════════


class DynamicCallDetector(ast.NodeVisitor):
    """Detect getattr/setattr/hasattr on forbidden symbols."""

    def __init__(self, filepath: str, whitelist: set[str] | None = None) -> None:
        self.filepath = filepath
        self.whitelist = whitelist or WHITELIST_FILES
        self.violations: list[Violation] = []

    def _is_whitelisted(self) -> bool:
        return _path_in_whitelist(self.filepath, self.whitelist)

    def _check_dynamic_call(self, node: ast.Call, func_name: str) -> None:
        """Check getattr(X, SYMBOL) / setattr(X, SYMBOL, V) / hasattr(X, SYMBOL)."""
        if self._is_whitelisted():
            return
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


# ═══════════════════════════════════════════════════════════════════════════
# Detector 2: CapResultOkPlacementDetector (--enforce)
# ═══════════════════════════════════════════════════════════════════════════


class CapResultOkPlacementDetector(ast.NodeVisitor):
    """Verify CapResult.ok(value, proof) is inside Kernel.success_scope().

    Tracks the active ``with Kernel.success_scope() as <var>:`` context
    and flags any ``CapResult.ok(...)`` call outside that scope.
    """

    def __init__(self, filepath: str, whitelist: set[str] | None = None) -> None:
        self.filepath = filepath
        self.whitelist = whitelist or CAPRESULT_OK_WHITELIST
        self.violations: list[Violation] = []
        # Stack of scope variable names assigned by enclosing success_scope() with blocks
        self._scope_var_stack: list[str] = []

    def _is_whitelisted(self) -> bool:
        return _path_in_whitelist(self.filepath, self.whitelist)

    def _is_success_scope_call(self, node: ast.expr) -> str | None:
        """If *node* is a ``Kernel.success_scope()`` or ``Kernel().success_scope()``
        call, return the canonical method name 'success_scope'; else None."""
        if not isinstance(node, ast.Call):
            return None
        # Match: <expr>.success_scope()
        if not isinstance(node.func, ast.Attribute):
            return None
        if node.func.attr != "success_scope":
            return None
        # The base must be Kernel or Kernel()
        base = node.func.value
        if isinstance(base, ast.Name) and base.id == "Kernel":
            return "success_scope"
        if isinstance(base, ast.Call):
            if isinstance(base.func, ast.Name) and base.func.id == "Kernel":
                return "success_scope"
        return None

    def _get_scope_var_name(self, item: ast.withitem) -> str | None:
        """If *item* binds a variable name, return it."""
        if item.optional_vars is not None and isinstance(item.optional_vars, ast.Name):
            return item.optional_vars.id
        return None

    def visit_With(self, node: ast.With) -> None:
        # Check if any withitem is a success_scope call
        entered_vars: list[str] = []
        for item in node.items:
            if self._is_success_scope_call(item.context_expr):
                var_name = self._get_scope_var_name(item)
                if var_name:
                    self._scope_var_stack.append(var_name)
                    entered_vars.append(var_name)
        # Visit body inside the scope
        self.generic_visit(node)
        # Pop scopes we pushed (in reverse order)
        for _ in entered_vars:
            self._scope_var_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_whitelisted():
            self.generic_visit(node)
            return
        # Check if this is CapResult.ok(value, proof).
        # Only catches the classmethod pattern CapResult.ok(...) — instance
        # methods (.map / .flat_map) cannot be statically type-checked at
        # the AST level because the receiver type is unknown.
        if isinstance(node.func, ast.Attribute):
            method_name = node.func.attr
            if method_name == "ok":
                base = node.func.value
                base_name = None
                if isinstance(base, ast.Name):
                    base_name = base.id
                elif isinstance(base, ast.Attribute):
                    base_name = base.attr
                if base_name == "CapResult":
                    # Verify we are inside a success_scope() context
                    if not self._scope_var_stack:
                        self.violations.append(
                            Violation(
                                file=self.filepath,
                                line=node.lineno,
                                col=node.col_offset,
                                rule="CAPRESULT_OK_OUTSIDE_SCOPE",
                                detail="CapResult.ok() must be called inside "
                                "'with Kernel.success_scope() as proof:'",
                            )
                        )
        self.generic_visit(node)


# ═══════════════════════════════════════════════════════════════════════════
# Detector 3: RawAccessDetector (--enforce)
# ═══════════════════════════════════════════════════════════════════════════


class RawAccessDetector(ast.NodeVisitor):
    """Detect ._raw attribute access on TypedClock types outside whitelist.

    MonotonicInstant, WallInstant, and Duration use ``._raw`` as an
    internal slot.  Direct access from outside ``typed_clock.py``
    (or whitelisted bridge adapters) is a type-safety violation.
    """

    # TypedClock type names whose ._raw is protected
    PROTECTED_TYPES: set[str] = {"MonotonicInstant", "WallInstant", "Duration"}

    def __init__(self, filepath: str, whitelist: set[str] | None = None) -> None:
        self.filepath = filepath
        self.whitelist = whitelist or RAW_ACCESS_WHITELIST
        self.violations: list[Violation] = []

    def _is_whitelisted(self) -> bool:
        return _path_in_whitelist(self.filepath, self.whitelist)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self._is_whitelisted():
            self.generic_visit(node)
            return
        if node.attr == "_raw":
            # Best-effort: check if the object is a TypedClock type.
            # We can't fully resolve types at AST level, so we flag ALL
            # ._raw accesses outside the whitelist.  If a non-TypedClock
            # type happens to have a ._raw attribute, it can be added to
            # the whitelist or suppressed with a comment.
            self.violations.append(
                Violation(
                    file=self.filepath,
                    line=node.lineno,
                    col=node.col_offset,
                    rule="RAW_ACCESS",
                    detail="._raw access on potential TypedClock type — "
                    "use public API (total_seconds(), etc.) or go through "
                    "whitelisted bridge functions in adapters.py",
                )
            )
        self.generic_visit(node)


# ═══════════════════════════════════════════════════════════════════════════
# Detector 4: FailOpenGuardDetector (--enforce)
# ═══════════════════════════════════════════════════════════════════════════


class FailOpenGuardDetector(ast.NodeVisitor):
    """Detect fail_open_guard / log_and_continue usage (DEPRECATED).

    These are legacy error-swallowing patterns from UGR v2.
    UGR-A07 marked them as DEPRECATED; UGR-A09 will replace them all.
    This detector provides CI visibility into remaining call sites.
    """

    DEPRECATED_FUNCTIONS: set[str] = {"fail_open_guard", "log_and_continue"}

    def __init__(self, filepath: str, whitelist: set[str] | None = None) -> None:
        self.filepath = filepath
        self.whitelist = whitelist or FAIL_OPEN_WHITELIST
        self.violations: list[Violation] = []

    def _is_whitelisted(self) -> bool:
        return _path_in_whitelist(self.filepath, self.whitelist)

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_whitelisted():
            self.generic_visit(node)
            return
        func_name = None
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
        if func_name in self.DEPRECATED_FUNCTIONS:
            self.violations.append(
                Violation(
                    file=self.filepath,
                    line=node.lineno,
                    col=node.col_offset,
                    rule="DEPRECATED_RESILIENCE",
                    detail=f"{func_name}() is DEPRECATED (UGR-A07) — "
                    f"migrate to specific exception handling or CapResult.match()",
                )
            )
        self.generic_visit(node)


# ═══════════════════════════════════════════════════════════════════════════
# Detector 5: ProofLeakDetector (--enforce)
# ═══════════════════════════════════════════════════════════════════════════


class ProofLeakDetector(ast.NodeVisitor):
    """Detect _SuccessProof variable assignment outside success_scope().

    A ``_SuccessProof`` (bound as ``proof`` in ``with ... as proof:``)
    must never be stored in a persistent location — doing so would allow
    ``CapResult.ok()`` with an expired/invalid proof after scope exit.

    This detector flags:
    - ``self.xxx = proof`` (attribute store)
    - ``container[key] = proof`` (subscript store)
    - ``global_var = proof`` (if var bound by success_scope)

    within the body of a ``with Kernel.success_scope() as proof:`` block.
    """

    def __init__(self, filepath: str, whitelist: set[str] | None = None) -> None:
        self.filepath = filepath
        self.whitelist = whitelist or PROOF_LEAK_WHITELIST
        self.violations: list[Violation] = []
        # Stack of sets of protected variable names for each nesting level
        self._protected_vars_stack: list[set[str]] = []

    def _is_whitelisted(self) -> bool:
        return _path_in_whitelist(self.filepath, self.whitelist)

    @staticmethod
    def _is_success_scope_call(node: ast.expr) -> str | None:
        """Same logic as CapResultOkPlacementDetector."""
        if not isinstance(node, ast.Call):
            return None
        if not isinstance(node.func, ast.Attribute):
            return None
        if node.func.attr != "success_scope":
            return None
        base = node.func.value
        if isinstance(base, ast.Name) and base.id == "Kernel":
            return "success_scope"
        if isinstance(base, ast.Call):
            if isinstance(base.func, ast.Name) and base.func.id == "Kernel":
                return "success_scope"
        return None

    @staticmethod
    def _get_scope_var_name(item: ast.withitem) -> str | None:
        if item.optional_vars is not None and isinstance(item.optional_vars, ast.Name):
            return item.optional_vars.id
        return None

    def _is_proof_leak(self, target: ast.expr, protected: set[str]) -> str | None:
        """If *target* stores into a persistent location, return the variable name.

        Persistent locations:
        - ``self.xxx = proof``  →  attribute on self/cls
        - ``obj.attr = proof``  →  attribute on any object
        - ``container[key] = proof`` → subscript store
        """
        if isinstance(target, ast.Attribute):
            # self.xxx = proof  or  obj.attr = proof
            return "attribute_store"
        if isinstance(target, ast.Subscript):
            # container[key] = proof
            return "subscript_store"
        return None

    def visit_With(self, node: ast.With) -> None:
        # Collect protected variable names from success_scope() withitems
        new_protected: set[str] = set()
        for item in node.items:
            if self._is_success_scope_call(item.context_expr):
                var_name = self._get_scope_var_name(item)
                if var_name:
                    new_protected.add(var_name)
        self._protected_vars_stack.append(new_protected)
        self.generic_visit(node)
        self._protected_vars_stack.pop()

    def _get_active_protected(self) -> set[str]:
        """Union of all protected variables in current scope stack."""
        result: set[str] = set()
        for s in self._protected_vars_stack:
            result |= s
        return result

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._is_whitelisted():
            self.generic_visit(node)
            return
        protected = self._get_active_protected()
        if not protected:
            self.generic_visit(node)
            return
        # Check if the value being assigned is a protected variable
        if isinstance(node.value, ast.Name) and node.value.id in protected:
            for target in node.targets:
                leak_type = self._is_proof_leak(target, protected)
                if leak_type:
                    self.violations.append(
                        Violation(
                            file=self.filepath,
                            line=node.lineno,
                            col=node.col_offset,
                            rule="PROOF_LEAK",
                            detail=f"'{node.value.id}' (success_scope proof) stored in "
                            f"{leak_type} — proof must not outlive its scope",
                        )
                    )
        # Also check for container[key] = proof (one target, single assignment)
        # (handled above via node.targets iteration)
        self.generic_visit(node)


# ═══════════════════════════════════════════════════════════════════════════
# Scan orchestration
# ═══════════════════════════════════════════════════════════════════════════


def _collect_violations(
    detector_cls: type, filepath: Path, rel_path: str, **kwargs: Any
) -> list[Violation]:
    """Instantiate *detector_cls*, visit the AST of *filepath*, return violations."""
    try:
        source = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError as e:
        return [
            Violation(
                file=rel_path,
                line=e.lineno or 0,
                col=e.offset or 0,
                rule="PARSE_ERROR",
                detail=f"SyntaxError: {e.msg}",
            )
        ]
    detector = detector_cls(rel_path, **kwargs)
    detector.visit(tree)
    return detector.violations


def scan_file(
    filepath: Path,
    whitelist: set[str] | None = None,
    enforce: bool = False,
) -> list[Violation]:
    """Scan a single .py file for AST violations.

    In baseline mode (enforce=False), only runs DynamicCallDetector.
    In enforce mode, runs all 5 detectors.
    """
    try:
        rel_path = str(filepath.relative_to(ROOT))
    except ValueError:
        # filepath is not under ROOT (e.g. in test tmp_path)
        rel_path = str(filepath)

    # Detector 1: always runs (baseline)
    violations = _collect_violations(DynamicCallDetector, filepath, rel_path, whitelist=whitelist)

    if enforce:
        # Detector 2: CapResult.ok() placement
        violations.extend(_collect_violations(CapResultOkPlacementDetector, filepath, rel_path))
        # Detector 3: ._raw access
        violations.extend(_collect_violations(RawAccessDetector, filepath, rel_path))
        # Detector 4: DEPRECATED fail_open_guard / log_and_continue
        violations.extend(_collect_violations(FailOpenGuardDetector, filepath, rel_path))
        # Detector 5: proof leak
        violations.extend(_collect_violations(ProofLeakDetector, filepath, rel_path))

    return violations


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
            violations = scan_file(py_file, whitelist=whitelist, enforce=enforce)
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
        help="Full enforcement mode — all 5 detectors (UGR-B03).",
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
        mode = "ENFORCE (5 detectors)" if args.enforce else "BASELINE (1 detector)"
        print(f"Mode: {mode}")
        print(f"Files scanned: {report.files_scanned}")
        if report.parse_errors:
            print(f"\nParse errors ({len(report.parse_errors)}):")
            for err in report.parse_errors:
                print(f"  {err}")
        if report.violations:
            # Group by rule for cleaner output
            non_parse = [v for v in report.violations if v.rule != "PARSE_ERROR"]
            if non_parse:
                # Count by rule
                from collections import Counter

                rule_counts = Counter(v.rule for v in non_parse)
                print(f"\nViolations ({len(non_parse)} total):")
                for rule, count in sorted(rule_counts.items()):
                    print(f"  [{rule}]: {count}")
                print()
                for v in non_parse:
                    print(f"  [{v.rule}] {v.file}:{v.line}:{v.col} — {v.detail}")
        if report.clean:
            print("\n[CLEAN] No violations found.")

    return 0 if report.clean else 1


if __name__ == "__main__":
    sys.exit(main())
