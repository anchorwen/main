"""Tests for core.contracts.phantom_contract — UGR v3.1 §B01.

Covers:
- PhantomStub creation and serialization roundtrip
- PhantomSerializer serialize/deserialize (all types)
- PhantomSerializer input_hash determinism
- PredicateRegistry register/get/version
- phantom decorator: __debug__ mode (assertion)
- phantom decorator: production mode (stub written to WAL)
- phantom decorator: hot_path=True (no stub)
- phantom decorator: no WAL configured (safe no-op)
- ContractViolation raised on predicate failure in __debug__
- verify_phantom_contracts script (MVP replay)
"""

from __future__ import annotations

import pytest

from core.contracts.phantom_contract import (
    ContractViolation,
    PhantomSerializer,
    PhantomStub,
    PredicateRegistry,
    phantom,
    set_phantom_wal,
)
from core.data.write_ahead_log import WALConfig, WriteAheadLog

# ═══════════════════════════════════════════════════════════════════════════
# PhantomStub
# ═══════════════════════════════════════════════════════════════════════════


class TestPhantomStub:
    def test_create_with_fields(self) -> None:
        stub = PhantomStub(
            contract_id="test_contract",
            recorded_at_wal_seq=42,
            contract_version=1,
            input_snapshot={"args": [1, 2]},
            input_hash="abc123",
            assumed_ok=True,
            timestamp_wall="2026-06-24T12:00:00Z",
            caller_module="test.module",
        )
        assert stub.contract_id == "test_contract"
        assert stub.recorded_at_wal_seq == 42
        assert stub.assumed_ok is True

    def test_to_payload_roundtrip(self) -> None:
        original = PhantomStub(
            contract_id="risk_budget_non_negative",
            recorded_at_wal_seq=99,
            contract_version=1,
            input_snapshot={"args": [-5.0], "kwargs": {}},
            input_hash="deadbeef",
            assumed_ok=False,
            timestamp_wall="2026-06-24T12:00:00Z",
            caller_module="core.contracts.phantom_contract",
        )
        payload = original.to_payload()
        restored = PhantomStub.from_payload(payload)
        assert restored.contract_id == original.contract_id
        assert restored.recorded_at_wal_seq == original.recorded_at_wal_seq
        assert restored.input_snapshot == original.input_snapshot
        assert restored.assumed_ok == original.assumed_ok

    def test_from_payload_defaults(self) -> None:
        stub = PhantomStub.from_payload({})
        assert stub.contract_id == ""
        assert stub.recorded_at_wal_seq == 0
        assert stub.contract_version == 1


# ═══════════════════════════════════════════════════════════════════════════
# PhantomSerializer
# ═══════════════════════════════════════════════════════════════════════════


class TestPhantomSerializer:
    def test_serialize_primitive_args(self) -> None:
        snapshot = PhantomSerializer.serialize_args((42, "hello", 3.14), {})
        assert snapshot["args"] == [42, "hello", 3.14]
        assert snapshot["kwargs"] == {}

    def test_serialize_kwargs(self) -> None:
        snapshot = PhantomSerializer.serialize_args((), {"budget": -100.0, "symbol": "XAU"})
        assert snapshot["args"] == []
        assert snapshot["kwargs"] == {"budget": -100.0, "symbol": "XAU"}

    def test_serialize_mixed(self) -> None:
        snapshot = PhantomSerializer.serialize_args((True,), {"limit": 5})
        assert snapshot["args"] == [True]
        assert snapshot["kwargs"] == {"limit": 5}

    def test_deserialize_roundtrip(self) -> None:
        original_args = (1, "test", 3.14, True, None)
        original_kwargs = {"key": "value", "num": 42}
        snapshot = PhantomSerializer.serialize_args(original_args, original_kwargs)
        restored_args, restored_kwargs = PhantomSerializer.deserialize_args(snapshot)
        assert restored_args == original_args
        assert restored_kwargs == original_kwargs

    def test_serialize_nested(self) -> None:
        snapshot = PhantomSerializer.serialize_args(([1, 2, 3], {"nested": True}), {})
        restored_args, _ = PhantomSerializer.deserialize_args(snapshot)
        assert restored_args[0] == [1, 2, 3]
        assert restored_args[1] == {"nested": True}

    def test_deterministic_hash(self) -> None:
        s1 = PhantomSerializer.serialize_args((1, "a"), {"x": 2})
        s2 = PhantomSerializer.serialize_args((1, "a"), {"x": 2})
        assert PhantomSerializer.compute_hash(s1) == PhantomSerializer.compute_hash(s2)

    def test_different_input_different_hash(self) -> None:
        s1 = PhantomSerializer.serialize_args((1,), {})
        s2 = PhantomSerializer.serialize_args((2,), {})
        assert PhantomSerializer.compute_hash(s1) != PhantomSerializer.compute_hash(s2)

    def test_hash_is_64_char_hex(self) -> None:
        snapshot = PhantomSerializer.serialize_args((), {})
        h = PhantomSerializer.compute_hash(snapshot)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ═══════════════════════════════════════════════════════════════════════════
