"""Minimal trainer adapter for CRT pipeline.

Modes:
1) Placeholder mode (default): write a dummy artifact + stub metrics.
2) Lane command mode: run external trainer command template by lane and ingest result JSON.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from scripts.training.crt_manifest import CRTManifestV1

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent  # D:\future


def utc_now_iso_z() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def safe_name(model_id: str) -> str:
    return model_id.replace("@", "_at_").replace("/", "-")


def load_lane_templates(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError('lane-command-file must be a JSON object: {"lane":"cmd template"}')
    return {str(k): str(v) for k, v in payload.items()}


def render_cmd(template: str, values: dict[str, str]) -> str:
    return template.format(**values)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="your_trainer")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--artifacts-dir", type=Path, default=None)
    p.add_argument("--trainer-version", default="your-trainer-0.1.0")
    p.add_argument("--metric-winrate", type=float, default=0.51)
    p.add_argument("--metric-sharpe", type=float, default=1.00)
    p.add_argument("--lane-command-file", type=Path, default=None)
    p.add_argument(
        "--lane-command-template",
        default="",
        help="Direct command template; overrides lane-command-file for this run.",
    )
    p.add_argument(
        "--result-json-path",
        default="",
        help="Optional trainer output JSON path; placeholders allowed.",
    )
    p.add_argument("--shell", action="store_true", help="Run lane command in shell")
    p.add_argument("--timeout-seconds", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    return p


def load_manifest(path: Path) -> CRTManifestV1:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return CRTManifestV1.model_validate(payload)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest_path = args.manifest
    m = load_manifest(manifest_path)

    artifacts_dir = args.artifacts_dir or (manifest_path.parent / "artifacts")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifacts_dir / f"{safe_name(m.model_id)}.onnx"
    values = {
        "manifest_path": str(manifest_path),
        "model_id": m.model_id,
        "training_run_id": str(m.training_run_id or ""),
        "lane": m.lane,
        "dataset_slice_id": m.dataset_slice_id,
        "artifact_path": str(artifact_path),
    }

    lane_templates = load_lane_templates(args.lane_command_file)

    # Resolve lane key: for mtx, distinguish transformer vs xgboost via feature_contract_id
    lookup_lane = m.lane
    if lookup_lane == "mtx" and m.feature_contract_id:
        if "xgboost" in m.feature_contract_id.lower():
            lookup_lane = "mtx_xgb"

    command_template = (
        args.lane_command_template.strip() or lane_templates.get(lookup_lane, "").strip()
    )
    result_path = None
    lane_command = ""
    lane_stdout = ""
    lane_stderr = ""
    lane_exit_code = 0

    if not args.dry_run:
        if command_template:
            lane_command = render_cmd(command_template, values)
            proc = subprocess.run(
                lane_command,
                shell=args.shell,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=(None if args.timeout_seconds <= 0 else args.timeout_seconds),
                check=False,
                cwd=str(PROJECT_ROOT),
            )
            lane_exit_code = int(proc.returncode)
            lane_stdout = (proc.stdout or "")[-5000:]
            lane_stderr = (proc.stderr or "")[-5000:]
            if lane_exit_code != 0:
                raise SystemExit(
                    f"lane trainer failed exit={lane_exit_code}; stderr={lane_stderr or '<empty>'}"
                )
            if args.result_json_path.strip():
                rp = render_cmd(args.result_json_path.strip(), values)
                result_path = Path(rp)
        else:
            # Placeholder artifact; replace with real export output.
            artifact_path.write_bytes(b"CRT_ONNX_PLACEHOLDER\n")

        payload = m.model_dump(mode="json")
        payload["trainer_version"] = args.trainer_version
        payload["artifact_primary"] = str(artifact_path)
        payload["metrics"] = dict(payload.get("metrics") or {})
        payload["metrics"]["trained"] = True
        payload["metrics"]["train_finished_at_utc"] = utc_now_iso_z()

        if command_template:
            payload["metrics"]["lane_command"] = lane_command
            payload["metrics"]["lane_exit_code"] = lane_exit_code
            if lane_stdout:
                payload["metrics"]["lane_stdout_tail"] = lane_stdout
            if lane_stderr:
                payload["metrics"]["lane_stderr_tail"] = lane_stderr
        else:
            payload["metrics"]["winrate"] = args.metric_winrate
            payload["metrics"]["sharpe"] = args.metric_sharpe

        if result_path and result_path.exists():
            result_obj = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(result_obj, dict):
                ext_metrics = result_obj.get("metrics")
                if isinstance(ext_metrics, dict):
                    payload["metrics"].update(ext_metrics)
                if isinstance(result_obj.get("artifact_primary"), str):
                    payload["artifact_primary"] = result_obj["artifact_primary"]
                if isinstance(result_obj.get("norm_artifact"), str):
                    payload["norm_artifact"] = result_obj["norm_artifact"]
                ext_notes = result_obj.get("risk_notes")
                if isinstance(ext_notes, list):
                    payload["risk_notes"] = list(payload.get("risk_notes") or []) + [
                        str(x) for x in ext_notes
                    ]

        payload["risk_notes"] = list(payload.get("risk_notes") or [])
        if "stub batch row; run real trainer before promotion" in payload["risk_notes"]:
            payload["risk_notes"].remove("stub batch row; run real trainer before promotion")
        if command_template:
            payload["risk_notes"].append("lane trainer command executed")
        else:
            payload["risk_notes"].append("placeholder artifact; replace with real trainer output")
        validated = CRTManifestV1.model_validate(payload)
        manifest_path.write_text(
            json.dumps(validated.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "model_id": m.model_id,
                "training_run_id": m.training_run_id,
                "artifact_primary": str(artifact_path),
                "lane_command_used": bool(command_template),
                "dry_run": bool(args.dry_run),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
