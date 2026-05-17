"""Atomic file writer with staging and rollback.

Uses os.replace() for atomic file swaps within the same filesystem.
Temp files are written alongside targets (target_path.with_suffix('.tmp.staging'))
to guarantee same-device atomicity — avoids OSError 18 (cross-device link).

Usage:
    writer = AtomicFileWriter([path1, path2])
    writer.backup()          # take snapshot of current state
    # ... mutate files ...
    writer.stage()           # write new content to .tmp.staging files
    writer.commit()          # os.replace each staged file, clean up
    # if exception:
    writer.rollback()        # restore from .bak files, clean staging
"""

from __future__ import annotations

import shutil
from pathlib import Path


class AtomicFileError(RuntimeError):
    """Raised when an atomic file operation fails irrecoverably."""


class AtomicFileWriter:
    """Transactional file writer — all-or-nothing across multiple files."""

    def __init__(self, targets: list[Path] | None = None) -> None:
        self._targets: list[Path] = [Path(t) for t in targets] if targets else []
        self._staging: dict[Path, Path] = {}  # target → staging path
        self._backups: dict[Path, Path] = {}  # target → backup path
        self._committed = False

    def add(self, target: str | Path) -> None:
        self._targets.append(Path(target))

    @property
    def committed(self) -> bool:
        return self._committed

    # ── staging helpers ──

    @staticmethod
    def staging_path(target: Path) -> Path:
        """Return the .tmp.staging path for a given target, in the same directory."""
        return target.with_suffix(target.suffix + ".tmp.staging")

    @staticmethod
    def backup_path(target: Path) -> Path:
        """Return the .bak path for a given target."""
        return target.with_suffix(target.suffix + ".bak")

    # ── operations ──

    def backup(self) -> None:
        """Snapshot current state of all target files."""
        for target in self._targets:
            bak = self.backup_path(target)
            if target.exists():
                shutil.copy2(target, bak)
                self._backups[target] = bak

    def stage_content(self, target: Path, content: str) -> Path:
        """Write new content to staging file. Returns staging path."""
        staging = self.staging_path(target)
        staging.write_text(content, encoding="utf-8")
        self._staging[target] = staging
        return staging

    def stage_copy(self, source: Path, target: Path) -> Path:
        """Copy a prepared file to staging location.  Returns staging path."""
        staging = self.staging_path(target)
        if source != staging:
            shutil.copy2(source, staging)
        self._staging[target] = staging
        return staging

    def commit(self) -> None:
        """Atomically replace all targets with their staged versions."""
        if self._committed:
            return
        errors: list[str] = []
        for target in self._targets:
            staging = self._staging.get(target)
            if not staging or not staging.exists():
                continue
            try:
                staging.replace(target)
            except OSError as exc:
                errors.append(f"{target}: {exc}")

        if errors:
            raise AtomicFileError(
                f"Atomic commit failed for {len(errors)} file(s): {'; '.join(errors)}"
            )

        self._cleanup()
        self._committed = True

    def rollback(self) -> None:
        """Restore all targets from backups, if a backup exists."""
        for target in list(self._backups.keys()):
            bak = self._backups[target]
            if bak.exists():
                try:
                    bak.replace(target)
                except OSError:
                    pass

        self._cleanup()

    # ── internal ──

    def _cleanup(self) -> None:
        """Remove all staging and backup files."""
        for staging in self._staging.values():
            self._unlink(staging)
        for bak in self._backups.values():
            self._unlink(bak)
        self._staging.clear()
        self._backups.clear()

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
