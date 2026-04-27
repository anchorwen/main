import threading
import time
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Protects dispatch calls from cascading failures.

    Tracks consecutive failures and opens the circuit when the
    threshold is reached.  After a cooldown period, allows a single
    probe request (half-open) and either resets or re-opens.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        half_open_max_calls: int = 1,
    ):
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._half_open_max = half_open_max_calls
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0
        self._half_open_calls = 0
        self._total_trips = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure_time >= self._cooldown_seconds:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
            return self._state

    def allow_request(self) -> bool:
        current = self.state
        if current == CircuitState.CLOSED:
            return True
        if current == CircuitState.HALF_OPEN:
            with self._lock:
                if self._half_open_calls < self._half_open_max:
                    self._half_open_calls += 1
                    return True
            return False
        return False

    def record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._half_open_calls = 0
            self._success_count += 1

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._total_trips += 1
            elif self._failure_count >= self._failure_threshold:
                self._state = CircuitState.OPEN
                self._total_trips += 1

    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_calls = 0

    def get_status(self) -> dict:
        return {
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "total_trips": self._total_trips,
            "failure_threshold": self._failure_threshold,
            "cooldown_seconds": self._cooldown_seconds,
        }


class RateLimiter:
    """Token bucket rate limiter for decision cycle throttling.

    Prevents the system from dispatching more than ``max_rate``
    decisions per ``window_seconds``.
    """

    def __init__(self, max_rate: int = 10, window_seconds: float = 1.0):
        self._max_tokens = max_rate
        self._window = window_seconds
        self._tokens = float(max_rate)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        self._total_allowed = 0
        self._total_throttled = 0

    def allow(self) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                self._total_allowed += 1
                return True
            self._total_throttled += 1
            return False

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        refill = elapsed / self._window * self._max_tokens
        self._tokens = min(self._tokens + refill, float(self._max_tokens))
        self._last_refill = now

    def get_status(self) -> dict:
        return {
            "tokens_available": round(self._tokens, 2),
            "max_rate": self._max_tokens,
            "window_seconds": self._window,
            "total_allowed": self._total_allowed,
            "total_throttled": self._total_throttled,
        }
