from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


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
                        k: v
                        for k, v in context.items()
                        if isinstance(v, str | int | float | bool | list | dict)
                    },
                }
                for ch in channels:
                    try:
                        ch.send(alert)
                    except Exception:  # BLE001:REVIEWED
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
    def with_default_rules(
        cls,
        metrics: Any = None,
        channels: list[AlertChannel] | None = None,
        *,
        rules_config: list[dict[str, Any]] | None = None,
    ) -> AlertService:
        """Create an AlertService with default rules.

        If *rules_config* is provided (from ``alert_system.rules`` YAML),
        rules are built from the declarative config.  Otherwise the
        hardcoded Phase A rules (5 rules) are used for backward
        compatibility.
        """
        svc = cls(channels=channels)
        if rules_config:
            for rule in build_rules_from_config(rules_config):
                svc.add_rule(rule)
        else:
            _register_legacy_rules(svc)
        return svc


# ── Shared rule builder (SSOT for LiveAlertHub + AlertService) ──────────────


def _build_simple_condition(
    context_key: str, operator: str, threshold: float | str
) -> Callable[[dict], bool]:
    """Build a condition lambda for a simple single-field rule.

    Supported operators: ``gt``, ``lt``, ``eq``.
    ``gt`` and ``lt`` require a numeric threshold; ``eq`` accepts any type.
    """
    if operator == "gt":
        if isinstance(threshold, str):
            raise TypeError(f"gt operator requires numeric threshold, got {threshold!r}")
        return lambda ctx: float(ctx.get(context_key, 0)) > threshold
    if operator == "lt":
        if isinstance(threshold, str):
            raise TypeError(f"lt operator requires numeric threshold, got {threshold!r}")
        return lambda ctx: float(ctx.get(context_key, 0)) < threshold
    if operator == "eq":
        return lambda ctx: ctx.get(context_key, "") == threshold
    raise ValueError(f"Unknown operator {operator!r} — expected gt, lt, or eq")


def _build_composite_condition(rc: dict[str, Any]) -> Callable[[dict], bool]:
    """Build a condition lambda for a composite (multi-field) rule.

    The *rc* dict must contain ``composite_kind`` to select the logic shape.
    Numeric thresholds are read from the config dict itself.
    """
    kind = rc.get("composite_kind", "")
    if kind == "win_rate_collapse":
        threshold = float(rc.get("threshold", 0.30))
        min_window = int(rc.get("min_window", 20))
        return lambda ctx: (
            ctx.get("rolling_win_rate", 1.0) < threshold
            and ctx.get("total_trades_window", 0) >= min_window
        )
    if kind == "strategy_degradation":
        wr_threshold = float(rc.get("wr_threshold", 0.30))
        pnl_threshold = float(rc.get("pnl_threshold", -3.0))
        return lambda ctx: (
            ctx.get("strategy_win_rate", 1.0) < wr_threshold
            and ctx.get("strategy_pnl", 0) < pnl_threshold
        )
    raise ValueError(
        f"Unknown composite_kind {kind!r} — expected win_rate_collapse or strategy_degradation"
    )


