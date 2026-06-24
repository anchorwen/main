"""Adapters — bridge old→new UGR v3.1 resilience types.

UGR v3.1 §A07: Provides conversion functions between legacy patterns
(try/except, raw floats, threading) and new resilience types
(CapResult, TypedClock, SupervisedScheduler).

These adapters enable incremental migration — new code uses v3.1 types
directly; old code bridges through these functions until it can be
migrated.

Usage::

    from core.contracts.adapters import bridge_result

    # Legacy pattern → CapResult
    def legacy_function() -> CapResult[int]:
        try:
            value = old_api()
            return CapResult.ok(value, proof)
        except ValueError as e:
            return CapResult.err(str(e))

    # CapResult → legacy pattern
    result: CapResult[int] = new_api()
    value, error = bridge_result(result)
    if error:
        logger.error("Operation failed: %s", error)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

from core.contracts.cap_result import CapResult, Kernel
from core.runtime.typed_clock import Clock, Duration, MonotonicInstant, WallInstant

T = TypeVar("T")

# ═══════════════════════════════════════════════════════════════════════════
# CapResult bridges
# ═══════════════════════════════════════════════════════════════════════════


def bridge_result(result: CapResult[T]) -> tuple[T | None, str | None]:
    """Convert CapResult[T] to legacy (value, error) tuple.

    Returns:
        (value, None) if ok — value is the wrapped payload.
        (None, error_message) if err — error_message is the error string.
    """
    return result.match(
        ok=lambda v: (v, None),
        err=lambda e: (None, e),
    )


def bridge_legacy(
    fn: Callable[..., T],
    *args: Any,
    error_types: tuple[type[Exception], ...] = (Exception,),
    **kwargs: Any,
) -> CapResult[T]:
    """Call a legacy function and wrap the result in CapResult.

    Usage::

        result = bridge_legacy(mt5.positions_get, symbol="BTCUSDc",
                              error_types=(ValueError, OSError))

    Returns:
        CapResult.ok(value) on success.
        CapResult.err(str(exception)) on caught exception.
    """
    try:
        value = fn(*args, **kwargs)
        # Create a valid proof via Kernel
        with Kernel().success_scope() as proof:
            return CapResult.ok(value, proof)
    except error_types as e:
        return CapResult.err(str(e))


def bridge_result_or_raise(
    result: CapResult[T],
) -> T:  # type: ignore[type-var]
    """Unwrap CapResult or raise RuntimeError with the error message.

    For call sites that cannot yet be migrated to CapResult-aware code.
    Prefer pattern-matching on CapResult where possible.
    """

    def _raise(err: str) -> T:
        raise RuntimeError(f"CapResult error: {err}")

    return result.match(ok=lambda v: v, err=_raise)


# ═══════════════════════════════════════════════════════════════════════════
# TypedClock bridges
# ═══════════════════════════════════════════════════════════════════════════


def bridge_monotonic_now() -> MonotonicInstant:
    """Return current monotonic time as a MonotonicInstant.

    Replacement for ``time.monotonic()`` in legacy code.
    """
    return Clock.monotonic()


def bridge_wall_now() -> WallInstant:
    """Return current wall-clock time as a WallInstant.

    Replacement for ``time.time()`` in legacy code.
    """
    return Clock.wall()


def bridge_mono_to_float(t: MonotonicInstant) -> float:
    """Extract raw float from MonotonicInstant for legacy consumers.

    .. warning::
        This bypasses type safety.  Prefer using Duration arithmetic
        and passing MonotonicInstant directly to v3.1-aware code.
    """
    return t._raw


def bridge_duration_seconds(d: Duration) -> float:
    """Extract seconds from Duration for legacy consumers.

    .. warning::
        Prefer using Duration arithmetic in v3.1-aware code.
    """
    return d.total_seconds()


def bridge_elapsed_since(t: MonotonicInstant) -> Duration:
    """Return Duration since *t* using the TypedClock.

    Replacement for ``time.monotonic() - start`` in legacy code.
    """
    return t.elapsed()


# ═══════════════════════════════════════════════════════════════════════════
# Fault tolerance bridges
# ═══════════════════════════════════════════════════════════════════════════


def bridge_degrade(
    component: str,
    fn: Callable[..., T],
    *args: Any,
    fallback: T | None = None,
    error_types: tuple[type[Exception], ...] = (Exception,),
    **kwargs: Any,
) -> tuple[T | None, str | None]:
    """Call *fn* with DEGRADE semantics — log, return fallback on error.

    Replacement for::

        try:
            value = fn()
        except Exception:
            with fail_open_guard("ctx"):
                value = None

    Returns (value, None) on success, (fallback, error_str) on caught error.
    """
    try:
        return (fn(*args, **kwargs), None)
    except error_types as e:
        import logging

        logger = logging.getLogger(__name__)
        logger.warning(
            "bridge_degrade: %s failed — %s: %s",
            component,
            type(e).__name__,
            str(e)[:200],
        )
        return (fallback, f"{type(e).__name__}: {str(e)[:200]}")


def bridge_retry(
    component: str,
    fn: Callable[..., T],
    *args: Any,
    max_retries: int = 3,
    backoff_base: float = 0.5,
    **kwargs: Any,
) -> T:
    """Call *fn* with exponential-backoff retry, raise on exhaustion.

    Replacement for manual retry loops in legacy code.
    """
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last_error = e
            if attempt < max_retries - 1:
                delay = backoff_base * (2**attempt)
                time.sleep(delay)
    raise RuntimeError(f"bridge_retry: {component} exhausted {max_retries} retries") from last_error
