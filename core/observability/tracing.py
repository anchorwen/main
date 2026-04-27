import uuid
import threading
import time
from datetime import datetime
from typing import Any


_current_context = threading.local()


def new_trace_id() -> str:
    return uuid.uuid4().hex


def new_span_id() -> str:
    return uuid.uuid4().hex[:16]


class Span:
    """A single unit of work within a trace."""

    def __init__(self, name: str, trace_id: str, parent_span_id: str | None = None):
        self.name = name
        self.trace_id = trace_id
        self.span_id = new_span_id()
        self.parent_span_id = parent_span_id
        self.start_time: float = time.monotonic()
        self.end_time: float | None = None
        self.status: str = "ok"
        self.attributes: dict[str, Any] = {}
        self.events: list[dict] = []

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        self.events.append({
            "name": name,
            "timestamp": datetime.utcnow().isoformat(),
            "attributes": attributes or {},
        })

    def set_error(self, error: str) -> None:
        self.status = "error"
        self.set_attribute("error.message", error)

    def finish(self) -> None:
        self.end_time = time.monotonic()

    @property
    def duration_ms(self) -> float:
        if self.end_time is None:
            return 0
        return round((self.end_time - self.start_time) * 1000, 3)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "attributes": self.attributes,
            "events": self.events,
        }


class TracingContext:
    """Manages trace context for a decision cycle.

    Creates a root span for the cycle and allows child spans to be
    attached for sub-operations (risk evaluation, dispatch, etc.).
    Collects all spans for export.
    """

    def __init__(self, trace_id: str | None = None):
        self.trace_id = trace_id or new_trace_id()
        self._spans: list[Span] = []
        self._current_span: Span | None = None

    def start_span(self, name: str) -> Span:
        parent_id = self._current_span.span_id if self._current_span else None
        span = Span(name=name, trace_id=self.trace_id, parent_span_id=parent_id)
        self._spans.append(span)
        self._current_span = span
        return span

    def end_span(self, span: Span) -> None:
        span.finish()
        if self._current_span is span:
            self._current_span = None
            for s in reversed(self._spans):
                if s is not span and s.end_time is None:
                    self._current_span = s
                    break

    @property
    def current_span(self) -> Span | None:
        return self._current_span

    def get_spans(self) -> list[dict]:
        return [s.to_dict() for s in self._spans]

    def get_trace_summary(self) -> dict:
        total_spans = len(self._spans)
        error_spans = sum(1 for s in self._spans if s.status == "error")
        root = self._spans[0] if self._spans else None
        return {
            "trace_id": self.trace_id,
            "span_count": total_spans,
            "error_count": error_spans,
            "root_span": root.name if root else None,
            "total_duration_ms": root.duration_ms if root else 0,
        }


def get_current_context() -> TracingContext | None:
    return getattr(_current_context, "ctx", None)


def set_current_context(ctx: TracingContext) -> None:
    _current_context.ctx = ctx


def clear_current_context() -> None:
    _current_context.ctx = None
