"""Tests for core.deployment.atomic_file_writer — Phase 3c gap fill.

Covers: AtomicFileWriter, AtomicFileError, staging_path, backup_path,
backup, stage_content, stage_copy, commit, rollback, _cleanup, _unlink.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from core.deployment.atomic_file_writer import (
    AtomicFileError,
    AtomicFileWriter,
    atomic_write_text,
)


class TestAtomicFileError:
    def test_is_runtime_error(self) -> None:
        err = AtomicFileError("test")
        assert isinstance(err, RuntimeError)

    def test_message(self) -> None:
        err = AtomicFileError("commit failed")
        assert str(err) == "commit failed"


class TestAtomicFileWriterInit:
    def test_empty_init(self) -> None:
        w = AtomicFileWriter()
        assert w._targets == []
        assert w._staging == {}
        assert w._backups == {}
        assert w.committed is False

    def test_init_with_targets(self) -> None:
        w = AtomicFileWriter([Path("/a/file.json"), Path("/b/file.yaml")])
        assert len(w._targets) == 2

    def test_init_with_string_targets(self) -> None:
        w = AtomicFileWriter([Path("/a/file.json")])
        assert isinstance(w._targets[0], Path)

    def test_add_target(self) -> None:
        w = AtomicFileWriter()
        w.add("/new/file.json")
        assert len(w._targets) == 1
        assert isinstance(w._targets[0], Path)


class TestStagingPath:
    def test_staging_path_suffix(self) -> None:
        p = Path("/data/file.json")
        sp = AtomicFileWriter.staging_path(p)
        assert str(sp).endswith(".json.tmp.staging")

    def test_staging_path_same_dir(self) -> None:
        p = Path("/data/sub/file.json")
        sp = AtomicFileWriter.staging_path(p)
        assert sp.parent == p.parent

    def test_backup_path_suffix(self) -> None:
        p = Path("/data/file.json")
        bp = AtomicFileWriter.backup_path(p)
        assert str(bp).endswith(".json.bak")


class TestBackup:
    def test_backup_creates_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "data.json"
            target.write_text("original content")
            w = AtomicFileWriter([target])
            w.backup()
            bak = AtomicFileWriter.backup_path(target)
            assert bak.exists()
            assert bak.read_text() == "original content"
            w._cleanup()

    def test_backup_skips_nonexistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "nonexistent.json"
            w = AtomicFileWriter([target])
            w.backup()  # should not raise
            assert len(w._backups) == 0


class TestStageContent:
    def test_stage_content_writes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "output.json"
            w = AtomicFileWriter([target])
            staging = w.stage_content(target, '{"key": "value"}')
            assert staging.exists()
            assert staging.read_text() == '{"key": "value"}'
            assert target in w._staging
            w._cleanup()


class TestStageCopy:
    def test_stage_copy_copies_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.json"
            source.write_text("source content")
            target = Path(tmpdir) / "target.json"
            w = AtomicFileWriter([target])
            staging = w.stage_copy(source, target)
            assert staging.exists()
            assert staging.read_text() == "source content"
            w._cleanup()

    def test_stage_copy_same_file_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "target.json"
            staging = AtomicFileWriter.staging_path(target)
            staging.write_text("same")
            w = AtomicFileWriter([target])
            w.stage_copy(staging, target)  # source == staging → no copy
            assert staging.read_text() == "same"
            w._cleanup()


class TestCommit:
    def test_commit_replaces_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "data.json"
            target.write_text("old")
            w = AtomicFileWriter([target])
            w.stage_content(target, "new content")
            w.commit()
            assert target.read_text() == "new content"
            assert w.committed is True

    def test_commit_no_staging_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "data.json"
            target.write_text("original")
            w = AtomicFileWriter([target])
            w.commit()  # no staging → skip, but cleanup runs
            assert target.read_text() == "original"

    def test_commit_cleans_up_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "data.json"
            target.write_text("old")
            w = AtomicFileWriter([target])
            staging = w.stage_content(target, "new")
            assert staging.exists()
            w.commit()
            assert not staging.exists()  # staging cleaned up
            assert w._staging == {}

    def test_double_commit_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "data.json"
            target.write_text("old")
            w = AtomicFileWriter([target])
            w.stage_content(target, "v1")
            w.commit()
            w.stage_content(target, "v2")
            w.commit()  # should be noop since already committed
            # target still has v1 because second commit was noop
            assert target.read_text() == "v1"


class TestRollback:
    def test_rollback_restores_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "data.json"
            target.write_text("original")
            w = AtomicFileWriter([target])
            w.backup()
            target.write_text("modified")
            w.rollback()
            assert target.read_text() == "original"
            # Backup should be cleaned up
            bak = AtomicFileWriter.backup_path(target)
            assert not bak.exists()

    def test_rollback_no_backup_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "data.json"
            target.write_text("content")
            w = AtomicFileWriter([target])
            w.rollback()  # no backup → no error
            assert target.read_text() == "content"


class TestCleanup:
    def test_cleanup_removes_staging_and_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "data.json"
            target.write_text("content")
            w = AtomicFileWriter([target])
            w.backup()
            w.stage_content(target, "staged")
            bak = AtomicFileWriter.backup_path(target)
            staging = AtomicFileWriter.staging_path(target)
            assert bak.exists()
            assert staging.exists()
            w._cleanup()
            assert not bak.exists()
            assert not staging.exists()
            assert w._staging == {}
            assert w._backups == {}

    def test_unlink_nonexistent_no_error(self) -> None:
        AtomicFileWriter._unlink(Path("/nonexistent/file.xyz"))
        # Should not raise


class TestLFByteOutput:
    """FIX-20260805-005 regression lock: writers must emit LF bytes, never CRLF.

    On Windows, text-mode write_text() without ``newline`` translates ``\\n`` →
    ``\\r\\n``, producing a CRLF working copy → git pseudo-diff → 8/19 training
    hash-lock rejection. These tests fail on Windows before the fix and pass
    after; on Linux they pass trivially (``newline="\\n"`` is the default).
    """

    def test_atomic_write_text_emits_lf_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "cfg.yaml"
            atomic_write_text(target, "a: 1\nb: 2\n")
            data = target.read_bytes()
            assert b"\r\n" not in data
            assert data.endswith(b"\n")

    def test_stage_content_emits_lf_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "out.json"
            w = AtomicFileWriter([target])
            staging = w.stage_content(target, '{"k": "v"}\n')
            data = staging.read_bytes()
            assert b"\r\n" not in data
            assert data.endswith(b"\n")
            w._cleanup()
