"""Tests for core.observability.live_alert_hub — Phase 3b coverage.

Covers: BackgroundDeliveryWorker._dedup_or_pass, _QueueChannel, _AlertAuditLog,
LiveAlertHub init/shutdown/send_critical/notify_trade/evaluate_and_dispatch/get_status.

Uses class-scoped fixtures to avoid per-test thread startup overhead.
"""

from __future__ import annotations

import json
import queue
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.observability.live_alert_hub import (
    BackgroundDeliveryWorker,
    LiveAlertHub,
    _AlertAuditLog,
    _QueueChannel,
)

# ═══════════════════════════════════════════════════════════════════════════
# BackgroundDeliveryWorker._dedup_or_pass (fast — no threads)
# ═══════════════════════════════════════════════════════════════════════════


class TestDedupOrPass:
    def make_worker(self, symbol: str = "XAUUSDc") -> BackgroundDeliveryWorker:
        return BackgroundDeliveryWorker(
            alert_queue=queue.Queue(),
            channel=MagicMock(),
            runbook_bridge=MagicMock(),
            symbol=symbol,
        )

    def test_trade_notification_always_passes(self) -> None:
        w = self.make_worker()
        alert = {"rule_name": "trade_notification", "severity": "info"}
        assert w._dedup_or_pass(alert) is alert

    def test_critical_always_passes(self) -> None:
        w = self.make_worker()
        alert = {"rule_name": "circuit_open", "severity": "critical"}
        assert w._dedup_or_pass(alert) is alert

    def test_first_non_critical_passes(self) -> None:
        w = self.make_worker()
        alert = {"rule_name": "win_rate_collapse", "severity": "warning"}
        assert w._dedup_or_pass(alert) is alert

    def test_duplicate_within_window_suppressed(self) -> None:
        w = self.make_worker()
        alert = {"rule_name": "daily_loss", "severity": "warning"}
        w._dedup_or_pass(alert)
        assert w._dedup_or_pass(alert) is None

    def test_duplicate_after_window_passes(self) -> None:
        w = self.make_worker()
        alert = {"rule_name": "strategy_degradation", "severity": "warning"}
        w._dedup_or_pass(alert)  # first → passes, caches at T1
        # Simulate window expiry by advancing monotonic clock past 60s default
        with patch.object(time, "monotonic", return_value=time.monotonic() + 61.0):
            assert w._dedup_or_pass(alert) is not None

    def test_max_dedup_burst_through(self) -> None:
        w = self.make_worker()
        w._MAX_DEDUP = 3
        alert = {"rule_name": "repeated_warning", "severity": "warning"}
        # Call 1: first pass (not in cache)
        assert w._dedup_or_pass(alert) is alert
        # Calls 2,3,4: suppressed (count: 0→1, 1→2, 2→3)
        for _ in range(3):
            assert w._dedup_or_pass(alert) is None
        # Call 5: old count=3 >= MAX_DEDUP=3 → burst
        result = w._dedup_or_pass(alert)
        assert result is not None
        assert result.get("aggregated_count") == 4  # count(3) + 1

    def test_dedup_with_aggregated_count_on_window_exit(self) -> None:
        w = self.make_worker()
        alert = {"rule_name": "test_rule", "severity": "warning"}
        w._dedup_or_pass(alert)  # first → passes, cache at (T1, 0)
        w._dedup_or_pass(alert)  # suppressed, cache now (T1, 1)
        # Simulate window expiry — count should be reported as aggregated_count
        with patch.object(time, "monotonic", return_value=time.monotonic() + 61.0):
            result = w._dedup_or_pass(alert)
        assert result is not None
        assert result.get("aggregated_count") == 2

    def test_cross_symbol_dedup_isolation(self) -> None:
        w_xau = self.make_worker("XAUUSDc")
        w_btc = self.make_worker("BTCUSDc")
        alert = {"rule_name": "win_rate_collapse", "severity": "warning"}
        assert w_xau._dedup_or_pass(alert) is alert
        assert w_btc._dedup_or_pass(alert) is alert  # different symbol

    def test_signal_stop_sets_event(self) -> None:
        w = self.make_worker()
        assert not w._stop_event.is_set()
        w.signal_stop()
        assert w._stop_event.is_set()

    def test_delivered_suppressed_counters_zero(self) -> None:
        w = self.make_worker()
        assert w.delivered_count == 0
        assert w.suppressed_count == 0

    def test_daemon_false(self) -> None:
        w = self.make_worker()
        assert w.daemon is False

    def test_thread_name(self) -> None:
        w = self.make_worker()
        assert w.name == "alert-delivery-worker"


# ═══════════════════════════════════════════════════════════════════════════
# _QueueChannel + _AlertAuditLog (fast — no threads)
# ═══════════════════════════════════════════════════════════════════════════


