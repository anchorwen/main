"""CRT lane trainer wrapper: Survival_V9 Institutional (lane=sur).

Bridges Survival_V9 1_V9_Institutional_Forge.py into the CRT pipeline.

Protocol:
1. Accept --manifest-path (CRT manifest JSON, read-only input)
2. Accept --result-json-path (where to write result.json for your_trainer.py to ingest)
3. Accept --artifact-path (target path for .onnx artifact)
4. Accept --dataset-csv (override training data CSV, default: V9_Symbiosis_Matrix.csv)
5. Accept --trainer-root (directory containing 1_V9_Institutional_Forge.py, default: data/training/sur_v9)
6. Accept --recipe (Training Recipe JSON — the single source of truth for hyperparameters)
7. Execute forge_v9_institutional() via subprocess, then copy artifacts to artifact-path
8. Write result.json with metrics / artifact_primary / norm_artifact / risk_notes

Usage (as lane command template):
  python scripts/training/trainers/sur_trainer.py \\
    --manifest-path {manifest_path} \\
    --result-json-path {manifest_path}.result.json \\
    --artifact-path {artifact_path} \\
    --recipe blueprints/recipes/sur-g2026.1-recipe-001.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent  # scripts/training/trainers/
PROJECT_ROOT = SCRIPTS_DIR.parent.parent.parent  # future/


def utc_now_iso_z() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sur_trainer",
        description="CRT lane trainer: Survival_V9 Institutional (lane=sur)",
    )
    p.add_argument(
        "--manifest-path", type=Path, required=True, help="Path to CRT manifest JSON (input)"
    )
    p.add_argument(
        "--result-json-path",
        type=Path,
        required=True,
        help="Path to write result.json for your_trainer.py ingestion",
    )
    p.add_argument(
        "--artifact-path", type=Path, required=True, help="Target path for .onnx artifact"
    )
    p.add_argument(
        "--dataset-csv",
        type=Path,
        default=None,
        help="CSV training data (default: <trainer-root>/V9_Symbiosis_Matrix.csv)",
    )
    p.add_argument(
        "--trainer-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "training" / "sur_v9",
        help="Directory containing 1_V9_Institutional_Forge.py (default: data/training/sur_v9)",
    )
    p.add_argument(
        "--epochs",
        type=int,
        default=None,
        help="Override training epochs (default: from recipe or 200)",
    )
    p.add_argument(
        "--recipe",
        type=Path,
        default=None,
        help="Path to Training Recipe JSON (single source of truth for hyperparameters)",
    )
    return p


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def run_sur_forge(
    trainer_root: Path,
    dataset_csv: Path,
    epochs: int,
) -> subprocess.CompletedProcess:
    """Run Survival_V9 forge as a subprocess via an inline script.

    We inject the training entry point as an inline Python -c script to avoid
    modifying the original trainer files. This preserves the original forge logic
    while redirecting CSV input and artifact output under CRT control.
    """
    forge_script = trainer_root / "1_V9_Institutional_Forge.py"
    if not forge_script.exists():
        raise FileNotFoundError(f"Forge script not found: {forge_script}")

    # Strategy: change cwd to trainer_root so relative paths resolve correctly,
    # then exec the forge script directly (not via import). We pass the dataset
    # CSV override via an env var, which the inline preamble uses to patch
    # sys.path and working directory.
    inline = rf"""
import os, sys
os.chdir(r"{trainer_root}")
sys.path.insert(0, r"{trainer_root}")

# Redirect pd.read_csv to use the specified dataset CSV
import pandas as _pd
_release_read_csv = _pd.read_csv
def _patched_read_csv(filepath_or_buffer, *args, **kwargs):
    return _release_read_csv(r"{dataset_csv}", *args, **kwargs)
_pd.read_csv = _patched_read_csv

print(f"[sur_forge] trainer_root={{os.getcwd()}}")
print(f"[sur_forge] dataset_csv_override={{os.environ.get('SUR_DATASET_CSV', '')}}")

# Execute the forge script directly (not via import)
forge_path = r"{forge_script}"
with open(forge_path, "r", encoding="utf-8") as f:
    code = f.read()
exec(compile(code, forge_path, "exec"), {{"__name__": "__main__"}})

# Verify output
expected = os.path.join(r"{trainer_root}", "v9_institutional_brain.onnx")
if os.path.exists(expected):
    print(f"RESULT_ARTIFACT={{expected}}")
    print(f"RESULT_NORM=r\"{trainer_root}\\V9_Params.mqh\"")
else:
    print("RESULT_ERROR=onnx_not_found")
    sys.exit(3)
"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["SUR_DATASET_CSV"] = str(dataset_csv)
    cmd = [sys.executable, "-c", inline]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3600,  # 1 hour max
        check=False,
        cwd=trainer_root,
        env=env,
    )
    return proc