#: Hardcoded fallback config matching the 11 rules agreed in Phase 1 audit.
#: Used when no YAML ``alert_system.rules`` is provided (backward compat).
_DEFAULT_RULES_CONFIG: list[dict[str, Any]] = [
    # ── Simple threshold rules ──
    {
        "id": "RULE-001",
        "name": "high_error_rate",
        "context_key": "error_rate",
        "operator": "gt",
        "threshold": 0.1,
        "severity": "critical",
        "cooldown_seconds": 60,
    },
    {
        "id": "RULE-002",
        "name": "circuit_breaker_open",
        "context_key": "circuit_state",
        "operator": "eq",
        "threshold": "open",
        "severity": "critical",
        "cooldown_seconds": 120,
    },
    {
        "id": "RULE-003",
        "name": "bridge_heartbeat_missed",
        "context_key": "bridge_last_ack_seconds",
        "operator": "gt",
        "threshold": 120,
        "severity": "critical",
        "cooldown_seconds": 120,
    },
    {
        "id": "RULE-004",
        "name": "brain_frozen",
        "context_key": "frozen_brain_count",
        "operator": "gt",
        "threshold": 0,
        "severity": "warning",
        "cooldown_seconds": 600,
    },
    {
        "id": "RULE-005",
        "name": "position_limit_near",
        "context_key": "position_utilization",
        "operator": "gt",
        "threshold": 0.8,
        "severity": "warning",
        "cooldown_seconds": 300,
    },
    {
        "id": "RULE-006",
        "name": "cycle_stall",
        "context_key": "cycle_duration_seconds",
        "operator": "gt",
        "threshold": 180,
        "severity": "critical",
        "cooldown_seconds": 300,
    },
    {
        "id": "RULE-007",
        "name": "daily_loss_exceeded",
        "context_key": "daily_pnl_usd",
        "operator": "lt",
        "threshold": -5.0,
        "severity": "critical",
        "cooldown_seconds": 300,
    },
    {
        "id": "RULE-008",
        "name": "consecutive_losses",
        "context_key": "consecutive_losses",
        "operator": "gt",
        "threshold": 8,
        "severity": "warning",
        "cooldown_seconds": 600,
    },
    # ── Composite rules (logic in code, values here) ──
    {
        "id": "RULE-009",
        "name": "win_rate_collapse",
        "type": "composite",
        "composite_kind": "win_rate_collapse",
        "threshold": 0.30,
        "min_window": 20,
        "severity": "critical",
        "cooldown_seconds": 600,
    },
    {
        "id": "RULE-010",
        "name": "strategy_degradation",
        "type": "composite",
        "composite_kind": "strategy_degradation",
        "wr_threshold": 0.30,
        "pnl_threshold": -3.0,
        "severity": "warning",
        "cooldown_seconds": 1800,
    },
    # ── Merged from alert_service.py (previously orphaned) ──
    {
        "id": "RULE-011",
        "name": "high_throttle_rate",
        "context_key": "throttle_rate",
        "operator": "gt",
        "threshold": 0.3,
        "severity": "warning",
        "cooldown_seconds": 300,
    },
]


def build_rules_from_config(
    rules_config: list[dict[str, Any]],
) -> list[AlertRule]:
    """Build AlertRule objects from a declarative config list.

    Config format (simple rule)::

        {
            "name": "high_error_rate",
            "context_key": "error_rate",
            "operator": "gt",       # gt | lt | eq
            "threshold": 0.1,       # numeric or string
            "severity": "critical",
            "cooldown_seconds": 60,
        }

    Config format (composite rule)::

        {
            "name": "win_rate_collapse",
            "type": "composite",
            "composite_kind": "win_rate_collapse",
            "threshold": 0.30,
            "min_window": 20,
            "severity": "critical",
            "cooldown_seconds": 600,
        }
    """
    rules: list[AlertRule] = []
    for rc in rules_config:
        name = rc["name"]
        severity = rc.get("severity", "warning")
        cooldown = float(rc.get("cooldown_seconds", 300))

        rule_type = rc.get("type", "simple")
        if rule_type == "composite":
            condition_fn = _build_composite_condition(rc)
        else:
            ctx_key: str = rc.get("context_key", name)
            operator: str = rc.get("operator", "gt")
            threshold = rc.get("threshold", 0)
            condition_fn = _build_simple_condition(ctx_key, operator, threshold)

        rules.append(
            AlertRule(
                name=name,
                condition_fn=condition_fn,
                severity=severity,
                cooldown_seconds=cooldown,
            )
        )
    return rules


def _register_legacy_rules(svc: AlertService) -> None:
    """Register the original 5 hardcoded Phase A rules.

    Kept for backward compatibility with callers that do not pass
    *rules_config*.  Uses the same ``_DEFAULT_RULES_CONFIG`` as the
    new path so thresholds are identical.
    """
    for rule in build_rules_from_config(_DEFAULT_RULES_CONFIG):
        svc.add_rule(rule)
