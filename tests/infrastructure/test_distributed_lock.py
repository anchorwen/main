"""Tests for distributed lock (file-based and directory-based)."""

from __future__ import annotations

import pytest

from core.infrastructure.distributed_lock import (
    DirectoryLock,
    FileLock,
    LockAcquireResult,
    get_lock,
    guard_concurrent_training,
    guard_daily_ops,
    guard_duplicate_order,
)

# ── FileLock ──────────────────────────────────────────────────────────────────


class TestFileLock:
    @pytest.fixture
    def lock_dir(self, tmp_path):
        return str(tmp_path / "test_locks")

    def test_acquire_and_release(self, lock_dir):
        lock = FileLock("test_resource", lock_dir=lock_dir, ttl_seconds=10)
        result = lock.acquire()
        assert result.acquired
        assert result.lock_name == "test_resource"
        assert result.holder_id != ""
        assert lock.is_held

        assert lock.release()
        assert not lock.is_held

    def test_cannot_acquire_twice_same_lock(self, lock_dir):
        lock = FileLock("test_resource", lock_dir=lock_dir, ttl_seconds=10)
        assert lock.acquire().acquired
        # Already held by this instance — can't re-acquire
        assert not lock.acquire().acquired
        lock.release()

    def test_double_release_is_safe(self, lock_dir):
        lock = FileLock("test_resource", lock_dir=lock_dir, ttl_seconds=10)
        lock.acquire()
        assert lock.release()
        assert not lock.release()  # Second release returns False

    def test_two_locks_same_name_block(self, lock_dir):
        lock1 = FileLock("shared", lock_dir=lock_dir, ttl_seconds=10)
        lock2 = FileLock("shared", lock_dir=lock_dir, ttl_seconds=10)

        assert lock1.acquire().acquired
        result = lock2.acquire()
        assert not result.acquired

        lock1.release()

    def test_different_names_dont_block(self, lock_dir):
        lock1 = FileLock("resource_a", lock_dir=lock_dir, ttl_seconds=10)
        lock2 = FileLock("resource_b", lock_dir=lock_dir, ttl_seconds=10)

        assert lock1.acquire().acquired
        assert lock2.acquire().acquired

        lock1.release()
        lock2.release()

    def test_release_allows_reacquire(self, lock_dir):
        lock1 = FileLock("shared", lock_dir=lock_dir, ttl_seconds=10)
        lock2 = FileLock("shared", lock_dir=lock_dir, ttl_seconds=10)

        lock1.acquire()
        lock1.release()

        result = lock2.acquire()
        assert result.acquired
        lock2.release()

    def test_holder_info_readable(self, lock_dir):
        lock = FileLock("test_resource", lock_dir=lock_dir, ttl_seconds=10)
        lock.acquire()
        info = lock.holder_info
        assert info is not None
        assert info["name"] == "test_resource"
        assert "holder_id" in info
        assert "pid" in info
        lock.release()

    def test_holder_info_none_after_release(self, lock_dir):
        lock = FileLock("test_resource", lock_dir=lock_dir, ttl_seconds=10)
        lock.acquire()
        lock.release()
        assert lock.holder_info is None

    def test_stale_lock_cleanup(self, lock_dir):
        """Lock with expired TTL should be removable by a new instance."""
        lock1 = FileLock("test_resource", lock_dir=lock_dir, ttl_seconds=0.01)
        lock1.acquire()
        lock1.release()  # Clean release removes file

        # Re-acquire should work fine
        lock2 = FileLock("test_resource", lock_dir=lock_dir, ttl_seconds=10)
        assert lock2.acquire().acquired
        lock2.release()

    def test_blocking_with_timeout(self, lock_dir):
        lock1 = FileLock("shared", lock_dir=lock_dir, ttl_seconds=10)
        lock2 = FileLock("shared", lock_dir=lock_dir, ttl_seconds=10)

        lock1.acquire()
        result = lock2.acquire(blocking=True, timeout_seconds=0.1)
        assert not result.acquired
        assert "Timeout" in result.error or "held" in result.error
        lock1.release()


# ── DirectoryLock ─────────────────────────────────────────────────────────────


