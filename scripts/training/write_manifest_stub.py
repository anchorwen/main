"""Write a CRT manifest JSON (stub or post-train). Usage: python -m scripts.training.write_manifest_stub ..."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.training.crt_manifest import build_manifest


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="write_manifest_stub")
    p.add_argument("--lane", default="sur")
    p.add_argument("--role", default="chlg", choices=["prd", "chlg", "cabl", "stub"])
    p.add_argument("--generation", default="g2026.1")
    p.add_argument(
        "--feature-contract-id",
        default="feat-v9-institutional-1.0.0",
        help="e.g. feat-v9-institutional-1.0.0",
    )
    p.add_argument("--dataset-slice-id", required=True)
    p.add_argument("--iface-semver", default="1.0.0")
    p.add_argument("--trainer-version", default="stub-0.1.0")
    p.add_argument("--train-seed", type=int, default=None)
    p.add_argument("--artifact-primary", default=None)
    p.add_argument("--norm-artifact", default=None)
    p.add_argument("--training-run-id", default=None)
    p.add_argument("--git-commit", default=None)
    p.add_argument("--output", type=Path, required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    m = build_manifest(
        lane=args.lane,
        role=args.role,
        generation=args.generation,
        feature_contract_id=args.feature_contract_id,
        dataset_slice_id=args.dataset_slice_id,
        iface_semver=args.iface_semver,
        trainer_version=args.trainer_version,
        git_commit=args.git_commit,
        train_seed=args.train_seed,
        artifact_primary=args.artifact_primary,
        norm_artifact=args.norm_artifact,
        training_run_id=args.training_run_id,
    )
    payload = m.model_dump(mode="json")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({"written": str(args.output), "model_id": m.model_id}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
