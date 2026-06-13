#!/usr/bin/env python
"""Import boundary enforcement — structural air-gap between system layers.

Iron Law 3: One-Way Dependency Air-Gap.  Physical (not procedural) enforcement
of import direction rules.  Violations are hard errors, not warnings.

Rules:
  1. scripts/training/  MUST NOT import core.execution.*
  2. core/execution/    MUST NOT import ML/DS libraries (pandas, sklearn, xgb, lgb, torch, tf)
  3. core/runtime/      MUST NOT import scripts.training.*
  4. core/features/     MUST NOT import scripts.*

Usage:
  python scripts/check_import_boundaries.py          # check all rules
  python scripts/check_import_boundaries.py --quiet  # exit code only
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Violation(NamedTuple):
    file: str
    rule: str
    detail: str


# ═══════════════════════════════════════════════════════════════════════════════
# Rule definitions
# ═══════════════════════════════════════════════════════════════════════════════

RULES = [
    {
        "name": "training-never-imports-execution",
        "description": "scripts/training/ MUST NOT import core.execution.*",
        "source_glob": "scripts/training/**/*.py",
        "forbidden": ("core.execution",),
        "type": "module_prefix",
    },
    {
        "name": "execution-never-imports-ml-libs",
        "description": "core/execution/ MUST NOT import pandas/sklearn/xgboost/lightgbm/torch/tensorflow",
        "source_glob": "core/execution/**/*.py",
        "forbidden": (
            "pandas", "sklearn", "xgboost", "lightgbm",
            "matplotlib", "torch", "tensorflow", "keras",
        ),
        "type": "module_prefix",
    },
    {
        "name": "runtime-never-imports-training",
        "description": "core/runtime/ MUST NOT import scripts.training.*",
        "source_glob": "core/runtime/**/*.py",
        "forbidden": ("scripts.training",),
        "type": "module_prefix",
    },
    {
        "name": "features-never-imports-scripts",
        "description": "core/features/ MUST NOT import scripts.*",
        "source_glob": "core/features/**/*.py",
        "forbidden": ("scripts.",),
        "type": "module_prefix",
    },
]

# Known exceptions (documented, with justification)
KNOWN_EXCEPTIONS: dict[str, set[str]] = {
    # MetaFilter requires LGB model inference in live trading path.
    # This is an architectural choice, not an accidental leak.
    # TODO: extract MetaFilter into a separate ZMQ sub-process (Phase 2).
    "core/execution/meta_exit_engine.py": {"lightgbm"},
    "core/execution/meta_signal_filter.py": {"lightgbm"},
    # Daily ops scheduler invokes governance cycle (lazy import inside function).
    # TODO: move governance_scheduler.py and daily_ops.py to core/ module.
    "core/runtime/daily_ops_scheduler.py": {"scripts"},
}


def check_file(file_path: Path, rule: dict) -> list[Violation]:
    """Check a single file against a single rule."""
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []

    violations: list[Violation] = []
    rel_path = str(file_path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    known = KNOWN_EXCEPTIONS.get(rel_path, set())

    for node in ast.walk(tree):
        imported: str | None = None

        if isinstance(node, ast.Import):
            for alias in node.names:
                imported = alias.name
                for forbidden in rule["forbidden"]:
                    if imported.startswith(forbidden) and imported.split(".")[0] not in known:
                        violations.append(
                            Violation(rel_path, rule["name"], f"imports {imported}")
                        )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported = node.module
                for forbidden in rule["forbidden"]:
                    if imported.startswith(forbidden) and imported.split(".")[0] not in known:
                        violations.append(
                            Violation(rel_path, rule["name"], f"from {imported} import ...")
                        )

    return violations


def check_all() -> list[Violation]:
    """Run all rules against the project. Returns list of violations."""
    all_violations: list[Violation] = []

    for rule in RULES:
        for py_file in PROJECT_ROOT.glob(rule["source_glob"]):
            if py_file.is_file():
                all_violations.extend(check_file(py_file, rule))

    return all_violations


def main(argv: list[str] | None = None) -> int:
    quiet = "--quiet" in (argv or sys.argv)

    violations = check_all()

    if violations:
        if not quiet:
            print(f"[import-linter] {len(violations)} import boundary violation(s):")
            by_rule: dict[str, list[Violation]] = {}
            for v in violations:
                by_rule.setdefault(v.rule, []).append(v)
            for rule_name, vlist in by_rule.items():
                print(f"\n  Rule: {rule_name}")
                for v in vlist:
                    print(f"    {v.file}: {v.detail}")
            print(f"\n[import-linter] FAIL — {len(violations)} violation(s)")
        return 1

    if not quiet:
        print("[import-linter] PASS — all import boundaries enforced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
