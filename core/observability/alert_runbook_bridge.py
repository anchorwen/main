"""Bridge between alert service and runbook engine.

When an alert fires, the bridge looks up the corresponding runbook actions
and attaches actionable SOP recommendations to the alert payload.
This turns raw alerts into operator-ready incident response cards.

Usage:
    from core.observability.alert_runbook_bridge import AlertRunbookBridge

    bridge = AlertRunbookBridge.with_default_mappings()
    enriched_alert = bridge.enrich(alert, context)
    # enriched_alert now includes: runbook_actions, diagnostic_steps, escalation_path
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class RunbookAction:
    """One step in a runbook procedure."""

    order: int
    action: str
    description: str
    priority: str = "P1"  # P0 (immediate) to P3 (advisory)
    owner: str = "operator"
    verify: str = ""  # how to confirm the action worked

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "action": self.action,
            "description": self.description,
            "priority": self.priority,
            "owner": self.owner,
            "verify": self.verify,
        }


@dataclass
class RunbookSOP:
    """A full standard operating procedure for an alert type."""

    alert_name: str
    title: str
    severity: str
    summary: str
    actions: list[RunbookAction] = field(default_factory=list)
    escalation_path: list[str] = field(default_factory=list)
    diagnostic_commands: list[str] = field(default_factory=list)
    rollback_steps: list[RunbookAction] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_name": self.alert_name,
            "title": self.title,
            "severity": self.severity,
            "summary": self.summary,
            "actions": [a.to_dict() for a in self.actions],
            "escalation_path": self.escalation_path,
            "diagnostic_commands": self.diagnostic_commands,
            "rollback_steps": [a.to_dict() for a in self.rollback_steps],
        }


# ── Default SOP library ───────────────────────────────────────────────────────


DEFAULT_SOP_LIBRARY: dict[str, RunbookSOP] = {
    "high_error_rate": RunbookSOP(
        alert_name="high_error_rate",
        title="Elevated Error Rate Detected",
        severity="critical",
        summary="System-wide error rate exceeds 10% threshold. Immediate investigation required.",
        actions=[
            RunbookAction(
                1,
                "CHECK_DASHBOARD",
                "Open Grafana error-rate dashboard",
                priority="P0",
                verify="Error rate visible and confirmed",
            ),
            RunbookAction(
                2,
                "INSPECT_LOGS",
                "Run `tail -200 logs/live_error.log` to identify error pattern",
                priority="P0",
                verify="Root cause error identified",
            ),
            RunbookAction(
                3,
                "CHECK_MT5_CONNECTION",
                "Verify MT5 terminal is responsive and connected",
                priority="P0",
                verify="MT5 heartbeat OK",
            ),
            RunbookAction(
                4,
                "CHECK_BRAIN_HEALTH",
                "Run health check on active brains",
                priority="P1",
                verify="All brains returning valid signals",
            ),
        ],
        escalation_path=["oncall_quant", "lead_engineer", "ceo"],
        diagnostic_commands=[
            "python scripts/live_auto_healthcheck.py",
            "python main.py status",
            "tail -100 logs/live_error.log",
        ],
        rollback_steps=[
            RunbookAction(1, "ROLLBACK_BRAIN", "Switch to previous brain version", priority="P0"),
            RunbookAction(
                2, "RESTART_ENGINE", "Controlled restart of engine process", priority="P1"
            ),
        ],
    ),
    "circuit_breaker_open": RunbookSOP(
        alert_name="circuit_breaker_open",
        title="Circuit Breaker Engaged",
        severity="critical",
        summary="Position/risk circuit breaker has opened. Trading halted.",
        actions=[
            RunbookAction(
                1,
                "ASSESS_EXPOSURE",
                "Review current positions and PnL",
                priority="P0",
                verify="Exposure report generated",
            ),
            RunbookAction(
                2,
                "CHECK_RISK_LIMITS",
                "Verify which risk limit was breached",
                priority="P0",
                verify="Breached limit identified",
            ),
            RunbookAction(
                3,
                "DECIDE_RESUME",
                "Determine if safe to resume or stay halted",
                priority="P0",
                verify="Decision documented in audit log",
            ),
        ],
        escalation_path=["risk_officer", "lead_engineer", "ceo"],
        diagnostic_commands=[
            "python scripts/live_daily_recap.py",
            "python main.py status",
        ],
        rollback_steps=[
            RunbookAction(
                1,
                "MANUAL_RESET",
                "Operator manually resets circuit breaker via admin console",
                priority="P0",
            ),
        ],
    ),
    "high_throttle_rate": RunbookSOP(
        alert_name="high_throttle_rate",
        title="High Signal Throttle Rate",
        severity="warning",
        summary="Over 30% of signals are being throttled. Strategy or regime may be overactive.",
        actions=[
            RunbookAction(
                1,
                "REVIEW_THROTTLE_LOG",
                "Check which strategies are being throttled",
                priority="P1",
                verify="Throttle source identified",
            ),
            RunbookAction(
                2,
                "CHECK_REGIME",
                "Verify regime detection is stable",
                priority="P2",
                verify="Regime not oscillating",
            ),
            RunbookAction(
                3,
                "ADJUST_COOLDOWN",
                "If needed, increase cooldown_bars in live config",
                priority="P2",
                verify="Throttle rate drops below 20%",
            ),
        ],
        escalation_path=["oncall_quant"],
        diagnostic_commands=[
            "python main.py status",
            "grep throttle logs/live_warning.log | tail -50",
        ],
        rollback_steps=[
            RunbookAction(
                1, "RESTORE_CONFIG", "Revert live.yaml to last known good", priority="P1"
            ),
        ],
    ),
    "brain_frozen": RunbookSOP(
        alert_name="brain_frozen",
        title="Brain Model Frozen",
        severity="warning",
        summary="One or more brain models are frozen (not producing updated signals).",
        actions=[
            RunbookAction(
                1,
                "IDENTIFY_FROZEN_BRAIN",
                "Check which brain ID is frozen",
                priority="P1",
                verify="Frozen brain ID confirmed",
            ),
            RunbookAction(
                2,
                "CHECK_MODEL_LOAD",
                "Verify ONNX model loaded successfully",
                priority="P1",
                verify="Model file present and valid",
            ),
            RunbookAction(
                3,
                "RELOAD_OR_FAILOVER",
                "Reload brain or fail over to shadow brain",
                priority="P1",
                verify="Brain producing signals again",
            ),
        ],
        escalation_path=["oncall_quant", "ml_engineer"],
        diagnostic_commands=[
            "python main.py status",
            "python scripts/live_auto_healthcheck.py",
        ],
        rollback_steps=[
            RunbookAction(
                1, "SWITCH_BRAIN", "Activate fallback brain in live config", priority="P0"
            ),
        ],
    ),
    "daily_loss_exceeded": RunbookSOP(
        alert_name="daily_loss_exceeded",
        title="Daily Loss Limit Exceeded",
        severity="critical",
        summary="Daily PnL has breached the loss limit threshold. Immediate risk control required.",
        actions=[
            RunbookAction(
                1,
                "CLOSE_ALL_POSITIONS",
                "Immediately close all open positions to prevent further losses",
                priority="P0",
                verify="All positions closed, MT5 shows zero exposure",
            ),
            RunbookAction(
                2,
                "SUSPEND_TRADING",
                "Pause automated trading for the remainder of the day",
                priority="P0",
                verify="Trading flag set to blocked, no new orders firing",
            ),
            RunbookAction(
                3,
                "NOTIFY_RISK_OFFICER",
                "Alert risk management team with PnL summary",
                priority="P0",
                verify="Risk officer acknowledged receipt",
            ),
            RunbookAction(
                4,
                "POST_MORTEM",
                "Review trade journal to identify loss drivers",
                priority="P1",
                verify="Root cause identified and documented",
            ),
        ],
        escalation_path=["risk_officer", "lead_engineer", "ceo"],
        diagnostic_commands=[
            "python scripts/live_daily_recap.py",
            "python main.py status",
        ],
        rollback_steps=[
            RunbookAction(
                1, "MANUAL_RESUME", "Risk officer manually clears trading block", priority="P0"
            ),
        ],
    ),
    "win_rate_collapse": RunbookSOP(
        alert_name="win_rate_collapse",
        title="Win Rate Collapse Detected",
        severity="critical",
        summary="Rolling win rate has dropped below critical threshold. Strategy may be broken.",
        actions=[
            RunbookAction(
                1,
                "FREEZE_ALL_BRAINS",
                "Freeze all active trading brains to prevent further degraded signals",
                priority="P0",
                verify="All brains status set to frozen in governance",
            ),
            RunbookAction(
                2,
                "TRIGGER_RETRAIN",
                "Initiate brain retraining pipeline with recent data",
                priority="P0",
                verify="Retraining job started, check training logs",
            ),
            RunbookAction(
                3,
                "NOTIFY_ML_ENGINEER",
                "Alert ML engineer to investigate model degradation",
                priority="P0",
                verify="Engineer acknowledged and investigating",
            ),
            RunbookAction(
                4,
                "ASSESS_MARKET_REGIME",
                "Check if market regime shift caused the collapse",
                priority="P1",
                verify="Regime analysis documented",
            ),
        ],
        escalation_path=["ml_engineer", "lead_engineer", "ceo"],
        diagnostic_commands=[
            "python main.py status",
            "python scripts/live_daily_recap.py",
            "python scripts/brain.py --diagnose",
        ],
        rollback_steps=[
            RunbookAction(
                1,
                "RESTORE_PREVIOUS_BRAIN",
                "Roll back to last known good brain version",
                priority="P0",
            ),
            RunbookAction(2, "RESUME_TRADING", "Unfreeze brains after validation", priority="P1"),
        ],
    ),
    "strategy_degradation": RunbookSOP(
        alert_name="strategy_degradation",
        title="Strategy Performance Degradation",
        severity="warning",
        summary="A specific strategy is underperforming — both PnL negative and win rate below threshold.",
        actions=[
            RunbookAction(
                1,
                "IDENTIFY_STRATEGY",
                "Identify which specific strategy/brain is degraded",
                priority="P1",
                verify="Degraded strategy brain_id confirmed",
            ),
            RunbookAction(
                2,
                "REDUCE_WEIGHT",
                "Reduce vote weight of degraded brain in Parliament",
                priority="P1",
                verify="Brain weight reduced, governance state updated",
            ),
            RunbookAction(
                3,
                "MARK_FOR_REVIEW",
                "Flag brain for manual review in governance",
                priority="P2",
                verify="Brain status set to probation",
            ),
            RunbookAction(
                4,
                "MONITOR",
                "Continue monitoring — escalate if degradation worsens",
                priority="P2",
                verify="Alert cleared or escalated within 24h",
            ),
        ],
        escalation_path=["oncall_quant", "ml_engineer"],
        diagnostic_commands=[
            "python main.py status",
            "python scripts/brain.py --diagnose",
        ],
        rollback_steps=[
            RunbookAction(
                1, "RESTORE_WEIGHT", "Restore brain weight after review clearance", priority="P2"
            ),
        ],
    ),
    "position_limit_near": RunbookSOP(
        alert_name="position_limit_near",
        title="Near Position Limit",
        severity="warning",
        summary="Position utilization exceeds 80%. Risk of hitting hard limits.",
        actions=[
            RunbookAction(
                1,
                "REVIEW_POSITIONS",
                "Check current open positions and exposures",
                priority="P1",
                verify="Position summary reviewed",
            ),
            RunbookAction(
                2,
                "REDUCE_SIZING",
                "Temporarily reduce position sizes if warranted",
                priority="P2",
                verify="Utilization drops below 70%",
            ),
        ],
        escalation_path=["risk_officer"],
        diagnostic_commands=[
            "python main.py status",
            "python scripts/live_daily_recap.py",
        ],
        rollback_steps=[],
    ),
}


# ── Bridge ────────────────────────────────────────────────────────────────────


class AlertRunbookBridge:
    """Enriches alerts with runbook SOP actions.

    When an alert fires, the bridge attaches the corresponding runbook
    to the alert payload so the operator receives actionable steps
    alongside the notification.
    """

    def __init__(self, sop_library: dict[str, RunbookSOP] | None = None) -> None:
        self._library = dict(sop_library) if sop_library else {}

    def register_sop(self, sop: RunbookSOP) -> None:
        self._library[sop.alert_name] = sop

    def get_sop(self, alert_name: str) -> RunbookSOP | None:
        return self._library.get(alert_name)

    def enrich(
        self, alert: dict[str, Any], context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Attach runbook actions to an alert payload.

        Returns a new dict (does not mutate the original).
        """
        enriched = dict(alert)
        rule_name = alert.get("rule_name", "")

        sop = self._library.get(rule_name)
        if sop is None:
            enriched["runbook"] = {
                "available": False,
                "message": f"No SOP defined for alert: {rule_name}",
            }
            return enriched

        enriched["runbook"] = {
            "available": True,
            "title": sop.title,
            "summary": sop.summary,
            "actions": [a.to_dict() for a in sop.actions],
            "escalation_path": sop.escalation_path,
            "diagnostic_commands": sop.diagnostic_commands,
            "rollback_steps": [a.to_dict() for a in sop.rollback_steps],
        }

        if context:
            enriched["runbook"]["context_summary"] = {
                k: v for k, v in context.items() if isinstance(v, str | int | float | bool)
            }

        enriched["runbook"]["generated_at"] = datetime.now(UTC).replace(tzinfo=None).isoformat()
        return enriched

    def enrich_batch(
        self, alerts: list[dict[str, Any]], context: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Enrich a list of fired alerts with their respective runbooks."""
        return [self.enrich(alert, context) for alert in alerts]

    @property
    def sop_names(self) -> list[str]:
        return sorted(self._library.keys())

    @classmethod
    def with_default_mappings(cls) -> AlertRunbookBridge:
        """Create a bridge pre-loaded with the default SOP library."""
        return cls(sop_library=DEFAULT_SOP_LIBRARY)


# ── Integration helper ────────────────────────────────────────────────────────


def enrich_alert_with_runbook(
    alert: dict[str, Any],
    context: dict[str, Any] | None = None,
    *,
    bridge: AlertRunbookBridge | None = None,
) -> dict[str, Any]:
    """Convenience function: enrich a single alert with SOP runbook actions.

    Uses the default SOP library if no bridge is provided.
    """
    b = bridge or AlertRunbookBridge.with_default_mappings()
    return b.enrich(alert, context)