# PredicateRegistry
# ═══════════════════════════════════════════════════════════════════════════


class TestPredicateRegistry:
    def test_register_and_get(self) -> None:
        def my_pred(x: int) -> bool:
            return x > 0

        PredicateRegistry.register("test_pred", version=1)(my_pred)
        assert PredicateRegistry.get("test_pred") is my_pred
        assert PredicateRegistry.get_version("test_pred") == 1

    def test_get_unknown_returns_none(self) -> None:
        assert PredicateRegistry.get("nonexistent") is None

    def test_get_version_unknown_returns_zero(self) -> None:
        assert PredicateRegistry.get_version("nonexistent") == 0

    def test_list_contracts(self) -> None:
        contracts = PredicateRegistry.list_contracts()
        assert "risk_budget_non_negative" in contracts
        assert contracts["risk_budget_non_negative"] == 1

    def test_mvp_predicate_registered(self) -> None:
        pred = PredicateRegistry.get("risk_budget_non_negative")
        assert pred is not None
        assert pred(100.0) is True
        assert pred(-1.0) is False
        assert pred(0.0) is True


# ═══════════════════════════════════════════════════════════════════════════
# Phantom decorator — __debug__ mode
# ═══════════════════════════════════════════════════════════════════════════


class TestPhantomDebug:
    def test_passes_when_predicate_ok(self) -> None:
        def _pred(x: int) -> bool:
            return x > 0

        @phantom(predicate=_pred, message="must be positive", contract_id="test_pos")
        def do_thing(x: int) -> int:
            return x * 2

        assert do_thing(5) == 10

    def test_raises_contract_violation_when_predicate_fails(self) -> None:
        def _pred(x: int) -> bool:
            return x < 0

        @phantom(
            predicate=_pred,
            message="must be negative",
            contract_id="test_neg",
            severity="critical",
        )
        def do_thing(x: int) -> int:
            return x * 2

        with pytest.raises(ContractViolation, match="test_neg"):
            do_thing(5)

    def test_function_body_not_executed_on_violation(self) -> None:
        called = False

        def _pred(x: int) -> bool:
            return False

        @phantom(predicate=_pred, message="fail", contract_id="test_fail")
        def do_thing(x: int) -> int:
            nonlocal called
            called = True
            return x

        with pytest.raises(ContractViolation):
            do_thing(1)
        assert not called


# ═══════════════════════════════════════════════════════════════════════════
# Phantom decorator — production mode
# ═══════════════════════════════════════════════════════════════════════════


class TestPhantomProduction:
    @pytest.fixture
    def wal(self, tmp_path):
        wal_path = tmp_path / "phantom_test.jsonl"
        wal_config = WALConfig(path=wal_path)
        return WriteAheadLog(wal_config)

    @pytest.fixture(autouse=True)
    def _reset_production_mode(self):
        import core.contracts.phantom_contract as _pc

        _pc._FORCE_PRODUCTION_MODE = False
        yield
        _pc._FORCE_PRODUCTION_MODE = False

    def test_stub_written_to_wal(self, wal: WriteAheadLog) -> None:
        set_phantom_wal(wal)

        def _pred(x: int) -> bool:
            return x > 0

        @phantom(
            predicate=_pred, message="must be positive", contract_id="test_stub", hot_path=False
        )
        def do_thing(x: int) -> int:
            return x * 2

        import core.contracts.phantom_contract as _pc

        _pc._FORCE_PRODUCTION_MODE = True
        try:
            result = do_thing(5)
        finally:
            _pc._FORCE_PRODUCTION_MODE = False
        assert result == 10

        stubs = [r for r in wal if r.type == "phantom_stub"]
        assert len(stubs) == 1
        assert stubs[0].payload["contract_id"] == "test_stub"

    def test_hot_path_no_stub(self, wal: WriteAheadLog) -> None:
        set_phantom_wal(wal)

        def _pred(x: int) -> bool:
            return x > 0

        @phantom(predicate=_pred, message="must be positive", contract_id="test_hot", hot_path=True)
        def do_thing(x: int) -> int:
            return x * 2

        import core.contracts.phantom_contract as _pc

        _pc._FORCE_PRODUCTION_MODE = True
        try:
            result = do_thing(5)
        finally:
            _pc._FORCE_PRODUCTION_MODE = False
        assert result == 10

        stubs = [r for r in wal if r.type == "phantom_stub"]
        assert len(stubs) == 0

    def test_no_wal_configured_safe_noop(self) -> None:
        set_phantom_wal(None)  # Test: deliberately pass None to test no-WAL path

        def _pred(x: int) -> bool:
            return x > 0

        @phantom(
            predicate=_pred, message="must be positive", contract_id="test_no_wal", hot_path=False
        )
        def do_thing(x: int) -> int:
            return x * 2

        import core.contracts.phantom_contract as _pc

        _pc._FORCE_PRODUCTION_MODE = True
        try:
            result = do_thing(5)
        finally:
            _pc._FORCE_PRODUCTION_MODE = False
        assert result == 10

    def test_stub_saves_input_snapshot(self, wal: WriteAheadLog) -> None:
        set_phantom_wal(wal)

        def _pred(budget: float) -> bool:
            return budget >= 0.0

        @phantom(
            predicate=_pred,
            message="budget must be non-negative",
            contract_id="risk_budget_non_negative",
            hot_path=False,
        )
        def evaluate_risk(budget: float) -> bool:
            return budget >= 0.0

        import core.contracts.phantom_contract as _pc

        _pc._FORCE_PRODUCTION_MODE = True
        try:
            evaluate_risk(-50.0)
        finally:
            _pc._FORCE_PRODUCTION_MODE = False

        stubs = [r for r in wal if r.type == "phantom_stub"]
        assert len(stubs) == 1
        snapshot = stubs[0].payload["input_snapshot"]
        assert snapshot["args"] == [-50.0]


