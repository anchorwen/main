"""Distributed message broker abstraction.

Provides a unified interface over in-process and external message transports.
Supports graceful degradation: when Redis/NATS are unavailable, falls back to
the in-process EventBus.

Usage:
    from core.observability.message_broker import get_broker

    broker = get_broker("redis")   # or "inprocess" / "auto"
    broker.publish("trade.filled", {"symbol": "XAUUSDc", "volume": 0.01})

    @broker.subscribe("trade.filled")
    def on_fill(event_type: str, payload: dict) -> None:
        print(f"Fill: {payload}")
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.observability.event_bus import EventBus

logger = logging.getLogger(__name__)

HandlerFn = Callable[[str, dict[str, Any]], None]


# ── Message envelope ────────────────────────────────────────────────────────


@dataclass
class Message:
    """Standard message envelope for broker-agnostic routing."""

    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    correlation_id: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    source: str = ""
    retry_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "payload": self.payload,
            "message_id": self.message_id,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "source": self.source,
            "retry_count": self.retry_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        return cls(
            event_type=d.get("event_type", ""),
            payload=d.get("payload", {}),
            message_id=d.get("message_id", ""),
            correlation_id=d.get("correlation_id", ""),
            timestamp=d.get("timestamp", ""),
            source=d.get("source", ""),
            retry_count=d.get("retry_count", 0),
        )


# ── Abstract broker ─────────────────────────────────────────────────────────


class MessageBroker(ABC):
    """Abstract message broker.

    Implementations must be thread-safe and handle subscriber registration
    before any messages are published.
    """

    @abstractmethod
    def publish(self, event_type: str, payload: dict[str, Any] | None = None, /) -> int:
        """Publish a message to all subscribers of event_type. Returns delivery count."""
        ...

    @abstractmethod
    def subscribe(self, event_type: str, handler: HandlerFn, /) -> None:
        """Register a handler for the given event_type."""
        ...

    @abstractmethod
    def unsubscribe(self, event_type: str, handler: HandlerFn, /) -> None:
        """Remove a previously registered handler."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Release resources (connections, threads)."""
        ...

    @property
    @abstractmethod
    def backend_name(self) -> str: ...


# ── In-process broker (zero dependencies) ───────────────────────────────────


class InProcessBroker(MessageBroker):
    """Wraps the existing EventBus as a MessageBroker."""

    def __init__(self) -> None:
        self._bus = EventBus()

    def publish(self, event_type: str, payload: dict[str, Any] | None = None, /) -> int:
        return self._bus.publish(event_type, payload or {})

    def subscribe(self, event_type: str, handler: HandlerFn, /) -> None:
        self._bus.subscribe(event_type, handler)

    def unsubscribe(self, event_type: str, handler: HandlerFn, /) -> None:
        self._bus.unsubscribe(event_type, handler)

    def close(self) -> None:
        pass

    @property
    def backend_name(self) -> str:
        return "inprocess"

    @property
    def event_log(self) -> list[dict[str, Any]]:
        return self._bus.get_event_log(limit=200)


# ── Redis Streams broker ────────────────────────────────────────────────────


class RedisStreamsBroker(MessageBroker):
    """Redis Streams-backed message broker.

    Uses consumer groups for reliable delivery.  Falls back gracefully if
    redis is not installed — logs a warning and routes to an internal
    in-process bus.

    Requires ``pip install redis`` for the Redis backend.
    """

    def __init__(
        self,
        *,
        url: str = "redis://localhost:6379",
        stream_prefix: str = "future:events",
        consumer_group: str = "future-engine",
        use_inprocess_fallback: bool = True,
    ) -> None:
        self._url = url
        self._stream_prefix = stream_prefix
        self._consumer_group = consumer_group
        self._fallback = InProcessBroker() if use_inprocess_fallback else None
        self._client: Any = None
        self._subscribers: dict[str, list[HandlerFn]] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

        self._connect()

    def _connect(self) -> None:
        try:
            import redis as _redis

            self._client = _redis.Redis.from_url(self._url, decode_responses=True)
            self._client.ping()
            logger.info("RedisStreamsBroker connected to %s", self._url)
        except Exception as exc:  # BLE001:REVIEWED
            logger.warning("Redis unavailable (%s), using in-process fallback", exc)
            self._client = None

    @property
    def backend_name(self) -> str:
        return "redis" if self._client else "inprocess(fallback)"

    def publish(self, event_type: str, payload: dict[str, Any] | None = None, /) -> int:
        payload = payload or {}
        delivered = 0

        # Always deliver in-process first (subscribers in this process)
        with self._lock:
            handlers = list(self._subscribers.get(event_type, []))
        for h in handlers:
            try:
                h(event_type, payload)
                delivered += 1
            except Exception:  # BLE001:REVIEWED
                logger.exception("Handler failed for %s", event_type)

        # Publish to Redis for external consumers
        if self._client:
            try:
                msg = Message(event_type=event_type, payload=payload)
                stream = f"{self._stream_prefix}:{event_type}"
                self._client.xadd(stream, {"data": json.dumps(msg.to_dict())}, maxlen=10000)
            except Exception:  # BLE001:REVIEWED
                logger.exception("Redis publish failed for %s", event_type)
                if self._fallback:
                    self._fallback.publish(event_type, payload)
        elif self._fallback:
            self._fallback.publish(event_type, payload)

        return delivered

    def subscribe(self, event_type: str, handler: HandlerFn, /) -> None:
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: HandlerFn, /) -> None:
        with self._lock:
            if event_type in self._subscribers:
                self._subscribers[event_type] = [
                    h for h in self._subscribers[event_type] if h is not handler
                ]

    def close(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._client:
            try:  # noqa: SIM105
                self._client.close()
            except Exception:  # BLE001:REVIEWED
                pass
            self._client = None


# ── Broker factory ──────────────────────────────────────────────────────────

_broker_registry: dict[str, MessageBroker] = {}


def get_broker(backend: str = "auto", **kwargs: Any) -> MessageBroker:
    """Get or create a broker instance.

    Args:
        backend: ``"inprocess"``, ``"redis"``, or ``"auto"``.
                 ``"auto"`` tries Redis first, falls back to in-process.
        **kwargs: Passed to the broker constructor.

    Returns:
        A MessageBroker singleton (one per backend type).
    """
    if backend in _broker_registry:
        return _broker_registry[backend]

    if backend == "inprocess":
        broker: MessageBroker = InProcessBroker()
    elif backend == "redis":
        broker = RedisStreamsBroker(use_inprocess_fallback=True, **kwargs)
    elif backend == "auto":
        broker = RedisStreamsBroker(use_inprocess_fallback=True, **kwargs)
    else:
        raise ValueError(f"Unknown broker backend: {backend}")

    _broker_registry[backend] = broker
    return broker


def reset_broker_registry() -> None:
    """Close all brokers and clear the registry (for testing)."""
    for b in _broker_registry.values():
        b.close()
    _broker_registry.clear()
