import threading
from datetime import UTC, datetime


class MetricsCollector:
    """In-process metrics collection with counters, gauges, and histograms.

    Thread-safe.  Intended for use as a singleton shared across the
    runtime.  Metrics can be snapshotted for export to external systems
    or rendered in diagnostic views.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, list[float]] = {}
        self._labels: dict[str, dict[str, str]] = {}

    def inc(self, name: str, value: float = 1.0, labels: dict | None = None) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value
            if labels:
                self._labels[key] = labels

    def gauge(self, name: str, value: float, labels: dict | None = None) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._gauges[key] = value
            if labels:
                self._labels[key] = labels

    def observe(self, name: str, value: float, labels: dict | None = None) -> None:
        key = self._key(name, labels)
        with self._lock:
            if key not in self._histograms:
                self._histograms[key] = []
            self._histograms[key].append(value)
            if labels:
                self._labels[key] = labels

    def get_counter(self, name: str, labels: dict | None = None) -> float:
        return self._counters.get(self._key(name, labels), 0)

    def get_gauge(self, name: str, labels: dict | None = None) -> float:
        return self._gauges.get(self._key(name, labels), 0)

    def get_histogram(self, name: str, labels: dict | None = None) -> dict:
        key = self._key(name, labels)
        values = self._histograms.get(key, [])
        if not values:
            return {
                "count": 0,
                "sum": 0,
                "min": 0,
                "max": 0,
                "mean": 0,
                "p50": 0,
                "p95": 0,
                "p99": 0,
            }
        sorted_v = sorted(values)
        n = len(sorted_v)
        return {
            "count": n,
            "sum": round(sum(sorted_v), 6),
            "min": round(sorted_v[0], 6),
            "max": round(sorted_v[-1], 6),
            "mean": round(sum(sorted_v) / n, 6),
            "p50": round(sorted_v[int(n * 0.5)], 6),
            "p95": round(sorted_v[min(int(n * 0.95), n - 1)], 6),
            "p99": round(sorted_v[min(int(n * 0.99), n - 1)], 6),
        }

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {k: self.get_histogram(k) for k in self._histograms},
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
            self._labels.clear()

    def _key(self, name: str, labels: dict | None) -> str:
        if not labels:
            return name
        suffix = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{suffix}}}"
