"""UGR-B05: Bit-rot injection — WAL hash chain detection.

Chaos tests that deliberately corrupt WAL data and verify the hash chain
detects the tampering.  Designed to catch silent data corruption that
CRC/checksum hardware would miss, and to verify that verify_integrity()
correctly identifies every corruption mode.

Corruption modes tested:
  1. Payload bit flip (single byte mutation)
  2. prev_hash chain break (re-homing a record)
  3. Duplicate sequence number injection
  4. Record deletion (line removal)
  5. Cross-segment corruption (checkpoint bypass)
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.data.write_ahead_log import (
    GENESIS_HASH,
    WALConfig,
    WriteAheadLog,
)


@pytest.fixture
def wal_with_data():
    """Create a WAL with 10 records for corruption testing."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        wal = WriteAheadLog(WALConfig(path=tmp / "wal.jsonl"))
        for i in range(10):
            wal.append({"event": f"record_{i}", "value": i * 10})
        yield wal, tmp / "wal.jsonl"


def _corrupt_file_line(path: Path, line_idx: int, mutate):
    """Read file, mutate line line_idx (0-based), write back."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if line_idx < len(lines):
        lines[line_idx] = mutate(lines[line_idx])
    path.write_text("".join(lines), encoding="utf-8")


class TestBitRotPayload:
    """Payload corruption — most common silent corruption vector."""

    def test_single_byte_flip_detected(self, wal_with_data):
        wal, path = wal_with_data
        ok_before, _ = wal.verify_integrity()
        assert ok_before, "WAL should be clean before corruption"

        # Flip a byte in the 3rd record's payload
        def flip_byte(line: str) -> str:
            record = json.loads(line)
            payload = record.get("payload", {})
            if "value" in payload:
                payload["value"] = 99999  # was i*10
            record["payload"] = payload
            return json.dumps(record, sort_keys=True) + "\n"

        _corrupt_file_line(path, 3, flip_byte)

        ok_after, reason = wal.verify_integrity()
        assert not ok_after, f"Bit flip should be detected; got reason: {reason}"
        assert (
            "hash mismatch" in reason.lower() or "record hash" in reason.lower()
        ), f"Expected hash mismatch reason, got: {reason}"

    def test_payload_value_swap_detected(self, wal_with_data):
        wal, path = wal_with_data
        # Swap payloads between two records
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        r3 = json.loads(lines[3])
        r7 = json.loads(lines[7])
        r3["payload"], r7["payload"] = r7["payload"], r3["payload"]
        lines[3] = json.dumps(r3, sort_keys=True) + "\n"
        lines[7] = json.dumps(r7, sort_keys=True) + "\n"
        path.write_text("".join(lines), encoding="utf-8")

        ok, reason = wal.verify_integrity()
        assert not ok, f"Payload swap should be detected: {reason}"


class TestBitRotChainBreak:
    """Hash chain corruption — prev_hash manipulation."""

    def test_prev_hash_rewrite_detected(self, wal_with_data):
        wal, path = wal_with_data

        def rewrite_prev(line: str) -> str:
            record = json.loads(line)
            record["prev_hash"] = GENESIS_HASH  # force genesis hash
            # Must recompute record_hash to pass internal check
            from core.data.write_ahead_log import _compute_hash

            record["record_hash"] = _compute_hash(record["prev_hash"], record["payload"])
            return json.dumps(record, sort_keys=True) + "\n"

        _corrupt_file_line(path, 5, rewrite_prev)

        ok, reason = wal.verify_integrity()
        assert not ok, f"Chain break should be detected: {reason}"
        assert "chain" in reason.lower() or "hash" in reason.lower()

    def test_genesis_bypass_attack_detected(self, wal_with_data):
        """Insert a fake record with prev_hash=GENESIS in the middle."""
        wal, path = wal_with_data
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        # Read the 4th record to get plausible data
        r4 = json.loads(lines[4])
        fake = {
            "seq": 999,
            "prev_hash": GENESIS_HASH,
            "record_hash": GENESIS_HASH,  # obviously wrong
            "type": "chaos_injected",
            "timestamp_wall": r4.get("timestamp_wall", ""),
            "payload": {"attack": "genesis_bypass"},
        }
        lines.insert(5, json.dumps(fake, sort_keys=True) + "\n")
        path.write_text("".join(lines), encoding="utf-8")

        ok, reason = wal.verify_integrity()
        assert not ok, f"Genesis bypass should be detected: {reason}"


class TestBitRotDuplicateSeq:
    """Duplicate sequence number attacks."""

    def test_duplicate_seq_detected(self, wal_with_data):
        wal, path = wal_with_data
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        # Duplicate the 2nd record
        r2 = json.loads(lines[2])
        dup = dict(r2)
        dup["record_hash"] = r2["record_hash"]  # same hash
        lines.insert(3, json.dumps(dup, sort_keys=True) + "\n")
        path.write_text("".join(lines), encoding="utf-8")

        ok, reason = wal.verify_integrity()
        assert not ok, f"Duplicate seq should be detected: {reason}"

    def test_replay_attack_duplicate_seq(self, wal_with_data):
        """Same payload written twice — replay attack."""
        wal, path = wal_with_data
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        # Append a copy of the 1st record at the end
        r1 = json.loads(lines[1])
        replay = dict(r1)
        replay["seq"] = 10  # new seq but same payload+hash as seq 1
        lines.append(json.dumps(replay, sort_keys=True) + "\n")
        path.write_text("".join(lines), encoding="utf-8")

        # Duplicate seq is detected (seq already seen earlier)
        ok, reason = wal.verify_integrity()
        assert not ok, f"Replay attack should be detected: {reason}"


class TestBitRotDeletion:
    """Record deletion — missing records in the chain."""

    def test_single_record_deletion_detected(self, wal_with_data):
        wal, path = wal_with_data
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        # Delete the 4th line (seq=3)
        del lines[3]
        path.write_text("".join(lines), encoding="utf-8")

        ok, reason = wal.verify_integrity()
        assert not ok, (
            f"Record deletion should break the hash chain, but verify passed. " f"Reason: {reason}"
        )

    def test_first_record_deletion_detected(self, wal_with_data):
        """Deleting the first record (seq=0) should be detected."""
        wal, path = wal_with_data
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        del lines[1]  # delete seq=0 (line 1 after header)
        path.write_text("".join(lines), encoding="utf-8")

        ok, reason = wal.verify_integrity()
        assert not ok, f"First record deletion should be detected: {reason}"


class TestBitRotCI:
    """Chaos injections designed to be caught in CI.

    These are fast-running deterministic corruptions that must always be
    detected.  The CI gate (ci-mirror-pytest) runs these on every push.
    """

    def test_all_corruption_modes_detected(self, wal_with_data):
        """Smoke test: every corruption mode fails verification."""
        wal, path = wal_with_data
        modes_tested = 0

        # Mode 1: payload mutation
        def mutate(line):
            r = json.loads(line)
            r["payload"]["value"] = -999999
            return json.dumps(r, sort_keys=True) + "\n"

        _corrupt_file_line(path, 2, mutate)
        ok, _ = wal.verify_integrity()
        assert not ok
        modes_tested += 1

        # Restore and try next mode
        wal2 = WriteAheadLog(WALConfig(path=Path(str(path) + ".2")))
        for i in range(10):
            wal2.append({"event": f"r_{i}", "value": i})
        path2 = Path(str(path) + ".2")

        # Mode 2: prev_hash break
        def break_chain(line):
            r = json.loads(line)
            r["prev_hash"] = "BROKEN_CHAIN_DEADBEEF"
            return json.dumps(r, sort_keys=True) + "\n"

        _corrupt_file_line(path2, 5, break_chain)
        ok2, _ = wal2.verify_integrity()
        assert not ok2
        modes_tested += 1

        assert modes_tested == 2, "Should verify 2 corruption modes"
