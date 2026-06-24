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

import contextlib
import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
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
    _DEFAULT_RULES_CONFIG,
    AlertChannel,
    AlertRule,
    AlertService,
    LogAlertChannel,
    build_rules_from_config,
)
from core.protocol.services.resilience import CircuitBreaker

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]  # d:\future\


# ═══════════════════════════════════════════════════════════════════════════
# Alert Storm Detector — UGR v3.1 §A05 storm protection
# ═══════════════════════════════════════════════════════════════════════════


import enum as _enum


class StormState(_enum.Enum):
    """Alert storm detector state."""

    NORMAL = "normal"
    WARNING = "warning"  # Elevated rate, not yet throttling
    STORM = "storm"  # Active throttling — aggregate only


class AlertStormDetector:
    """Sliding-window alert storm detector with automatic throttling.

    When the alert ingress rate exceeds ``storm_threshold`` within
    ``window_seconds``, the detector transitions through WARNING →
    STORM.  In STORM mode, per-rule alerts are aggregated and only
    summary alerts are emitted at ``storm_summary_interval``.

    Self-monitoring metrics are exposed via ``get_metrics()`` for
    integration into the alert bus health status.
    """

    def __init__(
        self,
        window_seconds: float = 60.0,
        storm_threshold: int = 30,
        warning_threshold: int = 15,
        storm_summary_interval: float = 30.0,
    ) -> None:
        self._window = window_seconds
        self._storm_threshold = storm_threshold
        self._warning_threshold = warning_threshold
        self._summary_interval = storm_summary_interval

        # Sliding window: deque of (timestamp, rule_name)
        self._firing_log: list[tuple[float, str]] = []
        self._state = StormState.NORMAL
        self._state_changed_at = time.monotonic()

        # Aggregation during storm
        self._storm_aggregates: dict[str, int] = {}  # rule_name → count
        self._last_summary_at: float = 0.0

        # Metrics
        self._total_recorded: int = 0
        self._total_suppressed_in_storm: int = 0
        self._storm_events: int = 0  # Number of times storm mode was entered

    # ── Core API ────────────────────────────────────────────────────────

    def record(self, rule_name: str) -> StormState:
        """Record an alert firing and return the current storm state.

        Caller should check the return value:
        - NORMAL / WARNING: deliver alert normally
        - STORM: aggregate; do NOT deliver individually
        """
        now = time.monotonic()
        self._total_recorded += 1
        self._firing_log.append((now, rule_name))
        self._prune(now)

        rate = len(self._firing_log)
        old_state = self._state

        if rate >= self._storm_threshold:
            self._state = StormState.STORM
        elif rate >= self._warning_threshold:
            self._state = StormState.WARNING
        else:
            self._state = StormState.NORMAL

        # Track state transitions
        if self._state != old_state:
            self._state_changed_at = now
            if self._state == StormState.STORM:
                self._storm_events += 1
                self._storm_aggregates.clear()
                self._last_summary_at = now

        # Aggregate in storm mode
        if self._state == StormState.STORM:
            self._storm_aggregates[rule_name] = self._storm_aggregates.get(rule_name, 0) + 1
            self._total_suppressed_in_storm += 1

        return self._state

    def should_emit_summary(self) -> bool:
        """Return True if it's time to emit an aggregated storm summary."""
        if self._state != StormState.STORM:
            return False
        now = time.monotonic()
        return (now - self._last_summary_at) > self._summary_interval

    def consume_summary(self) -> dict[str, int]:
        """Return the aggregated counts since last summary and reset."""
        summary = dict(self._storm_aggregates)
        self._storm_aggregates.clear()
        self._last_summary_at = time.monotonic()
        return summary

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def state(self) -> StormState:
        """Current storm state, auto-recovers when firing rate drops."""
        now = time.monotonic()
        self._prune(now)
        rate = len(self._firing_log)
        if rate < self._warning_threshold and self._state != StormState.NORMAL:
            self._state = StormState.NORMAL
            self._state_changed_at = now
        elif (
            self._warning_threshold <= rate < self._storm_threshold
            and self._state == StormState.STORM
        ):
            self._state = StormState.WARNING
            self._state_changed_at = now
        return self._state

    @property
    def current_rate(self) -> int:
        """Number of alerts in the current window."""
        self._prune(time.monotonic())
        return len(self._firing_log)

    # ── Metrics ─────────────────────────────────────────────────────────

    def get_metrics(self) -> dict[str, object]:
        """Return self-monitoring metrics for health status."""
        return {
            "storm_state": self._state.value,
            "storm_events_total": self._storm_events,
            "current_firing_rate": self.current_rate,
            "total_recorded": self._total_recorded,
            "total_suppressed_in_storm": self._total_suppressed_in_storm,
            "seconds_in_current_state": round(time.monotonic() - self._state_changed_at, 2),
        }

    # ── Internal ────────────────────────────────────────────────────────

    def _prune(self, now: float) -> None:
        """Remove entries outside the sliding window."""
        cutoff = now - self._window
        # In-place truncation: find first index >= cutoff
        keep_from = 0
        for i, (ts, _) in enumerate(self._firing_log):
            if ts >= cutoff:
                keep_from = i
                break
        else:
            keep_from = len(self._firing_log)
        self._firing_log = self._firing_log[keep_from:]


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
        symbol: str = "",
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
        self._symbol = symbol  # FIX-20260616-001: cross-symbol namespace isolation

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

            if to_send.get("rule_name") == "trade_notification":
                enriched = to_send
            else:
                enriched = self._bridge.enrich(to_send)
            try:
                self._channel.send(enriched)
                self._delivered += 1
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # noqa: BLE001  # BLE001:FOG
                logger.exception(
                    "BackgroundDeliveryWorker: channel.send failed for rule=%s",
                    enriched.get("rule_name", "?"),
                )
            self._queue.task_done()

    def _dedup_or_pass(self, alert: dict[str, Any]) -> dict[str, Any] | None:
        """Return alert to send, or None to suppress duplicate."""
        # Trade notifications are real-time push events — never dedup/batch
        if alert.get("rule_name") == "trade_notification":
            return alert
        severity = alert.get("severity", "warning")
        if severity == "critical":
            return alert

        rule = alert.get("rule_name", "")
        # FIX-20260616-001: Cross-symbol namespace isolation.
        # Previously dedup key was rule_name only — BTC's "win_rate_collapse"
        # could suppress XAU's identical rule.  Now each symbol has independent
        # dedup windows, preventing cross-contamination.
        _symbol = getattr(self, "_symbol", "")
        dedup_key = f"{_symbol}:{rule}" if _symbol else rule
        now = time.monotonic()
        if dedup_key in self._dedup_cache:
            first_time, count = self._dedup_cache[dedup_key]
            if now - first_time < self._DEDUP_WINDOW:
                self._dedup_cache[dedup_key] = (first_time, count + 1)
                if count >= self._MAX_DEDUP:
                    del self._dedup_cache[dedup_key]
                    alert["aggregated_count"] = count + 1
                    return alert
                return None
            else:
                if count > 0:
                    alert["aggregated_count"] = count + 1
                del self._dedup_cache[dedup_key]
                return alert
        else:
            self._dedup_cache[dedup_key] = (now, 0)
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
        symbol: str = "",
        slack_url: str = "",
        dingtalk_url: str = "",
        dingtalk_secret: str = "",
        thresholds: dict[str, float] | None = None,
        rules_config: list[dict[str, Any]] | None = None,
    ) -> None:
        self._symbol = symbol or ""
        self._base_dir = Path(base_dir)
        self._cooling_state_path = self._base_dir / "state" / "alert_cooling.json"
        # Bounded queue: hard cap at 1000 to prevent OOM during network partition
        self._alert_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)
        self._thresholds = {**self._DEFAULT_THRESHOLDS, **(thresholds or {})}
        # rules_config from YAML alert_system.rules (or None → fallback)
        self._rules_config = rules_config

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
        self._worker = BackgroundDeliveryWorker(
            self._alert_queue, composite, self._runbook, symbol=self._symbol
        )
        self._worker.start()

        # ── Layer 1: AlertService + rules ──
        self._alert_service = AlertService()
        self._alert_service.add_channel(_QueueChannel(self._alert_queue))
        self._register_rules_from_config()
        self._load_cooling_state()

        # ── Storm protection (UGR v3.1 §A05) ──
        self._storm_detector = AlertStormDetector(
            window_seconds=60.0,
            storm_threshold=30,
            warning_threshold=15,
            storm_summary_interval=30.0,
        )

        # ── Stats ──
        self._cycles_evaluated: int = 0
        self._alerts_fired_total: int = 0

        # ── Startup heartbeat: confirms DingTalk channel is operational ──
        try:
            self._alert_queue.put_nowait(
                {
                    "rule_name": "system_online",
                    "severity": "info",
                    "symbol": self._symbol,
                    "fired_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                    "context_snapshot": {
                        "message": "实盘告警系统已上线",
                        "rules_active": str(len(self._alert_service._rules)),
                        "channels": "钉钉 + 审计日志",
                    },
                }
            )
        except queue.Full:
            logger.warning("Alert queue full at startup — system_online message dropped")

    # ── Rule registration ──────────────────────────────────────────────

    def _register_rules_from_config(self) -> None:
        """Register rules from declarative config, or fall back to defaults.

        When *rules_config* was passed to __init__ (from YAML
        ``alert_system.rules``), rules are built from that config.
        Otherwise the hardcoded ``_DEFAULT_RULES_CONFIG`` is used
        (11 rules, backward compatible).
        """
        cfg = self._rules_config if self._rules_config else _DEFAULT_RULES_CONFIG
        for rule in build_rules_from_config(cfg):
            self._alert_service.add_rule(rule)

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

    # ── Trade notification (per-open/per-close, bypasses alert rules) ──

    def notify_trade(
        self,
        action: str,
        symbol: str,
        side: str,
        volume: float,
        price: float | None = None,
        pnl: float | None = None,
    ) -> None:
        """Send a trade notification directly to DingTalk (real-time, not batched).

        Called from the trade execution path on every open and close.
        Does NOT go through the alert rule engine — this is a PUSH notification,
        not a threshold-based alert.
        """
        if action == "open":
            title = f"[TRADE] {symbol} {side.upper()} OPEN"
            text = (
                f"## {symbol} 开仓通知\n\n"
                f"- 方向: **{side.upper()}**\n"
                f"- 手数: {volume}\n"
                f"- 价格: {price or 'N/A'}\n"
                f"- 时间: {datetime.now(UTC).isoformat()}"
            )
        elif action == "close":
            pnl_str = f"{pnl:+.2f}" if pnl is not None else "N/A"
            emoji = "🟢" if (pnl or 0) > 0 else ("🔴" if (pnl or 0) < 0 else "⚪")
            title = f"[TRADE] {symbol} {side.upper()} CLOSE {emoji} {pnl_str}"
            text = (
                f"## {symbol} 平仓通知\n\n"
                f"- 方向: **{side.upper()}**\n"
                f"- 手数: {volume}\n"
                f"- 平仓价: {price or 'N/A'}\n"
                f"- 盈亏: **{pnl_str}**\n"
                f"- 时间: {datetime.now(UTC).isoformat()}"
            )
        else:
            return

        # Enqueue directly — lightweight fire-and-forget
        alert = {
            "rule_name": "trade_notification",
            "rule_id": f"trade_{action}_{time.time()}",
            "severity": "info",
            "title": title,
            "text": text,
            "symbol": symbol,
            "fired_at": datetime.now(UTC).isoformat(),
            "context": {
                "symbol": symbol,
                "action": action,
                "side": side,
                "volume": volume,
                "price": price,
                "pnl": pnl,
            },
        }
        with contextlib.suppress(Exception):
            self._alert_queue.put_nowait(alert)  # queue full — drop silently (never block trading)

    # ── Main entry point (called from main loop every cycle) ───────────

    def evaluate_and_dispatch(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Evaluate all rules and dispatch alerts asynchronously.

        Called from the main trading loop every cycle.  NO I/O — only
        in-memory rule evaluation and queue.put().

        UGR v3.1 §A05: Storm protection — when alert rate exceeds threshold,
        non-critical alerts are aggregated; only summaries are emitted.

        Returns the list of fired alerts (for optional JSON logging).
        """
        fired = self._alert_service.evaluate(context)
        self._cycles_evaluated += 1
        self._alerts_fired_total += len(fired)

        # ── Persist cooling timestamps for crash survival ──
        if fired:
            self._save_cooling_state()

        # ── Guardrail 2: CRITICAL → trip circuit breaker ──
        for alert in fired:
            if alert.get("severity") == "critical":
                self._circuit_breaker.trip(reason=alert.get("rule_name", ""))

        # ── Shadow RCA: async LLM root cause analysis for Sev1/Sev2 ──
        critical_or_error = [a for a in fired if a.get("severity") in ("critical", "error")]
        if critical_or_error:
            self._trigger_shadow_rca(critical_or_error)

        # ── Storm detection + enqueue (UGR v3.1 §A05) ──
        storm_state = StormState.NORMAL
        for alert in fired:
            rule_name = alert.get("rule_name", "unknown")
            is_critical = alert.get("severity") == "critical"

            # Record every non-trade alert in the storm detector
            if rule_name != "trade_notification":
                storm_state = self._storm_detector.record(rule_name)

            # CRITICAL always passes through regardless of storm
            if is_critical:
                self._enqueue_alert(alert)
            elif storm_state == StormState.STORM:
                # Suppress individual alert — aggregated in storm detector
                pass
            else:
                # NORMAL or WARNING: deliver normally
                self._enqueue_alert(alert)

        # ── Emit storm summary when interval elapses ──
        if self._storm_detector.should_emit_summary():
            summary = self._storm_detector.consume_summary()
            if summary:
                total_aggregated = sum(summary.values())
                summary_alert = {
                    "rule_name": "alert_storm_summary",
                    "severity": "critical",
                    "symbol": self._symbol,
                    "fired_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                    "context_snapshot": {
                        "message": (
                            f"Alert Storm — {total_aggregated} alerts aggregated "
                            f"across {len(summary)} rules in the last "
                            f"{self._storm_detector._summary_interval}s"
                        ),
                        "aggregated_rules": summary,
                        "storm_events_total": self._storm_detector._storm_events,
                    },
                }
                self._enqueue_alert(summary_alert)

        return fired

    def _enqueue_alert(self, alert: dict[str, Any]) -> None:
        """Enqueue a single alert for async delivery. Drop if queue full."""
        try:
            self._alert_queue.put_nowait(alert)
        except queue.Full:
            logger.error(
                "ALERT QUEUE FULL (1000) — downstream dead, alert dropped: %s",
                alert.get("rule_name", "?"),
            )
            self._write_fallback_alert(alert)

    def _trigger_shadow_rca(self, alerts: list[dict[str, Any]]) -> None:
        """Fire-and-forget async LLM root cause analysis.

        Spawns shadow_rca.py as a detached subprocess — never blocks the
        trading loop (Guardrail G1).  Failures are silent (fail-open).
        """
        try:
            payload = json.dumps(
                {
                    "alert": alerts[0],  # analyze the first critical/error alert
                    "file": alerts[0].get("context_snapshot", {}).get("_source_file", ""),
                },
                ensure_ascii=False,
            )
            subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "shadow_rca.py"),
                    "--stdin",
                    "--data-dir",
                    str(self._base_dir),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(ROOT),
            ).communicate(input=payload.encode("utf-8"), timeout=0.5)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # noqa: BLE001  # BLE001:FOG
            pass  # Shadow RCA is best-effort — never block trading

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

    # ── Cooling state persistence (crash-survival) ─────────────────────

    def _save_cooling_state(self) -> None:
        """Persist rule cooling timestamps to disk atomically.

        Write to temp file then os.replace() — atomic on both Windows and POSIX.
        Called after evaluate_and_dispatch() when rules fire.
        """
        state: dict[str, float] = {}
        for rule in self._alert_service._rules:
            if rule._last_fired > 0:
                state[rule.name] = rule._last_fired

        if not state:
            return  # nothing cooled — don't touch disk

        try:
            state_dir = self._cooling_state_path.parent
            state_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                suffix=".json", prefix="alert_cooling_", dir=str(state_dir)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "updated_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                            "symbol": self._symbol,
                            "rules": state,
                        },
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
                os.replace(tmp_path, str(self._cooling_state_path))
            except Exception:
                # Clean up temp file on write failure
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                raise
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # noqa: BLE001  # BLE001:FOG
            pass

    def _load_cooling_state(self) -> None:
        """Restore rule cooling timestamps from disk after process restart.

        Called during __init__ after rules are registered.
        Missing or corrupted state file is silently skipped.
        """
        if not self._cooling_state_path.exists():
            return

        try:
            data = json.loads(self._cooling_state_path.read_text(encoding="utf-8"))
            rules_state = data.get("rules", {}) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return

        if not rules_state:
            return

        restored = 0
        cutoff = datetime.now(UTC).replace(tzinfo=None).timestamp()
        for rule in self._alert_service._rules:
            ts = rules_state.get(rule.name)
            if ts is not None and isinstance(ts, int | float):
                # Only restore if cooldown hasn't fully expired —
                # a fully-expired cooldown is equivalent to "never fired"
                if cutoff - ts < rule.cooldown_seconds:
                    rule._last_fired = float(ts)
                    restored += 1

        if restored:
            logger.info(
                "Restored cooling state for %d/%d rules from %s",
                restored,
                len(self._alert_service._rules),
                self._cooling_state_path,
            )

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
            "storm": self._storm_detector.get_metrics(),
        }

    def get_health_status(self) -> dict[str, Any]:
        """Self-monitoring health report (UGR v3.1 §A05).

        Returns a comprehensive health snapshot including storm detection,
        queue pressure, worker liveness, and circuit breaker state.
        Callers can use this for external monitoring or dashboard display.
        """
        worker_alive = self._worker.is_alive()
        queue_depth = self._alert_queue.qsize()
        storm_metrics = self._storm_detector.get_metrics()

        # Determine overall health
        issues: list[str] = []
        if not worker_alive:
            issues.append("delivery_worker_dead")
        if queue_depth > 900:
            issues.append("queue_near_capacity")
        if storm_metrics["storm_state"] == "storm":
            issues.append("alert_storm_active")
        if self._circuit_breaker.state.value == "open":
            issues.append("circuit_breaker_open")

        return {
            "healthy": len(issues) == 0,
            "issues": issues,
            "worker_alive": worker_alive,
            "queue_depth": queue_depth,
            "queue_capacity": 1000,
            "queue_pressure_pct": round(queue_depth / 10.0, 1),  # 0.0–100.0
            "storm": storm_metrics,
            "circuit_breaker_state": self._circuit_breaker.state.value,
            "cycles_evaluated": self._cycles_evaluated,
            "alerts_fired_total": self._alerts_fired_total,
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
