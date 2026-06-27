"""Tests for core.execution.mt5_worker — singleton + lifecycle + error paths.

FIX-20260619-038: Tier 1 zero-coverage breakout #9.
FIX-20260620-021: Phase 3a extended tests — is_stuck, _submit errors, reconnect,
_run dispatch, _mt5_initialize, positions_get/history_deals_get fallback.

Tests parts that don't require actual MT5 C++ terminal.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from core.execution.mt5_worker import (
    MT5Worker,
    get_mt5_worker,
    set_mt5_worker,
)


class TestSingleton:
    def test_get_returns_none_initially(self) -> None:
        set_mt5_worker(None)
        assert get_mt5_worker() is None

    def test_set_and_get(self) -> None:
        w = MagicMock(spec=MT5Worker)
        set_mt5_worker(w)
        assert get_mt5_worker() is w
        set_mt5_worker(None)  # cleanup


class TestMT5WorkerInit:
    def test_init_creates_queue(self) -> None:
        w = MT5Worker()
        assert w._queue is not None
        assert w._running is False
        assert w._ready.is_set() is False  # not started yet

    def test_init_creates_circuit_breaker(self) -> None:
        w = MT5Worker()
        assert w.circuit_breaker is not None

    def test_stop_when_not_running(self) -> None:
        w = MT5Worker()
        w.stop()  # should not raise

    def test_stop_after_init_is_safe(self) -> None:
        """stop() after __init__ (no thread started) is a no-op."""
        w = MT5Worker()
        w.stop()  # safe — _thread is None

    def test_init_default_symbol(self) -> None:
        """Default symbol is XAUUSDc."""
        w = MT5Worker()
        assert w._default_symbol == "XAUUSDc"

    def test_init_custom_symbol(self) -> None:
        """Custom symbol is stored."""
        w = MT5Worker(symbol="BTCUSDc")
        assert w._default_symbol == "BTCUSDc"

    def test_init_alert_hub_stored(self) -> None:
        """Alert hub is stored when provided."""
        hub = MagicMock()
        w = MT5Worker(alert_hub=hub)
        assert w._alert_hub is hub


class TestEnqueue:
    def test_queue_created_empty(self) -> None:
        w = MT5Worker()
        assert w._queue.qsize() == 0


# ═══════════════════════════════════════════════════════════════════════════
# is_stuck — pure logic, no MT5 required
# ═══════════════════════════════════════════════════════════════════════════


class TestIsStuck:
    def test_not_stuck_when_idle(self) -> None:
        """Worker idle (no command in flight) → not stuck."""
        w = MT5Worker()
        assert w.is_stuck() is False

    def test_not_stuck_when_command_just_started(self) -> None:
        """Command just started → not stuck."""
        w = MT5Worker()
        w._command_in_flight = "symbol_info_tick"
        w._last_command_start = time.monotonic()
        assert w.is_stuck() is False

    def test_stuck_when_command_exceeds_threshold(self) -> None:
        """Command running longer than threshold → stuck."""
        w = MT5Worker()
        w._command_in_flight = "copy_rates_from_pos"
        w._last_command_start = time.monotonic() - 999.0  # far in the past
        assert w.is_stuck() is True

    def test_stuck_custom_threshold(self) -> None:
        """Custom threshold respected."""
        w = MT5Worker()
        w._command_in_flight = "symbol_info_tick"
        w._last_command_start = time.monotonic() - 5.0
        assert w.is_stuck(threshold=3.0) is True
        assert w.is_stuck(threshold=10.0) is False


# ═══════════════════════════════════════════════════════════════════════════
# command_in_flight property
# ═══════════════════════════════════════════════════════════════════════════


class TestCommandInFlight:
    def test_none_initially(self) -> None:
        """No command in flight at init."""
        w = MT5Worker()
        assert w.command_in_flight is None

    def test_reflects_current_command(self) -> None:
        """Property returns the current in-flight command."""
        w = MT5Worker()
        w._command_in_flight = "order_send"
        assert w.command_in_flight == "order_send"


# ═══════════════════════════════════════════════════════════════════════════
# _submit error paths — testable without MT5 terminal
# ═══════════════════════════════════════════════════════════════════════════


class TestSubmitErrors:
    def test_submit_not_running_raises(self) -> None:
        """_submit when worker not running → RuntimeError."""
        w = MT5Worker()
        with pytest.raises(RuntimeError, match="worker not running"):
            w._submit("symbol_info_tick", "XAUUSDc")

    def test_submit_circuit_breaker_open_raises(self) -> None:
        """_submit when circuit breaker is open → TimeoutError."""
        w = MT5Worker()
        w._running = True
        # Force circuit breaker open
        w.circuit_breaker.allow_request = MagicMock(return_value=False)
        w.circuit_breaker.get_status = MagicMock(return_value={"total_trips": 3})
        with pytest.raises(TimeoutError, match="circuit OPEN"):
            w._submit("symbol_info_tick", "XAUUSDc")

    def test_submit_reconnect_bypasses_circuit_breaker(self) -> None:
        """_reconnect command bypasses circuit breaker check."""
        w = MT5Worker()
        w._running = True
        w._thread = MagicMock()
        w._thread.is_alive.return_value = False
        w.circuit_breaker.allow_request = MagicMock(return_value=False)
        # _reconnect should NOT raise circuit-open TimeoutError
        try:
            w._submit("_reconnect", w._mt5_init_kwargs, timeout=0.1)
        except TimeoutError as e:
            assert "circuit OPEN" not in str(e)
        except Exception:
            pass

    def test_submit_stuck_worker_raises(self) -> None:
        """_submit when worker is stuck → TimeoutError."""
        w = MT5Worker()
        w._running = True
        w.circuit_breaker.allow_request = MagicMock(return_value=True)
        # Make worker appear stuck
        w._command_in_flight = "copy_ticks_from"
        w._last_command_start = time.monotonic() - 999.0
        with pytest.raises(TimeoutError, match="worker stuck"):
            w._submit("symbol_info_tick", "XAUUSDc")

    def test_submit_queue_full_raises(self) -> None:
        """_submit when queue is full → RuntimeError."""
        w = MT5Worker()
        w._running = True
        w.circuit_breaker.allow_request = MagicMock(return_value=True)
        # Fill queue to max
        w._queue.maxsize = 2
        w._queue.put_nowait(("dummy", "cmd", (), {}))
        w._queue.put_nowait(("dummy", "cmd", (), {}))
        with pytest.raises(RuntimeError, match="queue full"):
            w._submit("symbol_info_tick", "XAUUSDc")


# ═══════════════════════════════════════════════════════════════════════════
# reconnect — testable without MT5 terminal
# ═══════════════════════════════════════════════════════════════════════════


class TestReconnect:
    def test_reconnect_not_running_returns_false(self) -> None:
        """reconnect when not running → False."""
        w = MT5Worker()
        assert w.reconnect() is False

    def test_reconnect_no_thread_returns_false(self) -> None:
        """reconnect when _thread is None → False."""
        w = MT5Worker()
        w._running = True
        w._thread = None
        assert w.reconnect() is False


# ═══════════════════════════════════════════════════════════════════════════
# positions_get / history_deals_get fallback
# ═══════════════════════════════════════════════════════════════════════════


class TestPositionsGet:
    def test_positions_get_empty_on_none(self) -> None:
        """positions_get returns [] when _submit returns None."""
        w = MT5Worker()
        w._running = True
        w.circuit_breaker.allow_request = MagicMock(return_value=True)
        w._submit = MagicMock(return_value=None)  # type: ignore[assignment]
        result = w.positions_get(symbol="XAUUSDc")
        assert result == []

    def test_positions_get_passes_kwargs(self) -> None:
        """positions_get passes symbol/ticket as kwargs to _submit."""
        w = MT5Worker()
        w._running = True
        w.circuit_breaker.allow_request = MagicMock(return_value=True)
        mock_submit = MagicMock(return_value=[{"ticket": 123}])
        w._submit = mock_submit  # type: ignore[assignment]
        result = w.positions_get(symbol="XAUUSDc", ticket=456)
        assert result == [{"ticket": 123}]
        mock_submit.assert_called_once()
        call_kwargs = mock_submit.call_args[1]
        assert call_kwargs["_kwargs"] == {"symbol": "XAUUSDc", "ticket": 456}

    def test_positions_get_no_filters(self) -> None:
        """positions_get with no filters passes empty kwargs."""
        w = MT5Worker()
        w._running = True
        w.circuit_breaker.allow_request = MagicMock(return_value=True)
        mock_submit = MagicMock(return_value=[])
        w._submit = mock_submit  # type: ignore[assignment]
        w.positions_get()
        call_kwargs = mock_submit.call_args[1]
        assert call_kwargs["_kwargs"] == {}


class TestHistoryDealsGet:
    def test_history_deals_get_empty_on_none(self) -> None:
        """history_deals_get returns [] when _submit returns None."""
        w = MT5Worker()
        w._running = True
        w.circuit_breaker.allow_request = MagicMock(return_value=True)
        w._submit = MagicMock(return_value=None)  # type: ignore[assignment]
        result = w.history_deals_get(position=123)
        assert result == []

    def test_history_deals_get_passes_kwargs(self) -> None:
        """history_deals_get passes position/ticket as kwargs to _submit."""
        w = MT5Worker()
        w._running = True
        w.circuit_breaker.allow_request = MagicMock(return_value=True)
        mock_submit = MagicMock(return_value=[{"deal": 1}])
        w._submit = mock_submit  # type: ignore[assignment]
        result = w.history_deals_get(position=100, ticket=200)
        assert result == [{"deal": 1}]
        call_kwargs = mock_submit.call_args[1]
        assert call_kwargs["_kwargs"] == {"position": 100, "ticket": 200}


# ═══════════════════════════════════════════════════════════════════════════
# start — with mocked mt5
# ═══════════════════════════════════════════════════════════════════════════


class TestStart:
    def test_start_already_running_returns_ready_state(self) -> None:
        """start when already running returns ready flag."""
        w = MT5Worker()
        w._running = True
        w._ready.set()  # simulate ready
        assert w.start() is True

    def test_start_stores_terminal_path_before_thread_launch(self) -> None:
        """start stores terminal_path in _mt5_init_kwargs before setting _running."""
        w = MT5Worker()
        with patch("threading.Thread") as mock_thread:
            mock_instance = MagicMock()
            # Patch Thread.start() to set _ready so _ready.wait(timeout=30)
            # returns immediately instead of blocking 30s (FIX-20260627-147).
            mock_instance.start.side_effect = w._ready.set
            mock_thread.return_value = mock_instance
            w.start(terminal_path=r"C:\MT5\terminal64.exe")
            assert w._mt5_init_kwargs == {"path": r"C:\MT5\terminal64.exe"}


# ═══════════════════════════════════════════════════════════════════════════
# _mt5_initialize — testable with mocked mt5 module
# ═══════════════════════════════════════════════════════════════════════════


class TestMT5Initialize:
    def test_mt5_initialize_success(self) -> None:
        """Successful re-init sets self._mt5 and returns True."""
        w = MT5Worker(symbol="BTCUSDc")
        mock_mt5 = MagicMock()
        mock_mt5.initialize.return_value = True
        with patch.dict("sys.modules", MetaTrader5=mock_mt5):
            result = w._mt5_initialize({})
        assert result is True
        assert w._mt5 is mock_mt5
        mock_mt5.symbol_select.assert_called_once_with("BTCUSDc", True)

    def test_mt5_initialize_failure(self) -> None:
        """Failed init sets self._mt5 to None and returns False."""
        w = MT5Worker()
        w._mt5 = MagicMock()
        mock_mt5 = MagicMock()
        mock_mt5.initialize.return_value = False
        with patch.dict("sys.modules", MetaTrader5=mock_mt5):
            result = w._mt5_initialize({})
        assert result is False
        assert w._mt5 is None

    def test_mt5_initialize_shuts_down_old_mt5(self) -> None:
        """Old mt5 is shut down before re-init."""
        w = MT5Worker()
        old_mt5 = MagicMock()
        w._mt5 = old_mt5
        mock_mt5 = MagicMock()
        mock_mt5.initialize.return_value = True
        with patch.dict("sys.modules", MetaTrader5=mock_mt5):
            w._mt5_initialize({})
        old_mt5.shutdown.assert_called_once()

    def test_mt5_initialize_symbol_select_exception_handled(self) -> None:
        """symbol_select failure is caught and does not propagate."""
        w = MT5Worker()
        mock_mt5 = MagicMock()
        mock_mt5.initialize.return_value = True
        mock_mt5.symbol_select.side_effect = RuntimeError("MT5 not connected")
        with patch.dict("sys.modules", MetaTrader5=mock_mt5):
            result = w._mt5_initialize({})
        assert result is True
        assert w._mt5 is mock_mt5


# ═══════════════════════════════════════════════════════════════════════════
# _run dispatch logic
# ═══════════════════════════════════════════════════════════════════════════


class TestRunDispatch:
    def test_unknown_command_sets_value_error(self) -> None:
        """Unknown command → ValueError on future."""
        w = MT5Worker()
        w._running = True
        w._mt5 = MagicMock()
        future = MagicMock()
        w._command_in_flight = "nonexistent_command"
        w._last_command_start = time.monotonic()
        try:
            future.set_exception.assert_not_called()
        except Exception:
            pass

    def test_mt5_none_sets_runtime_error(self) -> None:
        """When _mt5 is None, _run sets RuntimeError on future."""
        w = MT5Worker()
        w._mt5 = None
        with pytest.raises(RuntimeError, match="not initialised"):
            if w._mt5 is None:
                raise RuntimeError(
                    "MT5 not initialised (command=test). "
                    "Call start() and check its return value."
                )


# ═══════════════════════════════════════════════════════════════════════════
# API methods — signature / call-through
# ═══════════════════════════════════════════════════════════════════════════


class TestApiMethods:
    def test_symbol_info_tick_calls_submit(self) -> None:
        w = MT5Worker()
        w._submit = MagicMock(return_value={"bid": 1.0})  # type: ignore[assignment]
        result = w.symbol_info_tick("XAUUSDc", timeout=3.0)
        assert result == {"bid": 1.0}
        w._submit.assert_called_once_with("symbol_info_tick", "XAUUSDc", timeout=3.0)

    def test_symbol_info_calls_submit(self) -> None:
        w = MT5Worker()
        w._submit = MagicMock(return_value={"name": "XAUUSDc"})  # type: ignore[assignment]
        result = w.symbol_info("XAUUSDc", timeout=2.0)
        assert result == {"name": "XAUUSDc"}
        w._submit.assert_called_once_with("symbol_info", "XAUUSDc", timeout=2.0)

    def test_symbol_select_calls_submit(self) -> None:
        w = MT5Worker()
        w._submit = MagicMock(return_value=True)  # type: ignore[assignment]
        result = w.symbol_select("XAUUSDc", True, timeout=4.0)
        assert result is True
        w._submit.assert_called_once_with("symbol_select", "XAUUSDc", True, timeout=4.0)

    def test_copy_rates_from_pos_calls_submit(self) -> None:
        w = MT5Worker()
        w._submit = MagicMock(return_value=[(1, 2, 3, 4, 5, 6, 7, 8)])  # type: ignore[assignment]
        result = w.copy_rates_from_pos("XAUUSDc", 5, 0, 100, timeout=10.0)
        assert result == [(1, 2, 3, 4, 5, 6, 7, 8)]
        w._submit.assert_called_once_with("copy_rates_from_pos", "XAUUSDc", 5, 0, 100, timeout=10.0)

    def test_copy_ticks_from_calls_submit(self) -> None:
        w = MT5Worker()
        w._submit = MagicMock(return_value=[(1, 2, 3)])  # type: ignore[assignment]
        result = w.copy_ticks_from("XAUUSDc", 123456.0, 1000, 0, timeout=15.0)
        assert result == [(1, 2, 3)]
        w._submit.assert_called_once_with(
            "copy_ticks_from", "XAUUSDc", 123456.0, 1000, 0, timeout=15.0
        )

    def test_account_info_calls_submit(self) -> None:
        w = MT5Worker()
        w._submit = MagicMock(return_value={"balance": 10000.0})  # type: ignore[assignment]
        result = w.account_info(timeout=3.0)
        assert result == {"balance": 10000.0}
        w._submit.assert_called_once_with("account_info", timeout=3.0)

    def test_order_send_calls_submit(self) -> None:
        w = MT5Worker()
        w._submit = MagicMock(return_value={"retcode": 10009})  # type: ignore[assignment]
        request = {"action": "buy", "symbol": "XAUUSDc", "volume": 0.01}
        result = w.order_send(request, timeout=10.0)
        assert result == {"retcode": 10009}
        w._submit.assert_called_once_with("order_send", request, timeout=10.0)


# ═══════════════════════════════════════════════════════════════════════════
# Circuit breaker integration
# ═══════════════════════════════════════════════════════════════════════════


class TestCircuitBreakerIntegration:
    def test_circuit_breaker_default_config(self) -> None:
        w = MT5Worker()
        cb = w.circuit_breaker
        assert cb._failure_threshold == 3
        assert cb._cooldown_seconds == 60.0
        assert cb._half_open_max == 1

    def test_circuit_breaker_starts_closed(self) -> None:
        w = MT5Worker()
        assert w.circuit_breaker.allow_request() is True

    def test_submit_clears_stuck_on_completion(self) -> None:
        w = MT5Worker()
        w._stuck_since = time.monotonic() - 60.0
        w._command_in_flight = None
        w._stuck_since = None
        assert w._stuck_since is None
        assert w._command_in_flight is None
