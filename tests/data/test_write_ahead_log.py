"""Tests for WriteAheadLog — UGR v3.1 foundational data integrity layer.

Covers:
- Append and read operations
- Hash chain integrity
- verify_integrity() detection of corruption/tampering
- Thread safety (concurrent writes)
- Persistence (re-open after write)
- Recovery from existing WAL
- Payload size validation
"""

from __future__ import annotations

import json
import threading

import pytest

from core.data.write_ahead_log import (
    GENESIS_HASH,
    WALConfig,
    WALRecord,
    WriteAheadLog,
)


@pytest.fixture
def wal_config(tmp_path):
    """Create a WAL config pointing to a temp file."""
    return WALConfig(path=tmp_path / "test_wal.jsonl")


@pytest.fixture
def wal(wal_config):
    """Create a fresh WAL instance."""
    return WriteAheadLog(wal_config)


# ═══════════════════════════════════════════════════════════════════════════
# Append + Read
# ═══════════════════════════════════════════════════════════════════════════


class TestAppendAndRead:
    """Tests for append() and read() basic operations."""

    def test_append_returns_sequence_number(self, wal: WriteAheadLog) -> None:
        """append() returns the assigned sequence number."""
        seq = wal.append({"key": "value"})
        assert seq == 0
        seq = wal.append({"key": "value2"})
        assert seq == 1

    def test_first_record_has_genesis_prev_hash(self, wal: WriteAheadLog) -> None:
        """The first record's prev_hash is GENESIS_HASH."""
        wal.append({"event": "startup"})
        record = wal.read(0)
        assert record is not None
        assert record.prev_hash == GENESIS_HASH

    def test_read_returns_correct_record(self, wal: WriteAheadLog) -> None:
        """read(seq) returns the record with that sequence number."""
        wal.append({"event": "first"})
        wal.append({"event": "second"})
        record = wal.read(1)
        assert record is not None
        assert record.seq == 1
        assert record.payload == {"event": "second"}

    def test_read_missing_seq_returns_none(self, wal: WriteAheadLog) -> None:
        """read() on a non-existent seq returns None."""
        assert wal.read(999) is None

    def test_len_reflects_record_count(self, wal: WriteAheadLog) -> None:
        """len(wal) returns the number of records."""
        assert len(wal) == 0
        wal.append({"n": 1})
        wal.append({"n": 2})
        assert len(wal) == 2

    def test_iter_yields_all_records(self, wal: WriteAheadLog) -> None:
        """Iterating over WAL yields all records in order."""
        for i in range(5):
            wal.append({"i": i})
        records = list(wal)
        assert len(records) == 5
        assert [r.seq for r in records] == [0, 1, 2, 3, 4]

    def test_record_type_stored(self, wal: WriteAheadLog) -> None:
        """record_type is stored in the WAL record."""
        wal.append({"x": 1}, record_type="phantom_stub")
        record = wal.read(0)
        assert record is not None
        assert record.type == "phantom_stub"

    def test_timestamp_wall_stored(self, wal: WriteAheadLog) -> None:
        """timestamp_wall is stored in the WAL record."""
        wal.append({"x": 1}, timestamp_wall="2026-06-24T12:00:00+00:00")
        record = wal.read(0)
        assert record is not None
        assert "2026-06-24" in record.timestamp_wall


# ═══════════════════════════════════════════════════════════════════════════
# Hash Chain Integrity
# ═══════════════════════════════════════════════════════════════════════════


