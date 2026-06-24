"""CapResult[T] — Rust-style Result type with scope-gated proof token.

UGR v3.1 §修正2: _SuccessProof lifecycle management.
Zero-Tolerance Resilience Architecture — the foundational type for all
capability-based error handling in the system.

Creation::

    with Kernel.success_scope() as proof:
        result = CapResult.ok(value, proof)   # requires valid proof

    result = CapResult.err("reason")           # error path (no proof needed)

Consumption::

    result.match(
        ok=lambda v: handle_success(v),
        err=lambda e: handle_error(e),
    )

Design constraints (Iron Law #1 — cannot be bypassed):
- _SuccessProof cannot be instantiated directly (__init__ raises TypeError)
- _SuccessProof._valid is cleared on scope exit (proof becomes useless)
- CapResult has NO .unwrap() method — must match() or check is_ok()
- CapResult._value and ._error are private — access only through match()
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from contextlib import contextmanager
from typing import Generic, TypeVar

T = TypeVar("T")
U = TypeVar("U")

# ── Sentinel for unset values ──────────────────────────────────────────────
_UNSET: str = "__CAPRESULT_UNSET__"


# ═══════════════════════════════════════════════════════════════════════════
# Exceptions
# ═══════════════════════════════════════════════════════════════════════════


class CapProofExpired(Exception):
    """A _SuccessProof was used after leaving its success_scope."""


class CapProofReused(Exception):
    """A _SuccessProof was reused after already being consumed."""


# ═══════════════════════════════════════════════════════════════════════════
# _SuccessProof — scope-gated proof token
# ═══════════════════════════════════════════════════════════════════════════


class _SuccessProof:
    """Scope-gated proof token for CapResult.ok().

    Lifecycle:
        1. Created by Kernel.success_scope() via _create() — bypasses __init__
        2. Valid within the ``with`` block
        3. Invalidated on scope exit (__exit__ calls _invalidate())
        4. Any use after invalidation raises CapProofExpired

    Cannot be instantiated directly, imported, or stored beyond scope.
    The leading underscore marks this as a private implementation detail.
    """

    __slots__ = ("_nonce", "_valid", "_thread_id")

    def __init__(self) -> None:
        raise TypeError(
            "_SuccessProof cannot be instantiated directly. "
            "Use Kernel.success_scope() to obtain a proof token."
        )

    @classmethod
    def _create(cls, nonce: str) -> _SuccessProof:
        """Internal factory — bypasses __init__ to create a valid proof."""
        proof = object.__new__(cls)
        proof._nonce = nonce  # type: ignore[attr-defined]
        proof._valid = True
        proof._thread_id = threading.current_thread().ident  # type: ignore[attr-defined]
        return proof

    def _invalidate(self) -> None:
        """Mark this proof as expired. Called by Kernel on scope exit."""
        self._valid = False

    def _verify_thread(self) -> None:
        """Debug guard: verify this proof is used from its creating thread."""
        if __debug__:
            caller_tid = threading.current_thread().ident
            if self._thread_id != caller_tid:  # type: ignore[attr-defined]
                raise CapProofExpired(
                    f"_SuccessProof used from wrong thread: "
                    f"created in {self._thread_id}, "  # type: ignore[attr-defined]
                    f"used in {caller_tid}"
                )

    @property
    def is_valid(self) -> bool:
        """Check if this proof is still within its scope."""
        return bool(self._valid)

    def __repr__(self) -> str:
        state = "valid" if self.is_valid else "expired"
        return f"_SuccessProof({state})"


# ═══════════════════════════════════════════════════════════════════════════
# CapResult[T]
# ═══════════════════════════════════════════════════════════════════════════


class CapResult(Generic[T]):
    """A Rust-style Result type for capability-based error handling.

    ``CapResult.ok(value, proof)`` requires a valid ``_SuccessProof``
    obtained from ``Kernel.success_scope()``.  This makes it **physically
    impossible** to silently create an "ok" result without proving you
    are inside a success scope — eliminating the ``except: pass`` class
    of bugs.

    ``CapResult.err(reason)`` requires no proof — failures can always
    be recorded.

    There is NO ``.unwrap()`` method.  Callers must explicitly handle
    both cases via ``.match()``, ``.is_ok()``, or ``.is_err()``.
    """

    __slots__ = ("_value", "_error", "_is_ok")

    def __init__(self) -> None:
        raise TypeError(
            "CapResult cannot be instantiated directly. "
            "Use CapResult.ok(value, proof) or CapResult.err(reason)."
        )

    # ── Factory methods ──────────────────────────────────────────────────

    @classmethod
    def ok(cls, value: T, proof: _SuccessProof) -> CapResult[T]:
        """Create a successful result.

        Requires a valid _SuccessProof from Kernel.success_scope().
        Raises CapProofExpired if the proof has left its scope.
        """
        if not proof.is_valid:
            raise CapProofExpired(
                "_SuccessProof has left its success_scope. "
                "CapResult.ok() must be called inside "
                "'with Kernel.success_scope() as proof:'"
            )
        proof._verify_thread()
        instance = object.__new__(cls)
        instance._value = value  # type: ignore[attr-defined]
        instance._error = None  # type: ignore[attr-defined]
        instance._is_ok = True  # type: ignore[attr-defined]
        return instance

    @classmethod
    def err(cls, error: str) -> CapResult[T]:
        """Create a failed result. No proof required — errors are always allowed."""
        instance = object.__new__(cls)
        instance._value = None  # type: ignore[attr-defined]
        instance._error = error  # type: ignore[attr-defined]
        instance._is_ok = False  # type: ignore[attr-defined]
        return instance

    # ── Predicates ───────────────────────────────────────────────────────

    def is_ok(self) -> bool:
        """True if this result represents success."""
        return self._is_ok  # type: ignore[attr-defined]

    def is_err(self) -> bool:
        """True if this result represents failure."""
        return not self._is_ok  # type: ignore[attr-defined]

    # ── Consumption ──────────────────────────────────────────────────────

    def match(self, ok: Callable[[T], U], err: Callable[[str], U]) -> U:
        """Pattern-match on the result, forcing both branches to be handled.

        Example::

            message = result.match(
                ok=lambda v: f"Trade opened: {v}",
                err=lambda e: f"Trade rejected: {e}",
            )
        """
        if self._is_ok:  # type: ignore[attr-defined]
            return ok(self._value)  # type: ignore[attr-defined]
        return err(self._error)  # type: ignore[attr-defined]

    # ── Transformation (requires proof — upholds the chain of trust) ─────

    def map(self, fn: Callable[[T], U], proof: _SuccessProof) -> CapResult[U]:
        """Transform the ok value. Keeps the proof chain intact."""
        if not proof.is_valid:
            raise CapProofExpired("_SuccessProof has left its success_scope")
        proof._verify_thread()
        if self._is_ok:  # type: ignore[attr-defined]
            return CapResult.ok(fn(self._value), proof)  # type: ignore[attr-defined]
        return CapResult.err(self._error)  # type: ignore[attr-defined]

    def flat_map(self, fn: Callable[[T], CapResult[U]], proof: _SuccessProof) -> CapResult[U]:
        """Chain a fallible operation. Keeps the proof chain intact."""
        if not proof.is_valid:
            raise CapProofExpired("_SuccessProof has left its success_scope")
        proof._verify_thread()
        if self._is_ok:  # type: ignore[attr-defined]
            return fn(self._value)  # type: ignore[attr-defined]
        return CapResult.err(self._error)  # type: ignore[attr-defined]

    # ── Dunder ───────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        if self._is_ok:  # type: ignore[attr-defined]
            return f"CapResult.ok({self._value!r})"  # type: ignore[attr-defined]
        return f"CapResult.err({self._error!r})"  # type: ignore[attr-defined]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CapResult):
            return NotImplemented
        return (
            self._is_ok == other._is_ok  # type: ignore[attr-defined]
            and self._value == other._value  # type: ignore[attr-defined]
            and self._error == other._error  # type: ignore[attr-defined]
        )

    def __hash__(self) -> int:
        return hash((self._is_ok, self._value, self._error))  # type: ignore[attr-defined]


# ═══════════════════════════════════════════════════════════════════════════
# Kernel — proof token factory
# ═══════════════════════════════════════════════════════════════════════════


class Kernel:
    """System kernel — the sole entry point for obtaining _SuccessProof tokens.

    Usage::

        with Kernel.success_scope() as proof:
            result = CapResult.ok(computed_value, proof)

        # proof is now expired — any use will raise CapProofExpired
    """

    @staticmethod
    @contextmanager
    def success_scope():
        """Create a success scope with a valid _SuccessProof.

        The proof is valid only within this context manager.
        On exit, _invalidate() is called and the proof becomes useless.
        """
        proof = _SuccessProof._create(secrets.token_hex(16))
        try:
            yield proof
        finally:
            proof._invalidate()

    @staticmethod
    def try_operation(
        operation: Callable[[], T],
        error_context: str = "Operation failed",
    ) -> CapResult[T]:
        """Execute an operation inside a success scope.

        If the operation raises, returns CapResult.err().
        If it succeeds, returns CapResult.ok().

        This is the bridge between legacy try/except code and CapResult.
        """
        with Kernel.success_scope() as proof:
            try:
                value = operation()
                return CapResult.ok(value, proof)
            except Exception as exc:  # noqa: BLE001 — intentional: convert any error to CapResult.err
                return CapResult.err(f"{error_context}: {exc}")
