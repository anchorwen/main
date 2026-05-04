"""CRT lane trainer wrapper: Meta_ppo_v4.5 Microstructure Execution (lane=mtx).

Bridges D:\\ai\\Meta_ppo_v4.5 training scripts into the CRT pipeline.

Two training modes:
  1. Transformer (03_Profit_Aware_Training.py)  → V4.3_Transformer_Core.pth  → .onnx export
  2. XGBoost    (03_XGBoost_Training.py)        → V4.X_XGBoost_Core.json

Protocol:
1. Accept --manifest-path (CRT manifest JSON, read-only input)
2. Accept --result-json-path (where to write result.json for your_trainer.py to ingest)
3. Accept --artifact-path (target path for artifact)
4. Accept --trainer-root (directory containing training scripts, default: D:\\ai\\Meta_ppo_v4.5)
5. Accept --mode (transformer | xgboost, default: transformer)
6. Execute the selected training script, convert to ONNX if transformer mode, copy artifacts
7. Write result.json with metrics / artifact_primary / norm_artifact / risk_notes

Usage (as lane command template):
  python scripts/training/trainers/mtx_trainer.py \\
    --manifest-path {manifest_path} \\
    --result-json-path {manifest_path}.result.json \\
    --artifact-path {artifact_path} \\
    --mode transformer
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


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
        prog="mtx_trainer",
        description="CRT lane trainer: Meta_ppo_v4.5 Microstructure Execution (lane=mtx)",
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
    p.add_argument("--artifact-path", type=Path, required=True, help="Target path for the artifact")
    p.add_argument(
        "--mode",
        choices=["transformer", "xgboost"],
        default="transformer",
        help="Training mode: transformer (03_Profit_Aware_Training.py) or xgboost (03_XGBoost_Training.py)",
    )
    p.add_argument(
        "--trainer-root",
        type=Path,
        default=Path(r"D:\ai\Meta_ppo_v4.5"),
        help="Directory containing Meta_ppo_v4.5 training scripts (default: D:\\ai\\Meta_ppo_v4.5)",
    )
    return p


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def run_mtx_transformer(trainer_root: Path) -> subprocess.CompletedProcess:
    """Run 03_Profit_Aware_Training.py directly (not -c) to avoid
    multiprocessing / DataLoader ACCESS_VIOLATION on Windows.
    Then, in a second step, export the resulting .pth to ONNX."""
    train_script = trainer_root / "03_Profit_Aware_Training.py"
    if not train_script.exists():
        raise FileNotFoundError(f"Transformer training script not found: {train_script}")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # Step 1 – patch 03_Profit_Aware_Training.py to set num_workers=0
    # because >=2 DataLoader workers crash on Windows via 0xC0000005
    original = train_script.read_text(encoding="utf-8")
    patched = original.replace(
        "train_loader = DataLoader(TensorDataset(X_train, Y_train), batch_size=BATCH_SIZE, shuffle=True, num_workers=workers)",
        "train_loader = DataLoader(TensorDataset(X_train, Y_train), batch_size=BATCH_SIZE, shuffle=True, num_workers=0)",
    ).replace(
        "test_loader = DataLoader(TensorDataset(X_test, Y_test), batch_size=BATCH_SIZE, shuffle=False, num_workers=workers)",
        "test_loader = DataLoader(TensorDataset(X_test, Y_test), batch_size=BATCH_SIZE, shuffle=False, num_workers=0)",
    )
    patched_script = train_script.with_name("03_Profit_Aware_Training_patched.py")
    patched_script.write_text(patched, encoding="utf-8")

    # Step 2 – run training (no -c inline, so DataLoader multiprocessing works)
    proc_train = subprocess.run(
        [sys.executable, str(patched_script)],
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
        cwd=str(trainer_root),
        env=env,
    )
    if proc_train.returncode != 0:
        return proc_train  # training failed → bail out

    # Step 2 – export ONNX
    export_script = rf"""
import os, sys
sys.path.insert(0, r"{trainer_root}")
import torch
import torch.nn as nn

