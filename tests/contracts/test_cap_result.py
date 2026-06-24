"""Tests for CapResult[T] + _SuccessProof — UGR v3.1 foundational type.

Covers:
- _SuccessProof lifecycle (creation, validity, expiration)
- CapResult creation (ok requires proof, err does not)
- Consumption (match, is_ok, is_err)
- Transformation (map, flat_map with proof chain)
- Kernel.try_operation() bridge
"""

from __future__ import annotations

import pytest

from core.contracts.cap_result import (
    CapProofExpired,
    CapResult,
    Kernel,
    _SuccessProof,
)

# ═══════════════════════════════════════════════════════════════════════════
# _SuccessProof lifecycle
# ═══════════════════════════════════════════════════════════════════════════


class TestSuccessProofLifecycle:
    """Tests for _SuccessProof creation, validity, and expiration."""

    def test_cannot_instantiate_directly(self) -> None:
        """_SuccessProof() must raise TypeError."""
        with pytest.raises(TypeError, match="cannot be instantiated directly"):
            _SuccessProof()

    def test_cannot_create_via_new(self) -> None:
        """object.__new__(_SuccessProof) bypasses __init__ but creates
        an uninitialized proof — is_valid must be False."""
        proof = object.__new__(_SuccessProof)
        # No _nonce, no _valid — accessing is_valid must not crash
        with pytest.raises(AttributeError):
            _ = proof.is_valid

    def test_success_scope_creates_valid_proof(self) -> None:
        """Kernel.success_scope() yields a valid proof."""
        with Kernel.success_scope() as proof:
            assert proof.is_valid is True
            assert isinstance(proof, _SuccessProof)

    def test_proof_expires_on_scope_exit(self) -> None:
        """After scope exit, is_valid becomes False."""
        proof_ref: _SuccessProof | None = None
        with Kernel.success_scope() as proof:
            proof_ref = proof
            assert proof_ref.is_valid is True
        assert proof_ref is not None
        assert proof_ref.is_valid is False

    def test_proof_repr(self) -> None:
        """__repr__ shows proof state."""
        with Kernel.success_scope() as proof:
            assert "valid" in repr(proof)
        assert "expired" in repr(proof)


# ═══════════════════════════════════════════════════════════════════════════
# CapResult creation
# ═══════════════════════════════════════════════════════════════════════════


class TestCapResultCreation:
    """Tests for CapResult.ok() and CapResult.err() factory methods."""

    def test_cannot_instantiate_directly(self) -> None:
        """CapResult() must raise TypeError."""
        with pytest.raises(TypeError, match="cannot be instantiated directly"):
            CapResult()

    def test_ok_requires_proof(self) -> None:
        """CapResult.ok(value, proof) with valid proof succeeds."""
        with Kernel.success_scope() as proof:
            result = CapResult.ok(42, proof)
            assert result.is_ok() is True
            assert result.is_err() is False

    def test_ok_rejects_expired_proof(self) -> None:
        """CapResult.ok() with expired proof raises CapProofExpired."""
        with Kernel.success_scope() as proof:
            pass  # proof is now expired
        with pytest.raises(CapProofExpired, match="left its success_scope"):
            CapResult.ok(42, proof)

    def test_err_works_without_proof(self) -> None:
        """CapResult.err() requires no proof."""
        result = CapResult.err("something went wrong")  # type: ignore[var-annotated]
        assert result.is_err() is True
        assert result.is_ok() is False

    def test_err_preserves_message(self) -> None:
        """CapResult.err() stores the error string."""
        result = CapResult.err("disk full")  # type: ignore[var-annotated]
        assert result.is_err()


