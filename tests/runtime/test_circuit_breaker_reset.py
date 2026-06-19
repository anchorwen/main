"""Tests for core.runtime.circuit_breaker_reset — Strangler Fig #31.

FIX-20260620-083: New module zero-coverage breakout.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from core.runtime.circuit_breaker_reset import auto_reset_circuit_breaker


class _FakeConfig:
    circuit_breaker_cooldown_seconds = 300
    max_bridge_silence_seconds = 60
    cycle_stall_threshold_seconds = 30


class _FakeState:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class TestAutoResetCircuitBreaker:

    def test_noop_when_not_tripped(self) -> None:
        config = _FakeConfig()
        state = _FakeState(
            _circuit_breaker_tripped=False,
            _circuit_breaker_tripped_at=0.0,
            _circuit_breaker_trip_reason="",
            block_new_entries=False,
            _consecutive_degraded_cycles=0,
            _consecutive_stale_cycles=0,
            _consecutive_stale_features=0,
            _last_bridge_ack_time=999999.0,
            _strategies={},
        )
        auto_reset_circuit_breaker(config, state, degraded_wakeup=False, cycle_duration=1.0)
        assert state._circuit_breaker_tripped is False

    def test_budget_breached_resets_when_no_paused_strategies(self) -> None:
        config = _FakeConfig()
        # Create a fake strategy with a budget that is NOT paused
        strat = MagicMock()
        strat.budget.check_pause.return_value = False
        state = _FakeState(
            _circuit_breaker_tripped=True,
            _circuit_breaker_tripped_at=100.0,
            _circuit_breaker_trip_reason="budget_breached",
            block_new_entries=True,
            _consecutive_degraded_cycles=0,
            _consecutive_stale_cycles=0,
            _consecutive_stale_features=0,
            _last_bridge_ack_time=999999.0,
            _strategies={"swing": strat},
        )
        auto_reset_circuit_breaker(config, state, degraded_wakeup=False, cycle_duration=1.0)
        assert state._circuit_breaker_tripped is False
        assert state._circuit_breaker_trip_reason == ""
        assert state.block_new_entries is False

    def test_budget_breached_stays_tripped_when_strategy_paused(self) -> None:
        config = _FakeConfig()
        strat = MagicMock()
        strat.budget.check_pause.return_value = True  # still paused
        state = _FakeState(
            _circuit_breaker_tripped=True,
            _circuit_breaker_tripped_at=100.0,
            _circuit_breaker_trip_reason="budget_breached",
            block_new_entries=True,
            _consecutive_degraded_cycles=0,
            _last_bridge_ack_time=999999.0,
            _strategies={"swing": strat},
        )
        auto_reset_circuit_breaker(config, state, degraded_wakeup=False, cycle_duration=1.0)
        assert state._circuit_breaker_tripped is True  # still tripped

    def test_resets_when_cooldown_elapsed_and_all_clear(self) -> None:
        import time
        config = _FakeConfig()
        config.circuit_breaker_cooldown_seconds = 1  # very short
        tripped_at = time.time() - 10  # 10 seconds ago (cooldown elapsed)
        state = _FakeState(
            _circuit_breaker_tripped=True,
            _circuit_breaker_tripped_at=tripped_at,
            _circuit_breaker_trip_reason="bridge_silence",
            block_new_entries=True,
            _consecutive_degraded_cycles=5,
            _consecutive_stale_cycles=3,
            _consecutive_stale_features=2,
            _last_bridge_ack_time=time.time(),  # bridge alive
        )
        auto_reset_circuit_breaker(config, state, degraded_wakeup=False, cycle_duration=1.0)
        assert state._circuit_breaker_tripped is False
        assert state._consecutive_degraded_cycles == 0  # all counters reset
        assert state._consecutive_stale_cycles == 0
        assert state._consecutive_stale_features == 0

    def test_does_not_reset_when_bridge_dead(self) -> None:
        import time
        config = _FakeConfig()
        tripped_at = time.time() - 10
        state = _FakeState(
            _circuit_breaker_tripped=True,
            _circuit_breaker_tripped_at=tripped_at,
            _circuit_breaker_trip_reason="bridge_silence",
            _consecutive_degraded_cycles=5,
            _last_bridge_ack_time=0.0,  # bridge dead
        )
        auto_reset_circuit_breaker(config, state, degraded_wakeup=False, cycle_duration=1.0)
        assert state._circuit_breaker_tripped is True  # still tripped

    def test_does_not_reset_when_degraded_wakeup(self) -> None:
        import time
        config = _FakeConfig()
        tripped_at = time.time() - 10
        state = _FakeState(
            _circuit_breaker_tripped=True,
            _circuit_breaker_tripped_at=tripped_at,
            _circuit_breaker_trip_reason="cycle_stall",
            _consecutive_degraded_cycles=5,
            _last_bridge_ack_time=time.time(),
        )
        auto_reset_circuit_breaker(config, state, degraded_wakeup=True, cycle_duration=1.0)
        assert state._circuit_breaker_tripped is True

    def test_resets_counter_on_clean_cycle_when_not_tripped(self) -> None:
        config = _FakeConfig()
        state = _FakeState(
            _circuit_breaker_tripped=False,
            _circuit_breaker_tripped_at=0.0,
            _circuit_breaker_trip_reason="",
            _consecutive_degraded_cycles=3,
            _consecutive_stale_cycles=0,
            _consecutive_stale_features=0,
            _last_bridge_ack_time=0.0,
            _strategies={},
        )
        auto_reset_circuit_breaker(config, state, degraded_wakeup=False, cycle_duration=1.0)
        assert state._consecutive_degraded_cycles == 0

    def test_does_not_reset_counter_on_degraded_cycle(self) -> None:
        config = _FakeConfig()
        state = _FakeState(
            _circuit_breaker_tripped=False,
            _consecutive_degraded_cycles=3,
        )
        auto_reset_circuit_breaker(config, state, degraded_wakeup=True, cycle_duration=1.0)
        assert state._consecutive_degraded_cycles == 3  # unchanged
