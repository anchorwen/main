"""Distributed lock for multi-process mutual exclusion.

File-based advisory locks with automatic expiry — zero external dependencies.
Prevents duplicate order dispatch, concurrent training runs, and other
critical-section violations in multi-process deployments.

Supports three backends:
- ``file`` — OS-level file locks (works across processes on the same machine)
- ``directory`` — atomic directory creation (works across NFS, no fcntl dependency)
- ``auto`` — tries file first, falls back to directory

Usage:
    from core.infrastructure.distributed_lock import DistributedLock

    lock = DistributedLock("training_xgb_v9", backend="auto", ttl_seconds=3600)
    if lock.acquire():
        try:
            run_training()
        finally:
            lock.release()
    else:
        print("Another process is already training")
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Lock result ───────────────────────────────────────────────────────────────


@dataclass
class LockAcquireResult:
    acquired: bool
    lock_name: str
    holder_id: str
    expires_at: str
    wait_ms: float
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "acquired": self.acquired,
            "lock_name": self.lock_name,
            "holder_id": self.holder_id,
            "expires_at": self.expires_at,
            "wait_ms": self.wait_ms,
            "error": self.error,
        }


# ── Base lock ─────────────────────────────────────────────────────────────────


class BaseLock:
    """Abstract lock interface."""

    def acquire(self, *, blocking: bool = False, timeout_seconds: float = 0) -> LockAcquireResult:
        raise NotImplementedError

    def release(self) -> bool:
        raise NotImplementedError

    def refresh(self) -> bool:
        """Update the acquired_at timestamp. Optional — default no-op."""
        return self.is_held

    @property
    def is_held(self) -> bool:
        raise NotImplementedError


# ── File-based lock (fcntl / msvcrt) ──────────────────────────────────────────


class FileLock(BaseLock):
    """PID-file advisory lock.

    Writes the holder's PID and metadata to a lock file. On acquire, checks
    whether the previous holder is still alive. Works cross-platform without
    fcntl/msvcrt dependencies.
    """

    def __init__(
        self,
        name: str,
        *,
        lock_dir: str = "locks",
        ttl_seconds: float = 300.0,
    ) -> None:
        self._name = name
        self._ttl = ttl_seconds
        self._lock_dir = Path(lock_dir)
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = self._lock_dir / f"{name}.lock"
        self._holder_id = ""
        self._acquired = False

    def acquire(self, *, blocking: bool = False, timeout_seconds: float = 0.0) -> LockAcquireResult:
        t0 = time.time()

        # ── Guard: same instance cannot re-acquire ──
        if self._acquired:
            return LockAcquireResult(
                acquired=False,
                lock_name=self._name,
                holder_id=self._holder_id,
                expires_at="",
                wait_ms=(time.time() - t0) * 1000,
                error="Lock already held by this instance",
            )

        self._holder_id = uuid.uuid4().hex[:12]

        if self._lock_path.exists() and self._is_stale():
            logger.info("Removing stale lock: %s", self._name)
            self._force_release()

        deadline = t0 + timeout_seconds if blocking and timeout_seconds > 0 else t0

        _lock_data = json.dumps(
            {
                "name": self._name,
                "holder_id": self._holder_id,
                "pid": os.getpid(),
                "acquired_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                "ttl_seconds": self._ttl,
            }
        )

        while True:
            try:
                # Atomic exclusive create — os.O_CREAT|O_EXCL fails with
                # FileExistsError if the lock file already exists.
                # This is the cross-platform equivalent of O_CREAT|O_EXCL
                # and does NOT suffer from os.replace()'s overwrite semantics.
                fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, _lock_data.encode("utf-8"))
                finally:
                    os.close(fd)
                self._acquired = True
                return LockAcquireResult(
                    acquired=True,
                    lock_name=self._name,
                    holder_id=self._holder_id,
                    expires_at=datetime.now(UTC).replace(tzinfo=None).isoformat(),
                    wait_ms=(time.time() - t0) * 1000,
                )
            except FileExistsError:
                # Lock file already exists — held by another process
                pass
            except OSError:
                # Permission error, disk full, etc.
                pass
            if not blocking:
                return LockAcquireResult(
                    acquired=False,
                    lock_name=self._name,
                    holder_id="",
                    expires_at="",
                    wait_ms=(time.time() - t0) * 1000,
                    error="Lock held by another process",
                )
            if time.time() >= deadline:
                return LockAcquireResult(
                    acquired=False,
                    lock_name=self._name,
                    holder_id="",
                    expires_at="",
                    wait_ms=(time.time() - t0) * 1000,
                    error="Timeout waiting for lock",
                )
            time.sleep(0.05)

    def refresh(self) -> bool:
        """Update the acquired_at timestamp to extend the TTL.

        Must be called periodically (e.g. every 60s) during long-running
        operations.  If the lock file is missing or corrupted, returns False
        (the lock is considered lost).

        DQAF-20260616-004: Without refresh, a healthy long-running process
        appears stale after TTL expiry, and a hung process cannot be
        distinguished from a healthy one.
        """
        if not self._acquired:
            return False
        try:
            if not self._lock_path.exists():
                self._acquired = False
                return False
            _lock_data = json.dumps(
                {
                    "name": self._name,
                    "holder_id": self._holder_id,
                    "pid": os.getpid(),
                    "acquired_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                    "ttl_seconds": self._ttl,
                }
            )
            _tmp_path = self._lock_path.with_suffix(".tmp")
            _tmp_path.write_text(_lock_data, encoding="utf-8")
            os.replace(str(_tmp_path), str(self._lock_path))
            return True
        except OSError:
            return False

    def release(self) -> bool:
        if not self._acquired:
            return False
        self._acquired = False
        self._force_release()
        return True

    @property
    def is_held(self) -> bool:
        return self._acquired

    @property
    def holder_info(self) -> dict[str, Any] | None:
        """Read lock metadata without acquiring."""
        if not self._lock_path.exists():
            return None
        try:
            return json.loads(self._lock_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def _is_stale(self) -> bool:
        try:
            data = json.loads(self._lock_path.read_text(encoding="utf-8"))
            pid = data.get("pid", 0)
            # Check if holder process is still alive
            if pid and self._pid_exists(pid):
                acquired_at = data.get("acquired_at", "")
                if acquired_at:
                    acquired = datetime.fromisoformat(acquired_at)
                    age = (
                        datetime.now(UTC).replace(tzinfo=None) - acquired.replace(tzinfo=None)
                    ).total_seconds()
                    return age > data.get("ttl_seconds", self._ttl)
                return False
            return True  # holder process dead → stale
        except Exception:  # noqa: BLE001
            return True

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, SystemError):
            # Windows: os.kill(pid, 0) raises SystemError (signal 0 unsupported)
            return False

    def _force_release(self) -> None:
        try:
            if self._lock_path.exists():
                self._lock_path.unlink()
        except Exception:  # noqa: BLE001
            pass


# ── Directory-based lock (NFS-safe, no fcntl) ─────────────────────────────────


class DirectoryLock(BaseLock):
    """Atomic directory-creation lock.

    Uses ``os.mkdir`` which is atomic on most filesystems including NFS.
    No dependency on fcntl/flock — works everywhere Python runs.
    """

    def __init__(
        self,
        name: str,
        *,
        lock_dir: str = "locks",
        ttl_seconds: float = 300.0,
    ) -> None:
        self._name = name
        self._ttl = ttl_seconds
        self._lock_dir = Path(lock_dir)
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        self._dir_path = self._lock_dir / f"{name}.dir.lock"
        self._holder_id = ""
        self._acquired = False

    def acquire(self, *, blocking: bool = False, timeout_seconds: float = 0.0) -> LockAcquireResult:
        t0 = time.time()
        self._holder_id = uuid.uuid4().hex[:12]

        if self._dir_path.exists() and self._is_stale():
            logger.info("Removing stale dir lock: %s", self._name)
            self._force_release()

        deadline = t0 + timeout_seconds if blocking and timeout_seconds > 0 else t0

        while True:
            try:
                self._dir_path.mkdir()
                (self._dir_path / "metadata.json").write_text(
                    json.dumps(
                        {
                            "name": self._name,
                            "holder_id": self._holder_id,
                            "pid": os.getpid(),
                            "acquired_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                            "ttl_seconds": self._ttl,
                        }
                    )
                )
                self._acquired = True
                return LockAcquireResult(
                    acquired=True,
                    lock_name=self._name,
                    holder_id=self._holder_id,
                    expires_at=datetime.now(UTC).replace(tzinfo=None).isoformat(),
                    wait_ms=(time.time() - t0) * 1000,
                )
            except FileExistsError:
                if not blocking:
                    return LockAcquireResult(
                        acquired=False,
                        lock_name=self._name,
                        holder_id="",
                        expires_at="",
                        wait_ms=(time.time() - t0) * 1000,
                        error="Lock held by another process",
                    )
                if time.time() >= deadline:
                    return LockAcquireResult(
                        acquired=False,
                        lock_name=self._name,
                        holder_id="",
                        expires_at="",
                        wait_ms=(time.time() - t0) * 1000,
                        error="Timeout waiting for lock",
                    )
                time.sleep(0.05)

    def release(self) -> bool:
        if not self._acquired:
            return False
        self._acquired = False
        self._force_release()
        return True

    @property
    def is_held(self) -> bool:
        return self._acquired

    @property
    def holder_info(self) -> dict[str, Any] | None:
        meta = self._dir_path / "metadata.json"
        if not meta.exists():
            return None
        try:
            return json.loads(meta.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def _is_stale(self) -> bool:
        meta = self._dir_path / "metadata.json"
        if not meta.exists():
            return True
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            acquired_at = data.get("acquired_at", "")
            if not acquired_at:
                return True
            acquired = datetime.fromisoformat(acquired_at)
            age = (
                datetime.now(UTC).replace(tzinfo=None) - acquired.replace(tzinfo=None)
            ).total_seconds()
            return age > data.get("ttl_seconds", self._ttl)
        except Exception:  # noqa: BLE001
            return True

    def _force_release(self) -> None:
        import shutil

        try:
            if self._dir_path.exists():
                shutil.rmtree(self._dir_path)
        except Exception:  # noqa: BLE001
            pass


# ── Lock factory ──────────────────────────────────────────────────────────────


def get_lock(
    name: str,
    *,
    backend: str = "auto",
    lock_dir: str = "locks",
    ttl_seconds: float = 300.0,
) -> BaseLock:
    """Create a distributed lock for the given resource name.

    Args:
        name: Unique lock name (e.g. "training_xgb", "dispatch_order").
        backend: ``"file"``, ``"directory"``, or ``"auto"``.
        lock_dir: Directory for lock files.
        ttl_seconds: Maximum lock lifetime before considered stale.

    Returns:
        A BaseLock implementation.
    """
    if backend == "file":
        return FileLock(name, lock_dir=lock_dir, ttl_seconds=ttl_seconds)
    if backend == "directory":
        return DirectoryLock(name, lock_dir=lock_dir, ttl_seconds=ttl_seconds)
    if backend == "auto":
        try:
            lock = FileLock(name, lock_dir=lock_dir, ttl_seconds=ttl_seconds)
            lock.acquire()
            lock.release()
            return FileLock(name, lock_dir=lock_dir, ttl_seconds=ttl_seconds)
        except Exception:  # noqa: BLE001
            return DirectoryLock(name, lock_dir=lock_dir, ttl_seconds=ttl_seconds)
    raise ValueError(f"Unknown lock backend: {backend}")


# ── Higher-level guard functions ──────────────────────────────────────────────


def guard_duplicate_order(symbol: str, lock_dir: str = "locks") -> BaseLock:
    """Returns a lock preventing duplicate orders for the same symbol."""
    return get_lock(f"order_{symbol}", lock_dir=lock_dir, ttl_seconds=10.0)


def guard_concurrent_training(brain_id: str, lock_dir: str = "locks") -> BaseLock:
    """Returns a lock preventing concurrent training of the same brain."""
    return get_lock(f"training_{brain_id}", lock_dir=lock_dir, ttl_seconds=7200.0)


def guard_daily_ops(run_id: str, lock_dir: str = "locks") -> BaseLock:
    """Returns a lock preventing overlapping daily ops runs."""
    return get_lock(f"daily_ops_{run_id}", lock_dir=lock_dir, ttl_seconds=600.0)
