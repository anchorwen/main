import time
from core.observability.metrics_collector import MetricsCollector
from core.observability.metric_names import DECISIONS_TOTAL, DISPATCH_REJECTED, RECONCILIATION_BREACHED
from core.observability.audit_log import StructuredAuditLog
from core.observability.tracing import TracingContext, Span, new_trace_id
from core.observability.diagnostics_dashboard import DiagnosticsDashboard
from core.feedback.brain_performance_tracker import BrainPerformanceTracker


class TestMetricsCollector:
    def test_counter_increment(self):
        m = MetricsCollector()
        m.inc("requests")
        m.inc("requests")
        m.inc("requests", 3)
        assert m.get_counter("requests") == 5

    def test_counter_with_labels(self):
        m = MetricsCollector()
        m.inc("requests", labels={"method": "GET"})
        m.inc("requests", labels={"method": "POST"})
        m.inc("requests", labels={"method": "GET"})
        assert m.get_counter("requests", labels={"method": "GET"}) == 2
        assert m.get_counter("requests", labels={"method": "POST"}) == 1

    def test_gauge(self):
        m = MetricsCollector()
        m.gauge("temperature", 36.6)
        assert m.get_gauge("temperature") == 36.6
        m.gauge("temperature", 37.2)
        assert m.get_gauge("temperature") == 37.2

    def test_histogram(self):
        m = MetricsCollector()
        for v in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]:
            m.observe("latency", v)
        h = m.get_histogram("latency")
        assert h["count"] == 10
        assert h["min"] == 1
        assert h["max"] == 10
        assert h["mean"] == 5.5

    def test_empty_histogram(self):
        m = MetricsCollector()
        h = m.get_histogram("empty")
        assert h["count"] == 0

    def test_snapshot(self):
        m = MetricsCollector()
        m.inc("a")
        m.gauge("b", 42)
        m.observe("c", 1.5)
        s = m.snapshot()
        assert "timestamp" in s
        assert s["counters"]["a"] == 1
        assert s["gauges"]["b"] == 42
        assert s["histograms"]["c"]["count"] == 1

    def test_reset(self):
        m = MetricsCollector()
        m.inc("x")
        m.reset()
        assert m.get_counter("x") == 0


class TestStructuredAuditLog:
    def test_log_and_read(self, tmp_path):
        log = StructuredAuditLog(str(tmp_path))
        log.log(event_type="test_event", severity="info", actor="tester", detail={"key": "val"})
        log.log(event_type="another_event", severity="warning")

        entries = log.read_entries()
        assert len(entries) == 2
        assert entries[0]["event_type"] == "test_event"
        assert entries[0]["detail"]["key"] == "val"

    def test_log_decision(self, tmp_path):
        log = StructuredAuditLog(str(tmp_path))
        entry = log.log_decision(
            intent_id="i1", verdict_status="allow", symbol="XAUUSD",
            action="open", risk_tier="standard",
        )
        assert entry["event_type"] == "decision_cycle"
        assert entry["detail"]["intent_id"] == "i1"

    def test_log_dispatch(self, tmp_path):
        log = StructuredAuditLog(str(tmp_path))
        entry = log.log_dispatch(
            message_id="m1", target="exec_bridge", status="delivered",
            adapter_name="stub", trace_id="t1",
        )
        assert entry["event_type"] == "communication_dispatch"
        assert entry["trace_id"] == "t1"

    def test_log_risk_verdict_with_blocking(self, tmp_path):
        log = StructuredAuditLog(str(tmp_path))
        entry = log.log_risk_verdict(
            intent_id="i1", status="deny", risk_tier="critical",
            blocking_reasons=["drawdown_limit"],
        )
        assert entry["severity"] == "warning"

    def test_log_governance_signal_freeze(self, tmp_path):
        log = StructuredAuditLog(str(tmp_path))
        entry = log.log_governance_signal(
            brain_id="brain_bad", signal_type="governance_action_required",
            recommendation="freeze", health_signal="critical",
        )
        assert entry["severity"] == "critical"

    def test_log_reconciliation_breach(self, tmp_path):
        log = StructuredAuditLog(str(tmp_path))
        entry = log.log_reconciliation(
            message_id="m1", status="breached",
            mismatches=[{"type": "quantity_mismatch"}],
        )
        assert entry["severity"] == "error"
        assert entry["detail"]["mismatch_count"] == 1

    def test_read_by_date(self, tmp_path):
        log = StructuredAuditLog(str(tmp_path))
        log.log(event_type="e1")
        entries = log.read_entries(date_key=entries[0]["timestamp"][:10] if (entries := log.read_entries()) else None)
        assert len(entries) >= 1


