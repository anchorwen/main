"""Generate batch plan for CRT model batch production.

Creates manifest files conforming to CRTManifestV1 schema for all lane/seed/role combinations.

Usage:
  python scripts/training/generate_batch_plan.py                          # All lanes, default seeds
  python scripts/training/generate_batch_plan.py --generation g2026.1     # Explicit generation tag
  python scripts/training/generate_batch_plan.py --lanes sur,mtx          # Specific lanes only
  python scripts/training/generate_batch_plan.py --dry-run                # Print plan without writing
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # future/
DEFAULT_BATCH_DIR = PROJECT_ROOT / "batch_plans"


def utc_now_iso_z() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def resolve_git_commit_short(fallback: str = "unknown") -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return fallback


# ── Batch Plan Definition ──────────────────────────────────────────────
# g2026.1: First production batch across 3 lanes
# model_id format: CRT.<lane>.<role>.gYYYY.N@feat-<name>-<semver>

# v2: per-lane × per-timeframe batch plan matrix
# Each lane can specify timeframes dict; if absent, defaults to M5-only.
BATCH_PLAN = {
    "meta": {
        "batch_id": "g2026.2",
        "description": "Multi-TF production batch — per-lane × per-timeframe model matrix (Phase E)",
        "created_at_utc": None,  # filled at runtime
        "total_models": 0,  # computed at runtime
        "lanes": {
            "sur": {
                "role": "chlg",
                "description": "Survival_V9 Institutional trend predictor (MLP 40dim -> 3-head)",
                "feature_contract_id": "feat-sur-v9-institutional-1.0.0",
                "iface_semver": "1.0.0",
                "timeframes": {
                    "M5": {
                        "seeds": [42, 43, 44, 45, 46],
                        "dataset": "D:\\ai\\Survival_V9\\V9_Symbiosis_Matrix.csv",
                    },
                },
            },
            "mtx_transformer": {
                "role": "chlg",
                "description": "HMRE microstructure Transformer (32bar×9feat, CrossEntropyLoss, 3-class barrier)",
                "feature_contract_id": "feat-mtx-qtransformer-1.0.0",
                "iface_semver": "1.0.0",
                "timeframes": {
                    "M5": {
                        "seeds": [42, 43, 44, 45, 46],
                        "dataset": "data/training/micro_barrier_v2/train.npz",
                    },
                    "M15": {
                        "seeds": [42, 43, 44, 45, 46],
                        "dataset": "data/training/micro_barrier_m15_v2/train.npz",
                    },
                    "H1": {
                        "seeds": [42, 43, 44, 45, 46],
                        "dataset": "data/training/micro_barrier_h1_v2/train.npz",
                    },
                    "H4": {
                        "seeds": [42, 43, 44],
                        "dataset": "data/training/micro_barrier_h4_v2/train.npz",
                    },
                },
            },
            "mtx_xgboost": {
                "role": "chlg",
                "description": "HMRE microstructure XGBoost (288dim flat, multi:softmax, 3-class barrier)",
                "feature_contract_id": "feat-mtx-xgboost-1.0.0",
                "iface_semver": "1.0.0",
                "timeframes": {
                    "M5": {
                        "seeds": [42, 43, 44, 45, 46],
                        "dataset": "data/training/micro_barrier_v2/train.npz",
                    },
                    "M15": {
                        "seeds": [42, 43, 44, 45, 46],
                        "dataset": "data/training/micro_barrier_m15_v2/train.npz",
                    },
                    "H1": {
                        "seeds": [42, 43, 44, 45, 46],
                        "dataset": "data/training/micro_barrier_h1_v2/train.npz",
                    },
                    "H4": {
                        "seeds": [42, 43, 44],
                        "dataset": "data/training/micro_barrier_h4_v2/train.npz",
                    },
                },
            },
            "arb": {
                "role": "chlg",
                "description": "OU Statistical Arbitrage (Optuna TPE + Kalman, per-TF parameter search)",
                "feature_contract_id": "feat-arb-v6-ou-sniper-1.0.0",
                "iface_semver": "1.0.0",
                "timeframes": {
                    "M5": {"seeds": [42, 43, 44], "dataset": "data/raw/xauusdc_m5_merged.csv"},
                    "M15": {"seeds": [42, 43, 44], "dataset": "data/raw/xauusdc_m15_merged.csv"},
                    "H1": {"seeds": [42, 43, 44], "dataset": "data/raw/xauusdc_h1_merged.csv"},
                },
            },
            "xgb_inrepo": {
                "role": "chlg",
                "description": "In-repo XGBoost trained from dataset_builder NPZ output",
                "feature_contract_id": "feat-xgb-inrepo-v9-institutional-1.0.0",
                "iface_semver": "1.0.0",
                "timeframes": {
                    "M5": {"seeds": [42, 43, 44], "dataset": "data/training/train.npz"},
                },
            },
        },
    },
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="generate_batch_plan",
        description="Generate batch plan manifest entries for CRT model production",
    )
    p.add_argument(
        "--generation",
        default="g2026.1",
        help="Generation tag (default: g2026.1)",
    )
    p.add_argument(
        "--lanes",
        default="sur,mtx_transformer,mtx_xgboost,arb,xgb_inrepo",
        help="Comma-separated lane keys (default: all 5)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_BATCH_DIR,
        help=f"Output directory for batch plan (default: {DEFAULT_BATCH_DIR})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan to stdout without writing files",
    )
    p.add_argument(
        "--manifest-dir",
        type=Path,
        default=None,
        help="Override manifest output directory (default: <output-dir>/<generation>/manifests)",
    )
    return p


def generate_manifests(
    generation: str,
    lanes: list[str],
    plan_meta: dict,
    git_commit: str,
) -> list[dict]:
    """Generate individual CRT manifest entries conforming to CRTManifestV1 schema.

    v2: Expands per-lane timeframes dict into TF × seed matrix.
    Backward-compatible: if a lane has no 'timeframes' key (v1 format),
    falls back to top-level 'seeds'/'dataset' fields.
    """
    manifests = []

    for lane_key in lanes:
        lane_config = plan_meta["lanes"].get(lane_key)
        if lane_config is None:
            print(f"[WARN] Unknown lane key: {lane_key}, skipping", file=sys.stderr)
            continue

        role = lane_config["role"]
        lane_config.get("description", "")
        feature_contract_id = lane_config["feature_contract_id"]
        iface_semver = lane_config["iface_semver"]

        # Map lane key to CRT lane id
        if lane_key in ("mtx_transformer", "mtx_xgboost"):
            lane_id = "mtx"
        elif lane_key == "xgb_inrepo":
            lane_id = "xgbinrepo"
        else:
            lane_id = lane_key

        # v2: expand timeframes dict; v1 fallback: single M5 entry from top-level fields
        timeframes = lane_config.get("timeframes")
        if timeframes is None:
            # v1 backward-compat: wrap top-level seeds/dataset as M5-only
            timeframes = {
                "M5": {
                    "seeds": lane_config.get("seeds", [42]),
                    "dataset": lane_config.get("dataset", ""),
                },
            }

        for tf_key, tf_cfg in timeframes.items():
            seeds = tf_cfg.get("seeds", [42])
            dataset = tf_cfg.get("dataset", "")
            _tf_model_count = len(seeds)

            for seed in seeds:
                train_started = utc_now_iso_z()
                training_run_id = (
                    f"run-{generation}-{lane_id}-{tf_key}-s{seed}-{train_started.replace(':', '')}"
                )

                # model_id includes timeframe: CRT.<lane>.<role>.gYYYY.<TF>@feat-<name>-<semver>.s<N>
                model_id = (
                    f"CRT.{lane_id}.{role}.{generation}.{tf_key}" f"@{feature_contract_id}.s{seed}"
                )

                manifest = {
                    "schema_version": "crt_model_manifest.v1",
                    "model_id": model_id,
                    "lane": lane_id,
                    "role": role,
                    "generation": generation,
                    "timeframe": tf_key,
                    "feature_contract_id": feature_contract_id,
                    "iface_semver": iface_semver,
                    "dataset_slice_id": f"{generation}_{tf_key}_full",
                    "git_commit": git_commit,
                    "train_started_at_utc": train_started,
                    "trainer_version": "",
                    "metrics": {},
                    "risk_notes": ["multi-tf batch plan v2"],
                    "legacy_aliases": [],
                    "train_seed": seed,
                    "artifact_primary": None,
                    "norm_artifact": None,
                    "training_run_id": training_run_id,
                    "recipe_id": f"{lane_id}-{generation}-recipe-001",
                    "dataset_override": dataset,
                }

                manifests.append(manifest)

    return manifests


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Prepare meta
    plan_meta = json.loads(json.dumps(BATCH_PLAN["meta"]))
    plan_meta["created_at_utc"] = utc_now_iso_z()
    plan_meta["batch_id"] = args.generation

    git_commit = resolve_git_commit_short()

    lanes = [x.strip() for x in args.lanes.split(",") if x.strip()]

    # Generate manifests
    manifests = generate_manifests(args.generation, lanes, plan_meta, git_commit)

    # Update total
    plan_meta["total_models"] = len(manifests)

    # Output directory
    output_dir = args.output_dir / args.generation
    manifest_dir = args.manifest_dir or (output_dir / "manifests")

    if args.dry_run:
        print("=" * 70)
        print(f"  BATCH PLAN: {args.generation}  (DRY RUN)")
        print(f"  Models: {len(manifests)}  Lanes: {lanes}")
        print(f"  Output dir: {output_dir}")
        print("=" * 70)
        for i, m in enumerate(manifests):
            tf = m.get("timeframe", "M5")
            print(
                f"  [{i+1:02d}] {m['model_id']:64s}  lane={m['lane']:4s}  tf={tf:3s}  seed={m['train_seed']}  role={m['role']}"
            )
        print("=" * 70)
        return 0

    # Write batch plan
    manifest_dir.mkdir(parents=True, exist_ok=True)

    # Single combined manifest file with all entries
    combined_path = output_dir / "batch_manifest.json"
    combined_payload = {
        "batch_id": args.generation,
        "created_at_utc": plan_meta["created_at_utc"],
        "total_models": len(manifests),
        "models": manifests,
    }
    combined_path.write_text(
        json.dumps(combined_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[generate] Combined manifest: {combined_path}")

    # Also write individual manifest files per model (for parallel execution)
    for m in manifests:
        safe_name = m["model_id"].replace("@", "_at_").replace(".", "_").replace(":", "_")
        manifest_path = manifest_dir / f"{safe_name}.json"
        manifest_path.write_text(
            json.dumps(m, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(f"[generate] Individual manifests: {manifest_dir}/  ({len(manifests)} files)")
    print(f"\n{'=' * 70}")
    print(f"  BATCH PLAN: {args.generation}")
    print(f"  Models: {len(manifests)}")
    print(f"  Lanes: {lanes}")
    print(f"  Output: {output_dir}")
    print(f"{'=' * 70}")
    for i, m in enumerate(manifests):
        tf = m.get("timeframe", "M5")
        print(
            f"  [{i+1:02d}] {m['model_id']:64s}  lane={m['lane']:4s}  tf={tf:3s}  seed={m['train_seed']}  role={m['role']}"
        )
    print(f"{'=' * 70}")
    print(f"\n  Next: run_train_batch.py --batch-dir {output_dir} --dry-run  (verify)")
    print(f"  Then: run_train_batch.py --batch-dir {output_dir} --execute   (train)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
