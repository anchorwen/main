"""Write-Audit-Publish (WAP) — atomic staging → production with rollback.

FIX-20260611-022: WAP staging pattern for critical data files.
Before this, data was written directly to production files — a crash
mid-write could leave half-written, corrupt state that the rest of
the system would silently consume.

Pattern::

    # 1. Write to staging
    wap = WAPStore(Path("data/staging"), Path("data/production"))
    wap.stage("governance_state.json", new_content_bytes)

    # 2. Audit the staged content
    errors = wap.audit("governance_state.json", validator=validate_governance)
    if errors:
        wap.reject("governance_state.json")  # Discard staged version

    # 3. Publish (atomic rename to production)
    wap.publish("governance_state.json")

    # On crash: staging directory is cleaned up, production is untouched.

The WAPStore also supports automatic rollback via snapshots:
    wap.snapshot("governance_state.json")  # Save current production
    wap.stage("governance_state.json", new_content)
    wap.publish("governance_state.json")  # On failure: wap.rollback()
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


class WAPStore:
    """Write-Audit-Publish store with atomic staging and rollback.

    Usage::

        wap = WAPStore(staging_dir, production_dir)
        wap.snapshot("governance_state.json")
        wap.stage("governance_state.json", new_bytes)
        if wap.audit("governance_state.json", my_validator):
            wap.publish("governance_state.json")
        else:
            wap.rollback("governance_state.json")
    """

    def __init__(self, staging_dir: Path, production_dir: Path):
        self._staging = staging_dir
        self._production = production_dir
        self._snapshots = staging_dir / ".snapshots"
        self._staging.mkdir(parents=True, exist_ok=True)
        self._production.mkdir(parents=True, exist_ok=True)
        self._snapshots.mkdir(parents=True, exist_ok=True)

    # ── Stage ────────────────────────────────────────────────────────────

    def stage(self, filename: str, content: bytes | str) -> Path:
        """Write content to staging area.  Does NOT touch production.

        Returns the staging file path.
        """
        staging_path = self._staging / filename
        staging_path.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(content, str):
            content = content.encode("utf-8")

        # Atomic write to staging
        tmp = staging_path.with_suffix(".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, staging_path)
        return staging_path

    def stage_json(self, filename: str, data: dict[str, Any]) -> Path:
        """Stage a dict as JSON."""
        return self.stage(filename, json.dumps(data, indent=2))

    # ── Audit ────────────────────────────────────────────────────────────

    def audit(
        self,
        filename: str,
        validator: Callable[[Path], list[str]] | None = None,
    ) -> list[str]:
        """Audit staged content.  Returns list of error messages.

        If validator is None, performs basic structural checks:
        - File exists and is non-empty.
        - If JSON, valid JSON syntax.

        Returns empty list if audit passes.
        """
        staging_path = self._staging / filename
        if not staging_path.exists():
            return [f"Staged file not found: {staging_path}"]

        if staging_path.stat().st_size == 0:
            return [f"Staged file is empty: {staging_path}"]

        # Basic JSON validity check
        if filename.endswith(".json"):
            try:
                json.loads(staging_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError) as e:
                return [f"Invalid JSON in {filename}: {e}"]

        # Custom validator
        if validator is not None:
            return validator(staging_path)

        return []

    # ── Publish ──────────────────────────────────────────────────────────

    def publish(self, filename: str) -> bool:
        """Atomically move staged file to production.

        Uses os.replace() which is atomic on the same filesystem.
        Returns True on success.
        """
        staging_path = self._staging / filename
        production_path = self._production / filename

        if not staging_path.exists():
            return False

        production_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_path, production_path)
        return True

    # ── Reject ───────────────────────────────────────────────────────────

    def reject(self, filename: str) -> None:
        """Discard staged content without publishing."""
        staging_path = self._staging / filename
        if staging_path.exists():
            staging_path.unlink()

    # ── Snapshot / Rollback ───────────────────────────────────────────────

    def snapshot(self, filename: str) -> Path | None:
        """Save current production version as a rollback snapshot.

        Returns the snapshot path, or None if production doesn't exist.
        """
        production_path = self._production / filename
        if not production_path.exists():
            return None

        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
        snapshot_path = self._snapshots / f"{filename}.{ts}"
        shutil.copy2(production_path, snapshot_path)
        return snapshot_path

    def rollback(self, filename: str) -> bool:
        """Restore the most recent snapshot to production.

        Returns True if rollback succeeded, False if no snapshot exists.
        """
        snapshots = sorted(
            self._snapshots.glob(f"{filename}.*"),
            reverse=True,
        )
        if not snapshots:
            return False

        latest = snapshots[0]
        production_path = self._production / filename
        shutil.copy2(latest, production_path)
        return True

    def list_snapshots(self, filename: str) -> list[Path]:
        """List all snapshots for a file, newest first."""
        return sorted(
            self._snapshots.glob(f"{filename}.*"),
            reverse=True,
        )

    def cleanup_snapshots(self, filename: str, keep: int = 5) -> int:
        """Remove old snapshots, keeping the most recent ``keep``.

        Returns the number of snapshots removed.
        """
        snapshots = sorted(
            self._snapshots.glob(f"{filename}.*"),
        )
        if len(snapshots) <= keep:
            return 0
        removed = 0
        for old in snapshots[:-keep]:
            old.unlink()
            removed += 1
        return removed


# ── Convenience factory ─────────────────────────────────────────────────────


def create_wap_store(base_dir: str = "data") -> WAPStore:
    """Create a WAPStore with standard directory layout.

    base_dir/
      production/     ← Live data (what the system reads)
      staging/        ← Staged data (written, audited, then published)
        .snapshots/   ← Rollback snapshots
    """
    base = Path(base_dir)
    return WAPStore(
        staging_dir=base / "staging",
        production_dir=base / "production",
    )
