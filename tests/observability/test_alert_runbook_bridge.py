"""Tests for alert-to-runbook bridge."""

from __future__ import annotations

import pytest

from core.observability.alert_runbook_bridge import (
    AlertRunbookBridge,
    RunbookAction,
    RunbookSOP,
    enrich_alert_with_runbook,
)

# ── RunbookAction ─────────────────────────────────────────────────────────────


class TestRunbookAction:
    def test_to_dict(self):
        a = RunbookAction(
            order=1,
            action="CHECK_DASHBOARD",
            description="Open Grafana dashboard",
            priority="P0",
            owner="operator",
            verify="Errors confirmed",
        )
        d = a.to_dict()
        assert d["order"] == 1
        assert d["action"] == "CHECK_DASHBOARD"
        assert d["priority"] == "P0"
        assert d["verify"] == "Errors confirmed"


# ── RunbookSOP ────────────────────────────────────────────────────────────────


class TestRunbookSOP:
    def test_to_dict(self):
        sop = RunbookSOP(
            alert_name="high_error_rate",
            title="Test Alert",
            severity="critical",
            summary="Something is wrong",
            actions=[RunbookAction(1, "FIX_IT", "Fix the problem")],
            escalation_path=["oncall", "manager"],
            diagnostic_commands=["check_logs"],
            rollback_steps=[RunbookAction(1, "REVERT", "Undo change")],
        )
        d = sop.to_dict()
        assert d["alert_name"] == "high_error_rate"
        assert d["severity"] == "critical"
        assert len(d["actions"]) == 1
        assert d["escalation_path"] == ["oncall", "manager"]
        assert d["diagnostic_commands"] == ["check_logs"]


# ── AlertRunbookBridge ────────────────────────────────────────────────────────


class TestAlertRunbookBridge:
    @pytest.fixture
    def bridge(self):
        return AlertRunbookBridge.with_default_mappings()

    def test_default_mappings_cover_all_five(self, bridge):
        """All 8 default alert rules should have SOP coverage."""
        expected = {
            "high_error_rate",
            "circuit_breaker_open",
            "high_throttle_rate",
            "brain_frozen",
            "position_limit_near",
            "daily_loss_exceeded",
            "win_rate_collapse",
            "strategy_degradation",
        }
        assert set(bridge.sop_names) == expected

    def test_enrich_adds_runbook_to_alert(self, bridge):
        alert = {
            "rule_name": "high_error_rate",
            "severity": "critical",
            "fired_at": "2026-05-01T12:00:00",
        }
        enriched = bridge.enrich(alert)
        assert enriched["rule_name"] == "high_error_rate"
        assert enriched["runbook"]["available"] is True
        assert enriched["runbook"]["title"] == "Elevated Error Rate Detected"
        assert len(enriched["runbook"]["actions"]) == 4
        assert "escalation_path" in enriched["runbook"]
        assert len(enriched["runbook"]["diagnostic_commands"]) == 3

    def test_enrich_unknown_alert_returns_unavailable(self, bridge):
        alert = {"rule_name": "unknown_alert", "severity": "info"}
        enriched = bridge.enrich(alert)
        assert enriched["runbook"]["available"] is False
        assert "No SOP defined" in enriched["runbook"]["message"]

    def test_enrich_does_not_mutate_original(self, bridge):
        alert = {"rule_name": "high_error_rate", "severity": "critical"}
        original_keys = set(alert.keys())
        bridge.enrich(alert)
        assert set(alert.keys()) == original_keys
        assert "runbook" not in alert

    def test_enrich_with_context(self, bridge):
        alert = {"rule_name": "brain_frozen", "severity": "warning"}
        context = {"frozen_brain_count": 2, "active_brains": 3}
        enriched = bridge.enrich(alert, context)
        assert enriched["runbook"]["context_summary"]["frozen_brain_count"] == 2

    def test_enrich_batch(self, bridge):
        alerts = [
            {"rule_name": "high_error_rate", "severity": "critical"},
            {"rule_name": "brain_frozen", "severity": "warning"},
        ]
        enriched_list = bridge.enrich_batch(alerts)
        assert len(enriched_list) == 2
        assert enriched_list[0]["runbook"]["available"] is True
        assert enriched_list[1]["runbook"]["available"] is True

    def test_register_custom_sop(self, bridge):
        custom = RunbookSOP(
            alert_name="custom_alert",
            title="Custom Alert",
            severity="warning",
            summary="Custom SOP",
            actions=[RunbookAction(1, "DO_THING", "Do the thing")],
        )
        bridge.register_sop(custom)
        assert "custom_alert" in bridge.sop_names

        alert = {"rule_name": "custom_alert", "severity": "warning"}
        enriched = bridge.enrich(alert)
        assert enriched["runbook"]["available"] is True
        assert enriched["runbook"]["title"] == "Custom Alert"


# ── Per-SOP content validation ────────────────────────────────────────────────


class TestSOPContent:
    @pytest.fixture
    def bridge(self):
        return AlertRunbookBridge.with_default_mappings()

    def test_circuit_breaker_sop(self, bridge):
        enriched = bridge.enrich({"rule_name": "circuit_breaker_open"})
        r = enriched["runbook"]
        assert r["title"] == "Circuit Breaker Engaged"
        assert len(r["actions"]) >= 2
        assert "risk_officer" in r["escalation_path"]

    def test_high_throttle_sop(self, bridge):
        enriched = bridge.enrich({"rule_name": "high_throttle_rate"})
        r = enriched["runbook"]
        assert r["available"] is True
        assert len(r["actions"]) >= 2

    def test_brain_frozen_sop(self, bridge):
        enriched = bridge.enrich({"rule_name": "brain_frozen"})
        r = enriched["runbook"]
        assert len(r["rollback_steps"]) >= 1
        assert r["rollback_steps"][0]["action"] == "SWITCH_BRAIN"

    def test_position_limit_sop(self, bridge):
        enriched = bridge.enrich({"rule_name": "position_limit_near"})
        r = enriched["runbook"]
        assert r["rollback_steps"] == []
        assert len(r["actions"]) >= 1


# ── Convenience function ──────────────────────────────────────────────────────


class TestEnrichAlertWithRunbook:
    def test_convenience_function(self):
        alert = {"rule_name": "high_error_rate", "severity": "critical"}
        enriched = enrich_alert_with_runbook(alert)
        assert enriched["runbook"]["available"] is True

    def test_convenience_function_unknown_alert(self):
        alert = {"rule_name": "nonexistent"}
        enriched = enrich_alert_with_runbook(alert)
        assert enriched["runbook"]["available"] is False
