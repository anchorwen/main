"""Tests for SeverityRouter and BatchingAlertChannel in alert_service.py."""

from __future__ import annotations

from core.observability.alert_service import (
    BatchingAlertChannel,
    InMemoryAlertChannel,
    SeverityRouter,
)


class TestBatchingAlertChannel:
    def test_critical_passes_through_immediately(self):
        mem = InMemoryAlertChannel()
        batcher = BatchingAlertChannel(mem)
        batcher.send({"severity": "critical", "rule_name": "test_crit"})
        assert len(mem.get_alerts()) == 1
        assert mem.get_alerts()[0]["rule_name"] == "test_crit"

    def test_error_passes_through_immediately(self):
        mem = InMemoryAlertChannel()
        batcher = BatchingAlertChannel(mem)
        batcher.send({"severity": "error", "rule_name": "test_err"})
        assert len(mem.get_alerts()) == 1

    def test_info_queued_not_sent(self):
        mem = InMemoryAlertChannel()
        batcher = BatchingAlertChannel(mem)
        batcher.send({"severity": "info", "rule_name": "test_info"})
        # Not yet flushed — only 0 delivered to underlying channel
        assert len(mem.get_alerts()) == 0

    def test_flush_delivers_batched(self):
        mem = InMemoryAlertChannel()
        batcher = BatchingAlertChannel(mem)
        batcher.send({"severity": "warning", "rule_name": "w1"})
        batcher.send({"severity": "info", "rule_name": "i1"})
        batcher.flush()
        assert len(mem.get_alerts()) == 1
        batched = mem.get_alerts()[0]
        assert batched["rule_name"] == "batched_alerts"
        assert batched["context_snapshot"]["batched_count"] == 2

    def test_auto_flush_at_max_batch(self):
        mem = InMemoryAlertChannel()
        batcher = BatchingAlertChannel(mem, max_batch_size=3)
        for i in range(3):
            batcher.send({"severity": "warning", "rule_name": f"w{i}"})
        # Should auto-flush at 3
        assert len(mem.get_alerts()) == 1


class TestSeverityRouter:
    def test_routes_to_correct_channel(self):
        critical_ch = InMemoryAlertChannel()
        warning_ch = InMemoryAlertChannel()
        router = SeverityRouter({"critical": critical_ch, "warning": warning_ch})

        router.send({"severity": "critical", "rule_name": "panic"})
        assert len(critical_ch.get_alerts()) == 1
        assert len(warning_ch.get_alerts()) == 0

        router.send({"severity": "warning", "rule_name": "heads_up"})
        assert len(warning_ch.get_alerts()) == 1

    def test_falls_back_to_default(self):
        default_ch = InMemoryAlertChannel()
        router = SeverityRouter({}, default=default_ch)
        router.send({"severity": "info", "rule_name": "info_msg"})
        assert len(default_ch.get_alerts()) == 1

    def test_no_route_no_default_returns_false(self):
        router = SeverityRouter({})
        result = router.send({"severity": "info", "rule_name": "no_route"})
        assert result is False