# ═══════════════════════════════════════════════════════════════════════════
# Offline Verifier integration
# ═══════════════════════════════════════════════════════════════════════════


class TestOfflineVerifier:
    def test_verify_empty_wal(self, tmp_path) -> None:
        wal_path = tmp_path / "empty.jsonl"
        wal_path.write_text("", encoding="utf-8")
        from scripts.verify_phantom_contracts import verify

        report = verify(wal_path)
        assert report.total_stubs == 0
        assert not report.has_violations

    def test_verify_mvp_predicate_pass(self, tmp_path) -> None:
        wal_path = tmp_path / "test.jsonl"
        wal_config = WALConfig(path=wal_path)
        wal = WriteAheadLog(wal_config)
        snapshot = PhantomSerializer.serialize_args((100.0,), {})
        input_hash = PhantomSerializer.compute_hash(snapshot)
        wal.append(
            PhantomStub(
                contract_id="risk_budget_non_negative",
                recorded_at_wal_seq=0,
                contract_version=1,
                input_snapshot=snapshot,
                input_hash=input_hash,
                assumed_ok=True,
                timestamp_wall="2026-06-24T12:00:00Z",
                caller_module="test",
            ).to_payload(),
            record_type="phantom_stub",
        )
        from scripts.verify_phantom_contracts import verify

        report = verify(wal_path)
        assert report.total_stubs == 1
        assert report.replayed == 1
        assert not report.has_violations

    def test_verify_detects_violation(self, tmp_path) -> None:
        wal_path = tmp_path / "test_violation.jsonl"
        wal_config = WALConfig(path=wal_path)
        wal = WriteAheadLog(wal_config)
        snapshot = PhantomSerializer.serialize_args((-50.0,), {})
        input_hash = PhantomSerializer.compute_hash(snapshot)
        wal.append(
            PhantomStub(
                contract_id="risk_budget_non_negative",
                recorded_at_wal_seq=0,
                contract_version=1,
                input_snapshot=snapshot,
                input_hash=input_hash,
                assumed_ok=True,
                timestamp_wall="2026-06-24T12:00:00Z",
                caller_module="test",
            ).to_payload(),
            record_type="phantom_stub",
        )
        from scripts.verify_phantom_contracts import verify

        report = verify(wal_path)
        assert report.total_stubs == 1
        assert report.replayed == 1
        assert report.has_violations
        assert len(report.violations) == 1
        assert report.violations[0].contract_id == "risk_budget_non_negative"
        assert report.violations[0].actual_ok is False

    def test_verify_dedup_by_input_hash(self, tmp_path) -> None:
        wal_path = tmp_path / "test_dedup.jsonl"
        wal_config = WALConfig(path=wal_path)
        wal = WriteAheadLog(wal_config)
        snapshot = PhantomSerializer.serialize_args((100.0,), {})
        input_hash = PhantomSerializer.compute_hash(snapshot)
        payload = PhantomStub(
            contract_id="risk_budget_non_negative",
            recorded_at_wal_seq=0,
            contract_version=1,
            input_snapshot=snapshot,
            input_hash=input_hash,
            assumed_ok=True,
            timestamp_wall="2026-06-24T12:00:00Z",
            caller_module="test",
        ).to_payload()
        wal.append(payload, record_type="phantom_stub")
        wal.append(payload, record_type="phantom_stub")
        from scripts.verify_phantom_contracts import verify

        report = verify(wal_path)
        assert report.total_stubs == 2
        assert report.deduped_stubs == 1
        assert report.replayed == 1