def main(argv: list[str] | None = None) -> int:
    # Bypass Windows GBK encoding for stdout/stderr so emoji (🚀) and
    # other UTF-8 characters in subprocess output don't cause crashes.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[reportAttributeAccessIssue]
    except Exception:
        import io

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[reportAttributeAccessIssue]
    except Exception:
        import io

        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)

    manifest = load_manifest(args.manifest_path)
    model_id = manifest.get("model_id", "unknown")
    lane = manifest.get("lane", "sur")
    generation = manifest.get("generation", "g2026.1")

    trainer_root = args.trainer_root.resolve()
    if not trainer_root.exists():
        legacy = Path(r"D:\ai\Survival_V9")
        if legacy.exists():
            print(f"[sur_trainer] Internal trainer root not found: {trainer_root}")
            print(f"[sur_trainer] Falling back to legacy path: {legacy}")
            trainer_root = legacy
    dataset_csv = (args.dataset_csv or (trainer_root / "V9_Symbiosis_Matrix.csv")).resolve()
    artifact_path = args.artifact_path.resolve()
    result_path = args.result_json_path.resolve()

    # ── Load recipe if provided ──
    recipe_id: str | None = None
    label_contract_id: str | None = None
    epochs = args.epochs or 200

    if args.recipe:
        from core.contracts.training.training_recipe import TrainingRecipe

        recipe = TrainingRecipe.from_file(args.recipe)
        recipe_id = recipe.recipe_id
        label_contract_id = recipe.label_contract_ref.contract_id
        if args.epochs is None:
            epochs = recipe.training.epochs
        print(f"[sur_trainer] Recipe: {recipe_id}")
        print(f"[sur_trainer] Label contract: {label_contract_id}")
        print(
            f"[sur_trainer] Recipe params: lr={recipe.training.learning_rate}, "
            f"batch={recipe.training.batch_size}, dropout={recipe.training.dropout}, "
            f"optimizer={recipe.training.optimizer}"
        )

    if not dataset_csv.exists():
        print(f"[sur_trainer] ERROR: Dataset CSV not found: {dataset_csv}", file=sys.stderr)
        return 2

    print(f"[sur_trainer] Lane={lane}  Model={model_id}  Generation={generation}")
    print(f"[sur_trainer] Trainer root: {trainer_root}")
    print(f"[sur_trainer] Dataset CSV: {dataset_csv}")
    print(f"[sur_trainer] Artifact target: {artifact_path}")
    print(f"[sur_trainer] Starting Survival_V9 forge (epochs={epochs})...")

    proc = run_sur_forge(trainer_root, dataset_csv, epochs)

    print(proc.stdout or "")
    if proc.stderr:
        print(f"[sur_trainer] STDERR:\n{proc.stderr[-3000:]}", file=sys.stderr)

    # --- Build result.json ---
    result: dict[str, Any] = {
        "trainer": "sur_trainer",
        "trainer_version": "sur-v9-institutional-1.0.0",
        "completed_at_utc": utc_now_iso_z(),
        "model_id": model_id,
        "lane": lane,
        "generation": generation,
        "exit_code": proc.returncode,
        "metrics": {
            "train_finished": proc.returncode == 0,
            "trainer_exit_code": proc.returncode,
            "epochs_requested": epochs,
            "dataset_csv": str(dataset_csv),
        },
        "risk_notes": [],
        "artifact_primary": None,
        "norm_artifact": None,
    }

    if recipe_id:
        result["recipe_id"] = recipe_id
    if label_contract_id:
        result["label_contract_id"] = label_contract_id

    # Parse key lines from stdout
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("RESULT_ARTIFACT="):
            result["artifact_primary"] = line.split("=", 1)[1]
        elif line.startswith("RESULT_NORM="):
            result["norm_artifact"] = line.split("=", 1)[1]
        elif line.startswith("RESULT_ERROR="):
            result["risk_notes"].append(f"forge_error: {line.split('=',1)[1]}")

    if proc.returncode != 0:
        result["risk_notes"].append(f"forge exited with code {proc.returncode}")
        result["metrics"]["error_tail"] = proc.stderr[-2000:] if proc.stderr else ""
    else:
        # Copy artifacts to target locations
        onnx_src = trainer_root / "v9_institutional_brain.onnx"
        params_src = trainer_root / "V9_Params.mqh"

        if onnx_src.exists():
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(onnx_src, artifact_path)
            result["artifact_primary"] = str(artifact_path)
            result["metrics"]["artifact_size_bytes"] = artifact_path.stat().st_size
            print(f"[sur_trainer] Artifact copied: {artifact_path}")
        else:
            result["risk_notes"].append("ONNX artifact not found after forge")
            result["metrics"]["train_finished"] = False

        # Copy normalization params if present
        if params_src.exists():
            norm_target = artifact_path.with_name(artifact_path.stem + "_norm.mqh")
            shutil.copy2(params_src, norm_target)
            result["norm_artifact"] = str(norm_target)
            print(f"[sur_trainer] Norm params copied: {norm_target}")

    # Write result.json
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[sur_trainer] Result written: {result_path}")

    if proc.returncode != 0:
        print(f"[sur_trainer] FAILED exit={proc.returncode}", file=sys.stderr)
        return proc.returncode

    print("[sur_trainer] SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