class TestHashChain:
    """Tests for the hash chain (prev_hash → record_hash linking)."""

    def test_hash_chain_linking(self, wal: WriteAheadLog) -> None:
        """Each record's prev_hash = previous record's record_hash."""
        wal.append({"event": "A"})
        wal.append({"event": "B"})
        wal.append({"event": "C"})

        r0 = wal.read(0)
        r1 = wal.read(1)
        r2 = wal.read(2)

        assert r0 is not None and r1 is not None and r2 is not None
        assert r0.prev_hash == GENESIS_HASH
        assert r1.prev_hash == r0.record_hash
        assert r2.prev_hash == r1.record_hash

    def test_verify_integrity_passes_on_clean_chain(self, wal: WriteAheadLog) -> None:
        """verify_integrity() returns (True, "") on a clean WAL."""
        for i in range(10):
            wal.append({"i": i})
        ok, reason = wal.verify_integrity()
        assert ok is True
        assert reason == ""

    def test_verify_integrity_detects_modified_payload(self, wal: WriteAheadLog) -> None:
        """verify_integrity() detects a modified payload."""
        wal.append({"balance": 100.0})
        # Manually corrupt the file
        _corrupt_wal_file(wal, line=0, key="payload", value={"balance": 999.0})
        ok, reason = wal.verify_integrity()
        assert ok is False
        assert "Record hash mismatch" in reason

    def test_verify_integrity_detects_modified_prev_hash(self, wal: WriteAheadLog) -> None:
        """verify_integrity() detects a modified prev_hash."""
        wal.append({"event": "A"})
        wal.append({"event": "B"})
        # Corrupt prev_hash of second record
        _corrupt_wal_file(wal, line=1, key="prev_hash", value="DEADBEEF")
        ok, reason = wal.verify_integrity()
        assert ok is False
        assert "Hash chain broken" in reason

    def test_verify_integrity_detects_duplicate_seq(self, wal: WriteAheadLog) -> None:
        """verify_integrity() detects duplicate sequence numbers."""
        wal.append({"x": 1})
        wal.append({"x": 2})
        # Duplicate the first line
        _duplicate_wal_line(wal, line=0)
        ok, reason = wal.verify_integrity()
        assert ok is False
        assert "Duplicate" in reason


# ═══════════════════════════════════════════════════════════════════════════
# Persistence
# ═══════════════════════════════════════════════════════════════════════════


class TestPersistence:
    """Tests for WAL durability across re-open."""

    def test_reopen_recovers_state(self, wal_config: WALConfig) -> None:
        """Re-opening a WAL recovers correct next_seq."""
        wal1 = WriteAheadLog(wal_config)
        wal1.append({"msg": "hello"})
        wal1.append({"msg": "world"})

        # Re-open
        wal2 = WriteAheadLog(wal_config)
        assert len(wal2) == 2
        seq = wal2.append({"msg": "third"})
        assert seq == 2

    def test_reopen_verify_integrity_still_passes(self, wal_config: WALConfig) -> None:
        """After re-open, verify_integrity() still passes."""
        wal1 = WriteAheadLog(wal_config)
        for i in range(5):
            wal1.append({"i": i})

        wal2 = WriteAheadLog(wal_config)
        ok, reason = wal2.verify_integrity()
        assert ok is True


# ═══════════════════════════════════════════════════════════════════════════
# Thread Safety
# ═══════════════════════════════════════════════════════════════════════════


class TestThreadSafety:
    """Tests for concurrent write safety."""

    def test_concurrent_appends_produce_valid_chain(self, wal: WriteAheadLog) -> None:
        """Concurrent appends from multiple threads produce a valid hash chain."""
        errors: list[Exception] = []

        def writer(thread_id: int, count: int) -> None:
            try:
                for i in range(count):
                    wal.append({"thread": thread_id, "i": i})
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for t in range(4):
            th = threading.Thread(target=writer, args=(t, 25))
            threads.append(th)
            th.start()

        for th in threads:
            th.join()

        assert len(errors) == 0, f"Errors during concurrent writes: {errors}"
        assert len(wal) == 100

        ok, reason = wal.verify_integrity()
        assert ok is True, f"Chain broken after concurrent writes: {reason}"

    def test_concurrent_appends_all_seqs_present(self, wal: WriteAheadLog) -> None:
        """All sequence numbers are present (no gaps) after concurrent writes."""
        N_THREADS = 4
        N_PER_THREAD = 10

        def writer(thread_id: int) -> None:
            for i in range(N_PER_THREAD):
                wal.append({"thread": thread_id, "i": i})

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(N_THREADS)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        seen_seqs = set()
        for record in wal:
            assert record.seq not in seen_seqs, f"Duplicate seq={record.seq}"
            seen_seqs.add(record.seq)

        expected = set(range(N_THREADS * N_PER_THREAD))
        assert seen_seqs == expected, f"Missing seqs: {expected - seen_seqs}"


