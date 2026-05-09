"""Checkpoint Manager — save/resume for long-running training jobs.

Every trainer can optionally checkpoint its state (model weights, optimizer
state, scheduler state, epoch, best metrics) and resume from the last saved
checkpoint after interruption.

Usage:
    ckpt = CheckpointManager(Path("checkpoints/xgb_run_001"))
    ckpt.save(epoch=42, model_state=booster, metrics={"val_acc": 0.72})
    ...
    state = ckpt.load()  # or ckpt.latest() for non-blocking
    if state:
        resume_from_epoch = state["epoch"]
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class CheckpointInfo:
    """Metadata about one checkpoint."""

    path: Path
    epoch: int
    metrics: dict[str, Any]
    created_at: str
    file_size_bytes: int = 0


class CheckpointManager:
    """Manages checkpoint save/load/cleanup for a single training run.

    Directory layout:
        checkpoints/{run_id}/
            checkpoint.json        ← index (latest epoch + metrics summary)
            epoch_0042.ckpt        ← per-epoch checkpoint files
            best.ckpt              ← best-so-far copy

    The checkpoint payload is serialized as JSON for portability. Binary
    artifacts (model weights) are referenced by path, not embedded.
    """

    def __init__(self, run_dir: Path, max_keep: int = 5):
        self._dir = Path(run_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_keep = max_keep
        self._index_path = self._dir / "checkpoint.json"
        self._best_path = self._dir / "best.ckpt"

    # ── Save ──

    def save(
        self,
        epoch: int,
        model_state: dict[str, Any] | None = None,
        optimizer_state: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        is_best: bool = False,
    ) -> Path:
        """Save a checkpoint.

        Args:
            epoch: Current epoch (0-indexed or 1-indexed, caller's choice).
            model_state: Serializable model weights/params.
            optimizer_state: Serializable optimizer state.
            metrics: Current metrics dict (val_acc, loss, etc.).
            extra: Any additional state to preserve.
            is_best: If True, also copy to best.ckpt.

        Returns:
            Path to the saved checkpoint file.
        """
        now_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload: dict[str, Any] = {
            "epoch": epoch,
            "created_at": now_utc,
            "metrics": metrics or {},
            "model_state": model_state,
            "optimizer_state": optimizer_state,
        }
        if extra:
            payload["extra"] = extra

        ckpt_path = self._dir / f"epoch_{epoch:04d}.ckpt"
        ckpt_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        # Update index
        self._write_index(epoch, metrics or {}, now_utc)

        if is_best:
            shutil.copy2(ckpt_path, self._best_path)

        # Rotate old checkpoints
        self._rotate()
        return ckpt_path

    # ── Load ──

    def load(self, path: Path | None = None) -> dict[str, Any] | None:
        """Load a specific checkpoint, or the latest if path is None."""
        target = path or self.latest_path()
        if target is None or not target.exists():
            return None
        return json.loads(target.read_text(encoding="utf-8"))

    def latest(self) -> dict[str, Any] | None:
        """Load the latest checkpoint (convenience)."""
        return self.load()

    def best(self) -> dict[str, Any] | None:
        """Load the best checkpoint."""
        if not self._best_path.exists():
            return None
        return json.loads(self._best_path.read_text(encoding="utf-8"))

    # ── Query ──

    def latest_path(self) -> Path | None:
        """Return the path to the latest epoch checkpoint."""
        index = self._read_index()
        if index is None:
            return None
        latest_epoch = index.get("latest_epoch")
        if latest_epoch is None:
            return None
        ckpt = self._dir / f"epoch_{latest_epoch:04d}.ckpt"
        return ckpt if ckpt.exists() else None

    def latest_epoch(self) -> int:
        """Return the latest checkpointed epoch number, or -1 if none."""
        index = self._read_index()
        if index is None:
            return -1
        return index.get("latest_epoch", -1)

    def latest_metrics(self) -> dict[str, Any]:
        """Return the latest checkpoint's metrics, or empty dict."""
        index = self._read_index()
        if index is None:
            return {}
        return index.get("latest_metrics", {})

    def list_checkpoints(self) -> list[CheckpointInfo]:
        """List all checkpoints sorted by epoch."""
        ckpts: list[CheckpointInfo] = []
        for f in sorted(self._dir.glob("epoch_*.ckpt")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                ckpts.append(
                    CheckpointInfo(
                        path=f,
                        epoch=data.get("epoch", -1),
                        metrics=data.get("metrics", {}),
                        created_at=data.get("created_at", ""),
                        file_size_bytes=f.stat().st_size,
                    )
                )
            except (json.JSONDecodeError, KeyError):
                continue
        return ckpts

    def has_checkpoint(self) -> bool:
        return self.latest_path() is not None

    # ── Internal ──

    def _write_index(self, epoch: int, metrics: dict[str, Any], created_at: str) -> None:
        self._index_path.write_text(
            json.dumps(
                {"latest_epoch": epoch, "latest_metrics": metrics, "updated_at": created_at},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _read_index(self) -> dict[str, Any] | None:
        if not self._index_path.exists():
            return None
        try:
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _rotate(self) -> None:
        """Keep only the N most recent checkpoints."""
        ckpts = sorted(
            self._dir.glob("epoch_*.ckpt"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for old in ckpts[self._max_keep :]:
            old.unlink(missing_ok=True)
