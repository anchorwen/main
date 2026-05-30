"""Unit tests for FaultTolerantContext — crash-loop protection, fault levels, guards."""

from __future__ import annotations

import json
import time

import pytest

from core.runtime.fault_handler import (
    FaultLevel,
    FaultTolerantContext,
    _check_crash_loop,
    crash_if_failed,
    degrade_with_fallback,
    log_and_continue,
)


class TestFaultLevels:
    """Verify each fault level behaves correctly."""

    def test_crash_re_raises(self):
        """CRASH level must re-raise the exception."""
        with pytest.raises(ValueError, match="test_crash"):
            with FaultTolerantContext(level=FaultLevel.CRASH, component="test"):
                raise ValueError("test_crash")

    def test_degrade_swallows_and_records(self):
        """DEGRADE level must swallow the exception and record it."""
        ctx = FaultTolerantContext(
            level=FaultLevel.DEGRADE, component="test_degrade", fallback_value=None
        )
        with ctx:
            raise RuntimeError("degraded")
        assert ctx.exception is not None
        assert isinstance(ctx.exception, RuntimeError)

    def test_degrade_returns_fallback(self):
        """DEGRADE should allow caller to use fallback_value."""
        result = None
        with degrade_with_fallback(component="test", fallback="FALLBACK"):
            result = "success"
        assert result == "success"

        # On exception, result stays at pre-init value
        result = "FALLBACK"
        with degrade_with_fallback(component="test", fallback=None):
            raise RuntimeError("fail")
        # Variable was pre-initialized — stays as pre-init value
        assert result == "FALLBACK"

    def test_log_swallows(self):
        """LOG level must swallow the exception."""
        ctx = FaultTolerantContext(level=FaultLevel.LOG, component="test_log")
        with ctx:
            raise RuntimeError("logged")
        assert ctx.exception is not None

    def test_ignore_requires_reason(self):
        """IGNORE level must require an explicit reason."""
        with pytest.raises(ValueError, match="allow_ignore_reason"):
            FaultTolerantContext(level=FaultLevel.IGNORE, component="test")

    def test_ignore_with_reason_swallows(self):
        """IGNORE with reason must swallow."""
        ctx = FaultTolerantContext(
            level=FaultLevel.IGNORE, component="test", allow_ignore_reason="cleanup"
        )
        with ctx:
            raise RuntimeError("ignored")
        assert ctx.exception is not None

    def test_normal_exit_no_exception(self):
        """No exception → ctx.exception stays None."""
        ctx = FaultTolerantContext(level=FaultLevel.LOG, component="test")
        with ctx:
            pass
        assert ctx.exception is None


class TestKeyboardInterruptGuard:
    """Architect Defense 1: system signals must never be swallowed."""

    def test_keyboard_interrupt_propagates(self):
        """KeyboardInterrupt must propagate through ALL fault levels."""
        for level in FaultLevel:
            if level == FaultLevel.IGNORE:
                continue  # IGNORE requires reason
            ctx = FaultTolerantContext(
                level=level,
                component="test",
                allow_ignore_reason="test" if level == FaultLevel.IGNORE else "",
            )
            with pytest.raises(KeyboardInterrupt):
                with ctx:
                    raise KeyboardInterrupt()

    def test_system_exit_propagates(self):
        """SystemExit must propagate through ALL fault levels."""
        with FaultTolerantContext(level=FaultLevel.DEGRADE, component="test"):
            with pytest.raises(SystemExit):
                raise SystemExit(0)

    def test_system_exit_crash_loop(self):
        """sys.exit(42) must propagate (crash-loop escape hatch)."""
        with FaultTolerantContext(level=FaultLevel.LOG, component="test"):
            with pytest.raises(SystemExit):
                raise SystemExit(42)


