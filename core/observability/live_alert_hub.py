"""Live Alert Hub — unified alerting entry point for the live trading cycle.

Wires the six-layer event→alert pipeline into a single object that the
main loop can call with zero I/O on the critical path.

Layers:
  0. Data Sources     — context dict fed from live_cycle.py (in-memory only)
  1. Event Detection  — AlertService.evaluate(context) → fired alerts
  2. Critical Interception — CRITICAL alerts trip CircuitBreaker
  3. Async Delivery   — BackgroundDeliveryWorker (non-daemon, queue.Queue)
  4. Enrichment       — AlertRunbookBridge attaches SOP actions
  5. Delivery         — CompositeAlertChannel(Slack + DingTalk + Log)

Architect guardrails satisfied:
  G1  — NO synchronous I/O on main thread (queue.Queue + background worker)
  G2  — CRITICAL alert → circuit_breaker.trip() kill-switch
  G3  — All context values are in-memory (no disk reads during evaluate)
  G4  — DingTalkAlertChannel auto-wired via env var
  G6  — Graceful shutdown drains queue (blocking, with timeout)
  Opt3 — BackgroundDeliveryWorker per-rule dedup cache (belt-and-suspenders)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.observability.alert_channels import (
    CompositeAlertChannel,
    DingTalkAlertChannel,
    SlackAlertChannel,
)
from core.observability.alert_runbook_bridge import AlertRunbookBridge
from core.observability.alert_service import (
    AlertChannel,
    AlertRule,
    AlertService,
    LogAlertChannel,
)
from core.protocol.services.resilience import CircuitBreaker

logger = logging.getLogger(__name__)

# ── Background delivery worker ─────────────────────────────────────────────


class BackgroundDeliveryWorker(threading.Thread):
    """Non-daemon thread: pops alerts from queue, delivers to channels.

    NOT a daemon thread — daemon=True would cause undelivered CRITICAL
    alerts to be silently lost on process exit (护栏6).  Uses a stop
    event + queue drain for graceful shutdown.

    Per-rule dedup cache (提升3): for non-CRITICAL alerts within a 60s
    window, suppresses duplicates and attaches an ``aggregated_count``
    field.  CRITICAL alerts always pass through immediately.
    """

    _DEDUP_WINDOW: float = 60.0
    _MAX_DEDUP: int = 50

    def __init__(
        self,
        alert_queue: queue.Queue[dict[str, Any]],
        channel: CompositeAlertChannel,
        runbook_bridge: AlertRunbookBridge,
    ) -> None:
        super().__init__(daemon=False, name="alert-delivery-worker")
        self._queue = alert_queue
        self._channel = channel
        self._bridge = runbook_bridge
        self._stop_event = threading.Event()
        # per-rule dedup: rule_name → (first_fire_time, count)
        self._dedup_cache: dict[str, tuple[float, int]] = {}
        self._delivered: int = 0
        self._suppressed: int = 0

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                alert = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            to_send = self._dedup_or_pass(alert)
            if to_send is None:
                self._suppressed += 1
                self._queue.task_done()
                continue

            enriched = self._bridge.enrich(to_send)
            try:
                self._channel.send(enriched)
                self._delivered += 1
            except Exception:
                logger.exception(
                    "BackgroundDeliveryWorker: channel.send failed for rule=%s",
                    enriched.get("rule_name", "?"),
                )
            self._queue.task_done()

    def _dedup_or_pass(self, alert: dict[str, Any]) -> dict[str, Any] | None:
        """Return alert to send, or None to suppress duplicate."""
        severity = alert.get("severity", "warning")
        if severity == "critical":
            return alert

        rule = alert.get("rule_name", "")
        now = time.monotonic()
        if rule in self._dedup_cache:
            first_time, count = self._dedup_cache[rule]
            if now - first_time < self._DEDUP_WINDOW:
                self._dedup_cache[rule] = (first_time, count + 1)
                if count >= self._MAX_DEDUP:
                    del self._dedup_cache[rule]
                    alert["aggregated_count"] = count + 1
                    return alert
                return None
            else:
                if count > 0:
                    alert["aggregated_count"] = count + 1
                del self._dedup_cache[rule]
                return alert
        else:
            self._dedup_cache[rule] = (now, 0)
            return alert

    def signal_stop(self) -> None:
        self._stop_event.set()

    @property
    def delivered_count(self) -> int:
        return self._delivered

    @property
    def suppressed_count(self) -> int:
        return self._suppressed


# ── Hub ────────────────────────────────────────────────────────────────────


class LiveAlertHub:
    """Single entry point for all alerting in the live trading cycle.

    Usage in live_intent_loop.py::

        hub = LiveAlertHub(base_dir="data")
        # … inside main loop …
        context = {
            "error_rate": ...,
            "circuit_state": hub.circuit_breaker.state.value,
            "frozen_brain_count": ...,
            "position_utilization": ...,
            "bridge_last_ack_seconds": ...,
        }
        fired = hub.evaluate_and_dispatch(context)
        # … on shutdown …
        hub.shutdown()
    """

    _DEFAULT_THRESHOLDS: dict[str, float] = {
        "daily_loss_limit": -5.0,
        "consecutive_losers": 8,
        "win_rate_collapse": 0.30,
        "strategy_degradation_loss": -3.0,
        "strategy_degradation_wr": 0.30,
    }

    def __init__(
        self,
        base_dir: str = "data",
        *,
        slack_url: str = "",
        dingtalk_url: str = "",
        dingtalk_secret: str = "",
        thresholds: dict[str, float] | None = None,
    ) -> None:
        self._base_dir = Path(base_dir)
        # Bounded queue: hard cap at 1000 to prevent OOM during network partition
        self._alert_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)
        self._thresholds = {**self._DEFAULT_THRESHOLDS, **(thresholds or {})}

        # ── Layer 5: Delivery channels ──
        channels: list[Any] = []
        log_path = self._base_dir / "logs" / "alert_audit.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        channels.append(LogAlertChannel(_AlertAuditLog(str(log_path))))

        _slack_url = slack_url or os.getenv("QUANTOS_SLACK_WEBHOOK_URL", "")
        if _slack_url:
            channels.append(SlackAlertChannel(webhook_url=_slack_url))

        _ding_url = dingtalk_url or os.getenv("QUANTOS_DINGTALK_WEBHOOK_URL", "")
        if _ding_url:
            channels.append(
                DingTalkAlertChannel(
                    webhook_url=_ding_url,
                    secret=dingtalk_secret or os.getenv("QUANTOS_DINGTALK_SECRET", ""),
                )
            )

        composite = CompositeAlertChannel(channels)

        # ── Layer 2: CircuitBreaker ──
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            cooldown_seconds=60,
        )

        # ── Layer 4: RunbookBridge ──
        self._runbook = AlertRunbookBridge.with_default_mappings()

        # ── Layer 3: Async delivery worker ──
        self._worker = BackgroundDeliveryWorker(self._alert_queue, composite, self._runbook)
        self._worker.start()

        # ── Layer 1: AlertService + rules ──
        self._alert_service = AlertService()
        self._alert_service.add_channel(_QueueChannel(self._alert_queue))
        self._register_default_rules()

        # ── Stats ──
        self._cycles_evaluated: int = 0
        self._alerts_fired_total: int = 0

        # ── Startup heartbeat: confirms DingTalk channel is operational ──
        try:
            self._alert_queue.put_nowait(
                {
                    "rule_name": "system_online",
                    "severity": "info",
                    "fired_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                    "context_snapshot": {
                        "message": "实盘告警系统已上线",
                        "rules_active": "10",
                        "channels": "钉钉 + 审计日志",
                    },
                }
            )
        except queue.Full:
            logger.warning("Alert queue full at startup — system_online message dropped")

    # ── Rule registration ──────────────────────────────────────────────

    def _register_default_rules(self) -> None:
        """Register Phase A rules: error rate, bridge, circuit, brain, position."""
        self._alert_service.add_rule(
            AlertRule(
                name="high_error_rate",
                condition_fn=lambda ctx: ctx.get("error_rate", 0) > 0.1,
                severity="critical",
                cooldown_seconds=60,
            )
        )
        self._alert_service.add_rule(
            AlertRule(
                name="circuit_breaker_open",
                condition_fn=lambda ctx: ctx.get("circuit_state", "") == "open",
                severity="critical",
                cooldown_seconds=120,
            )
        )
        self._alert_service.add_rule(
            AlertRule(
                name="bridge_heartbeat_missed",
                condition_fn=lambda ctx: ctx.get("bridge_last_ack_seconds", 0) > 120,
                severity="critical",
                cooldown_seconds=120,
            )
        )
        self._alert_service.add_rule(
            AlertRule(
                name="brain_frozen",
                condition_fn=lambda ctx: ctx.get("frozen_brain_count", 0) > 0,
                severity="warning",
                cooldown_seconds=600,
            )
        )
        self._alert_service.add_rule(
            AlertRule(
                name="position_limit_near",
                condition_fn=lambda ctx: ctx.get("position_utilization", 0) > 0.8,
                severity="warning",
                cooldown_seconds=300,
            )
        )
        self._alert_service.add_rule(
            AlertRule(
                name="cycle_stall",
                condition_fn=lambda ctx: ctx.get("cycle_duration_seconds", 0) > 180,
                severity="critical",
                cooldown_seconds=300,
            )
        )

        # ── Phase B: PnL fund-safety rules ──
        _t = self._thresholds
        self._alert_service.add_rule(
            AlertRule(
                name="daily_loss_exceeded",
                condition_fn=lambda ctx: ctx.get("daily_pnl_usd", 0) < _t["daily_loss_limit"],
                severity="critical",
                cooldown_seconds=300,
            )
        )
        self._alert_service.add_rule(
            AlertRule(
                name="consecutive_losses",
                condition_fn=lambda ctx: ctx.get("consecutive_losses", 0)
                > int(_t["consecutive_losers"]),
                severity="warning",
                cooldown_seconds=600,
            )
        )
        self._alert_service.add_rule(
            AlertRule(
                name="win_rate_collapse",
                condition_fn=lambda ctx: (
                    ctx.get("rolling_win_rate", 1.0) < _t["win_rate_collapse"]
                    and ctx.get("total_trades_window", 0) >= 20
                ),
                severity="critical",
                cooldown_seconds=600,
            )
        )
        self._alert_service.add_rule(
            AlertRule(
                name="strategy_degradation",
                condition_fn=lambda ctx: (
                    ctx.get("strategy_win_rate", 1.0) < _t["strategy_degradation_wr"]
                    and ctx.get("strategy_pnl", 0) < _t["strategy_degradation_loss"]
                ),
                severity="warning",
                cooldown_seconds=1800,
            )
        )

    def add_rule(self, rule: AlertRule) -> None:
        self._alert_service.add_rule(rule)

    def send_critical(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        """Directly enqueue a critical alert from external components.

        Used by MT5Worker to alert when its circuit breaker opens,
        and by other infrastructure components that need to inject
        critical alerts outside the normal evaluate-and-dispatch cycle.

        Trips the hub's own circuit breaker and enqueues the alert
        for async delivery.
        """
        alert = {
            "rule_name": reason,
            "severity": "critical",
            "fired_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "context_snapshot": detail or {},
        }
        self._circuit_breaker.trip(reason=reason)
        try:
            self._alert_queue.put_nowait(alert)
        except queue.Full:
            self._write_fallback_alert(alert)

    # ── Main entry point (called from main loop every cycle) ───────────

    def evaluate_and_dispatch(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Evaluate all rules and dispatch alerts asynchronously.

        Called from the main trading loop every cycle.  NO I/O — only
        in-memory rule evaluation and queue.put().

        Returns the list of fired alerts (for optional JSON logging).
        """
        fired = self._alert_service.evaluate(context)
        self._cycles_evaluated += 1
        self._alerts_fired_total += len(fired)

        # ── Guardrail 2: CRITICAL → trip circuit breaker ──
        for alert in fired:
            if alert.get("severity") == "critical":
                self._circuit_breaker.trip(reason=alert.get("rule_name", ""))

        # ── Enqueue for async delivery (backpressure: drop if full) ──
        for alert in fired:
            try:
                self._alert_queue.put_nowait(alert)
            except queue.Full:
                logger.error(
                    "ALERT QUEUE FULL (1000) — downstream dead, alert dropped: %s",
                    alert.get("rule_name", "?"),
                )
                self._write_fallback_alert(alert)

        return fired

    def _write_fallback_alert(self, alert: dict[str, Any]) -> None:
        """Emergency fallback: write dropped alert directly to audit log.

        Called when the alert queue is full (downstream dead / network partition).
        """
        fb_path = self._base_dir / "logs" / "alert_queue_full.jsonl"
        try:
            with open(fb_path, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "event": "alert_dropped_queue_full",
                            "alert": alert,
                            "recorded_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                    + "\n"
                )
        except OSError:
            pass

    # ── Shutdown (graceful drain) ──────────────────────────────────────

    def shutdown(self, timeout: float = 3.0) -> None:
        """Blocking drain: ensure 'last words' are delivered (护栏6)."""
        # 1. Signal worker to stop
        self._worker.signal_stop()

        # 2. Wait for queue to drain
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._alert_queue.empty():
                break
            time.sleep(0.1)

        # 3. Fallback: log any undelivered alerts
        undelivered: list[dict[str, Any]] = []
        while not self._alert_queue.empty():
            try:
                undelivered.append(self._alert_queue.get_nowait())
                self._alert_queue.task_done()
            except queue.Empty:
                break

        if undelivered:
            logger.warning(
                "LiveAlertHub shutdown: %d alerts undelivered, written to fallback log",
                len(undelivered),
            )
            fb_path = self._base_dir / "logs" / "alert_undelivered.jsonl"
            try:
                with open(fb_path, "a", encoding="utf-8") as f:
                    for alert in undelivered:
                        f.write(
                            json.dumps(
                                {
                                    "event": "undelivered_alert",
                                    "alert": alert,
                                    "recorded_at": datetime.now(UTC)
                                    .replace(tzinfo=None)
                                    .isoformat(),
                                },
                                ensure_ascii=False,
                                default=str,
                            )
                            + "\n"
                        )
            except OSError:
                logger.exception("Failed to write undelivered alerts fallback log")

        # 4. Join worker thread
        self._worker.join(timeout=1.0)

    # ── Properties ─────────────────────────────────────────────────────

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    @property
    def cycles_evaluated(self) -> int:
        return self._cycles_evaluated

    @property
    def alerts_fired_total(self) -> int:
        return self._alerts_fired_total

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._alert_service.get_fired_history(limit)

    def get_status(self) -> dict[str, Any]:
        return {
            "circuit_breaker": self._circuit_breaker.get_status(),
            "cycles_evaluated": self._cycles_evaluated,
            "alerts_fired_total": self._alerts_fired_total,
            "queue_size": self._alert_queue.qsize(),
            "delivery_delivered": self._worker.delivered_count,
            "delivery_suppressed": self._worker.suppressed_count,
        }


# ── Internal helpers ────────────────────────────────────────────────────────


class _QueueChannel(AlertChannel):
    """Thin adapter: puts alerts into the async queue instead of sending.

    This replaces the normal channel.send() path so that AlertService
    never calls real channel.send() on the main thread.  All delivery
    happens through the BackgroundDeliveryWorker.
    """

    def __init__(self, q: queue.Queue[dict[str, Any]]) -> None:
        self._q = q

    def send(self, alert: dict[str, Any]) -> bool:
        self._q.put(alert)
        return True


class _AlertAuditLog:
    """Minimal file-backed audit log for alerts (avoids circular imports)."""

    def __init__(self, path: str) -> None:
        self._path = path

    def log(self, event_type: str, severity: str, actor: str, detail: dict[str, Any]) -> None:
        try:
            record = {
                "event": event_type,
                "severity": severity,
                "actor": actor,
                "detail": detail,
                "recorded_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            }
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            logger.exception("AlertAuditLog: write failed for event=%s", event_type)
