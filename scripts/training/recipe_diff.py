"""Recipe diff tool — compare two Training Recipe JSON files for ablation studies.

Outputs a structured diff showing what changed between two recipes, useful for:
  - Ablation experiment documentation ("what did we change?")
  - Audit trail ("why does model B perform differently from model A?")
  - Recipe promotion workflow ("diff chlg vs prd before promotion")

Usage:
  python scripts/training/recipe_diff.py \\
    blueprints/recipes/sur-g2026.1-recipe-001.json \\
    blueprints/recipes/sur-g2026.2-recipe-001.json

  python scripts/training/recipe_diff.py --json recipe_a.json recipe_b.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dict with dot-separated keys."""
    result: dict[str, Any] = {}
    for k, v in d.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and not isinstance(v, list):
            result.update(_flatten(v, full_key))
        else:
            result[full_key] = v
    return result


def diff_recipes(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Compute structured diff between two recipe dicts.

    Returns:
        {
            "identical": [...],
            "changed": [{"key": ..., "old": ..., "new": ...}],
            "added": [...],
            "removed": [...],
        }
    """
    flat_a = _flatten(a)
    flat_b = _flatten(b)

    keys_a = set(flat_a)
    keys_b = set(flat_b)

    identical: list[str] = []
    changed: list[dict[str, Any]] = []
    added: list[str] = []
    removed: list[str] = []

    for key in sorted(keys_a | keys_b):
        in_a = key in keys_a
        in_b = key in keys_b

        if in_a and in_b:
            va = flat_a[key]
            vb = flat_b[key]
            if va == vb:
                identical.append(key)
            else:
                changed.append({"key": key, "old": va, "new": vb})
        elif in_a and not in_b:
            removed.append(key)
        else:
            added.append(key)

    return {
        "identical": identical,
        "changed": changed,
        "added": added,
        "removed": removed,
    }


def _summarize_identity(a: dict[str, Any]) -> str:
    """One-line identity summary."""
    mi = a.get("model_identity", {})
    lcr = a.get("label_contract_ref", {})
    return (
        f"{mi.get('lane', '?')}/{mi.get('role', '?')}/{mi.get('generation', '?')} "
        f"feat={mi.get('feature_contract_id', '?')} "
        f"label={lcr.get('contract_id', '?')}"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="recipe_diff",
        description="Compare two Training Recipe JSON files for ablation studies",
    )
    p.add_argument("recipe_a", type=Path, help="First recipe JSON")
    p.add_argument("recipe_b", type=Path, help="Second recipe JSON")
    p.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON diff (default: human-readable)",
    )
    p.add_argument(
        "--summary",
        action="store_true",
        help="Only print one-line summary of what changed",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    for p in (args.recipe_a, args.recipe_b):
        if not p.exists():
            print(f"[ERROR] Recipe not found: {p}", file=sys.stderr)
            return 2

    a = json.loads(args.recipe_a.read_text(encoding="utf-8"))
    b = json.loads(args.recipe_b.read_text(encoding="utf-8"))

    # Validate schema
    for label, d in [("A", a), ("B", b)]:
        sv = d.get("schema_version", "")
        if sv != "training_recipe.v1":
            print(f"[ERROR] Recipe {label} has wrong schema: {sv}", file=sys.stderr)
            return 3

    diff = diff_recipes(a, b)

    if args.summary:
        n_changed = len(diff["changed"])
        n_added = len(diff["added"])
        n_removed = len(diff["removed"])
        changed_keys = ", ".join(d["key"] for d in diff["changed"])
        print(f"changed={n_changed} [{changed_keys}]  " f"added={n_added}  removed={n_removed}")
        return 0

    if args.json:
        output = {
            "recipe_a": str(args.recipe_a),
            "recipe_b": str(args.recipe_b),
            "identity_a": _summarize_identity(a),
            "identity_b": _summarize_identity(b),
            **diff,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    # ── Human-readable output ──
    print("=" * 72)
    print("  RECIPE DIFF")
    print(f"  A: {args.recipe_a}")
    print(f"  B: {args.recipe_b}")
    print("=" * 72)
    print()
    print(f"  A: {_summarize_identity(a)}")
    print(f"  B: {_summarize_identity(b)}")
    print()

    if diff["changed"]:
        print(f"  CHANGED ({len(diff['changed'])}):")
        print(f"  {'─' * 60}")
        for d in diff["changed"]:
            print(f"  {d['key']}:")
            print(f"    - {json.dumps(d['old'], ensure_ascii=False)}")
            print(f"    + {json.dumps(d['new'], ensure_ascii=False)}")
        print()

    if diff["added"]:
        print(f"  ADDED ({len(diff['added'])}):")
        for k in diff["added"]:
            print(f"    + {k}")
        print()

    if diff["removed"]:
        print(f"  REMOVED ({len(diff['removed'])}):")
        for k in diff["removed"]:
            print(f"    - {k}")
        print()

    if not diff["changed"] and not diff["added"] and not diff["removed"]:
        print("  (no differences — recipes are identical)")
        print()

    print(f"  IDENTICAL: {len(diff['identical'])} fields unchanged")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
