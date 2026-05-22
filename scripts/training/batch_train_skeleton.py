"""Batch training skeleton: emits one manifest per job + prints placeholder train command.

Wire YOUR_TRAIN_CMD to your real trainer (PyTorch / XGBoost / external repo).

Example:
  python scripts/training/batch_train_skeleton.py \\
    --output-dir data/models/crt_batch_smoke \\
    --seeds 42,43,44 \\
    --lane sur --role chlg --generation g2026.1 \\
    --feature-contract-id feat-v9-institutional-1.0.0
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from scripts.training.crt_manifest import build_manifest


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="batch_train_skeleton")
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--seeds", default="42,43,44", help="Comma-separated RNG seeds")
    p.add_argument("--lane", default="sur")
    p.add_argument("--role", default="chlg", choices=["prd", "chlg", "cabl", "stub"])
    p.add_argument("--generation", default="g2026.1")
    p.add_argument("--feature-contract-id", default="feat-v9-institutional-1.0.0")
    p.add_argument("--iface-semver", default="1.0.0")
    p.add_argument("--trainer-version", default="batch-skeleton-0.1.0")
    p.add_argument(
        "--jobs-file",
        type=Path,
        default=None,
        help="Optional JSON array of {seed, dataset_slice_id} overrides default slice_stub_seed_<seed>",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    seeds = [int(s.strip()) for s in args.seeds.split(",") if s.strip()]
    jobs: list[dict[str, str | int]] = []
    if args.jobs_file:
        raw = json.loads(args.jobs_file.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise SystemExit("jobs-file must be a JSON array")
        jobs = raw
    else:
        for seed in seeds:
            jobs.append({"seed": seed, "dataset_slice_id": f"slice_stub_seed_{seed}_v1"})

    args.output_dir.mkdir(parents=True, exist_ok=True)
    train_cmd_env = os.environ.get("CRT_TRAIN_CMD", "").strip()
    plan_path = args.output_dir / "batch_plan.jsonl"
    plan_path.write_text("", encoding="utf-8")

    for job in jobs:
        seed = int(job["seed"])
        slice_id = str(job["dataset_slice_id"])
        run_id = f"run_seed_{seed}__{slice_id}".replace(" ", "_")[:240]
        gen = str(job.get("generation", args.generation))
        m = build_manifest(
            lane=args.lane,
            role=args.role,
            generation=gen,
            feature_contract_id=args.feature_contract_id,
            dataset_slice_id=slice_id,
            iface_semver=args.iface_semver,
            trainer_version=args.trainer_version,
            train_seed=seed,
            training_run_id=run_id,
            metrics={"phase": "skeleton", "note": "replace after train", "training_run_id": run_id},
            risk_notes=["stub batch row; run real trainer before promotion"],
        )
        safe_id = m.model_id.replace("@", "_at_").replace("/", "-")
        subdir = args.output_dir / f"job_seed_{seed}"
        subdir.mkdir(parents=True, exist_ok=True)
        man_path = subdir / f"{safe_id}.manifest.json"
        man_path.write_text(
            json.dumps(m.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        line = {
            "model_id": m.model_id,
            "training_run_id": run_id,
            "manifest_path": str(man_path),
            "train_seed": seed,
            "dataset_slice_id": slice_id,
            "suggested_command": train_cmd_env or "<set CRT_TRAIN_CMD to your trainer>",
        }
        with plan_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

        print(json.dumps(line, ensure_ascii=False))

    print(json.dumps({"batch_plan": str(plan_path), "jobs": len(jobs)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
