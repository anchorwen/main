import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from core.contracts.domain_keys import (
    CIRCUIT_STATE_OPEN,
    PAYLOAD_KEY_CIRCUIT_STATE,
    PAYLOAD_KEY_ERROR_RATE,
    PAYLOAD_KEY_FROZEN_BRAIN_COUNT,
    PAYLOAD_KEY_POSITION_UTILIZATION,
    PAYLOAD_KEY_THROTTLE_RATE,
)


class AlertRule:
    """Declarative alert trigger condition."""

    def __init__(
        self,
        name: str,
        condition_fn: Callable[[dict], bool],
        severity: str = "warning",
        cooldown_seconds: float = 300.0,
    ):
        self.name = name
        self.condition_fn = condition_fn
        self.severity = severity
        self.cooldown_seconds = cooldown_seconds
        self._last_fired: float = 0

    def should_fire(self, context: dict) -> bool:
        if not self.condition_fn(context):
            return False
        now = datetime.now(UTC).replace(tzinfo=None).timestamp()
        if now - self._last_fired < self.cooldown_seconds:
            return False
        self._last_fired = now
        return True


class AlertChannel:
    """Base class for alert delivery channels."""

    def send(self, alert: dict) -> bool:
        raise NotImplementedError


class LogAlertChannel(AlertChannel):
    """Writes alerts to the audit log."""

    def __init__(self, audit_log):
        self._audit = audit_log

    def send(self, alert: dict) -> bool:
        if self._audit is None:
            return False
        self._audit.log(
            event_type="alert",
            severity=alert.get("severity", "warning"),
            actor="alert_service",
            detail=alert,
        )
        return True


class InMemoryAlertChannel(AlertChannel):
    """Captures alerts in memory for testing."""

    def __init__(self):
        self._alerts: list[dict] = []

    def send(self, alert: dict) -> bool:
        self._alerts.append(alert)
        return True

    def get_alerts(self) -> list[dict]:
        return list(self._alerts)

    def clear(self) -> None:
        self._alerts.clear()


class BatchingAlertChannel(AlertChannel):
    """Accumulates non-critical alerts and flushes them in batches.

    ``critical`` and ``error`` severity alerts pass through immediately.
    ``warning`` alerts are batched every ``batch_interval_seconds``.
    ``info`` alerts are batched and only flushed once per ``batch_interval_seconds``.

    The batched alerts are forwarded to the wrapped ``target`` channel as a
    single compound alert.
    """

    def __init__(
        self,
        target: AlertChannel,
        *,
        batch_interval_seconds: float = 300.0,
        max_batch_size: int = 20,
    ):
        self._target = target
        self._batch_interval = batch_interval_seconds
        self._max_batch = max_batch_size
        self._buffer: list[dict] = []
        self._last_flush: float = 0.0

    def send(self, alert: dict) -> bool:
        severity = alert.get("severity", "warning")
        if severity in ("critical", "error"):
            return self._target.send(alert)

        self._buffer.append(alert)
        if len(self._buffer) >= self._max_batch:
            return self._flush()
        return True

    def _flush(self) -> bool:
        import time

        if not self._buffer:
            return True
        batched = {
            "rule_name": "batched_alerts",
            "severity": "warning",
            "fired_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "context_snapshot": {"batched_count": len(self._buffer)},
            "batched_alerts": list(self._buffer),
        }
        self._buffer.clear()
        self._last_flush = time.time()
        return self._target.send(batched)

    def flush(self) -> bool:
        """Explicitly flush the batch buffer (e.g., end-of-day)."""
        return self._flush()


class SeverityRouter(AlertChannel):
    """Routes alerts to different channels based on severity.

    Typical setup::

        critical_ch = SlackAlertChannel()   # P0 — instant
        batched_ch = BatchingAlertChannel(SlackAlertChannel())  # P1/P2 — batched
        router = SeverityRouter({
            "critical": critical_ch,
            "error": critical_ch,
            "warning": batched_ch,
            "info": batched_ch,
        }, default=batched_ch)
    """

    def __init__(
        self,
        routes: dict[str, AlertChannel] | None = None,
        default: AlertChannel | None = None,
    ):
        self._routes = dict(routes) if routes else {}
        self._default = default

    def add_route(self, severity: str, channel: AlertChannel) -> None:
        self._routes[severity] = channel

    def send(self, alert: dict) -> bool:
        severity = alert.get("severity", "warning")
        channel = self._routes.get(severity, self._default)
        if channel is None:
            return False
        return channel.send(alert)


class AlertService:
    """Evaluates alert rules against system state and dispatches to channels."""

    def __init__(self, channels: list[AlertChannel] | None = None):
        self._lock = threading.Lock()
        self._rules: list[AlertRule] = []
        self._channels = list(channels) if channels else []
        self._fired_history: list[dict] = []
        self._max_history = 500

    def add_rule(self, rule: AlertRule) -> None:
        with self._lock:
            self._rules.append(rule)

    def add_channel(self, channel: AlertChannel) -> None:
        with self._lock:
            self._channels.append(channel)

    def evaluate(self, context: dict) -> list[dict]:
        fired = []
        with self._lock:
            rules = list(self._rules)
            channels = list(self._channels)

        for rule in rules:
            if rule.should_fire(context):
                alert = {
                    "rule_name": rule.name,
                    "severity": rule.severity,
                    "fired_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                    "context_snapshot": {
                        k: v for k, v in context.items() if isinstance(v, str | int | float | bool)
                    },
                }
                for ch in channels:
                    try:
                        ch.send(alert)
                    except Exception:
                        logging.exception("AlertService failed sending alert to channel=%s", ch)
                fired.append(alert)

        with self._lock:
            self._fired_history.extend(fired)
            if len(self._fired_history) > self._max_history:
                self._fired_history = self._fired_history[-self._max_history :]

        return fired

    def get_fired_history(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self._fired_history[-limit:])

    @classmethod
    def with_default_rules(cls, metrics=None, channels: list[AlertChannel] | None = None):
        svc = cls(channels=channels)
        svc.add_rule(
            AlertRule(
                name="high_error_rate",
                condition_fn=lambda ctx: ctx.get(PAYLOAD_KEY_ERROR_RATE, 0) > 0.1,
                severity="critical",
                cooldown_seconds=60,
            )
        )
        svc.add_rule(
            AlertRule(
                name="circuit_breaker_open",
                condition_fn=lambda ctx: ctx.get(PAYLOAD_KEY_CIRCUIT_STATE) == CIRCUIT_STATE_OPEN,
                severity="critical",
                cooldown_seconds=120,
            )
        )
        svc.add_rule(
            AlertRule(
                name="high_throttle_rate",
                condition_fn=lambda ctx: ctx.get(PAYLOAD_KEY_THROTTLE_RATE, 0) > 0.3,
                severity="warning",
                cooldown_seconds=300,
            )
        )
        svc.add_rule(
            AlertRule(
                name="brain_frozen",
                condition_fn=lambda ctx: ctx.get(PAYLOAD_KEY_FROZEN_BRAIN_COUNT, 0) > 0,
                severity="warning",
                cooldown_seconds=600,
            )
        )
        svc.add_rule(
            AlertRule(
                name="position_limit_near",
                condition_fn=lambda ctx: ctx.get(PAYLOAD_KEY_POSITION_UTILIZATION, 0) > 0.8,
                severity="warning",
                cooldown_seconds=300,
            )
        )
        return svc