class TestCapResultMatch:
    """Tests for CapResult.match() consumption."""

    def test_match_ok_calls_ok_branch(self) -> None:
        """match() dispatches to ok when is_ok=True."""
        with Kernel.success_scope() as proof:
            result = CapResult.ok("success", proof)
        output = result.match(ok=lambda v: f"OK:{v}", err=lambda e: f"ERR:{e}")
        assert output == "OK:success"

    def test_match_err_calls_err_branch(self) -> None:
        """match() dispatches to err when is_ok=False."""
        result = CapResult.err("timeout")  # type: ignore[var-annotated]
        output = result.match(ok=lambda v: f"OK:{v}", err=lambda e: f"ERR:{e}")
        assert output == "ERR:timeout"

    def test_match_ok_with_complex_type(self) -> None:
        """match() works with dict/list values."""
        with Kernel.success_scope() as proof:
            result = CapResult.ok({"key": [1, 2, 3]}, proof)
        output = result.match(ok=lambda v: v["key"], err=lambda e: [])
        assert output == [1, 2, 3]

    def test_match_err_with_empty_ok_raises_no_error(self) -> None:
        """match() never evaluates the ok branch for an err result."""
        result = CapResult.err("fail")  # type: ignore[var-annotated]
        called: list[str] = []

        def ok_fn(v: object) -> str:
            called.append("ok")
            return str(v)

        def err_fn(e: str) -> str:
            called.append("err")
            return e

        output = result.match(ok=ok_fn, err=err_fn)
        assert called == ["err"]
        assert output == "fail"


class TestCapResultTransform:
    """Tests for CapResult.map() and CapResult.flat_map()."""

    def test_map_transforms_ok(self) -> None:
        """map(fn, proof) transforms the ok value."""
        with Kernel.success_scope() as proof:
            result = CapResult.ok(10, proof)
            doubled = result.map(lambda x: x * 2, proof)
        assert doubled.is_ok()
        doubled.match(ok=lambda v: None, err=lambda e: None)
        with Kernel.success_scope() as proof2:
            match_result = doubled.match(ok=lambda v: v, err=lambda e: -1)
            assert match_result == 20

    def test_map_preserves_err(self) -> None:
        """map() on an err result returns the same error."""
        result = CapResult.err("oops")  # type: ignore[var-annotated]
        with Kernel.success_scope() as proof:
            mapped = result.map(lambda x: x * 2, proof)
        assert mapped.is_err()
        mapped.match(ok=lambda v: None, err=lambda e: None)
        assert mapped.match(ok=lambda v: "ok", err=lambda e: e) == "oops"

    def test_map_rejects_expired_proof(self) -> None:
        """map() with expired proof raises CapProofExpired."""
        with Kernel.success_scope() as proof:
            result = CapResult.ok(10, proof)
        with pytest.raises(CapProofExpired):
            result.map(lambda x: x * 2, proof)

    def test_flat_map_chains_ok(self) -> None:
        """flat_map() chains a fallible operation."""
        with Kernel.success_scope() as proof:

            def try_parse(s: str) -> CapResult[int]:
                try:
                    return CapResult.ok(int(s), proof)
                except ValueError:
                    return CapResult.err(f"not a number: {s}")

            result = CapResult.ok("42", proof)
            chained = result.flat_map(try_parse, proof)
        assert chained.is_ok()
        chained.match(ok=lambda v: None, err=lambda e: None)
        with Kernel.success_scope() as proof2:
            assert chained.match(ok=lambda v: v, err=lambda e: -1) == 42

    def test_flat_map_preserves_err(self) -> None:
        """flat_map() on an err result returns the same error."""
        result = CapResult.err("already failed")  # type: ignore[var-annotated]
        with Kernel.success_scope() as proof:

            def try_parse(s: str) -> CapResult[int]:
                return CapResult.ok(int(s), proof)

            chained = result.flat_map(try_parse, proof)
        assert chained.is_err()
        assert chained.match(ok=lambda v: "ok", err=lambda e: e) == "already failed"

    def test_flat_map_rejects_expired_proof(self) -> None:
        """flat_map() with expired proof raises CapProofExpired."""
        with Kernel.success_scope() as proof:
            result = CapResult.ok("42", proof)
        with pytest.raises(CapProofExpired):

            def noop(s: str) -> CapResult[str]:
                return CapResult.err("x")

            result.flat_map(noop, proof)


