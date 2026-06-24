import logging
import threading
from collections.abc import Callable


class EventBus:
    """In-process publish/subscribe event bus.

    Decouples producers from consumers. Subscribers register for
    specific event types and receive events synchronously in
    registration order. Thread-safe.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[Callable]] = {}
        self._event_log: list[dict] = []
        self._max_log = 1000

    def subscribe(self, event_type: str, handler: Callable) -> None:
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Callable) -> None:
        with self._lock:
            if event_type in self._subscribers:
                self._subscribers[event_type] = [
                    h for h in self._subscribers[event_type] if h is not handler
                ]

    def publish(self, event_type: str, payload: dict | None = None) -> int:
        payload = payload or {}
        with self._lock:
            handlers = list(self._subscribers.get(event_type, []))
            self._event_log.append({"event_type": event_type, "payload": payload})
            if len(self._event_log) > self._max_log:
                self._event_log = self._event_log[-self._max_log :]

        delivered = 0
        for handler in handlers:
            try:
                handler(event_type, payload)
                delivered += 1
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                logging.exception("EventBus handler failed for event_type=%s", event_type)
        return delivered

    def get_event_log(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self._event_log[-limit:])

    def get_subscriber_count(self, event_type: str | None = None) -> int:
        with self._lock:
            if event_type:
                return len(self._subscribers.get(event_type, []))
            return sum(len(v) for v in self._subscribers.values())

    def clear(self) -> None:
        with self._lock:
            self._subscribers.clear()
            self._event_log.clear()
