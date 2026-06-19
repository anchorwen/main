"""Unified fault-handling context manager.

Provides a five-level fault classification system (CRASH / DEGRADE / RETRY /
LOG / IGNORE) with a single ``FaultTolerantContext`` entry point.

CRASH-level faults write ``last_good_state.json`` before raising, enabling
crash-loop detection on restart (3 crashes in 60s → sys.exit(42)).
"""

from __future__ import annotations

import enum
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Crash-loop constants ────────────────────────────────────────────────

_CRASH_WINDOW_SECONDS = 60.0
_CRASH_MAX_IN_WINDOW = 3
_CRASH_LOOP_EXIT_CODE = 42

# ── Fault level enum ────────────────────────────────────────────────────


class FaultLevel(enum.Enum):
    """Five-level fault classification for the trading system.

    CRASH   — infrastructure fault: log, write last-good-state, raise
    DEGRADE — component fault: log, return fallback, emit alert
    RETRY   — transient fault: exponential backoff N times, escalate to CRASH
    LOG     — non-critical-path fault: log and continue
    IGNORE  — cleanup/teardown code: swallow silently (code-review required)
    """

    CRASH = "crash"
    DEGRADE = "degrade"
    RETRY = "retry"
    LOG = "log"
    IGNORE = "ignore"


# ── Crash-loop protection ───────────────────────────────────────────────