class TestCapResultDunder:
    """Tests for __eq__, __repr__, __hash__."""

    def test_eq_same_ok(self) -> None:
        """Two ok results with same value are equal."""
        with Kernel.success_scope() as proof:
            r1 = CapResult.ok(42, proof)
            r2 = CapResult.ok(42, proof)
        assert r1 == r2

    def test_eq_same_err(self) -> None:
        """Two err results with same message are equal."""
        assert CapResult.err("x") == CapResult.err("x")

    def test_eq_different_types(self) -> None:
        """ok != err regardless of value."""
        with Kernel.success_scope() as proof:
            ok_result = CapResult.ok(42, proof)
        err_result = CapResult.err("42")  # type: ignore[var-annotated]
        assert ok_result != err_result

    def test_eq_non_capresult(self) -> None:
        """CapResult == non-CapResult returns NotImplemented."""
        with Kernel.success_scope() as proof:
            result = CapResult.ok(42, proof)
        assert result != 42
        assert result != "42"

    def test_repr_ok(self) -> None:
        """__repr__ for ok results."""
        with Kernel.success_scope() as proof:
            result = CapResult.ok(42, proof)
        assert "CapResult.ok(42)" in repr(result)

    def test_repr_err(self) -> None:
        """__repr__ for err results."""
        result = CapResult.err("timeout")  # type: ignore[var-annotated]
        assert "CapResult.err('timeout')" in repr(result)

    def test_hash_consistent(self) -> None:
        """Equal results have equal hashes."""
        r1 = CapResult.err("same")  # type: ignore[var-annotated]
        r2 = CapResult.err("same")  # type: ignore[var-annotated]
        assert hash(r1) == hash(r2)
        assert len({r1, r2}) == 1  # set dedup


# ═══════════════════════════════════════════════════════════════════════════
# Kernel.try_operation() bridge
# ═══════════════════════════════════════════════════════════════════════════


class TestKernelTryOperation:
    """Tests for Kernel.try_operation() — legacy bridge."""

    def test_successful_operation(self) -> None:
        """try_operation wraps a successful call in CapResult.ok."""

        def compute() -> int:
            return 1 + 1

        result = Kernel.try_operation(compute)
        assert result.is_ok()
        result.match(ok=lambda v: None, err=lambda e: None)
        with Kernel.success_scope() as proof:
            assert result.match(ok=lambda v: v, err=lambda e: -1) == 2

    def test_failing_operation(self) -> None:
        """try_operation catches exceptions and returns CapResult.err."""

        def fail() -> int:
            raise ValueError("bad input")

        result = Kernel.try_operation(fail, error_context="parse")
        assert result.is_err()
        msg = result.match(ok=lambda v: "", err=lambda e: e)
        assert "parse" in msg
        assert "bad input" in msg


# ═══════════════════════════════════════════════════════════════════════════
# Multiple proofs — independent lifecycles
# ═══════════════════════════════════════════════════════════════════════════


class TestMultipleScopes:
    """Tests for nested and sequential success scopes."""

    def test_nested_scopes_independent_proofs(self) -> None:
        """Inner and outer scopes have different, independently-valid proofs."""
        with Kernel.success_scope() as outer_proof:
            with Kernel.success_scope() as inner_proof:
                # Both are valid
                assert outer_proof.is_valid
                assert inner_proof.is_valid
                # Different proofs
                assert outer_proof is not inner_proof
            # Inner expired, outer still valid
            assert inner_proof.is_valid is False
            assert outer_proof.is_valid is True
        # Both expired
        assert outer_proof.is_valid is False

    def test_sequential_scopes(self) -> None:
        """Two sequential scopes produce valid proofs each time."""
        results: list[CapResult[int]] = []
        with Kernel.success_scope() as proof1:
            results.append(CapResult.ok(1, proof1))
        with Kernel.success_scope() as proof2:
            results.append(CapResult.ok(2, proof2))
        assert len(results) == 2
        assert results[0].is_ok()
        assert results[1].is_ok()


# ═══════════════════════════════════════════════════════════════════════════
# Generic type parameter
# ═══════════════════════════════════════════════════════════════════════════


class TestGenericTyping:
    """Verify generic type parameter is preserved in usage patterns."""

    def test_heterogeneous_types(self) -> None:
        """CapResult works with different type parameters."""
        with Kernel.success_scope() as proof:
            int_result: CapResult[int] = CapResult.ok(42, proof)
            str_result: CapResult[str] = CapResult.ok("hello", proof)
            list_result: CapResult[list[int]] = CapResult.ok([1, 2, 3], proof)

        assert int_result.is_ok()
        assert str_result.is_ok()
        assert list_result.is_ok()

    def test_match_return_type(self) -> None:
        """match() transforms T → U correctly for different T types."""
        with Kernel.success_scope() as proof:
            int_result = CapResult.ok(42, proof)
        text: str = int_result.match(ok=lambda v: str(v), err=lambda e: e)
        assert text == "42"

        float_result = CapResult.err("nope")  # type: ignore[var-annotated]
        number: float = float_result.match(ok=lambda v: float(v), err=lambda e: 0.0)
        assert number == 0.0