pth_path = os.path.join(r"{trainer_root}", "V4.3_Transformer_Core.pth")
if not os.path.exists(pth_path):
    print("RESULT_ERROR=pth_not_found")
    sys.exit(4)

SEQ_LEN = 64
NUM_FEATURES = 9

class QuantTransformer(nn.Module):
    def __init__(self, num_features, d_model=64, n_heads=4, num_layers=2, dropout=0.2):
        super(QuantTransformer, self).__init__()
        self.feature_embedding = nn.Linear(num_features, d_model)
        self.pos_encoder = nn.Parameter(torch.zeros(1, SEQ_LEN, d_model))
        encoder_layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=n_heads, dropout=dropout, batch_first=True)
        self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)
        self.decoder = nn.Sequential(
            nn.Linear(d_model, 64),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(64, 1)
        )

    def forward(self, x):
        x = self.feature_embedding(x) + self.pos_encoder
        x = self.transformer_encoder(x)
        x = x.mean(dim=1)
        return self.decoder(x)

model = QuantTransformer(num_features=NUM_FEATURES)
model.load_state_dict(torch.load(pth_path, map_location="cpu"))
model.eval()
dummy = torch.randn(1, SEQ_LEN, NUM_FEATURES)
onnx_path = os.path.join(r"{trainer_root}", "mtx_transformer_core.onnx")
torch.onnx.export(
    model, dummy, onnx_path,
    export_params=True, do_constant_folding=True,
    input_names=["input"], output_names=["score"],
)
print(f"RESULT_ARTIFACT={{onnx_path}}")
"""
    proc_onnx = subprocess.run(
        [sys.executable, "-c", export_script],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        cwd=str(trainer_root),
        env=env,
    )
    # Merge stdout/stderr: training output + ONNX output
    combined_stdout = (proc_train.stdout or "") + (proc_onnx.stdout or "")
    combined_stderr = (proc_train.stderr or "") + (proc_onnx.stderr or "")
    # Return a synthetic CompletedProcess with merged outputs
    return subprocess.CompletedProcess(
        args=[sys.executable, str(train_script)],
        returncode=proc_onnx.returncode if proc_onnx.returncode != 0 else 0,
        stdout=combined_stdout,
        stderr=combined_stderr,
    )


def run_mtx_xgboost(trainer_root: Path) -> subprocess.CompletedProcess:
    """Run 03_XGBoost_Training.py directly."""
    train_script = trainer_root / "03_XGBoost_Training.py"
    if not train_script.exists():
        raise FileNotFoundError(f"XGBoost training script not found: {train_script}")

    inline = rf"""
import os, sys
os.chdir(r"{trainer_root}")
sys.path.insert(0, r"{trainer_root}")
exec(open(r"{train_script}", encoding="utf-8").read())

# Verify output
xgb_path = os.path.join(r"{trainer_root}", "V4.X_XGBoost_Core.json")
if os.path.exists(xgb_path):
    print(f"RESULT_ARTIFACT={{xgb_path}}")
else:
    print("RESULT_ERROR=xgb_json_not_found")
    sys.exit(5)
"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-c", inline]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
        cwd=trainer_root,
        env=env,
    )
    return proc