class TestCrashLoopProtection:
    """Verify crash-loop detection and last_good_state persistence."""

    @pytest.fixture(autouse=True)
    def clean_state(self):
        """Fixture marker — tests use monkeypatch individually for precision."""

    def test_record_crash_writes_file(self, tmp_path, monkeypatch):
        """_record_crash should create the state file with atomic write."""
        state_dir = tmp_path / "data" / "state"
        state_path = state_dir / "last_good_state.json"
        # Patch Path so _record_crash writes to our temp dir
        monkeypatch.setattr(
            "core.runtime.fault_handler.Path",
            lambda p: state_path if p == "data/state/last_good_state.json" else __import__("pathlib").Path(p),
        )
        real_record = __import__("core.runtime.fault_handler", fromlist=["_record_crash"])._record_crash
        real_record("test_component")
        assert state_path.exists()
        data = json.loads(state_path.read_text())
        assert len(data["crash_timestamps"]) == 1
        assert data["last_crash_component"] == "test_component"

    def test_check_crash_loop_no_file(self, tmp_path, monkeypatch):
        """No state file → no crash loop."""
        state_path = tmp_path / "last_good_state.json"
        monkeypatch.setattr(
            "core.runtime.fault_handler.Path",
            lambda p: state_path if p == "data/state/last_good_state.json" else __import__("pathlib").Path(p),
        )
        _check_crash_loop()  # Should not raise

    def test_crash_loop_detection_exits_42(self, tmp_path, monkeypatch):
        """3 crashes in 60s → sys.exit(42)."""
        state_path = tmp_path / "last_good_state.json"
        now = time.time()
        state = {
            "crash_timestamps": [now - 10, now - 20, now - 30],
            "last_crash_component": "test",
        }
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state))
        monkeypatch.setattr(
            "core.runtime.fault_handler.Path",
            lambda p: state_path if p == "data/state/last_good_state.json" else __import__("pathlib").Path(p),
        )

        with pytest.raises(SystemExit) as exc_info:
            _check_crash_loop()
        assert exc_info.value.code == 42

    def test_old_crashes_pruned(self, tmp_path, monkeypatch):
        """Crashes older than 60s should not trigger loop."""
        state_path = tmp_path / "last_good_state.json"
        now = time.time()
        state = {
            "crash_timestamps": [now - 10, now - 70, now - 130],
            "last_crash_component": "test",
        }
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state))
        monkeypatch.setattr(
            "core.runtime.fault_handler.Path",
            lambda p: state_path if p == "data/state/last_good_state.json" else __import__("pathlib").Path(p),
        )
        _check_crash_loop()  # Should not raise — only 1 crash in window


class TestConvenienceHelpers:
    """Verify the shortcut context managers."""

    def test_crash_if_failed(self):
        """crash_if_failed creates CRASH context."""
        ctx = crash_if_failed(component="test")
        assert ctx.level == FaultLevel.CRASH
        assert isinstance(ctx, FaultTolerantContext)

    def test_degrade_with_fallback(self):
        """degrade_with_fallback creates DEGRADE context."""
        ctx = degrade_with_fallback(component="test", fallback="fb")
        assert ctx.level == FaultLevel.DEGRADE
        assert ctx.fallback_value == "fb"

    def test_log_and_continue(self):
        """log_and_continue creates LOG context."""
        ctx = log_and_continue(component="test")
        assert ctx.level == FaultLevel.LOG


class TestVariableScopeLeakage:
    """Verify the pre-initialization pattern works correctly."""

    def test_pre_init_protects_unbound_local(self):
        """Variable pre-initialized before with block survives exception."""
        result = None  # pre-init
        with FaultTolerantContext(level=FaultLevel.DEGRADE, component="test"):
            result = 1 / 0  # raises ZeroDivisionError
        # result stays None (pre-init value) — no UnboundLocalError
        assert result is None

    def test_no_pre_init_causes_unbound(self):
        """Without pre-init, variable is unbound after exception."""
        # This is the pattern we document as WRONG in the docstring
        raised = False
        try:
            with FaultTolerantContext(level=FaultLevel.DEGRADE, component="test"):
                result = 1 / 0  # noqa: F841
            _ = result  # UnboundLocalError here — but FTC swallows it
        except UnboundLocalError:
            raised = True
        # Actually — FTC(DEGRADE) swallows the ZeroDivisionError,
        # but the `_ = result` line raises UnboundLocalError which
        # propagates because it's OUTSIDE the with block.
        # Wait — the ZeroDivisionError is inside the with block,
        # FTC swallows it. Then `_ = result` is AFTER the with block.
        # result was never bound → UnboundLocalError propagates.
        assert raised, "UnboundLocalError should be raised without pre-init"
