"""Reference integrity audit — scan for hardcoded paths and cross-validate.

Usage:
    python scripts/audit/reference_integrity.py              # full audit
    python scripts/audit/reference_integrity.py --output report.json  # JSON export
    python scripts/audit/reference_integrity.py --check-only  # exit 1 if stale refs

Scans scripts/, core/, tests/, apps/ for hardcoded paths to:
  - configs/brains/*.json
  - data/models/*
  - configs/brains/*normalization*.json

Cross-validates against filesystem and governance state.
"""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from core.deployment.brain_lifecycle_manager import BrainLifecycleManager
from core.deployment.path_defaults import REQUIRED_PATHS, resolve, validate_defaults


def _build_parser() -> ArgumentParser:
    p = ArgumentParser(description="Reference integrity audit")
    p.add_argument("--output", type=str, default=None, help="Write JSON report to file")
    p.add_argument("--check-only", action="store_true", help="Exit 1 if any stale references found")
    p.add_argument(
        "--scan-dirs",
        nargs="*",
        default=["scripts", "core", "tests", "apps"],
        help="Directories to scan (default: scripts core tests apps)",
    )
    return p


def main() -> int:
    args = _build_parser().parse_args()

    mgr = BrainLifecycleManager(project_root=PROJECT_ROOT)

    # ── 1. Reference audit ──
    ref_report = mgr.audit_hardcoded_references(scan_dirs=args.scan_dirs)

    # ── 2. Startup integrity ──
    integrity = mgr.verify_startup_integrity()

    # ── 3. Required path check ──
    path_issues = validate_defaults()

    # ── Print report ──
    sep = "=" * 60
    dash = "-" * 60
    print(f"\n{sep}")
    print("  REFERENCE INTEGRITY AUDIT")
    print(sep)

    print(f"\nScanned {ref_report.scanned_files} Python files.\n")

    if ref_report.stale_references:
        print(f"[WARN] STALE REFERENCES ({len(ref_report.stale_references)}):")
        for ref in sorted(ref_report.stale_references):
            print(f"   {ref}")
    else:
        print("[OK] No stale references found.")

    if ref_report.hardcoded_brain_paths:
        print(f"\n   Hardcoded brain paths ({len(ref_report.hardcoded_brain_paths)}):")
        for f, line, path in sorted(ref_report.hardcoded_brain_paths):
            print(f"   {f}:{line}  ->  {path}")

    if ref_report.hardcoded_model_paths:
        print(f"\n   Hardcoded model paths ({len(ref_report.hardcoded_model_paths)}):")
        for f, line, path in sorted(ref_report.hardcoded_model_paths):
            print(f"   {f}:{line}  ->  {path}")

    # ── Integrity report ──
    print(f"\n{dash}")
    print("  INTEGRITY CHECK")
    print(dash)

    if integrity.missing_config_files:
        print(f"\n[WARN] MISSING CONFIG FILES ({len(integrity.missing_config_files)}):")
        for f in integrity.missing_config_files:
            print(f"   MISS {f}")
    else:
        print("\n[OK] All live.yaml registry entries exist on disk.")

    if integrity.missing_yaml_entries:
        print(f"\n[WARN] DISK FILES NOT IN LIVE.YAML ({len(integrity.missing_yaml_entries)}):")
        for f in integrity.missing_yaml_entries:
            print(f"   MISS {f}")

    if integrity.missing_artifacts:
        print(f"\n[WARN] MISSING ARTIFACTS ({len(integrity.missing_artifacts)}):")
        for a in integrity.missing_artifacts:
            print(f"   MISS {a}")

    if integrity.missing_norm_configs:
        print(f"\n[WARN] MISSING NORMALIZATION CONFIGS ({len(integrity.missing_norm_configs)}):")
        for n in integrity.missing_norm_configs:
            print(f"   MISS {n}")

    if integrity.governance_orphans:
        print(f"\n[WARN] GOVERNANCE ORPHANS ({len(integrity.governance_orphans)}):")
        for g in integrity.governance_orphans:
            print(f"   MISS {g} (in governance_state but no config on disk)")

    if integrity.hardcoded_path_mismatches:
        print(f"\n[WARN] REQUIRED PATH MISMATCHES ({len(integrity.hardcoded_path_mismatches)}):")
        for p in integrity.hardcoded_path_mismatches:
            print(f"   MISS {p}")

    # ── Required paths ──
    print(f"\n{dash}")
    print("  REQUIRED DEFAULTS")
    print(dash)
    for name in sorted(REQUIRED_PATHS):
        val = globals().get(name)
        if val and isinstance(val, str):
            file_path = resolve(val)
            status = "[OK]" if file_path.exists() else "[MISS]"
            print(f"   {status} {name}: {val}")

    # ── JSON export ──
    if args.output:
        output_data = {
            "scanned_files": ref_report.scanned_files,
            "stale_references": ref_report.stale_references,
            "hardcoded_brain_paths": [
                {"file": f, "line": l, "path": p} for f, l, p in ref_report.hardcoded_brain_paths
            ],
            "hardcoded_model_paths": [
                {"file": f, "line": l, "path": p} for f, l, p in ref_report.hardcoded_model_paths
            ],
            "integrity": {
                "valid": integrity.valid,
                "missing_config_files": integrity.missing_config_files,
                "missing_yaml_entries": integrity.missing_yaml_entries,
                "missing_artifacts": integrity.missing_artifacts,
                "governance_orphans": integrity.governance_orphans,
                "hardcoded_path_mismatches": integrity.hardcoded_path_mismatches,
            },
        }
        Path(args.output).write_text(json.dumps(output_data, indent=2, ensure_ascii=False))
        print(f"\nReport written to {args.output}")

    # ── Exit code ──
    has_issues = bool(
        ref_report.stale_references
        or integrity.missing_config_files
        or integrity.governance_orphans
        or integrity.hardcoded_path_mismatches
        or path_issues
    )
    if args.check_only and has_issues:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