def parse_accuracy_from_stdout(stdout: str) -> dict[str, Any]:
    """Extract accuracy metrics from training script print output."""
    metrics: dict[str, Any] = {}
    val_acc_match = re.search(r"Val Acc:?\s*[\d\s]*\(Acc:\s*([\d.]+)%\)", stdout)
    if val_acc_match:
        metrics["val_accuracy_pct"] = float(val_acc_match.group(1))

    best_iter_match = re.search(r"最佳树数量:\s*(\d+)", stdout)
    if best_iter_match:
        metrics["best_iterations"] = int(best_iter_match.group(1))

    xgb_acc_match = re.search(r"盲区极限胜率.*?:\s*([\d.]+)%", stdout)
    if xgb_acc_match:
        metrics["test_accuracy_pct"] = float(xgb_acc_match.group(1))

    return metrics


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    manifest = load_manifest(args.manifest_path)
    model_id = manifest.get("model_id", "unknown")
    lane = manifest.get("lane", "mtx")
    generation = manifest.get("generation", "g2026.1")

    trainer_root = args.trainer_root.resolve()
    artifact_path = args.artifact_path.resolve()
    result_path = args.result_json_path.resolve()

    print(f"[mtx_trainer] Lane={lane}  Model={model_id}  Generation={generation}")
    print(f"[mtx_trainer] Mode={args.mode}  Trainer root: {trainer_root}")
    print(f"[mtx_trainer] Artifact target: {artifact_path}")

    # Run training
    if args.mode == "transformer":
        print("[mtx_trainer] Starting Meta_ppo_v4.5 Transformer training...")
        proc = run_mtx_transformer(trainer_root)
    else:
        print("[mtx_trainer] Starting Meta_ppo_v4.5 XGBoost training...")
        proc = run_mtx_xgboost(trainer_root)

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    print(stdout[:5000] if stdout else "(no stdout)")
    if stderr:
        print(f"[mtx_trainer] STDERR:\n{stderr[-3000:]}", file=sys.stderr)

    # --- Build result.json ---
    return_code = proc.returncode if proc.returncode is not None else -1
    result: dict[str, Any] = {
        "trainer": "mtx_trainer",
        "trainer_version": "mtx-ppo-v4.5-1.0.0",
        "completed_at_utc": utc_now_iso_z(),
        "model_id": model_id,
        "lane": lane,
        "generation": generation,
        "mode": args.mode,
        "exit_code": return_code,
        "metrics": {
            "train_finished": return_code == 0,
            "trainer_exit_code": return_code,
            "mode": args.mode,
            "trainer_root": str(trainer_root),
        },
        "risk_notes": [],
        "artifact_primary": None,
        "norm_artifact": None,
    }

    # Parse RESULT_ lines from stdout (defend None)
    stdout_lines = (stdout or "").splitlines()
    for line in stdout_lines:
        line = line.strip()
        if line.startswith("RESULT_ARTIFACT="):
            result["artifact_primary"] = line.split("=", 1)[1]
        elif line.startswith("RESULT_ERROR="):
            result["risk_notes"].append(f"training_error: {line.split('=', 1)[1]}")

    # Parse accuracy metrics from training output
    result["metrics"].update(parse_accuracy_from_stdout(stdout or ""))

    if return_code != 0:
        result["risk_notes"].append(f"training exited with code {return_code}")
        result["metrics"]["error_tail"] = stderr[-2000:] if stderr else ""
    else:
        # Copy artifacts to target
        if args.mode == "transformer":
            onnx_src = trainer_root / "mtx_transformer_core.onnx"
            pth_src = trainer_root / "V4.3_Transformer_Core.pth"
        else:
            onnx_src = None
            pth_src = trainer_root / "V4.X_XGBoost_Core.json"

        if args.mode == "transformer" and onnx_src and onnx_src.exists():
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(onnx_src, artifact_path)
            result["artifact_primary"] = str(artifact_path)
            result["metrics"]["artifact_size_bytes"] = artifact_path.stat().st_size
            print(f"[mtx_trainer] ONNX artifact copied: {artifact_path}")

            # Also copy the .pth as companion artifact
            if pth_src.exists():
                pth_target = artifact_path.with_name(artifact_path.stem + ".pth")
                shutil.copy2(pth_src, pth_target)
                result["metrics"]["pth_artifact_size_bytes"] = pth_target.stat().st_size
                print(f"[mtx_trainer] PTH companion copied: {pth_target}")
        elif args.mode == "xgboost" and pth_src.exists():
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pth_src, artifact_path)
            result["artifact_primary"] = str(artifact_path)
            result["metrics"]["artifact_size_bytes"] = artifact_path.stat().st_size
            print(f"[mtx_trainer] XGBoost JSON artifact copied: {artifact_path}")
        else:
            result["risk_notes"].append("Artifact not found after training")
            result["metrics"]["train_finished"] = False

    # Write result.json
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[mtx_trainer] Result written: {result_path}")

    if proc.returncode != 0:
        print(f"[mtx_trainer] FAILED exit={proc.returncode}", file=sys.stderr)
        return proc.returncode

    print("[mtx_trainer] SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
