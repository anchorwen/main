"""Iron Law #13: Universal Telemetry Protocol — Structured Event Backbone.

All system events (alerts, health reports, trade signals) MUST be emitted as
strongly-typed frozen dataclasses inheriting from ``BaseTelemetryEvent``.
Flat dicts and string-concatenated context are forbidden at the source.

Architecture (Dimensions):
  D1 — Data as Contract: every event is a frozen, typed object.
  D2 — Edge Rendering: producers know nothing of Chinese/Markdown/DingTalk.
  D3 — Closed-Loop Remediation (reserved): machine-readable events enable
       the Self-Healing Engine to match runbook_id and auto-remediate.

Design principles:
  - ``frozen=True`` prevents accidental mutation after emission.
  - ``payload`` is always a typed dict/dataclass — never a pre-flattened
    string blob.  Rendering is the channelʼs job (D2).
  - ``module`` identifies the producing subsystem for routing and auditing.

Usage::

    from core.observability.event_schema import (
        BaseTelemetryEvent,
        DataHealthPayload,
        EventSeverity,
    )

    event = BaseTelemetryEvent(
        event_id="evt-...",
        rule_id="RULE-012",
        severity=EventSeverity.FATAL,
        module="core.observability.data_health",
        payload={
            "data_health": DataHealthPayload(
                failed_sources=[{"source": "lgb", "code": "E01", "msg": "timeout"}],
                warned_sources=[],
                metrics={"fail_count": 1.0, "latency_ms": 150.3},
            ),
        },
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventSeverity(Enum):
    """Unified severity taxonomy for all telemetry events.

    Maps to existing alert severities:
      INFO  → info
      WARN  → warning
      ERROR → error
      FATAL → critical
    """

    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    FATAL = "FATAL"

    def to_alert_severity(self) -> str:
        """Backward-compatible mapping to alert channel severity labels."""
        _MAP: dict[EventSeverity, str] = {
            EventSeverity.INFO: "info",
            EventSeverity.WARN: "warning",
            EventSeverity.ERROR: "error",
            EventSeverity.FATAL: "critical",
        }
        return _MAP[self]


# ── Unified Event Backbone ──────────────────────────────────────────────────


@dataclass(frozen=True)
class BaseTelemetryEvent:
    """Iron Law #13: Universal Telemetry Protocol root.

    Every system alert, health check failure, and trade signal MUST be
    emitted as an instance of this class (or a typed subclass).  Flat
    ``dict`` with string-concatenated values is forbidden at the source.

    Attributes:
        event_id: Unique event identifier (UUID or content-hash).
        rule_id: Corresponding alert rule (e.g. ``"RULE-012"``).
        severity: Unified severity level.
        timestamp: ISO-8601 UTC timestamp from ``utc_now_iso()``.
        module: Producing subsystem (e.g. ``"core.observability.data_health"``).
        payload: Strongly-typed business context — never pre-flattened.
    """

    event_id: str
    rule_id: str
    severity: EventSeverity
    timestamp: str
    module: str
    payload: dict[str, Any] = field(default_factory=dict)


# ── Data Health Payload ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class FailedSource:
    """A single failed health-check source.

    Attributes:
        source: Check name (e.g. ``"journal_completeness"``).
        code: Primary status code (e.g. ``"JOURNAL_SLA_VIOLATION"``).
        message: Human-readable detail (capped at 200 chars by the producer).
    """

    source: str
    code: str
    message: str = ""


@dataclass(frozen=True)
class DataHealthPayload:
    """Structured payload for data-health alerts (RULE-012 through RULE-016).

    Carries the full micro-dimensional detail of a ``HealthReport`` so that
    edge renderers (DingTalk, Slack, Web UI) can display actionable
    per-source bullet lists without re-parsing strings.

    Attributes:
        failed_sources: Sources in ``fail`` or ``missing`` status.
        warned_sources: Sources in ``warn`` status.
        metrics: Aggregate counts and latency for summary rendering.
    """

    failed_sources: list[dict[str, str]] = field(default_factory=list)
    warned_sources: list[dict[str, str]] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


# ── Severity helper ─────────────────────────────────────────────────────────

SEVERITY_ALERT_MAP: dict[str, str] = {
    "INFO": "info",
    "WARN": "warning",
    "ERROR": "error",
    "FATAL": "critical",
}
