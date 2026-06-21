"""State Writer — the sole authorised I/O channel for all ephemeral state files.

Every state JSON written by the system MUST pass through this gate.
Direct ``json.dump()`` to a state path is an architecture violation
(Iron Law #0 — Pre-Edit Mandatory Checklist).

Physical guarantees provided by this module:

    1. Schema Dictatorship  — data is validated BEFORE any byte touches disk
    2. Atomic Write          — .tmp → fsync → os.replace (no partial files)
    3. Cross-Symbol Guard    — alpha_registry rejects foreign-symbol pollution
    4. Audit Trail           — every write logs (logical_id, path, size, validator)

Usage::

    from core.state.catalog import lookup, DataIntegrityError
    from core.state.writer import StateWriter

    writer = StateWriter(data_dir="data")
    artifact = lookup("LEADERBOARD")

    try:
        result = writer.write_artifact(artifact, "XAUUSDc", leaderboard_dict)
        print(f"Written: {result['path']} ({result['size_bytes']} bytes)")
    except DataIntegrityError as exc:
        logger.error("State write rejected: %s", exc)

See Also:
    - Catalog:   core/state/catalog.py
    - Iron Law:  CLAUDE.md § Iron Law #0 (Pre-Edit Checklist)
    - DQAF-046:  XAU dual-track feature pipeline (related catalog pattern)
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.state.catalog import (
    ALPHA_ID_SYMBOL_PREFIXES,
    CrossSymbolContaminationError,
    StateArtifact,
)

logger = logging.getLogger(__name__)

# ── Write result type ──────────────────────────────────────────────────


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


# ═══════════════════════════════════════════════════════════════════════════════
# StateWriter
# ═══════════════════════════════════════════════════════════════════════════════


class StateWriter:
    """Authoritative writer for all ephemeral state files.

    Each instance is bound to one symbol's data directory.  All writes
    go through :meth:`write_artifact` which enforces schema validation,
    cross-symbol guards, and atomic I/O.
    """

    def __init__(self, data_dir: str | Path, *, symbol: str = "XAUUSDc"):
        """Initialise the writer for a specific data directory.

        Args:
            data_dir: Path to the symbol's data directory (e.g. ``data`` or ``data_btc``).
            symbol: The trading symbol this directory serves (e.g. ``XAUUSDc``).
        """
        self._data_dir = Path(data_dir).resolve()
        self._symbol = symbol

    # ── Public API ──────────────────────────────────────────────────────

    def write_artifact(
        self,
        artifact: StateArtifact,
        symbol: str,
        data: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Validate *data* against *artifact* schema and atomically write to disk.

        This is the ONE method that all state-file writes must go through.

        Args:
            artifact: Catalog entry defining the state file.
            symbol: Trading symbol (``XAUUSDc`` or ``BTCUSDc``) — used for
                    cross-symbol contamination checks.
            data: The serialisable dict to write.
            dry_run: If True, validate only — do not touch disk.

        Returns:
            Dict with ``written``, ``path``, ``size_bytes``, ``validated`` keys.

        Raises:
            DataIntegrityError: Schema validation failed.
            CrossSymbolContaminationError: Cross-symbol invariant violated.
            OSError: Disk write failed (after temp-file cleanup attempt).
        """
        target_path = self._resolve_path(artifact)

        # ── Gate 1: Required fields ──
        if artifact.required_fields:
            from core.state.catalog import _must_have_fields
            _must_have_fields(data, artifact.required_fields)

        # ── Gate 2: Schema validation ──
        artifact.schema_validator(data)

        # ── Gate 3: Cross-symbol contamination guard ──
        if artifact.cross_symbol_guard:
            self._check_cross_symbol(data, symbol, artifact.logical_id)

        if dry_run:
            return {
                "written": True,
                "dry_run": True,
                "path": str(target_path),
                "validated": True,
                "artifact_id": artifact.logical_id,
            }

        # ── Gate 4: Atomic write ──
        size_bytes = self._atomic_write(target_path, data)

        logger.info(
            "StateWriter: wrote %s → %s (%d bytes)",
            artifact.logical_id,
            target_path,
            size_bytes,
        )

        return {
            "written": True,
            "path": str(target_path),
            "size_bytes": size_bytes,
            "validated": True,
            "artifact_id": artifact.logical_id,
            "written_at": _utc_now_iso(),
        }

    # ── Internal ────────────────────────────────────────────────────────

    def _resolve_path(self, artifact: StateArtifact) -> Path:
        """Resolve the target file path from the artifact's path_template."""
        return self._data_dir / artifact.path_template

    def _atomic_write(self, target_path: Path, data: dict[str, Any]) -> int:
        """Write data to a temp file, fsync, then atomically rename.

        Guarantees:
            - The target file is never in a partially-written state.
            - If the process dies mid-write, only the .tmp file is left behind.
            - On success, the target file is a complete, valid JSON document.

        Returns:
            Size of the written file in bytes.
        """
        target_path = target_path.resolve()
        parent = target_path.parent
        parent.mkdir(parents=True, exist_ok=True)

        # Serialize BEFORE opening the file — catch JSON errors early
        json_bytes = json.dumps(data, indent=2, ensure_ascii=False, default=str).encode("utf-8")

        # Write to a sibling .tmp file in the same directory
        # (same filesystem → os.replace is atomic on POSIX and Windows)
        tmp_fd = -1
        tmp_path = ""
        try:
            # Create temp file in the same directory as the target
            tmp_fd, tmp_path = tempfile.mkstemp(
                suffix=".tmp",
                prefix=f".{target_path.name}.",
                dir=str(parent),
            )
            # Write all bytes
            os.write(tmp_fd, json_bytes)
            # Flush to OS
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            tmp_fd = -1

            # Atomic rename — replaces target in a single filesystem operation
            os.replace(tmp_path, str(target_path))

            # Ensure directory metadata is durable
            self._fsync_dir(parent)

            return len(json_bytes)

        except Exception:
            # Clean up temp file on failure
            if tmp_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(tmp_fd)
            if tmp_path and os.path.exists(tmp_path):
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
            raise

    @staticmethod
    def _fsync_dir(dir_path: Path) -> None:
        """Flush directory metadata to disk (best-effort, not available on all platforms)."""
        with contextlib.suppress(OSError):
            fd = os.open(str(dir_path), os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)

    def _check_cross_symbol(
        self,
        data: dict[str, Any],
        symbol: str,
        artifact_id: str,
    ) -> None:
        """Verify that alpha_ids in the data belong to the target symbol.

        This prevents btc_swing from leaking into XAU's alpha_registry
        (the exact bug discovered during the DQAF-044 architecture audit).
        """
        records = data.get("records") or data.get("alphas") or []
        if isinstance(records, dict):
            records = list(records.values())

        foreign_ids: list[str] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            alpha_id = rec.get("alpha_id") or rec.get("id") or ""
            if not alpha_id:
                continue

            expected_symbol = None
            for prefix, sym in ALPHA_ID_SYMBOL_PREFIXES.items():
                if alpha_id.startswith(prefix):
                    expected_symbol = sym
                    break

            if expected_symbol and expected_symbol != symbol:
                foreign_ids.append(alpha_id)

        if foreign_ids:
            raise CrossSymbolContaminationError(
                f"{artifact_id}: cross-symbol contamination detected — "
                f"target symbol is {symbol!r} but alpha_ids {foreign_ids} "
                f"belong to a different symbol",
                artifact_id=artifact_id,
                foreign_ids=foreign_ids,
            )

    # ── Factory methods ─────────────────────────────────────────────────

    @classmethod
    def from_state_path(cls, state_file_path: str | Path) -> StateWriter:
        """Create a StateWriter by inferring data_dir and symbol from a state file path.

        Walks up the directory tree to find ``data`` or ``data_btc`` and
        derives the symbol from the directory name.

        This is the preferred factory for modules like governance_service,
        alpha registry, and execution_state that receive a full file path
        rather than a data_dir + symbol pair.

        Raises:
            ValueError: If the path does not reside under a recognised data directory.
        """
        p = Path(state_file_path).resolve()
        for parent in p.parents:
            if parent.name == "data_btc":
                return cls(str(parent), symbol="BTCUSDc")
            if parent.name == "data":
                return cls(str(parent), symbol="XAUUSDc")
        raise ValueError(
            f"Cannot determine data directory from path: {state_file_path!r}. "
            f"State files must reside under 'data/' or 'data_btc/'."
        )

    # ── Convenience methods ─────────────────────────────────────────────

    def write_leaderboard(self, data: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        """Write leaderboard.json."""
        from core.state.catalog import lookup
        return self.write_artifact(lookup("LEADERBOARD"), self._symbol, data, dry_run=dry_run)

    def write_alpha_allocation(self, data: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        """Write alpha_allocation.json."""
        from core.state.catalog import lookup
        return self.write_artifact(lookup("ALPHA_ALLOCATION"), self._symbol, data, dry_run=dry_run)

    def write_governance_state(self, data: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        """Write governance_state.json."""
        from core.state.catalog import lookup
        return self.write_artifact(lookup("GOVERNANCE_STATE"), self._symbol, data, dry_run=dry_run)

    def write_alpha_registry(self, data: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        """Write alpha_registry.json (cross-symbol guard active)."""
        from core.state.catalog import lookup
        return self.write_artifact(lookup("ALPHA_REGISTRY"), self._symbol, data, dry_run=dry_run)

    def write_daily_ops_state(self, data: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        """Write state/daily_ops_state.json."""
        from core.state.catalog import lookup
        return self.write_artifact(lookup("DAILY_OPS_STATE"), self._symbol, data, dry_run=dry_run)