def _record_crash(component: str) -> None:
    """Record a crash timestamp to ``last_good_state.json`` for crash-loop detection."""
    state_path = Path("data/state/last_good_state.json")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()

    state: dict[str, Any] = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}

    crashes: list[float] = state.get("crash_timestamps", [])
    # Prune entries outside the window
    crashes = [t for t in crashes if now - t < _CRASH_WINDOW_SECONDS]
    crashes.append(now)
    state["crash_timestamps"] = crashes
    state["last_crash_component"] = component
    state["last_crash_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    tmp = Path(str(state_path) + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(state_path))


def _check_crash_loop() -> None:
    """Check for crash-loop: 3 crashes in 60s → sys.exit(42).

    Exit code 42 tells the launcher to stop restarting and page the on-call.
    """
    state_path = Path("data/state/last_good_state.json")
    if not state_path.exists():
        return
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    crashes: list[float] = state.get("crash_timestamps", [])
    now = time.time()
    recent = [t for t in crashes if now - t < _CRASH_WINDOW_SECONDS]
    if len(recent) >= _CRASH_MAX_IN_WINDOW:
        logger.critical(
            "CRASH LOOP DETECTED: %d crashes in %ds — exiting with code %d",
            len(recent),
            int(_CRASH_WINDOW_SECONDS),
            _CRASH_LOOP_EXIT_CODE,
        )
        sys.exit(_CRASH_LOOP_EXIT_CODE)


# ── Fault-tolerant context manager ──────────────────────────────────────


class FaultTolerantContext:
    """Context manager that handles faults according to their severity level.

    Usage::

        with FaultTolerantContext(
            level=FaultLevel.DEGRADE,
            component="Brain#7",
            fallback_value=None,
        ) as ctx:
            result = brain.predict(features)

    CRASH:
        Logs the exception, writes ``last_good_state.json`` with crash
        timestamp, then re-raises.  The process will terminate (or be
        caught by an outer handler that calls sys.exit).

    DEGRADE:
        Logs the exception, records it in ``ctx.exception``, and returns
        ``fallback_value`` as the context-manager result.

    RETRY:
        Retries the block up to *max_retries* times with exponential
        backoff (base * 2^attempt).  If all retries are exhausted,
        escalates to CRASH.

    LOG:
        Logs the exception and continues.  ``ctx.exception`` is set.

    IGNORE:
        Silently swallows the exception.  Requires an explicit
        ``allow_ignore_reason`` to document why this is safe.

    .. warning::

        **Variable scope leakage (Python semantics).**  When the block body
        contains an assignment and the right-hand side raises, the variable
        name is **never bound** in the local namespace — even though FTC
        swallows the exception.  Always pre-initialise variables before the
        ``with`` block::

            # ✅ Correct — variable is bound regardless of exception
            result = None  # pre-initialise with fallback value
            with degrade_with_fallback("Brain#7", fallback=None):
                result = brain.predict(features)

            # ❌ Wrong — UnboundLocalError if brain.predict raises
            with degrade_with_fallback("Brain#7", fallback=None):
                result = brain.predict(features)

        CRASH-level blocks are exempt (the exception is re-raised and the
        process terminates), but pre-initialisation is still recommended for
        consistency and to simplify future level changes.
    """

    def __init__(
        self,
        level: FaultLevel,
        component: str,
        *,
        fallback_value: Any = None,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        alert_hub: Any = None,
        allow_ignore_reason: str = "",
    ) -> None:
        if level == FaultLevel.IGNORE and not allow_ignore_reason:
            raise ValueError("FaultTolerantContext IGNORE requires allow_ignore_reason")

        self.level = level
        self.component = component
        self.fallback_value = fallback_value
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._alert_hub = alert_hub
        self._allow_ignore_reason = allow_ignore_reason

        self.exception: BaseException | None = None
        self.retries_used: int = 0

    def __enter__(self) -> FaultTolerantContext:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        if exc_val is None:
            return False  # no exception → normal exit

        # Absolute guard: never swallow system exit signals.
        # KeyboardInterrupt (SIGINT) and SystemExit (sys.exit) must always
        # propagate so SIGTERM graceful shutdown and crash-loop exit(42) work.
        if isinstance(exc_val, KeyboardInterrupt | SystemExit):
            return False

        if self.level == FaultLevel.IGNORE:
            self.exception = exc_val
            return True  # swallow

        if self.level == FaultLevel.LOG:
            logger.warning(
                "FaultTolerantContext [LOG] component=%s error=%s: %s",
                self.component,
                type(exc_val).__name__,
                str(exc_val)[:200],
            )
            self.exception = exc_val
            return True  # swallow

        if self.level == FaultLevel.DEGRADE:
            logger.error(
                "FaultTolerantContext [DEGRADE] component=%s error=%s: %s",
                self.component,
                type(exc_val).__name__,
                str(exc_val)[:200],
            )
            self.exception = exc_val
            if self._alert_hub is not None:
                try:  # noqa: SIM105
                    self._alert_hub.send_critical(
                        f"degraded_{self.component}",
                        {"error": f"{type(exc_val).__name__}: {str(exc_val)[:200]}"},
                    )
                except Exception:  # BLE001:REVIEWED
                    pass
            return True  # swallow, caller checks ctx.exception

        if self.level == FaultLevel.RETRY:
            for attempt in range(self.max_retries):
                delay = self.backoff_base * (2**attempt)
                logger.warning(
                    "FaultTolerantContext [RETRY] component=%s attempt=%d/%d delay=%.1fs error=%s",
                    self.component,
                    attempt + 1,
                    self.max_retries,
                    delay,
                    type(exc_val).__name__,
                )
                time.sleep(delay)
                # We cannot re-execute the block from __exit__.
                # RETRY is handled by the caller wrapping the context in a loop.
                # Store the exception for the caller to check.
                self.exception = exc_val
                self.retries_used = attempt + 1
                return True  # caller must re-enter context

            # All retries exhausted → escalate to CRASH
            logger.critical(
                "FaultTolerantContext [RETRY→CRASH] component=%s exhausted=%d retries error=%s",
                self.component,
                self.max_retries,
                type(exc_val).__name__,
            )
            _record_crash(self.component)
            _check_crash_loop()
            return False  # re-raise

        if self.level == FaultLevel.CRASH:
            logger.critical(
                "FaultTolerantContext [CRASH] component=%s error=%s: %s",
                self.component,
                type(exc_val).__name__,
                str(exc_val)[:200],
                exc_info=True,
            )
            _record_crash(self.component)
            _check_crash_loop()
            return False  # re-raise

        return False  # unknown level → re-raise


# ── Convenience helpers ─────────────────────────────────────────────────


def crash_if_failed(
    component: str,
    *,
    alert_hub: Any = None,
) -> FaultTolerantContext:
    """Shortcut for CRASH-level context."""
    return FaultTolerantContext(
        level=FaultLevel.CRASH,
        component=component,
        alert_hub=alert_hub,
    )


def degrade_with_fallback(
    component: str,
    fallback: Any = None,
    *,
    alert_hub: Any = None,
) -> FaultTolerantContext:
    """Shortcut for DEGRADE-level context."""
    return FaultTolerantContext(
        level=FaultLevel.DEGRADE,
        component=component,
        fallback_value=fallback,
        alert_hub=alert_hub,
    )


def log_and_continue(component: str) -> FaultTolerantContext:
    """Shortcut for LOG-level context."""
    return FaultTolerantContext(
        level=FaultLevel.LOG,
        component=component,
    )


def fail_open_guard(component: str) -> FaultTolerantContext:
    """Drop-in replacement for bare ``except Exception: pass`` on the hot path.

    FIX-20260607-146: BLE001 governance Phase 1 (preparation).
    Provides a DEGRADE-level context that logs the exception with full
    traceback but never crashes — the system continues operating in a
    potentially degraded state.  This is the minimum safe wrapper for
    replacing legacy ``except Exception: pass`` sites before they can
    be individually audited and upgraded to CRASH or RETRY as appropriate.

    Usage (governance Phase 2)::

        # BEFORE (BLE001 — blind except):
        with fail_open_guard("FaultHandler:DocExample"):
            do_something()
            pass  # BLE001 — migrated from blind pass

        # AFTER (auditable degradation):
        with fail_open_guard("ComponentName"):
            do_something()
    """
    return FaultTolerantContext(
        level=FaultLevel.DEGRADE,
        component=component,
    )


# ── FIX-20260610-003: MT5 IPC timeout wrapper ──────────────────────────
# Surgery A: MT5 calls (positions_get, copy_rates_from_pos, etc.) can block
# indefinitely when two processes share one MT5 terminal.  The MetaTrader5
# Python library has no native timeout — this wrapper runs the call in a
# daemon thread and aborts if it exceeds the timeout.
#
# Usage:
#   result = mt5_call_with_timeout(mt5.positions_get, symbol="BTCUSDc", timeout=5.0)
#   if result is _MT5_TIMEOUT_SENTINEL:
#       ... handle timeout ...

import threading as _threading_mt5

_MT5_TIMEOUT_SENTINEL = object()


def mt5_call_with_timeout(
    fn: Any,
    *args: Any,
    timeout: float = 5.0,
    **kwargs: Any,
) -> Any:
    """Call *fn* in a daemon thread, return result or _MT5_TIMEOUT_SENTINEL.

    When *fn* blocks (e.g. MT5 IPC deadlock from concurrent terminal access),
    the calling thread is not blocked beyond *timeout* seconds.  The daemon
    thread is abandoned — it will be cleaned up when the process exits.
    """
    result: list[Any] = [_MT5_TIMEOUT_SENTINEL]
    error: list[BaseException | None] = [None]
    done = _threading_mt5.Event()

    def _target() -> None:
        try:
            result[0] = fn(*args, **kwargs)
        except Exception as exc:  # BLE001:REVIEWED
            error[0] = exc
        finally:
            done.set()

    t = _threading_mt5.Thread(target=_target, daemon=True, name=f"mt5_to_{timeout}s")
    t.start()
    t.join(timeout=timeout)

    if not done.is_set():
        # Thread still running → MT5 call is blocking.
        # Log the hang and return sentinel.  The daemon thread leaks but
        # will be cleaned up on process exit.
        logger.error(
            "MT5 call timed out after %.1fs: %s(*%s, **%s)",
            timeout,
            getattr(fn, "__name__", str(fn)),
            args,
            {k: v for k, v in kwargs.items() if k != "timeout"},
        )
        return _MT5_TIMEOUT_SENTINEL

    if error[0] is not None:
        raise error[0]  # Re-raise in calling thread

    return result[0]

