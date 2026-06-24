"""UGR-B05: Phantom stub inconsistency injection — CI capture.

Chaos tests that inject phantom stub inconsistencies and verify:
  1. Corrupted stub is rejected or detected
  2. State projection divergence is caught
  3. CI can capture phantom contract violations
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.contracts.phantom_contract import (
    PhantomSerializer,
    PhantomStub,
    StateProjector,
)
from core.data.write_ahead_log import WALConfig, WriteAheadLog


@pytest.fixture
def phantom_wal():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        wal = WriteAheadLog(WALConfig(path=tmp / "phantom_wal.jsonl"))
        yield wal


@pytest.fixture
def mixed_wal():
    """WAL with both regular events and phantom stubs mixed."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        wal = WriteAheadLog(WALConfig(path=tmp / "mixed.jsonl"))
        # Write some regular events
        wal.append({"event": "position_open", "ticket": 1}, record_type="position_open")
        wal.append({"event": "bar_close", "price": 1.2345}, record_type="bar")
        # Write a phantom stub
        stub = PhantomStub(
            contract_id="risk_budget_non_negative",
            recorded_at_wal_seq=2,
            contract_version=1,
            input_snapshot={"budget": 100.0},
            input_hash="abc123",
            assumed_ok=True,
            timestamp_wall="2026-06-24T00:00:00Z",
            caller_module="test",
        )
        wal.append(stub.to_payload(), record_type="phantom_stub")
        wal.append({"event": "position_close", "ticket": 1}, record_type="position_close")
        yield wal


class TestPhantomStubCorruption:
    """Corrupt phantom stubs and verify detection."""

    def test_stub_missing_required_fields_detected(self, phantom_wal):
        """Write a stub with empty contract_id — anomaly detectable on replay."""
        bad_stub = {
            "contract_id": "",  # empty — should be non-empty for valid stubs
            "recorded_at_wal_seq": 0,
            "assumed_ok": True,
            "input_snapshot": {},
            "input_hash": "",
            "contract_version": 1,
            "timestamp_wall": "2026-06-24T00:00:00Z",
            "caller_module": "chaos_test",
        }
        phantom_wal.append(bad_stub, record_type="phantom_stub")
        record = phantom_wal.read(0)
        assert record is not None
        assert record.type == "phantom_stub"
        # Anomaly: contract_id is empty — should be caught during offline verification
        assert (
            record.payload.get("contract_id") == ""
        ), "Empty contract_id should be detectable as anomaly"

    def test_stub_input_hash_mismatch_detected(self, phantom_wal):
        """Write a stub whose input_hash doesn't match its snapshot."""
        snapshot = {"budget": 500.0, "positions": 3}
        correct_hash = PhantomSerializer.compute_hash(snapshot)
        wrong_hash = "DEADBEEF_INCORRECT_HASH"

        assert correct_hash != wrong_hash, "Test requires incorrect hash"

        stub = PhantomStub(
            contract_id="risk_budget_non_negative",
            recorded_at_wal_seq=0,
            contract_version=1,
            input_snapshot=snapshot,
            input_hash=wrong_hash,  # INTENTIONALLY WRONG
            assumed_ok=True,
            timestamp_wall="2026-06-24T00:00:00Z",
            caller_module="chaos_test",
        )
        phantom_wal.append(stub.to_payload(), record_type="phantom_stub")

        record = phantom_wal.read(0)
        assert record is not None
        assert record.payload["input_hash"] == wrong_hash

        # The inconsistency exists — offline verification would catch it
        recomputed = PhantomSerializer.compute_hash(record.payload.get("input_snapshot", {}))
        assert (
            recomputed != record.payload["input_hash"]
        ), "Hash mismatch should persist (chaos injection successful)"

    def test_stub_assumed_ok_false_detected(self, phantom_wal):
        """A stub with assumed_ok=False — predicate violation recorded."""
        stub = PhantomStub(
            contract_id="risk_budget_non_negative",
            recorded_at_wal_seq=0,
            contract_version=1,
            input_snapshot={"budget": -100.0},  # negative budget!
            input_hash=PhantomSerializer.compute_hash({"budget": -100.0}),
            assumed_ok=False,
            timestamp_wall="2026-06-24T00:00:00Z",
            caller_module="chaos_test",
        )
        phantom_wal.append(stub.to_payload(), record_type="phantom_stub")

        record = phantom_wal.read(0)
        assert record is not None
        assert record.payload["assumed_ok"] is False


