"""Single-threaded MT5 engine — all MT5 C++ calls execute on one dedicated thread.

Solves T1-C1 (per-call daemon threads), T1-C2 (repeated init/shutdown),
and T1-C3 (non-thread-safe mixed access).

Usage::

    worker = MT5Worker()
    worker.start(terminal_path=r"C:\\...")
    tick = worker.symbol_info_tick("XAUUSDc")
    pos = worker.positions_get(symbol="XAUUSDc")
    worker.stop()
"""

from __future__ import annotations

import queue
import random
import threading
import time
from concurrent.futures import Future
from typing import Any

from core.protocol.services.resilience import CircuitBreaker
from core.runtime.fault_handler import fail_open_guard

# ── Module-level singleton ──────────────────────────────────────────

_mt5_worker: MT5Worker | None = None


def get_mt5_worker() -> MT5Worker | None:
    """Return the process-wide MT5Worker singleton, or None if not started."""
    return _mt5_worker


def set_mt5_worker(worker: MT5Worker | None) -> None:
    """Set the process-wide MT5Worker singleton."""
    global _mt5_worker
    _mt5_worker = worker


# ── Worker ──────────────────────────────────────────────────────────


class MT5Worker:
    """Single-threaded MT5 engine.

    All MT5 C++ calls are serialised through one dedicated daemon thread.
    Callers from any thread submit commands and block on a Future with a
    configurable timeout.

    Constants (TIMEFRAME_*, TRADE_RETCODE_*, etc.) are pure integers with
    no thread-affinity requirement — import them directly from MetaTrader5.
    """

    # Seconds the worker loop waits on an empty queue before checking _running.
    _QUEUE_POLL_INTERVAL = 1.0

    def __init__(self, alert_hub: Any = None, *, symbol: str = "XAUUSDc") -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=1000)
        self._running = False
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._mt5: Any = None
        self._mt5_init_kwargs: dict[str, Any] = {}
        self._default_symbol: str = symbol  # FIX-20260601-031: no hardcoded symbol

        # Per-command execution tracking (for hung-MT5 detection)
        self._command_in_flight: str | None = None
        self._last_command_start: float = 0.0
        self._stuck_since: float | None = None  # set when first TimeoutError detected

        # Exponential backoff for reconnect
        self._reconnect_attempt: int = 0
        self._reconnect_max_attempts: int = 5
        self._reconnect_backoff: list[float] = [1.0, 2.0, 4.0, 8.0, 30.0]

        # Alert hub for circuit breaker cross-propagation
        self._alert_hub = alert_hub

        # Circuit breaker: 3 consecutive failures → open for 60s
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=3,
            cooldown_seconds=60.0,
            half_open_max_calls=1,
        )

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self, terminal_path: str | None = None) -> bool:
        """Start the worker thread and initialise MT5.  Returns ``True`` on success."""
        if self._running:
            return self._ready.is_set()

        self._mt5_init_kwargs = {}
        if terminal_path:
            self._mt5_init_kwargs["path"] = terminal_path

        self._running = True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="MT5Worker")
        self._thread.start()
        self._ready.wait(timeout=30.0)
        return self._ready.is_set()

    def stop(self) -> None:
        """Shutdown MT5 and join the worker thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            # Send poison pill and wait
            try:  # noqa: SIM105
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            self._thread.join(timeout=10.0)
        self._mt5 = None
        self._thread = None

    def reconnect(self, timeout: float = 15.0) -> bool:
        """Re-initialise MT5 on the worker thread with exponential backoff.

        Safe to call when the worker is running — ``mt5.initialize()``
        supports being called multiple times from the same thread.

        Backoff sequence: 1s → 2s → 4s → 8s → 30s. Max 5 retries.
        Resets backoff counter on success.
        """
        if not self._running or self._thread is None:
            return False

        while self._reconnect_attempt < self._reconnect_max_attempts:
            delay = self._reconnect_backoff[
                min(self._reconnect_attempt, len(self._reconnect_backoff) - 1)
            ]
            time.sleep(delay + random.uniform(0, 1.0))  # jitter: break rate-limit sync
            ok = self._submit("_reconnect", self._mt5_init_kwargs, timeout=timeout)
            if ok:
                self._reconnect_attempt = 0
                return True
            self._reconnect_attempt += 1

        return False

    # ── Public data API ──────────────────────────────────────────

    def symbol_info_tick(self, symbol: str, timeout: float = 5.0) -> Any:
        """Return the current tick for *symbol* (MT5 ``symbol_info_tick`` return)."""
        return self._submit("symbol_info_tick", symbol, timeout=timeout)

    def symbol_info(self, symbol: str, timeout: float = 5.0) -> Any:
        """Return symbol metadata (MT5 ``symbol_info`` return)."""
        return self._submit("symbol_info", symbol, timeout=timeout)

    def symbol_select(self, symbol: str, enable: bool, timeout: float = 5.0) -> bool:
        """Enable/disable *symbol* in MarketWatch."""
        return self._submit("symbol_select", symbol, enable, timeout=timeout)

    def copy_rates_from_pos(
        self,
        symbol: str,
        timeframe: int,
        start_pos: int,
        count: int,
        timeout: float = 10.0,
    ) -> Any:
        """Copy *count* bars from *start_pos* (MT5 ``copy_rates_from_pos`` return)."""
        return self._submit(
            "copy_rates_from_pos", symbol, timeframe, start_pos, count, timeout=timeout
        )

    def copy_ticks_from(
        self,
        symbol: str,
        from_date: float,
        count: int,
        flags: int,
        timeout: float = 15.0,
    ) -> Any:
        """Copy *count* ticks from *from_date* (MT5 ``copy_ticks_from`` return)."""
        return self._submit("copy_ticks_from", symbol, from_date, count, flags, timeout=timeout)

    def positions_get(
        self,
        *,
        symbol: str | None = None,
        ticket: int | None = None,
        timeout: float = 5.0,
    ) -> list[Any]:
        """Return open positions (MT5 ``positions_get`` return as list).

        Returns an empty list on None / error — never returns None.
        """
        kwargs: dict[str, Any] = {}
        if symbol is not None:
            kwargs["symbol"] = symbol
        if ticket is not None:
            kwargs["ticket"] = ticket
        result = self._submit("positions_get", timeout=timeout, _kwargs=kwargs)
        return result if result is not None else []

    def account_info(self, timeout: float = 5.0) -> Any:
        """Return account info (MT5 ``account_info`` return)."""
        return self._submit("account_info", timeout=timeout)

    def history_deals_get(
        self,
        *,
        position: int | None = None,
        ticket: int | None = None,
        timeout: float = 5.0,
    ) -> list[Any]:
        """Return history deals (MT5 ``history_deals_get`` return as list).

        Returns an empty list on None / error.
        """
        kwargs: dict[str, Any] = {}
        if position is not None:
            kwargs["position"] = position
        if ticket is not None:
            kwargs["ticket"] = ticket
        result = self._submit("history_deals_get", timeout=timeout, _kwargs=kwargs)
        return result if result is not None else []

    # ── Public action API ────────────────────────────────────────

    def order_send(self, request: dict[str, Any], timeout: float = 10.0) -> Any:
        """Send an order to MT5 (MT5 ``order_send`` return)."""
        return self._submit("order_send", request, timeout=timeout)

    def is_stuck(self, threshold: float = 30.0) -> bool:
        """Return True if the worker appears hung (command in-flight > *threshold* seconds)."""
        if self._command_in_flight is None:
            return False
        return (time.monotonic() - self._last_command_start) > threshold

    @property
    def command_in_flight(self) -> str | None:
        """The command currently executing on the worker thread, or None if idle."""
        return self._command_in_flight

    # ── Internals ────────────────────────────────────────────────

    def _submit(
        self,
        command: str,
        *args: Any,
        timeout: float = 30.0,
        _kwargs: dict[str, Any] | None = None,
    ) -> Any:
        """Enqueue a command and block on its Future."""
        if not self._running:
            raise RuntimeError(f"MT5Worker._submit({command}): worker not running")

        # Circuit breaker: fast-fail if MT5 connectivity is known-broken
        if command not in ("_reconnect",) and not self.circuit_breaker.allow_request():
            raise TimeoutError(
                f"MT5Worker.{command} rejected: circuit OPEN "
                f"(trips={self.circuit_breaker.get_status()['total_trips']})"
            )

        # Fast-fail: if the worker is already stuck on a hung C++ call,
        # there is no point queuing.  The caller should use cached/stub data.
        if self.is_stuck(threshold=max(timeout, 30.0)):
            raise TimeoutError(
                f"MT5Worker.{command} rejected: worker stuck on "
                f"'{self._command_in_flight}' for "
                f"{time.monotonic() - self._last_command_start:.0f}s"
            )

        future: Future = Future()
        try:
            self._queue.put_nowait((future, command, args, _kwargs or {}))
        except queue.Full as err:
            raise RuntimeError(
                f"MT5Worker command queue full (1000) — " f"rejected {command} to prevent OOM"
            ) from err
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            # Mark worker as stuck so future callers fast-fail
            if self._stuck_since is None:
                self._stuck_since = time.monotonic()
            raise TimeoutError(f"MT5Worker.{command} timed out after {timeout}s") from None

    # ── Worker loop (runs on the dedicated MT5 thread) ───────────

    def _run(self) -> None:
        """Main worker loop — initialises MT5, processes commands, shuts down."""
        import MetaTrader5 as mt5

        # ── Initialise ──
        if not mt5.initialize(**self._mt5_init_kwargs):
            # Signal failure but keep running so callers get RuntimeError
            self._ready.set()
            self._mt5 = None
        else:
            self._mt5 = mt5
            self._ready.set()

        # ── Command dispatch table ──
        _dispatch: dict[str, Any] = {
            "symbol_info_tick": lambda a, kw: self._mt5.symbol_info_tick(*a, **kw),
            "symbol_info": lambda a, kw: self._mt5.symbol_info(*a, **kw),
            "symbol_select": lambda a, kw: self._mt5.symbol_select(*a, **kw),
            "copy_rates_from_pos": lambda a, kw: self._mt5.copy_rates_from_pos(*a, **kw),
            "copy_ticks_from": lambda a, kw: self._mt5.copy_ticks_from(*a, **kw),
            "positions_get": lambda a, kw: self._mt5.positions_get(**kw),
            "account_info": lambda a, kw: self._mt5.account_info(),
            "history_deals_get": lambda a, kw: self._mt5.history_deals_get(**kw),
            "order_send": lambda a, kw: self._mt5.order_send(*a, **kw),
            "_reconnect": lambda a, kw: self._mt5_initialize(a[0]),
        }

        while self._running:
            try:
                item = self._queue.get(timeout=self._QUEUE_POLL_INTERVAL)
            except queue.Empty:
                continue

            if item is None:  # poison pill
                break

            future, command, args, kwargs = item
            try:
                if self._mt5 is None:
                    future.set_exception(
                        RuntimeError(
                            f"MT5 not initialised (command={command}). "
                            "Call start() and check its return value."
                        )
                    )
                    continue

                handler = _dispatch.get(command)
                if handler is None:
                    future.set_exception(ValueError(f"Unknown MT5Worker command: {command}"))
                    continue

                # Track command execution for hung-MT5 detection
                self._command_in_flight = command
                self._last_command_start = time.monotonic()
                result = handler(args, kwargs)
                future.set_result(result)
                # Circuit breaker: only record business commands, not housekeeping
                if command not in ("_reconnect",):
                    self.circuit_breaker.record_success()
            except Exception as exc:  # BLE001:REVIEWED
                future.set_exception(exc)
                if command not in ("_reconnect",):
                    was_open = (
                        self.circuit_breaker.state.value == "open"
                        if hasattr(self.circuit_breaker, "state")
                        else False
                    )
                    self.circuit_breaker.record_failure()
                    # Detect CB transition to OPEN → alert hub
                    if not was_open and self._alert_hub is not None:
                        with fail_open_guard("MT5Worker:CircuitBreakerAlert"):
                            is_open = (
                                self.circuit_breaker.state.value == "open"
                                if hasattr(self.circuit_breaker, "state")
                                else False
                            )
                            if is_open:
                                self._alert_hub.send_critical(
                                    "mt5_circuit_open",
                                    {
                                        "command": command,
                                        "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                                        "total_trips": self.circuit_breaker.get_status().get(
                                            "total_trips", 0
                                        ),
                                    },
                                )
                            pass  # BLE001 — migrated from blind pass
            finally:
                self._command_in_flight = None
                self._stuck_since = None  # clear stuck marker on successful completion

        # ── Shutdown ──
        if self._mt5 is not None:
            self._mt5.shutdown()
            self._mt5 = None

    def _mt5_initialize(self, kwargs: dict[str, Any]) -> bool:
        """Re-initialise MT5 from within the worker thread.

        Automatically re-selects XAUUSDc after successful re-init so that
        tick data and symbol info are immediately available to all callers.
        """
        import MetaTrader5 as mt5

        if self._mt5 is not None:
            self._mt5.shutdown()
        ok = mt5.initialize(**kwargs)
        if ok:
            self._mt5 = mt5
            try:  # noqa: SIM105
                self._mt5.symbol_select(self._default_symbol, True)
            except Exception:  # BLE001:REVIEWED
                pass
        else:
            self._mt5 = None
        return ok
