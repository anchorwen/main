"""Batch training executor for CRT pipeline.

Reads individual manifest files from a batch plan directory and invokes
your_trainer.py for each model.

Usage:
  # Dry-run (default) – preview what would be executed
  python scripts/training/run_train_batch.py --batch-dir batch_plans/g2026.1

  # Execute all models
  python scripts/training/run_train_batch.py --batch-dir batch_plans/g2026.1 --execute

  # Execute with lane filter
  python scripts/training/run_train_batch.py --batch-dir batch_plans/g2026.1 --execute --lane sur

  # Execute with limit (first N models)
  python scripts/training/run_train_batch.py --batch-dir batch_plans/g2026.1 --execute --limit 3
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent  # D:\future
YOUR_TRAINER = THIS_DIR / "your_trainer.py"
LANE_COMMAND_FILE = THIS_DIR / "lane_trainers.json"


def render_command(template: str, job: dict) -> str:
    """Replace {placeholder} tokens in template with values from job dict."""
    result = template
    for key, value in job.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


def utc_now_iso_z() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_train_batch")
    p.add_argument(
        "--batch-dir",
        type=Path,
        required=True,
        help="Path to batch plan directory (contains manifests/ subdir)",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Actually run training; without this flag only dry-run",
    )
    p.add_argument(
        "--lane", type=str, default="", help="Only run models for this lane (e.g. sur, mtx, arb)"
    )
    p.add_argument("--limit", type=int, default=0, help="Only run first N models (0 = all)")
    p.add_argument(
        "--shell", action="store_true", default=True, help="Use shell for subprocess (default True)"
    )
    p.add_argument("--no-shell", dest="shell", action="store_false", help="Disable shell mode")
    p.add_argument(
        "--timeout-seconds",
        type=int,
        default=0,
        help="Timeout per model in seconds (0 = no timeout)",
    )
    p.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Directory for run reports (default: <batch-dir>/reports)",
    )
    return p


def collect_manifests(batch_dir: Path, lane_filter: str, limit: int) -> list[Path]:
    """Return sorted list of manifest paths, optionally filtered."""
    manifests_dir = batch_dir / "manifests"
    if not manifests_dir.is_dir():
        raise FileNotFoundError(f"Manifests directory not found: {manifests_dir}")

    paths = sorted(manifests_dir.glob("*.json"))
    if not paths:
        raise FileNotFoundError(f"No manifest files found in {manifests_dir}")

    if lane_filter:
        # Filter by lane in filename: CRT.<lane>.*.json
        filtered = []
        for p in paths:
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
                if m.get("lane") == lane_filter:
                    filtered.append(p)
            except Exception:  # noqa: BLE001
                # Include files we can't parse; they'll fail later
                if f".{lane_filter}." in p.name:
                    filtered.append(p)
        paths = filtered

    if limit > 0:
        paths = paths[:limit]

    return paths


def run_one(
    manifest_path: Path,
    execute: bool,
    shell: bool,
    timeout: int | None,
) -> dict[str, Any]:
    """Invoke your_trainer.py for one manifest. Returns run record dict."""
    parts = manifest_path.parts
    artifacts_dir = Path(*parts[:-1]) / "artifacts"
    result_json = manifest_path.with_suffix(manifest_path.suffix + ".result.json")

    cmd = [
        sys.executable,
        str(YOUR_TRAINER),
        "--manifest",
        str(manifest_path),
        "--lane-command-file",
        str(LANE_COMMAND_FILE),
        "--result-json-path",
        str(result_json),
        "--shell",
        "--artifacts-dir",
        str(artifacts_dir),
    ]

    record: dict[str, Any] = {
        "manifest": str(manifest_path),
        "model_id": "",
        "lane": "",
        "action": "execute" if execute else "dry-run",
        "command": " ".join(f'"{c}"' if " " in c else c for c in cmd),
        "started_at_utc": utc_now_iso_z(),
        "exit_code": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "duration_seconds": 0.0,
        "error": None,
    }

    try:
        # Load model_id + lane from manifest for reporting
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        record["model_id"] = str(m.get("model_id", ""))
        record["lane"] = str(m.get("lane", ""))
    except Exception:  # noqa: BLE001
        pass

    if not execute:
        record["exit_code"] = 0
        return record

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=shell,
            check=False,
            cwd=str(PROJECT_ROOT),
        )
        record["exit_code"] = proc.returncode
        record["stdout_tail"] = proc.stdout[-3000:] if proc.stdout else ""
        record["stderr_tail"] = proc.stderr[-3000:] if proc.stderr else ""
    except subprocess.TimeoutExpired as e:
        record["exit_code"] = -1
        record["error"] = f"Timeout after {timeout}s"
        record["stderr_tail"] = str(e)[:3000]
    except Exception as e:  # noqa: BLE001
        record["exit_code"] = -2
        record["error"] = str(e)[:3000]
    finally:
        record["duration_seconds"] = round(time.perf_counter() - t0, 3)

    return record


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    batch_dir = args.batch_dir.resolve()

    if not batch_dir.is_dir():
        print(f"[ERROR] Batch directory not found: {batch_dir}", file=sys.stderr)
        return 1

    manifests = collect_manifests(batch_dir, args.lane, args.limit)

    mode_str = "EXECUTE" if args.execute else "DRY-RUN"
    filter_str = f" lane={args.lane}" if args.lane else ""
    limit_str = f" limit={args.limit}" if args.limit else ""

    print(f"[{mode_str}] Batch: {batch_dir.name}  Models: {len(manifests)}{filter_str}{limit_str}")
    print("=" * 70)

    # Determine report path
    report_dir = args.report_dir or (batch_dir / "reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"run_report_{utc_now_iso_z().replace(':', '-')}.json"

    records: list[dict[str, Any]] = []
    n_ok = 0
    n_fail = 0

    for i, mp in enumerate(manifests, 1):
        tag = f"[{i:03d}/{len(manifests):03d}]"
        record = run_one(
            mp,
            execute=args.execute,
            shell=args.shell,
            timeout=(None if args.timeout_seconds <= 0 else args.timeout_seconds),
        )
        records.append(record)

        status = "OK" if record["exit_code"] == 0 else f"FAIL(exit={record['exit_code']})"
        if record["exit_code"] == 0:
            n_ok += 1
        else:
            n_fail += 1

        dur = f" {record['duration_seconds']:.1f}s" if record["duration_seconds"] > 0 else ""
        mid = record["model_id"] or mp.stem[:60]
        print(f"  {tag} {status}  {mid}{dur}")
        if record["error"]:
            print(f"          ERROR: {record['error'][:120]}")

    # Write report
    summary = {
        "batch_dir": str(batch_dir),
        "mode": "execute" if args.execute else "dry-run",
        "total": len(manifests),
        "ok": n_ok,
        "fail": n_fail,
        "lane_filter": args.lane or None,
        "limit": args.limit or None,
        "generated_at_utc": utc_now_iso_z(),
        "records": records,
    }
    report_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("=" * 70)
    print(f"[{mode_str}] DONE: {n_ok}/{len(manifests)} OK, {n_fail} FAILED")
    print(f"  Report: {report_path}")

    return 1 if n_fail > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
