"""Register a trained CRT model as a brain registry entry.

Reads a CRTManifestV1 and produces a brain_registry_entry.v1 JSON file
ready for deployment via BrainFactory.

Usage:
  python scripts/training/register_brain.py --manifest <path>             # Auto brain_id
  python scripts/training/register_brain.py --manifest <path> --dry-run   # Preview only
  python scripts/training/register_brain.py --manifest <path> --brain-id MyBrain
  python scripts/training/register_brain.py --manifest <path> --artifact-path D:\\models\\m.onnx
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent

LANE_TO_BRAIN_TYPE: dict[str, str] = {
    "sur": "onnx_v9",
    "mtx": "xgboost_v4.5",
    "arb": "ou_params_v6",
    "xgbinrepo": "xgboost_v4.5",
    "online_sgd": "online_sgd",
}

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "configs" / "brains"

DEFAULT_DEPLOYMENT_SCOPE: dict[str, Any] = {
    "symbols": ["XAUUSD"],
    "sessions": ["all"],
    "regimes": ["trend", "volatile_trend"],
}


def _safe_brain_id(model_id: str) -> str:
    return model_id.replace("@", "_at_").replace(".", "_").replace(":", "_")


def build_brain_entry(
    manifest: dict[str, Any],
    artifact_path: str | None,
    brain_id: str | None,
) -> dict[str, Any]:
    lane = manifest.get("lane", "")
    brain_type = LANE_TO_BRAIN_TYPE.get(lane, "onnx_v9")

    model_id = manifest.get("model_id", "unknown")
    resolved_brain_id = brain_id or _safe_brain_id(model_id)
    resolved_artifact = artifact_path or manifest.get("artifact_primary", "")

    generation = manifest.get("generation", "")
    feature_contract_id = manifest.get("feature_contract_id", "")

    return {
        "schema_version": "brain_registry_entry.v1",
        "brain_id": resolved_brain_id,
        "brain_type": brain_type,
        "brain_role": "alpha_brain",
        "model_version": generation,
        "status": "shadow",
        "artifact_path": resolved_artifact,
        "feature_schema_id": feature_contract_id,
        "deployment_scope": DEFAULT_DEPLOYMENT_SCOPE,
    }


def register_brain(entry: dict[str, Any], output_dir: Path, *, skip_gate: bool = False) -> Path:
    # Registration gate — block if any check fails (skip for tests / legacy)
    if not skip_gate:
        from core.deployment.brain_registration_gate import BrainRegistrationGate

        gate = BrainRegistrationGate()
        result = gate.validate(entry)
        if not result.passed:
            raise ValueError(
                f"Registration gate rejected {entry.get('brain_id', '?')}: "
                f"{len(result.failures)} check(s) failed — "
                f"{', '.join(check for check, _ in result.failures)}"
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    brain_id = entry["brain_id"]
    out_path = output_dir / f"{brain_id}.json"
    out_path.write_text(
        json.dumps(entry, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return out_path


def _print_register_reminder(config_filename: str) -> None:
    print("\n  ╔══════════════════════════════════════════════════════════════╗")
    print("  ║  NEXT STEP: Register this brain with the one-click CLI:  ║")
    print("  ║                                                            ║")
    print(
        f"  ║  python scripts/brain.py register configs/brains/{config_filename} --status shadow  ║"
    )
    print("  ║  python scripts/brain.py validate                          ║")
    print("  ║                                                            ║")
    print("  ║  DO NOT manually edit live.yaml or governance_state.json. ║")
    print("  ╚══════════════════════════════════════════════════════════════╝")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="register_brain",
        description="Register a trained CRT model as a brain registry entry",
    )
    p.add_argument("--manifest", type=Path, required=True, help="Path to CRT manifest JSON")
    p.add_argument(
        "--artifact-path",
        type=str,
        default=None,
        help="Override artifact path (default: from manifest.artifact_primary)",
    )
    p.add_argument(
        "--brain-id",
        type=str,
        default=None,
        help="Brain ID (default: derived from model_id)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory for brain entry JSON (default: {DEFAULT_OUTPUT_DIR})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print preview without writing file",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    manifest_path = args.manifest
    if not manifest_path.exists():
        print(f"[ERROR] Manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = build_brain_entry(manifest, args.artifact_path, args.brain_id)

    if args.dry_run:
        print("=== DRY RUN: Brain Entry Preview ===")
        print(json.dumps(entry, ensure_ascii=False, indent=2))
        target = args.output_dir / f"{entry['brain_id']}.json"
        print(f"\n[dry-run] Would write to: {target}")
        print("[dry-run] No files written.")
        return 0

    out_path = register_brain(entry, args.output_dir)
    print(f"[register_brain] Brain entry written: {out_path}")
    print(f"  brain_id:   {entry['brain_id']}")
    print(f"  brain_type: {entry['brain_type']}")
    print(f"  lane:       {manifest.get('lane', '?')}")
    _print_register_reminder(out_path.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
