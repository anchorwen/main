"""Lightweight Experiment Tracker — structured training run logging.

Provides a file-based experiment log (JSONL) with no external dependencies.
Designed as a zero-cost bridge to MLflow/W&B: every run is recorded in the
same schema, so migrating to a full experiment platform later is a one-shot
export.

Usage:
    tracker = ExperimentTracker(Path("experiments/"))
    run = tracker.start_run(
        architecture="deep_res_mlp",
        recipe_id="deep-res-mlp-g2026.1",
        tags=["v9", "resmlp"],
    )
    ... training ...
    tracker.log_metrics(run.run_id, {"val_acc": 0.72, "loss": 0.35})
    tracker.end_run(run.run_id, status="completed")
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class RunInfo:
    """Metadata for one training run."""

    run_id: str
    architecture: str
    status: str  # "running" | "completed" | "failed"
    started_at: str
    ended_at: str = ""
    recipe_id: str = ""
    dataset_id: str = ""
    tags: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    metrics: list[dict[str, Any]] = field(default_factory=list)  # [{step, ...metrics}]
    best_metrics: dict[str, Any] = field(default_factory=dict)
    model_path: str = ""
    exit_code: int = 0
    error_message: str = ""


class ExperimentTracker:
    """File-based experiment tracker with JSONL backend.

    Directory layout:
        experiments/
            runs.jsonl           ← all runs (append-only)
            {run_id}/
                metrics.jsonl     ← per-step metrics log
                run.json          ← run summary
    """

    def __init__(self, root_dir: Path | str):
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._runs_path = self._root / "runs.jsonl"

    # ── Lifecycle ──

    def start_run(
        self,
        architecture: str,
        *,
        recipe_id: str = "",
        dataset_id: str = "",
        tags: list[str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> RunInfo:
        run_id = (
            f"{architecture}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        )
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        run = RunInfo(
            run_id=run_id,
            architecture=architecture,
            status="running",
            started_at=now,
            recipe_id=recipe_id,
            dataset_id=dataset_id,
            tags=tags or [],
            params=params or {},
        )

        self._write_run_summary(run)
        self._append_runs_index(run)
        return run

    def log_metrics(self, run_id: str, metrics: dict[str, Any], step: int | None = None) -> None:
        """Log metrics for a given step."""
        run_dir = self._root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "metrics": metrics,
        }
        if step is not None:
            entry["step"] = step
        with open(run_dir / "metrics.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def end_run(
        self,
        run_id: str,
        status: str = "completed",
        *,
        best_metrics: dict[str, Any] | None = None,
        model_path: str = "",
        exit_code: int = 0,
        error_message: str = "",
    ) -> None:
        """Mark a run as finished and update the summary."""
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        run = self._read_run_summary(run_id)
        if run is None:
            return
        run.status = status
        run.ended_at = now
        run.exit_code = exit_code
        run.error_message = error_message
        if best_metrics:
            run.best_metrics = best_metrics
        if model_path:
            run.model_path = model_path
        self._write_run_summary(run)

    # ── Query ──

    def get_run(self, run_id: str) -> RunInfo | None:
        return self._read_run_summary(run_id)

    def list_runs(
        self, architecture: str | None = None, status: str | None = None
    ) -> list[RunInfo]:
        runs: list[RunInfo] = []
        for run_dir in sorted(self._root.iterdir()):
            if not run_dir.is_dir():
                continue
            run_json = run_dir / "run.json"
            if not run_json.exists():
                continue
            try:
                r = self._read_run_summary(run_dir.name)
                if r is None:
                    continue
                if architecture and r.architecture != architecture:
                    continue
                if status and r.status != status:
                    continue
                runs.append(r)
            except (json.JSONDecodeError, TypeError):
                continue
        return runs

    def get_metrics(self, run_id: str) -> list[dict[str, Any]]:
        metrics_path = self._root / run_id / "metrics.jsonl"
        if not metrics_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in metrics_path.read_text(encoding="utf-8").strip().split("\n"):
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries

    # ── Internal ──

    def _run_dir(self, run_id: str) -> Path:
        d = self._root / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_run_summary(self, run: RunInfo) -> None:
        path = self._run_dir(run.run_id) / "run.json"
        path.write_text(
            json.dumps(
                {
                    "run_id": run.run_id,
                    "architecture": run.architecture,
                    "status": run.status,
                    "started_at": run.started_at,
                    "ended_at": run.ended_at,
                    "recipe_id": run.recipe_id,
                    "dataset_id": run.dataset_id,
                    "tags": run.tags,
                    "params": run.params,
                    "best_metrics": run.best_metrics,
                    "model_path": run.model_path,
                    "exit_code": run.exit_code,
                    "error_message": run.error_message,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _read_run_summary(self, run_id: str) -> RunInfo | None:
        path = self._root / run_id / "run.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return RunInfo(
            run_id=data.get("run_id", run_id),
            architecture=data.get("architecture", ""),
            status=data.get("status", "unknown"),
            started_at=data.get("started_at", ""),
            ended_at=data.get("ended_at", ""),
            recipe_id=data.get("recipe_id", ""),
            dataset_id=data.get("dataset_id", ""),
            tags=data.get("tags", []),
            params=data.get("params", {}),
            best_metrics=data.get("best_metrics", {}),
            model_path=data.get("model_path", ""),
            exit_code=data.get("exit_code", 0),
            error_message=data.get("error_message", ""),
        )

    def _append_runs_index(self, run: RunInfo) -> None:
        summary = {
            "run_id": run.run_id,
            "architecture": run.architecture,
            "status": run.status,
            "started_at": run.started_at,
            "ended_at": run.ended_at,
            "recipe_id": run.recipe_id,
            "dataset_id": run.dataset_id,
            "tags": run.tags,
            "best_metrics": run.best_metrics,
            "model_path": run.model_path,
            "exit_code": run.exit_code,
            "error_message": run.error_message,
        }
        with open(self._runs_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")


# Module-level singleton for convenience
tracker = ExperimentTracker(Path("experiments"))
