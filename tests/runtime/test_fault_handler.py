"""Tests for core.runtime.fault_handler — UGR v3.1 §A07 enhancements.

Covers:
- SystemError never swallowed by FaultTolerantContext
- fail_open_guard emits DeprecationWarning
- KeyboardInterrupt + SystemExit still propagate
"""

from __future__ import annotations

import pytest

from core.runtime.fault_handler import (
    FaultLevel,
    FaultTolerantContext,
    fail_open_guard,
)


class TestSystemErrorGuard:
    """UGR v3.1 §A07: SystemError is never swallowed."""

    def test_system_error_propagates(self) -> None:
        """SystemError must propagate — it signals CPython internal errors."""
        propagated = False
        try:
            with FaultTolerantContext(
                level=FaultLevel.DEGRADE,
                component="test_system_error",
                fallback_value=None,
            ):
                raise SystemError("simulated interpreter error")
        except SystemError:
            propagated = True
        assert propagated, "SystemError was swallowed — it MUST propagate"

    def test_keyboard_interrupt_still_propagates(self) -> None:
        """KeyboardInterrupt still propagates (regression check)."""
        propagated = False
        try:
            with FaultTolerantContext(
                level=FaultLevel.DEGRADE,
                component="test_keyboard",
                fallback_value=None,
            ):
                raise KeyboardInterrupt()
        except KeyboardInterrupt:
            propagated = True
        assert propagated

    def test_system_exit_still_propagates(self) -> None:
        """SystemExit still propagates (regression check)."""
        propagated = False
        try:
            with FaultTolerantContext(
                level=FaultLevel.DEGRADE,
                component="test_system_exit",
                fallback_value=None,
            ):
                raise SystemExit(0)
        except SystemExit:
            propagated = True
        assert propagated

    def test_normal_exception_still_degraded(self) -> None:
        """Normal exceptions are still caught in DEGRADE mode."""
        with FaultTolerantContext(
            level=FaultLevel.DEGRADE,
            component="test_normal",
            fallback_value="fallback",
        ) as ctx:
            raise ValueError("expected error")
        assert ctx.exception is not None
        assert isinstance(ctx.exception, ValueError)


class TestFailOpenGuardDeprecated:
    """UGR v3.1 §A07: fail_open_guard emits DeprecationWarning."""

    def test_emits_deprecation_warning(self) -> None:
        with pytest.warns(DeprecationWarning, match="DEPRECATED"):
            with fail_open_guard("test_deprecation"):
                pass

    def test_still_functions_as_degrade(self) -> None:
        """Despite deprecation, fail_open_guard still works."""
        with pytest.warns(DeprecationWarning):
            with fail_open_guard("test_still_works") as ctx:
                raise ValueError("caught")
        assert ctx.exception is not None

    def test_warning_message_includes_component(self) -> None:
        with pytest.warns(DeprecationWarning, match="my_component_name"):
            with fail_open_guard("my_component_name"):
                pass