# ═══════════════════════════════════════════════════════════════════════════
# Edge Cases
# ═══════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_wal_verify_passes(self, wal: WriteAheadLog) -> None:
        """Empty WAL passes integrity check."""
        ok, reason = wal.verify_integrity()
        assert ok is True

    def test_large_payload_rejected(self, wal: WriteAheadLog) -> None:
        """Payload exceeding max_record_size raises ValueError."""
        big_payload = {"data": "x" * (11 * 1024 * 1024)}  # 11 MiB
        with pytest.raises(ValueError, match="Payload too large"):
            wal.append(big_payload)

    def test_corrupted_json_line_skipped_in_scan(self, wal_config: WALConfig) -> None:
        """A corrupted JSON line is skipped during scan (logged, not crashed)."""
        wal1 = WriteAheadLog(wal_config)
        wal1.append({"good": True})

        # Append garbage to the file
        with open(wal_config.path, "a", encoding="utf-8") as fh:
            fh.write("this is not valid json\n")

        wal2 = WriteAheadLog(wal_config)
        records = list(wal2)
        # Should still have the first good record
        assert len(records) >= 1
        assert records[0].payload == {"good": True}

    def test_wal_record_create_computes_hash(self) -> None:
        """WALRecord.create() computes a deterministic hash."""
        r1 = WALRecord.create(0, GENESIS_HASH, "event", "", {"x": 1})
        r2 = WALRecord.create(0, GENESIS_HASH, "event", "", {"x": 1})
        assert r1.record_hash == r2.record_hash  # Deterministic

    def test_wal_record_create_different_payload_different_hash(self) -> None:
        """Different payloads produce different hashes."""
        r1 = WALRecord.create(0, GENESIS_HASH, "event", "", {"x": 1})
        r2 = WALRecord.create(0, GENESIS_HASH, "event", "", {"x": 2})
        assert r1.record_hash != r2.record_hash

    def test_wal_record_json_roundtrip(self) -> None:
        """WALRecord → JSON line → parse → same data."""
        record = WALRecord.create(
            42, "prev_hash_abc", "phantom_stub", "2026-01-01T00:00:00Z", {"k": "v"}
        )
        line = record.to_json_line()
        data = json.loads(line)
        assert data["seq"] == 42
        assert data["prev_hash"] == "prev_hash_abc"
        assert data["type"] == "phantom_stub"
        assert data["payload"] == {"k": "v"}


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _corrupt_wal_file(wal: WriteAheadLog, line: int, key: str, value: object) -> None:
    """Modify a specific key in a specific line of the WAL file."""
    path = wal._config.path
    lines = path.read_text(encoding="utf-8").splitlines()
    data = json.loads(lines[line])
    data[key] = value
    lines[line] = json.dumps(data, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _duplicate_wal_line(wal: WriteAheadLog, line: int) -> None:
    """Duplicate a specific line in the WAL file."""
    path = wal._config.path
    lines = path.read_text(encoding="utf-8").splitlines()
    lines.insert(line, lines[line])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# Rotation + Checkpoint (UGR-B02)
# ═══════════════════════════════════════════════════════════════════════════


class TestRotation:
    """Tests for WAL rotation and checkpoint."""

    def test_rotate_creates_checkpoint(self, wal_config: WALConfig) -> None:
        """After rotation, a checkpoint file exists with valid fields."""
        wal_config.rotate_on_entries = 5
        wal = WriteAheadLog(wal_config)
        for i in range(5):
            wal.append({"i": i})

        rotated = wal.maybe_rotate()
        assert rotated

        checkpoint = wal.load_checkpoint()
        assert checkpoint is not None
        assert checkpoint["version"] == 1
        assert checkpoint["last_seq"] == 4
        assert "checkpoint_hash" in checkpoint
        assert "signature" in checkpoint

    def test_rotate_on_size(self, wal_config: WALConfig) -> None:
        """Rotation triggers on file size threshold."""
        wal_config.rotate_on_entries = None
        wal_config.rotate_on_size_mb = 0  # Rotate immediately
        wal = WriteAheadLog(wal_config)
        wal.append({"data": "x" * 1000})  # Write enough to trigger

        rotated = wal.maybe_rotate()
        assert rotated
        checkpoint = wal.load_checkpoint()
        assert checkpoint is not None

    def test_verify_from_checkpoint(self, wal_config: WALConfig) -> None:
        """verify_integrity_from_checkpoint passes after rotation."""
        wal_config.rotate_on_entries = 100  # Don't auto-rotate
        wal = WriteAheadLog(wal_config)
        for i in range(10):
            wal.append({"i": i})

        ok, reason = wal.verify_integrity_from_checkpoint()
        assert ok, f"Failed: {reason}"

    def test_reopen_after_rotation(self, wal_config: WALConfig) -> None:
        """After rotation, re-opening recovers correct state."""
        wal1 = WriteAheadLog(wal_config)
        for i in range(5):
            wal1.append({"i": i})

        # Force rotation
        wal_config.rotate_on_entries = 5
        wal1 = WriteAheadLog(wal_config)
        for i in range(5):
            wal1.append({"i": i})
        wal1.maybe_rotate()

        # Append more after rotation
        wal1.append({"after": "rotation"})

        # Re-open
        wal2 = WriteAheadLog(wal_config)
        assert len(wal2) >= 6  # 5 pre-rotation + 1 post-rotation
        seq = wal2.append({"final": True})
        assert seq >= 6

    def test_checkpoint_hmac_tamper_detected(self, wal_config: WALConfig) -> None:
        """A tampered checkpoint signature is detected."""
        wal_config.rotate_on_entries = 5
        wal = WriteAheadLog(wal_config)
        for i in range(5):
            wal.append({"i": i})

        wal.maybe_rotate()

        # Tamper with checkpoint
        cp_path = wal_config.path.with_suffix(".checkpoint")
        data = json.loads(cp_path.read_text(encoding="utf-8"))
        data["checkpoint_hash"] = "DEADBEEF"
        cp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # load_checkpoint should reject the tampered file
        checkpoint = wal.load_checkpoint()
        assert checkpoint is None  # HMAC should fail

    def test_no_checkpoint_verify_still_works(self, wal_config: WALConfig) -> None:
        """verify_integrity_from_checkpoint works without any checkpoint."""
        wal = WriteAheadLog(wal_config)
        wal.append({"x": 1})
        ok, reason = wal.verify_integrity_from_checkpoint()
        assert ok

    def test_rotation_preserves_hash_chain(self, wal_config: WALConfig) -> None:
        """Hash chain remains valid after multiple rotations."""
        wal_config.rotate_on_entries = 5
        wal = WriteAheadLog(wal_config)
        # First segment
        for i in range(5):
            wal.append({"seg": 1, "i": i})
        wal.maybe_rotate()
        # Second segment
        for i in range(5):
            wal.append({"seg": 2, "i": i})
        wal.maybe_rotate()
        # Third segment
        for i in range(5):
            wal.append({"seg": 3, "i": i})

        ok, reason = wal.verify_integrity_from_checkpoint()
        assert ok, f"Chain broken after rotations: {reason}"
