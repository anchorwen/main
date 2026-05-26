"""Parameter contract validator for OU artifact JSON files.

Prevents silent parameter regression when artifacts are split, rebased,
or regenerated.  Validates bounds, structure, and cross-file consistency.

Called from verify.py --quick and verify.py --full.
Exit 0 = all valid, exit 1 = violations found.

Usage:
  python scripts/validate_artifacts.py              # validate all artifacts
  python scripts/validate_artifacts.py --quick       # artifact files only (fast)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# ── Parameter bounds (per-field) ──────────────────────────────────────────
# Each entry: (min_inclusive, max_inclusive, label)
OU_PARAM_BOUNDS: dict[str, tuple[float, float, str]] = {
    "z_entry": (0.5, 4.0, "Entry Z-Score threshold"),
    "z_exit": (0.05, 2.5, "Exit Z-Score threshold"),
    "max_half_life": (15, 150, "Max half-life in bars"),
    "theta_min": (0.0001, 0.10, "Min theta for OU detection"),
    "window": (30, 600, "Rolling window size in bars"),
}

# ── Cross-file drift rules ────────────────────────────────────────────────
# When a split artifact has a known parent, these ratios flag regressions.
# Each rule: (max_increase_ratio, max_decrease_ratio, label)
#   increase > ratio → warning (e.g. z_entry going up = tighter entry)
#   decrease > ratio → warning (e.g. max_half_life going down = more blocking)
CROSS_FILE_RULES: dict[str, tuple[float, float, str]] = {
    "z_entry": (1.5, 0.5, "z_entry drift (increase = tighter, decrease = looser)"),
    "max_half_life": (2.0, 0.70, "max_half_life drift (decrease = more blocking)"),
    "z_exit": (3.0, 0.10, "z_exit drift (decrease = harder to exit)"),
}

# Parent artifact → list of child artifacts to cross-check
PARENT_CHILD_MAP: dict[str, list[str]] = {
    "arb_params_v7.json": ["arb_params_v7_m5.json", "arb_params_v7_m15.json"],
}


def _load_artifact(path: Path) -> dict[str, Any]:
    """Load a JSON artifact file. Returns empty dict on failure."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[artifact] ERROR loading {path.name}: {exc}")
        return {}


def _validate_ou_artifact(artifact: dict[str, Any], filename: str) -> list[str]:
    """Validate a single OU artifact against parameter bounds.

    Returns a list of violation messages (empty = all good).
    """
    violations: list[str] = []

    opt = artifact.get("optimal_params")
    if not isinstance(opt, dict):
        violations.append(f"{filename}: missing or invalid 'optimal_params' key")
        return violations

    for param, (lo, hi, label) in OU_PARAM_BOUNDS.items():
        value = opt.get(param)
        if value is None:
            violations.append(f"{filename}: missing parameter '{param}' ({label})")
            continue
        try:
            fval = float(value)
        except (TypeError, ValueError):
            violations.append(f"{filename}: parameter '{param}' is not numeric: {value!r}")
            continue
        if not (lo <= fval <= hi):
            violations.append(f"{filename}: {param}={fval} out of bounds [{lo}, {hi}] ({label})")

    return violations


def _cross_check_artifacts(
    artifacts: dict[str, dict[str, Any]],
) -> list[str]:
    """Compare split artifacts against their parent for parameter drift.

    Returns a list of violation messages (empty = all good).
    """
    violations: list[str] = []

    for parent_name, child_names in PARENT_CHILD_MAP.items():
        parent = artifacts.get(parent_name)
        if parent is None:
            continue  # parent not present — skip cross-check
        parent_opt = parent.get("optimal_params", {})

        for child_name in child_names:
            child = artifacts.get(child_name)
            if child is None:
                continue  # child not present — skip
            child_opt = child.get("optimal_params", {})

            for param, (inc_ratio, dec_ratio, label) in CROSS_FILE_RULES.items():
                p_val = parent_opt.get(param)
                c_val = child_opt.get(param)
                if p_val is None or c_val is None:
                    continue

                try:
                    p_f = float(p_val)
                    c_f = float(c_val)
                except (TypeError, ValueError):
                    continue

                if p_f == 0:
                    continue

                ratio = c_f / p_f

                if param == "z_entry" and ratio > inc_ratio:
                    violations.append(
                        f"{child_name}: {param}={c_f} is {ratio:.2f}× parent "
                        f"({parent_name} {param}={p_f}) — exceeds max increase {inc_ratio}×. "
                        f"({label})"
                    )
                elif param == "max_half_life" and ratio < dec_ratio:
                    violations.append(
                        f"{child_name}: {param}={c_f} is {ratio:.2f}× parent "
                        f"({parent_name} {param}={p_f}) — below min ratio {dec_ratio}. "
                        f"({label})"
                    )
                elif param == "z_exit" and ratio < dec_ratio:
                    violations.append(
                        f"{child_name}: {param}={c_f} is {ratio:.2f}× parent "
                        f"({parent_name} {param}={p_f}) — below min ratio {dec_ratio}. "
                        f"({label})"
                    )

    return violations


def main() -> int:
    models_dir = ROOT / "data" / "models"
    if not models_dir.is_dir():
        print(f"[artifact] models directory not found: {models_dir}")
        return 1

    # ── Discover OU artifact files ──
    artifact_files: list[Path] = []
    for pattern in ["arb_params_v7*.json"]:
        artifact_files.extend(sorted(models_dir.glob(pattern)))

    if not artifact_files:
        print("[artifact] No OU artifact files found — skipping validation")
        return 0

    # ── Load all artifacts ──
    artifacts: dict[str, dict[str, Any]] = {}
    load_errors = 0
    for fp in artifact_files:
        art = _load_artifact(fp)
        if art:
            artifacts[fp.name] = art
        else:
            load_errors += 1

    # ── Phase 1: Per-artifact bounds validation ──
    all_violations: list[str] = []
    for name, art in artifacts.items():
        all_violations.extend(_validate_ou_artifact(art, name))

    # ── Phase 2: Cross-file consistency ──
    all_violations.extend(_cross_check_artifacts(artifacts))

    # ── Report ──
    if load_errors:
        print(f"[artifact] {load_errors} file(s) failed to load")

    if all_violations:
        print(f"[artifact] {len(all_violations)} parameter contract violation(s):")
        for v in all_violations:
            print(f"  - {v}")
        return 1

    print(f"[artifact] OK: {len(artifacts)} artifact(s) validated, no violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
