"""Tests for core.contracts.adapters — UGR v3.1 §A07 old→new type bridges.

Covers:
- bridge_result: CapResult → (value, error) tuple
- bridge_legacy: Callable → CapResult
- bridge_result_or_raise: CapResult → T (or raise)
- bridge_monotonic_now / bridge_wall_now
- bridge_mono_to_float / bridge_duration_seconds / bridge_elapsed_since
- bridge_degrade / bridge_retry
"""

from __future__ import annotations

import time
from typing import Any, cast

import pytest

from core.contracts.adapters import (
    bridge_degrade,
    bridge_duration_seconds,
    bridge_elapsed_since,
    bridge_legacy,
    bridge_mono_to_float,
    bridge_monotonic_now,
    bridge_result,
    bridge_result_or_raise,
    bridge_retry,
    bridge_wall_now,
)
from core.contracts.cap_result import CapResult, Kernel
from core.runtime.typed_clock import MonotonicInstant, WallInstant

# ═══════════════════════════════════════════════════════════════════════════
# CapResult bridges
# ═══════════════════════════════════════════════════════════════════════════


class TestBridgeResult:
    """Tests for bridge_result — CapResult → (value, error)."""

    def test_ok_returns_value_none(self) -> None:
        with Kernel().success_scope() as proof:
            result = CapResult.ok(42, proof)
        value, error = bridge_result(
            cast(Any, result)
        )  # TECH_DEBT-009: CapResult[T] 桥接泛型收窄 (A3)
        assert value == 42
        assert error is None

    def test_err_returns_none_error(self) -> None:
        result: CapResult[Any] = CapResult.err(
            "something broke"
        )  # TECH_DEBT-009: err 无值泛型无法推断 (A3)
        value, error = bridge_result(cast(Any, result))
        assert value is None
        assert error == "something broke"

    def test_ok_proof_must_be_valid(self) -> None:
        """bridge_result on ok with invalid proof still works (unwrapping)."""
        # Valid proof
        with Kernel().success_scope() as proof:
            result = CapResult.ok("hello", proof)
        value, error = bridge_result(
            cast(Any, result)
        )  # TECH_DEBT-009: CapResult[T] 桥接泛型收窄 (A3)
        assert value == "hello"
        assert error is None


class TestBridgeLegacy:
    """Tests for bridge_legacy — Callable → CapResult."""

    def test_successful_call(self) -> None:
        def good_fn(x: int) -> int:
            return x * 2

        result = bridge_legacy(good_fn, 21, error_types=(ValueError,))
        assert result.is_ok()
        value, error = bridge_result(
            cast(Any, result)
        )  # TECH_DEBT-009: CapResult[T] 桥接泛型收窄 (A3)
        assert value == 42
        assert error is None

    def test_caught_error(self) -> None:
        def bad_fn() -> int:
            raise ValueError("bad input")

        result = bridge_legacy(bad_fn, error_types=(ValueError, TypeError))
        assert not result.is_ok()
        _value, error = bridge_result(
            cast(Any, result)
        )  # TECH_DEBT-009: CapResult[T] 桥接泛型收窄 (A3)
        assert error is not None
        assert "bad input" in error

    def test_unhandled_error_propagates(self) -> None:
        def bad_fn() -> int:
            raise RuntimeError("unexpected")

        with pytest.raises(RuntimeError, match="unexpected"):
            bridge_legacy(bad_fn, error_types=(ValueError,))


class TestBridgeResultOrRaise:
    """Tests for bridge_result_or_raise."""

    def test_ok_returns_value(self) -> None:
        with Kernel().success_scope() as proof:
            result = CapResult.ok("data", proof)
        assert bridge_result_or_raise(result) == "data"

    def test_err_raises(self) -> None:
        result: CapResult[Any] = CapResult.err(
            "fatal error"
        )  # TECH_DEBT-009: err 无值泛型无法推断 (A3)
        with pytest.raises(RuntimeError, match="fatal error"):
            bridge_result_or_raise(result)


# ═══════════════════════════════════════════════════════════════════════════
# TypedClock bridges
# ═══════════════════════════════════════════════════════════════════════════


class TestTypedClockBridges:
    """Tests for TypedClock adapter functions."""

    def test_bridge_monotonic_now_returns_correct_type(self) -> None:
        t = bridge_monotonic_now()
        assert isinstance(t, MonotonicInstant)

    def test_bridge_wall_now_returns_correct_type(self) -> None:
        t = bridge_wall_now()
        assert isinstance(t, WallInstant)

    def test_bridge_mono_to_float_roundtrip(self) -> None:
        t = MonotonicInstant(123.456)
        f = bridge_mono_to_float(t)
        assert f == pytest.approx(123.456)

    def test_bridge_duration_seconds(self) -> None:
        from core.runtime.typed_clock import Duration

        d = Duration(5.0)
        assert bridge_duration_seconds(d) == pytest.approx(5.0)

    def test_bridge_elapsed_since(self) -> None:
        t = bridge_monotonic_now()
        time.sleep(0.05)
        d = bridge_elapsed_since(t)
        assert d.total_seconds() > 0


# ═══════════════════════════════════════════════════════════════════════════
# Fault tolerance bridges
# ═══════════════════════════════════════════════════════════════════════════


class TestBridgeDegrade:
    """Tests for bridge_degrade."""

    def test_success(self) -> None:
        value, error = bridge_degrade(
            "test", lambda x: x * 2, 5, fallback=0, error_types=(ValueError,)
        )
        assert value == 10
        assert error is None

    def test_error_returns_fallback(self) -> None:
        value, error = bridge_degrade(
            "test",
            lambda: (_ for _ in ()).throw(ValueError("boom")),
            fallback=-1,
            error_types=(ValueError,),
        )
        assert value == -1
        assert error is not None
        assert "ValueError" in error
        assert "boom" in error

    def test_unhandled_error_propagates(self) -> None:
        with pytest.raises(RuntimeError):
            bridge_degrade(
                "test",
                lambda: (_ for _ in ()).throw(RuntimeError("nope")),
                fallback=None,
                error_types=(ValueError,),
            )


class TestBridgeRetry:
    """Tests for bridge_retry."""

    def test_success_first_try(self) -> None:
        result = bridge_retry("test", lambda x: x + 1, 5, max_retries=3)
        assert result == 6

    def test_retry_then_succeed(self) -> None:
        counter = {"attempts": 0}

        def flaky() -> int:
            counter["attempts"] += 1
            if counter["attempts"] < 3:
                raise ValueError("not yet")
            return 42

        result = bridge_retry("test", flaky, max_retries=5, backoff_base=0.01)
        assert result == 42
        assert counter["attempts"] == 3

    def test_exhausted_retries_raises(self) -> None:
        def always_fails() -> int:
            raise ValueError("always")

        with pytest.raises(RuntimeError, match="exhausted"):
            bridge_retry("test", always_fails, max_retries=2, backoff_base=0.01)
