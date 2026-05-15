#!/usr/bin/env python3
"""Analyze cross-module dependencies for impact assessment.

Usage:
    python scripts/analyze_deps.py <module-name>
    python scripts/analyze_deps.py --list    # List all modules
    python scripts/analyze_deps.py --graph   # Print full dependency graph

Module names match blueprint filenames (without .md prefix).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPS_FILE = ROOT / "blueprints" / "system" / "DEPENDENCY_GRAPH.md"

# Dependency data from DEPENDENCY_GRAPH.md — kept in sync manually.
# Format: module → list of modules it imports from
DEPENDENCY_MAP: dict[str, list[str]] = {
    "runtime-live": [
        "alpha",
        "brains-adapters",
        "brains-services",
        "contracts-domain",
        "contracts-ids",
        "deployment-config",
        "deployment-lifecycle",
        "execution-guards",
        "execution-orders",
        "execution-reentry",
        "features-service",
        "feedback-performance",
        "feedback-pnl",
        "feedback-online",
        "protocol-governance",
        "protocol-parliament",
        "protocol-services",
        "risk-policies",
        "risk-regime",
        "risk-portfolio",
        "runtime-state",
        "strategies",
    ],
    "deployment-config": [
        "brains-adapters",
        "brains-services",
        "contracts-domain",
        "execution-orders",
        "features-service",
        "feedback-performance",
        "protocol-governance",
        "protocol-services",
        "risk-policies",
        "runtime-state",
        "deployment-lifecycle",
    ],
    "deployment-lifecycle": [
        "contracts-domain",
        "contracts-ids",
        "runtime-state",
    ],
    "execution-orders": [
        "brains-services",
        "contracts-domain",
        "contracts-ids",
        "deployment-config",
        "deployment-lifecycle",
        "protocol-services",
        "risk-portfolio",
        "runtime-live",
    ],
    "execution-guards": [
        "contracts-domain",
    ],
    "execution-reentry": [],
    "brains-adapters": [
        "contracts-domain",
        "contracts-ids",
        "brains-schema",
        "brains-services",
    ],
    "brains-services": [
        "brains-adapters",
        "contracts-domain",
        "features-service",
        "feedback-performance",
        "feedback-pnl",
    ],
    "brains-schema": [],
    "protocol-services": [
        "contracts-domain",
        "contracts-ids",
        "execution-orders",
    ],
    "protocol-governance": [
        "contracts-domain",
        "contracts-ids",
    ],
    "protocol-parliament": [
        "brains-schema",
        "contracts-domain",
    ],
    "risk-policies": [
        "contracts-domain",
        "contracts-ids",
    ],
    "risk-regime": [],
    "risk-portfolio": [
        "contracts-domain",
        "risk-policies",
    ],
    "feedback-performance": [],
    "feedback-pnl": [],
    "feedback-online": [
        "features-service",
        "brains-adapters",
    ],
    "features-service": [
        "contracts-domain",
        "contracts-ids",
    ],
    "features-rolling": [],
    "contracts-domain": [],
    "contracts-ids": [],
    "runtime-state": [
        "contracts-domain",
    ],
}

# Reverse index: who depends on me
_REVERSE_MAP: dict[str, list[str]] | None = None


def _build_reverse_map() -> dict[str, list[str]]:
    global _REVERSE_MAP
    if _REVERSE_MAP is not None:
        return _REVERSE_MAP
    _REVERSE_MAP = {}
    for module, deps in DEPENDENCY_MAP.items():
        for dep in deps:
            _REVERSE_MAP.setdefault(dep, []).append(module)
    return _REVERSE_MAP


def print_module_deps(module: str) -> None:
    """Print inbound and outbound dependencies for a module."""
    if module not in DEPENDENCY_MAP:
        print(f"ERROR: Unknown module '{module}'", file=sys.stderr)
        print("Available modules:", file=sys.stderr)
        for m in sorted(DEPENDENCY_MAP):
            print(f"  {m}", file=sys.stderr)
        sys.exit(1)

    reverse = _build_reverse_map()

    print(f"=== {module} ===")
    print()
    print("Imports from (outbound):")
    deps = DEPENDENCY_MAP.get(module, [])
    if deps:
        for d in sorted(deps):
            print(f"  <- {d}")
        print(f"  Total: {len(deps)} dependencies")
    else:
        print("  (none - leaf module)")
    print()

    print("Depended on by (inbound):")
    dependents = reverse.get(module, [])
    if dependents:
        for d in sorted(dependents):
            print(f"  -> {d}")
        print(f"  Total: {len(dependents)} dependents")
    else:
        print("  (none - terminal module)")
    print()

    # Impact analysis
    if dependents:
        print("IMPACT ANALYSIS: Changing this module affects:")
        for d in sorted(dependents):
            print(f"  [!] {d}")
        print()
        print("Before modifying this module, review:")
        for d in sorted(dependents):
            print(f"  1. Read blueprints/modules/{d}.md -> Cross-Module Contracts")
        print(f"  2. Check {module} -> {', '.join(sorted(dependents))} contracts")


def list_modules() -> None:
    """List all known modules."""
    print("Available modules:")
    for m in sorted(DEPENDENCY_MAP):
        deps = DEPENDENCY_MAP[m]
        rev = _build_reverse_map().get(m, [])
        print(f"  {m:<30}  <- {len(deps):>2} deps  -> {len(rev):>2} dependents")


def print_full_graph() -> None:
    """Print the full dependency graph in ASCII format."""
    reverse = _build_reverse_map()

    # Sort by dependency depth (leaf first)
    def _depth(m: str, visited: set | None = None) -> int:
        if visited is None:
            visited = set()
        if m in visited:
            return 0
        visited.add(m)
        deps = DEPENDENCY_MAP.get(m, [])
        if not deps:
            return 1
        return 1 + max(_depth(d, visited) for d in deps)

    modules_by_depth = sorted(DEPENDENCY_MAP, key=lambda m: _depth(m, set()))

    print("Dependency Graph (leaf → hub):")
    print("=" * 60)
    for m in modules_by_depth:
        depth = _depth(m, set())
        indent = "  " * depth
        deps = DEPENDENCY_MAP.get(m, [])
        rev = reverse.get(m, [])
        dep_str = ", ".join(sorted(deps)[:5])
        if len(deps) > 5:
            dep_str += f", ... (+{len(deps) - 5})"
        rev_str = ", ".join(sorted(rev)[:3])
        if len(rev) > 3:
            rev_str += f", ... (+{len(rev) - 3})"
        print(f"{indent}{m}")
        if deps:
            print(f"{indent}  <- depends on: [{dep_str}]")
        if rev:
            print(f"{indent}  -> used by:    [{rev_str}]")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze cross-module dependencies")
    parser.add_argument("module", nargs="?", help="Module name to analyze")
    parser.add_argument("--list", action="store_true", help="List all modules")
    parser.add_argument("--graph", action="store_true", help="Print full dependency graph")
    args = parser.parse_args()

    if args.list:
        list_modules()
    elif args.graph:
        print_full_graph()
    elif args.module:
        print_module_deps(args.module)
    else:
        parser.print_help()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
