"""WriteAheadLog — Append-only, fsync'd, hash-chained journal.

UGR v3.1 §修正4: Hash-chained WAL with checkpoint + rotation support.
This is the foundational data integrity layer of the resilience architecture.

Basic version (UGR-A03):
- Append-only JSONL format
- fsync after every write
- Hash chain: record_hash = SHA256(prev_hash + serialized_payload)
- Thread-safe writes via threading.Lock
- verify_integrity() detects tampering/corruption

Full version (UGR-B02):
- Rotation with atomic checkpoint
- Multi-segment verification
- Checkpoint signature (HMAC)

Records are stored as JSONL (one JSON object per line)::

    {"seq":0,"prev_hash":"GENESIS_uGRv31","record_hash":"abc...","type":"event",
     "timestamp_wall":"2026-06-24T...","payload":{...}}
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

GENESIS_HASH: str = "GENESIS_uGRv31_20260624"
MAX_RECORD_SIZE: int = 10 * 1024 * 1024  # 10 MiB — reject oversized payloads

# Rotation defaults (UGR-B02)
DEFAULT_ROTATION_SIZE_MB: int = 100  # Rotate when WAL exceeds this size
DEFAULT_ROTATION_ENTRIES: int = 1_000_000  # Rotate when entry count exceeds this
CHECKPOINT_VERSION: int = 1  # Current checkpoint schema version

# HMAC key for checkpoint signing
# In production, this should be set from an environment variable or secure store.
# The default is a hardcoded key per the spec (UGR-P04 §4).
_HMAC_KEY: bytes = b"uGRv31_WAL_Checkpoint_Key_20260624"


# ═══════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class WALConfig:
    """WriteAheadLog configuration."""

    path: Path  # Path to the WAL JSONL file
    fsync_on_write: bool = True  # Always fsync after append
    max_record_size: int = MAX_RECORD_SIZE
    create_if_missing: bool = True
    # Rotation settings (UGR-B02)
    rotate_on_size_mb: int | None = DEFAULT_ROTATION_SIZE_MB
    rotate_on_entries: int | None = DEFAULT_ROTATION_ENTRIES
    archive_dir: Path | None = None  # Directory for rotated segment files
    disk_quota_mb: int | None = None  # UGR-A10: hard quota; reject append if exceeded


@dataclass(slots=True)
class WALRecord:
    """An immutable record in the WAL.

    After construction, record_hash is deterministic from prev_hash + payload.
    """

    seq: int
    prev_hash: str
    record_hash: str
    type: str
    timestamp_wall: str
    payload: dict[str, Any]

    @classmethod
    def create(
        cls,
        seq: int,
        prev_hash: str,
        record_type: str,
        timestamp_wall: str,
        payload: dict[str, Any],
    ) -> WALRecord:
        """Create a WALRecord and compute its hash."""
        record_hash = _compute_hash(prev_hash, payload)
        return cls(
            seq=seq,
            prev_hash=prev_hash,
            record_hash=record_hash,
            type=record_type,
            timestamp_wall=timestamp_wall,
            payload=payload,
        )

    def to_json_line(self) -> str:
        """Serialize to a single JSON line (newline-terminated)."""
        return (
            json.dumps(
                {
                    "seq": self.seq,
                    "prev_hash": self.prev_hash,
                    "record_hash": self.record_hash,
                    "type": self.type,
                    "timestamp_wall": self.timestamp_wall,
                    "payload": self.payload,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )


# ═══════════════════════════════════════════════════════════════════════════
# WriteAheadLog
# ═══════════════════════════════════════════════════════════════════════════


class WriteAheadLog:
    """Append-only, fsync'd, hash-chained write-ahead log.

    Usage::

        wal = WriteAheadLog(WALConfig(path=Path("wal.jsonl")))
        seq = wal.append({"event": "position_opened", ...})
        record = wal.read(seq)
        ok, reason = wal.verify_integrity()
    """

    def __init__(self, config: WALConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._next_seq: int = 0
        self._last_hash: str = GENESIS_HASH
        self._file: object | None = None

        if config.create_if_missing and not config.path.exists():
            config.path.parent.mkdir(parents=True, exist_ok=True)
            config.path.write_text("", encoding="utf-8")

        if config.path.exists():
            self._recover_state()

    # ── Core API ────────────────────────────────────────────────────────

    def append(
        self,
        payload: dict[str, Any],
        *,
        record_type: str = "event",
        timestamp_wall: str = "",
    ) -> int:
        """Append a record to the WAL. Returns the sequence number.

        Thread-safe. fsync's before returning (unless fsync_on_write=False).
        """
        import datetime

        if timestamp_wall == "":
            timestamp_wall = datetime.datetime.now(datetime.UTC).isoformat()

        # Validate payload size
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(payload_json.encode("utf-8")) > self._config.max_record_size:
            raise ValueError(
                f"Payload too large: {len(payload_json)} bytes "
                f"(max {self._config.max_record_size})"
            )

        with self._lock:
            seq = self._next_seq
            record = WALRecord.create(
                seq=seq,
                prev_hash=self._last_hash,
                record_type=record_type,
                timestamp_wall=timestamp_wall,
                payload=payload,
            )

            self._write_line(record.to_json_line())

            self._next_seq = seq + 1
            self._last_hash = record.record_hash
            return seq

    def read(self, seq: int) -> WALRecord | None:
        """Read a single record by sequence number. Returns None if not found."""
        for record in self._scan():
            if record.seq == seq:
                return record
            if record.seq > seq:
                break
        return None

    def __len__(self) -> int:
        """Number of records in the WAL."""
        return self._next_seq

    def __iter__(self) -> Iterator[WALRecord]:
        """Iterate over all records in order."""
        return self._scan()

    # ── Quota (UGR-A10) ─────────────────────────────────────────────────

    @property
    def size_mb(self) -> float:
        """Current WAL file size in megabytes."""
        if not self._config.path.exists():
            return 0.0
        return self._config.path.stat().st_size / (1024 * 1024)

    def check_quota(self) -> tuple[bool, str]:
        """Check whether the WAL is within its disk quota.

        Returns:
            (True, "") if within quota or no quota configured.
            (False, reason) if quota exceeded.
        """
        quota_mb = self._config.disk_quota_mb
        if quota_mb is None:
            return True, ""
        current_mb = self.size_mb
        if current_mb >= quota_mb:
            return False, (
                f"WAL disk quota exceeded: {current_mb:.1f} MiB "
                f"(limit: {quota_mb} MiB) at {self._config.path}"
            )
        return True, ""

    # ── Integrity ───────────────────────────────────────────────────────

    def verify_integrity(self) -> tuple[bool, str]:
        """Verify the hash chain from genesis to end.

        Returns:
            (True, "") if integrity confirmed.
            (False, reason) if tampering or corruption detected.
        """
        expected_hash: str = GENESIS_HASH
        seen_seqs: set[int] = set()

        for record in self._scan():
            # Check for duplicate sequence numbers
            if record.seq in seen_seqs:
                return False, f"Duplicate sequence number: seq={record.seq}"
            seen_seqs.add(record.seq)

            # Check prev_hash links to previous record
            if record.prev_hash != expected_hash:
                return False, (
                    f"Hash chain broken at seq={record.seq}: "
                    f"expected prev_hash={expected_hash[:16]}..., "
                    f"got prev_hash={record.prev_hash[:16]}..."
                )

            # Check record_hash is correct
            computed_hash = _compute_hash(record.prev_hash, record.payload)
            if record.record_hash != computed_hash:
                return False, (
                    f"Record hash mismatch at seq={record.seq}: "
                    f"stored={record.record_hash[:16]}..., "
                    f"computed={computed_hash[:16]}..."
                )

            expected_hash = record.record_hash

        # Check for missing sequence numbers
        if seen_seqs and max(seen_seqs) + 1 != len(seen_seqs):
            missing = sorted(set(range(max(seen_seqs) + 1)) - seen_seqs)
            return False, f"Missing sequence numbers: {missing[:10]}..."

        return True, ""

    # ── Rotation (UGR-B02) ────────────────────────────────────────────

    def maybe_rotate(self) -> bool:
        """Rotate the WAL if size or entry thresholds are exceeded.

        Returns True if rotation was performed.
        Safe to call from any thread; rotation is serialized via _lock.
        """
        should_rotate = False
        if self._config.rotate_on_size_mb is not None:
            if self._config.path.exists():
                size_mb = self._config.path.stat().st_size / (1024 * 1024)
                if size_mb >= self._config.rotate_on_size_mb:
                    should_rotate = True
        if self._config.rotate_on_entries is not None:
            if self._next_seq >= self._config.rotate_on_entries:
                should_rotate = True

        if not should_rotate:
            return False

        with self._lock:
            return self._rotate()

    def _rotate(self) -> bool:
        """Execute WAL rotation with atomic checkpoint.

        Steps (per wal_checkpoint_design.md §1.2):
        1. fsync current WAL
        2. Write checkpoint file atomically
        3. Rename current WAL → archived segment
        4. Create new empty WAL, chain from checkpoint hash
        """
        import datetime

        now = datetime.datetime.now(datetime.UTC).isoformat()

        # Step 1+2: Write checkpoint atomically
        last_seq = self._next_seq - 1
        checkpoint_data = {
            "version": CHECKPOINT_VERSION,
            "last_seq": last_seq,
            "checkpoint_hash": self._last_hash,
            "rotated_at": now,
            "genesis_hash": GENESIS_HASH,
            "segment_first_seq": 0,  # Updated on multi-segment recovery
            "segment_last_seq": last_seq,
        }
        checkpoint_data["signature"] = _compute_checkpoint_hmac(checkpoint_data)

        checkpoint_path = self._config.path.with_suffix(".checkpoint")
        _write_checkpoint_atomic(checkpoint_path, checkpoint_data)

        # Step 3: Archive current WAL
        archive_dir = self._config.archive_dir or self._config.path.parent
        archive_dir.mkdir(parents=True, exist_ok=True)
        ts = now.replace(":", "").replace("-", "")[:15]  # e.g. 20260624T143000
        segment_name = f"{self._config.path.stem}_{ts}_{last_seq:06d}.jsonl"
        segment_path = archive_dir / segment_name

        os.replace(str(self._config.path), str(segment_path))

        # Step 4: Create new WAL
        self._config.path.write_text("", encoding="utf-8")
        # Chain starts from checkpoint hash
        # (next_seq continues from last_seq + 1; last_hash is checkpoint hash)
        return True

    # ── Checkpoint (UGR-B02) ───────────────────────────────────────────

    def load_checkpoint(self) -> dict[str, object] | None:
        """Load the current checkpoint, if one exists.

        Returns None if no checkpoint file exists.
        """
        checkpoint_path = self._config.path.with_suffix(".checkpoint")
        if not checkpoint_path.exists():
            return None

        try:
            data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        # Verify HMAC
        if "signature" in data:
            expected_sig = data.pop("signature")
            actual_sig = _compute_checkpoint_hmac(data)
            data["signature"] = expected_sig
            if not _constant_time_compare(expected_sig, actual_sig):
                return None  # Tampered checkpoint — treat as missing

        return data

    # ── Integrity (updated for rotation — UGR-B02) ─────────────────────

    def verify_integrity_from_checkpoint(self) -> tuple[bool, str]:
        """Verify hash chain from the last checkpoint.

        Rotation-safe: starts from checkpoint hash instead of GENESIS_HASH.
        Falls back to genesis verification if no checkpoint exists.
        """
        checkpoint = self.load_checkpoint()
        if checkpoint is not None:
            raw_last_seq = checkpoint.get("last_seq", -1)
            start_seq = (int(raw_last_seq) if isinstance(raw_last_seq, int | float) else -1) + 1
            expected_hash = str(checkpoint.get("checkpoint_hash", GENESIS_HASH))
        else:
            start_seq = 0
            expected_hash = GENESIS_HASH

        seen_seqs: set[int] = set()

        for record in self._scan():
            if record.seq < start_seq:
                continue

            if record.seq in seen_seqs:
                return False, f"Duplicate sequence number: seq={record.seq}"
            seen_seqs.add(record.seq)

            if record.prev_hash != expected_hash:
                return False, (
                    f"Hash chain broken at seq={record.seq}: "
                    f"expected prev_hash={expected_hash[:16]}..., "
                    f"got prev_hash={record.prev_hash[:16]}..."
                )

            computed_hash = _compute_hash(record.prev_hash, record.payload)
            if record.record_hash != computed_hash:
                return False, (
                    f"Record hash mismatch at seq={record.seq}: "
                    f"stored={record.record_hash[:16]}..., "
                    f"computed={computed_hash[:16]}..."
                )

            expected_hash = record.record_hash

        return True, ""

    # ── Internal ────────────────────────────────────────────────────────

    def _scan(self) -> Iterator[WALRecord]:
        """Stream all records from the WAL file."""
        if not self._config.path.exists():
            return

        with open(self._config.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    yield WALRecord(
                        seq=data["seq"],
                        prev_hash=data["prev_hash"],
                        record_hash=data["record_hash"],
                        type=data.get("type", "event"),
                        timestamp_wall=data.get("timestamp_wall", ""),
                        payload=data.get("payload", {}),
                    )
                except (json.JSONDecodeError, KeyError) as e:
                    # Corrupted line — skip but log
                    import sys

                    print(f"[WAL] WARNING: Skipping corrupted line: {e}", file=sys.stderr)
                    continue

    def _recover_state(self) -> None:
        """Recover next_seq and last_hash by scanning the WAL file."""
        max_seq = -1
        last_hash = GENESIS_HASH

        for record in self._scan():
            if record.seq > max_seq:
                max_seq = record.seq
                last_hash = record.record_hash

        self._next_seq = max_seq + 1
        self._last_hash = last_hash

    def _write_line(self, line: str) -> None:
        """Append a line to the WAL file and fsync."""
        with open(self._config.path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            if self._config.fsync_on_write:
                os.fsync(fh.fileno())


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _compute_checkpoint_hmac(data: dict[str, object]) -> str:
    """Compute HMAC-SHA256 signature for a checkpoint file.

    Uses a hardcoded key (per spec UGR-P04 §4).
    Signature covers all fields EXCEPT 'signature' itself.
    """
    import hmac

    payload = json.dumps(
        {k: v for k, v in data.items() if k != "signature"},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hmac.new(_HMAC_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _write_checkpoint_atomic(checkpoint_path: Path, data: dict[str, object]) -> None:
    """Write checkpoint file atomically via temp file + os.replace().

    Per wal_checkpoint_design.md §1.3.
    """
    tmp_path = Path(str(checkpoint_path) + ".tmp")
    tmp_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    # fsync temp file before atomic replace
    fd = os.open(str(tmp_path), os.O_RDWR)
    os.fsync(fd)
    os.close(fd)
    os.replace(str(tmp_path), str(checkpoint_path))


def _constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks on HMAC."""
    import hmac as _hmac_mod

    return _hmac_mod.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _compute_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    """SHA256(prev_hash + serialized_payload)."""
    payload_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    content = prev_hash.encode("utf-8") + payload_str.encode("utf-8")
    return hashlib.sha256(content).hexdigest()
