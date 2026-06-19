"""Unit tests for protocol resilience — CircuitBreaker + RateLimiter.

Pure state machine logic tests. Time-dependent transitions tested
via direct state manipulation.
Part of Test 1: protocol dedicated test suite.
"""

from __future__ import annotations

from core.protocol.services.resilience import CircuitBreaker, CircuitState, RateLimiter


# ── CircuitBreaker ────────────────────────────────────────────────────────


class TestCircuitBreakerInitial:
    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED

    def test_initial_status(self):
        cb = CircuitBreaker(failure_threshold=5, cooldown_seconds=30.0)
        st = cb.get_status()
        assert st["state"] == "closed"
        assert st["failure_count"] == 0
        assert st["total_trips"] == 0
        assert st["failure_threshold"] == 5

    def test_custom_params(self):
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0)
        assert cb.get_status()["failure_threshold"] == 3
        assert cb.get_status()["cooldown_seconds"] == 60.0


class TestCircuitBreakerAllowRequest:
    def test_allows_when_closed(self):
        cb = CircuitBreaker()
        assert cb.allow_request() is True
        assert cb.allow_request() is True

    def test_denies_when_open(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False
        assert cb.allow_request() is False


class TestCircuitBreakerRecordFailure:
    def test_opens_on_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_below_threshold_stays_closed(self):
        cb = CircuitBreaker(failure_threshold=5)
        for _ in range(4):
            cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb.get_status()["failure_count"] == 4

    def test_trip_count(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.get_status()["total_trips"] == 1
        cb.reset()
        cb.record_failure()
        cb.record_failure()
        assert cb.get_status()["total_trips"] == 2


class TestCircuitBreakerRecordSuccess:
    def test_success_increments_counter(self):
        cb = CircuitBreaker()
        cb.record_success()
        cb.record_success()
        assert cb.get_status()["success_count"] == 2


class TestCircuitBreakerTrip:
    def test_force_opens_regardless_of_count(self):
        cb = CircuitBreaker(failure_threshold=100)
        cb.trip("daily_loss_limit")
        assert cb.state == CircuitState.OPEN

    def test_trip_count_increments(self):
        cb = CircuitBreaker()
        cb.trip("test")
        assert cb.get_status()["total_trips"] == 1


class TestCircuitBreakerReset:
    def test_resets_to_closed(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.get_status()["failure_count"] == 0

    def test_reset_clears_failure_count(self):
        cb = CircuitBreaker(failure_threshold=5)
        cb.record_failure()
        cb.record_failure()
        cb.reset()
        assert cb.get_status()["failure_count"] == 0


# ── RateLimiter ───────────────────────────────────────────────────────────


class TestRateLimiterInitial:
    def test_starts_with_full_tokens(self):
        rl = RateLimiter(max_rate=10, window_seconds=1.0)
        assert rl.get_status()["tokens_available"] == 10.0

    def test_custom_params(self):
        rl = RateLimiter(max_rate=5, window_seconds=2.0)
        st = rl.get_status()
        assert st["max_rate"] == 5
        assert st["window_seconds"] == 2.0


class TestRateLimiterAllow:
    def test_allows_up_to_max_rate(self):
        rl = RateLimiter(max_rate=3, window_seconds=60.0)
        assert rl.allow() is True
        assert rl.allow() is True
        assert rl.allow() is True
        assert rl.allow() is False

    def test_rate_1_single_allow(self):
        rl = RateLimiter(max_rate=1, window_seconds=60.0)
        assert rl.allow() is True
        assert rl.allow() is False


class TestRateLimiterCounters:
    def test_total_allowed_and_throttled(self):
        rl = RateLimiter(max_rate=2, window_seconds=60.0)
        rl.allow()
        rl.allow()
        rl.allow()  # throttled
        st = rl.get_status()
        assert st["total_allowed"] == 2
        assert st["total_throttled"] == 1