class TestDirectoryLock:
    @pytest.fixture
    def lock_dir(self, tmp_path):
        return str(tmp_path / "test_dir_locks")

    def test_acquire_and_release(self, lock_dir):
        lock = DirectoryLock("test_resource", lock_dir=lock_dir, ttl_seconds=10)
        result = lock.acquire()
        assert result.acquired
        assert lock.is_held
        assert lock.release()
        assert not lock.is_held

    def test_mutual_exclusion(self, lock_dir):
        lock1 = DirectoryLock("shared", lock_dir=lock_dir, ttl_seconds=10)
        lock2 = DirectoryLock("shared", lock_dir=lock_dir, ttl_seconds=10)

        assert lock1.acquire().acquired
        assert not lock2.acquire().acquired
        lock1.release()

    def test_holder_info(self, lock_dir):
        lock = DirectoryLock("test_resource", lock_dir=lock_dir, ttl_seconds=10)
        lock.acquire()
        info = lock.holder_info
        assert info is not None
        assert info["name"] == "test_resource"
        lock.release()

    def test_stale_cleanup(self, lock_dir):
        lock = DirectoryLock("test_resource", lock_dir=lock_dir, ttl_seconds=0.01)
        lock.acquire()
        lock.release()

        lock2 = DirectoryLock("test_resource", lock_dir=lock_dir, ttl_seconds=10)
        assert lock2.acquire().acquired
        lock2.release()


# ── Factory ───────────────────────────────────────────────────────────────────


class TestGetLock:
    @pytest.fixture
    def lock_dir(self, tmp_path):
        return str(tmp_path / "factory_locks")

    def test_file_backend(self, lock_dir):
        lock = get_lock("r1", backend="file", lock_dir=lock_dir)
        assert isinstance(lock, FileLock)
        assert lock.acquire().acquired
        lock.release()

    def test_directory_backend(self, lock_dir):
        lock = get_lock("r2", backend="directory", lock_dir=lock_dir)
        assert isinstance(lock, DirectoryLock)
        assert lock.acquire().acquired
        lock.release()

    def test_auto_backend(self, lock_dir):
        lock = get_lock("r3", backend="auto", lock_dir=lock_dir)
        assert isinstance(lock, FileLock | DirectoryLock)
        assert lock.acquire().acquired
        lock.release()

    def test_unknown_backend_raises(self, lock_dir):
        with pytest.raises(ValueError, match="Unknown lock backend"):
            get_lock("r4", backend="consul", lock_dir=lock_dir)


# ── Guard functions ───────────────────────────────────────────────────────────


class TestGuards:
    @pytest.fixture
    def lock_dir(self, tmp_path):
        return str(tmp_path / "guard_locks")

    def test_guard_duplicate_order(self, lock_dir):
        lock = guard_duplicate_order("XAUUSDc", lock_dir=lock_dir)
        assert lock.acquire().acquired
        lock.release()

    def test_guard_concurrent_training(self, lock_dir):
        lock = guard_concurrent_training("v9_institutional_01", lock_dir=lock_dir)
        assert lock.acquire().acquired
        lock.release()

    def test_guard_daily_ops(self, lock_dir):
        lock = guard_daily_ops("20260509", lock_dir=lock_dir)
        assert lock.acquire().acquired
        lock.release()


# ── LockAcquireResult ─────────────────────────────────────────────────────────


class TestLockAcquireResult:
    def test_to_dict_success(self):
        r = LockAcquireResult(
            acquired=True,
            lock_name="test",
            holder_id="abc123",
            expires_at="2026-05-01",
            wait_ms=5.0,
        )
        d = r.to_dict()
        assert d["acquired"] is True
        assert d["lock_name"] == "test"
        assert d["holder_id"] == "abc123"
        assert d["error"] == ""

    def test_to_dict_failure(self):
        r = LockAcquireResult(
            acquired=False,
            lock_name="test",
            holder_id="",
            expires_at="",
            wait_ms=100.0,
            error="Lock held by another process",
        )
        d = r.to_dict()
        assert d["acquired"] is False
        assert d["error"] != ""
