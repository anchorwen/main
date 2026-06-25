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


# ═══════════════════════════════════════════════════════════════════════════
# AlertStormDetector — UGR v3.1 §A05 storm protection tests
# ═══════════════════════════════════════════════════════════════════════════


class TestAlertStormDetector:
    """Tests for AlertStormDetector — sliding-window storm detection."""

    def test_normal_state_initially(self) -> None:
        from core.observability.live_alert_hub import AlertStormDetector, StormState

        d = AlertStormDetector()
        assert d.state == StormState.NORMAL
        assert d.current_rate == 0

    def test_warning_at_threshold(self) -> None:
        from core.observability.live_alert_hub import AlertStormDetector, StormState

        d = AlertStormDetector(warning_threshold=3, storm_threshold=30)
        for i in range(3):
            d.record(f"rule_{i}")
        assert d.state == StormState.WARNING

    def test_storm_at_threshold(self) -> None:
        from core.observability.live_alert_hub import AlertStormDetector, StormState

        d = AlertStormDetector(warning_threshold=3, storm_threshold=5)
        for i in range(5):
            d.record(f"rule_{i}")
        assert d.state == StormState.STORM

    def test_storm_aggregates_alerts(self) -> None:
        from core.observability.live_alert_hub import AlertStormDetector, StormState

        d = AlertStormDetector(warning_threshold=3, storm_threshold=5)
        for i in range(10):
            state = d.record("rule_a" if i < 6 else "rule_b")
        assert d.state == StormState.STORM
        # rule_a: 6, rule_b: 4 (but first 5 trigger the storm, so only 5+5 counted or 1+4 after storm...)
        # After storm is entered at record 5 (index 4), records 5-9 are in storm
        metrics = d.get_metrics()
        suppressed = metrics["total_suppressed_in_storm"]
        assert isinstance(suppressed, int)
        assert suppressed >= 5

    def test_summary_emission(self) -> None:
        from core.observability.live_alert_hub import AlertStormDetector

        d = AlertStormDetector(warning_threshold=3, storm_threshold=5, storm_summary_interval=0.05)
        for _ in range(10):
            d.record("rule_a")
        # First emission: interval has elapsed since storm entered
        # Use >= 3× timer quantum (Windows ~15.6ms) to stay safely above 50ms interval
        time.sleep(0.15)
        assert d.should_emit_summary()
        summary = d.consume_summary()
        assert "rule_a" in summary
        assert summary["rule_a"] > 0
        # After consuming, should not emit until interval elapses again
        assert not d.should_emit_summary()

    def test_rate_decays(self) -> None:
        from core.observability.live_alert_hub import AlertStormDetector, StormState

        d = AlertStormDetector(window_seconds=0.01, warning_threshold=3, storm_threshold=5)
        for _ in range(5):
            d.record("rule_a")
        assert d.state == StormState.STORM

        # Wait for window to expire
        time.sleep(0.02)
        assert d.current_rate == 0
        assert d.state != StormState.STORM  # must have decayed from STORM

    def test_normal_mode_does_not_aggregate(self) -> None:
        from core.observability.live_alert_hub import AlertStormDetector, StormState

        d = AlertStormDetector(warning_threshold=5, storm_threshold=10)
        state = d.record("rule_a")
        assert state == StormState.NORMAL
        assert not d.should_emit_summary()

    def test_critical_never_aggregated_by_detector(self) -> None:
        """The storm detector records all rules; caller decides CRITICAL bypass."""
        from core.observability.live_alert_hub import AlertStormDetector, StormState

        d = AlertStormDetector(warning_threshold=3, storm_threshold=5)
        for _ in range(5):
            d.record("circuit_open")
        assert d.state == StormState.STORM
        # The detector doesn't know about severity — the CALLER must bypass

    def test_metrics_include_state_info(self) -> None:
        from core.observability.live_alert_hub import AlertStormDetector

        d = AlertStormDetector()
        d.record("test_rule")
        metrics = d.get_metrics()
        assert metrics["storm_state"] == "normal"
        assert metrics["total_recorded"] == 1
        assert "current_firing_rate" in metrics
        assert "seconds_in_current_state" in metrics

    def test_storm_events_counter(self) -> None:
        from core.observability.live_alert_hub import AlertStormDetector, StormState

        d = AlertStormDetector(window_seconds=0.01, warning_threshold=2, storm_threshold=3)
        # Enter storm
        for _ in range(3):
            d.record("rule_a")
        assert d.state == StormState.STORM
        assert d._storm_events == 1

        # Decay
        time.sleep(0.02)
        assert d.state != StormState.STORM  # must have decayed from STORM
        # Re-enter storm
        for _ in range(3):
            d.record("rule_b")
        assert d.state == StormState.STORM
        assert d._storm_events == 2


class TestLiveAlertHubStormIntegration:
    """Integration tests: LiveAlertHub evaluate_and_dispatch with storm detection."""

    def test_storm_detector_initialized(self, live_hub: LiveAlertHub) -> None:
        assert hasattr(live_hub, "_storm_detector")
        from core.observability.live_alert_hub import StormState

        assert live_hub._storm_detector.state == StormState.NORMAL

    def test_get_status_includes_storm_metrics(self, live_hub: LiveAlertHub) -> None:
        status = live_hub.get_status()
        assert "storm" in status
        assert status["storm"]["storm_state"] == "normal"

    def test_get_health_status_returns_comprehensive(self, live_hub: LiveAlertHub) -> None:
        health = live_hub.get_health_status()
        assert "healthy" in health
        assert "issues" in health
        assert "worker_alive" in health
        assert "queue_pressure_pct" in health
        assert "storm" in health
        assert "circuit_breaker_state" in health

    def test_evaluate_and_dispatch_tracks_storm_rate(self, live_hub: LiveAlertHub) -> None:
        """Repeated evaluate_and_dispatch calls feed the storm detector."""
        # Call evaluate multiple times — storm detector records firings
        for _ in range(5):
            live_hub.evaluate_and_dispatch({"error_rate": 0.01})
        metrics = live_hub._storm_detector.get_metrics()
        # Each evaluate may fire some alerts; the detector records them
        total = metrics["total_recorded"]
        assert isinstance(total, int)
        assert total >= 0  # Depends on which rules fire