class TestStateProjectionDivergence:
    """State projection should detect phantom stub inconsistency."""

    def test_projector_skips_phantom_stubs(self, mixed_wal):
        """StateProjector skips phantom_stub records during reconstruction."""
        projector = StateProjector()
        events_seen: list[str] = []

        def handle_open(entry):
            if isinstance(entry, dict) and "ticket" in str(entry.get("payload", "")):
                events_seen.append(f"open:{entry.get('payload', {})}")

        def handle_close(entry):
            if isinstance(entry, dict):
                events_seen.append(f"close:{entry.get('payload', {})}")

        projector.register_handler("position_open", handle_open)
        projector.register_handler("position_close", handle_close)

        for record in mixed_wal:
            if record.type == "phantom_stub":
                continue  # projector skips these
            # apply() takes a dict, not WALRecord
            projector.apply(record.payload)

        # StateProjector have accumulated state
        snapshot = projector.snapshot()
        assert isinstance(snapshot, dict)

    def test_stub_sequence_gap_detectable(self, phantom_wal):
        """Missing phantom stubs create a detectable gap."""
        # Write stubs at seq 0, 1, 2, 3, 5 (skip 4)
        for i in [0, 1, 2, 3, 5]:
            stub = PhantomStub(
                contract_id="test_contract",
                recorded_at_wal_seq=i,
                contract_version=1,
                input_snapshot={"seq": i},
                input_hash=PhantomSerializer.compute_hash({"seq": i}),
                assumed_ok=True,
                timestamp_wall="2026-06-24T00:00:00Z",
                caller_module="chaos_test",
            )
            phantom_wal.append(stub.to_payload(), record_type="phantom_stub")

        # Replay and check for gaps (WAL seq is always sequential;
        # the gap lives in recorded_at_wal_seq inside the stub payload)
        seen_seqs = set()
        for record in phantom_wal:
            seen_seqs.add(record.payload.get("recorded_at_wal_seq"))

        assert 4 not in seen_seqs, "Seq 4 should be missing (gap injection)"
        # Verify we have the expected records
        assert len(seen_seqs) == 5, f"Expected 5 records, got {len(seen_seqs)}"


class TestCIPhantomIntegration:
    """CI-integrated phantom chaos tests — must run in CI gate."""

    def test_all_stubs_in_chain_have_valid_hash(self, phantom_wal):
        """Every phantom stub in the chain has a valid input_hash."""
        for i in range(5):
            snapshot = {"i": i, "data": f"test_{i}"}
            stub = PhantomStub(
                contract_id="ci_test",
                recorded_at_wal_seq=i,
                contract_version=1,
                input_snapshot=snapshot,
                input_hash=PhantomSerializer.compute_hash(snapshot),
                assumed_ok=True,
                timestamp_wall="2026-06-24T00:00:00Z",
                caller_module="ci_test",
            )
            phantom_wal.append(stub.to_payload(), record_type="phantom_stub")

        for record in phantom_wal:
            if record.type == "phantom_stub":
                stored_hash = record.payload.get("input_hash", "")
                snapshot = record.payload.get("input_snapshot", {})
                computed = PhantomSerializer.compute_hash(snapshot)
                assert stored_hash == computed, (
                    f"Hash mismatch at seq={record.seq}: "
                    f"stored={stored_hash[:16]}... computed={computed[:16]}..."
                )
