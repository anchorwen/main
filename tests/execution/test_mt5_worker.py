"""Tests for core.execution.mt5_worker — singleton + lifecycle.

FIX-20260619-038: Tier 1 zero-coverage breakout #9.
Tests parts that don't require actual MT5 C++ terminal.
"""

from __future__ import annotations

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


class TestEnqueue:
    def test_queue_created_empty(self) -> None:
        w = MT5Worker()
        assert w._queue.qsize() == 0