class TestTracingContext:
    def test_create_trace(self):
        ctx = TracingContext()
        assert len(ctx.trace_id) == 32
        assert ctx.get_spans() == []

    def test_start_and_end_span(self):
        ctx = TracingContext()
        span = ctx.start_span("decision_cycle")
        span.set_attribute("symbol", "XAUUSD")
        time.sleep(0.02)
        ctx.end_span(span)
        spans = ctx.get_spans()
        assert len(spans) == 1
        assert spans[0]["name"] == "decision_cycle"
        assert spans[0]["duration_ms"] > 0
        assert spans[0]["attributes"]["symbol"] == "XAUUSD"

    def test_nested_spans(self):
        ctx = TracingContext()
        root = ctx.start_span("cycle")
        child = ctx.start_span("risk_eval")
        child.set_attribute("policy_count", 3)
        ctx.end_span(child)
        ctx.end_span(root)
        spans = ctx.get_spans()
        assert len(spans) == 2
        assert spans[1]["parent_span_id"] == spans[0]["span_id"]

    def test_span_events(self):
        ctx = TracingContext()
        span = ctx.start_span("dispatch")
        span.add_event("adapter_selected", {"adapter": "stub"})
        span.add_event("transport_delivered")
        ctx.end_span(span)
        assert len(ctx.get_spans()[0]["events"]) == 2

    def test_span_error(self):
        ctx = TracingContext()
        span = ctx.start_span("failing_op")
        span.set_error("connection timeout")
        ctx.end_span(span)
        assert ctx.get_spans()[0]["status"] == "error"

    def test_trace_summary(self):
        ctx = TracingContext()
        root = ctx.start_span("cycle")
        ctx.start_span("sub")
        ctx.end_span(ctx.current_span)
        ctx.end_span(root)
        summary = ctx.get_trace_summary()
        assert summary["span_count"] == 2
        assert summary["error_count"] == 0
        assert summary["root_span"] == "cycle"


class TestDiagnosticsDashboard:
    def test_full_snapshot(self, tmp_path):
        metrics = MetricsCollector()
        metrics.inc(DECISIONS_TOTAL, 50)
        metrics.inc(DISPATCH_REJECTED, 2)
        metrics.gauge("open_positions", 5)
        metrics.observe("cycle_time_ms", 12.5)

        audit = StructuredAuditLog(str(tmp_path / "audit"))
        audit.log(event_type="decision_cycle", severity="info")
        audit.log(event_type="risk_verdict", severity="warning")

        tracker = BrainPerformanceTracker()
        for _ in range(15):
            tracker.record_outcome("alpha_v1", {"composite_score": 0.85, "execution_outcome": "success"})
        for _ in range(12):
            tracker.record_outcome("bad_brain", {"composite_score": 0.1, "execution_outcome": "breach"})

        dash = DiagnosticsDashboard(
            metrics_collector=metrics,
            audit_log=audit,
            brain_performance_tracker=tracker,
        )
        snap = dash.build_snapshot()

        assert "generated_at" in snap
        assert snap["metrics"]["counters"][DECISIONS_TOTAL] == 50
        assert snap["metrics"]["gauges"]["open_positions"] == 5
        assert snap["brain_health"]["brain_count"] == 2
        assert snap["brain_health"]["healthy_count"] == 1
        assert snap["brain_health"]["degraded_count"] == 1
        assert snap["audit_summary"]["entry_count"] == 2
        assert snap["audit_summary"]["severity_counts"]["info"] == 1
        assert snap["audit_summary"]["severity_counts"]["warning"] == 1

        alerts = snap["alerts"]
        critical_alerts = [a for a in alerts if a["level"] == "critical"]
        assert len(critical_alerts) >= 1
        assert critical_alerts[0]["brain_id"] == "bad_brain"

    def test_snapshot_without_components(self):
        dash = DiagnosticsDashboard()
        snap = dash.build_snapshot()
        assert snap["metrics"] is None
        assert snap["brain_health"] is None
        assert snap["audit_summary"] is None
        assert snap["alerts"] == []

    def test_reconciliation_breach_alert(self):
        metrics = MetricsCollector()
        metrics.inc(RECONCILIATION_BREACHED, 3)
        dash = DiagnosticsDashboard(metrics_collector=metrics)
        snap = dash.build_snapshot()
        breach_alerts = [a for a in snap["alerts"] if a["source"] == "reconciliation"]
        assert len(breach_alerts) == 1
        assert "3" in breach_alerts[0]["message"]