class TestQueueChannel:
    def test_send_puts_into_queue(self) -> None:
        q: queue.Queue[dict] = queue.Queue()
        ch = _QueueChannel(q)
        alert = {"test": "data"}
        assert ch.send(alert) is True
        assert q.qsize() == 1
        assert q.get_nowait() == alert


class TestAlertAuditLog:
    def test_log_writes_jsonl_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = str(Path(tmpdir) / "audit.jsonl")
            audit = _AlertAuditLog(log_path)
            audit.log("test_event", "warning", "test_actor", {"key": "value"})
            lines = Path(log_path).read_text().strip().split("\n")
            assert len(lines) == 1
            record = json.loads(lines[0])
            assert record["event"] == "test_event"
            assert record["severity"] == "warning"
            assert record["actor"] == "test_actor"
            assert record["detail"] == {"key": "value"}
            assert "recorded_at" in record


# ═══════════════════════════════════════════════════════════════════════════
# LiveAlertHub integration tests (class-scoped fixture to share one hub)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="class")
def live_hub():
    """Single LiveAlertHub shared across tests in the class."""
    with tempfile.TemporaryDirectory() as tmpdir:
        hub = LiveAlertHub(base_dir=tmpdir, symbol="XAUUSDc")
        yield hub
        hub.shutdown(timeout=1.0)


class TestLiveAlertHub:
    def test_init_state(self, live_hub: LiveAlertHub) -> None:
        assert live_hub._symbol == "XAUUSDc"
        assert live_hub._alert_service is not None
        assert live_hub._circuit_breaker is not None
        assert live_hub._worker is not None
        assert live_hub._worker.is_alive()
        assert live_hub.cycles_evaluated == 0
        assert live_hub.alerts_fired_total == 0

    def test_circuit_breaker_property(self, live_hub: LiveAlertHub) -> None:
        assert live_hub.circuit_breaker is live_hub._circuit_breaker

    def test_custom_thresholds_merged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hub = LiveAlertHub(
                base_dir=tmpdir,
                symbol="BTCUSDc",
                thresholds={"daily_loss_limit": -10.0},
            )
            assert hub._thresholds["daily_loss_limit"] == -10.0
            assert hub._thresholds["consecutive_losers"] == 8
            hub.shutdown(timeout=0.5)

    def test_get_status_keys(self, live_hub: LiveAlertHub) -> None:
        status = live_hub.get_status()
        for key in (
            "circuit_breaker",
            "cycles_evaluated",
            "alerts_fired_total",
            "queue_size",
            "delivery_delivered",
            "delivery_suppressed",
        ):
            assert key in status

    def test_send_critical_enqueues(self, live_hub: LiveAlertHub) -> None:
        qsize_before = live_hub._alert_queue.qsize()
        live_hub.send_critical("test_reason", {"key": "val"})
        assert live_hub._alert_queue.qsize() > qsize_before

    def test_notify_trade_open_enqueues(self, live_hub: LiveAlertHub) -> None:
        qsize_before = live_hub._alert_queue.qsize()
        live_hub.notify_trade(
            action="open",
            symbol="XAUUSDc",
            side="long",
            volume=0.1,
            price=4700.0,
        )
        assert live_hub._alert_queue.qsize() > qsize_before

    def test_notify_trade_close_enqueues(self, live_hub: LiveAlertHub) -> None:
        qsize_before = live_hub._alert_queue.qsize()
        live_hub.notify_trade(
            action="close",
            symbol="XAUUSDc",
            side="long",
            volume=0.1,
            price=4750.0,
            pnl=5.0,
        )
        assert live_hub._alert_queue.qsize() > qsize_before

    def test_notify_trade_unknown_action_noop(self, live_hub: LiveAlertHub) -> None:
        qsize_before = live_hub._alert_queue.qsize()
        live_hub.notify_trade(
            action="unknown",
            symbol="XAUUSDc",
            side="long",
            volume=0.1,
            price=100.0,
        )
        assert live_hub._alert_queue.qsize() == qsize_before

    def test_evaluate_and_dispatch_increments(self, live_hub: LiveAlertHub) -> None:
        live_hub.evaluate_and_dispatch({"error_rate": 0.01})
        assert live_hub.cycles_evaluated >= 1

    def test_evaluate_returns_list(self, live_hub: LiveAlertHub) -> None:
        fired = live_hub.evaluate_and_dispatch({})
        assert isinstance(fired, list)

    def test_get_history(self, live_hub: LiveAlertHub) -> None:
        history = live_hub.get_history(limit=10)
        assert isinstance(history, list)

    def test_add_rule(self, live_hub: LiveAlertHub) -> None:
        from core.observability.alert_service import AlertRule

        mock_service = MagicMock()
        live_hub._alert_service = mock_service
        rule = AlertRule(
            name="test_rule",
            condition_fn=lambda ctx: ctx.get("error_rate", 0) > 0.5,
            severity="warning",
        )
        live_hub.add_rule(rule)
        mock_service.add_rule.assert_called_once_with(rule)
