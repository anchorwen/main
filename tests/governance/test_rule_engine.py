"""Tests for core.governance.governance_rule_engine — declarative rule engine.

Covers:
  - GovernanceRule: matches() and execute()
  - GovernanceRuleEngine: add_rule(), _most_severe(), evaluate(), execute_transitions()
  - with_default_rules(): each built-in rule's condition/action

Zero I/O.  Uses SimpleNamespace for mock governance_service and
unittest.mock.MagicMock for audit_log (both stdlib).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.governance.governance_rule_engine import (
    GovernanceRule,
    GovernanceRuleEngine,
)


# ── GovernanceRule ──────────────────────────────────────────────────────────


def test_rule_matches_true():
    """Rule.matches() returns True when condition_fn returns True."""
    rule = GovernanceRule(
        name="test_rule",
        condition_fn=lambda ctx: ctx.get("health") == "bad",
        action_fn=lambda ctx: {"transition_to": "frozen"},
        priority=100,
    )
    assert rule.matches({"health": "bad"}) is True


def test_rule_matches_false():
    """Rule.matches() returns False when condition_fn returns False."""
    rule = GovernanceRule(
        name="test_rule",
        condition_fn=lambda ctx: ctx.get("health") == "bad",
        action_fn=lambda ctx: {"transition_to": "frozen"},
    )
    assert rule.matches({"health": "good"}) is False


def test_rule_execute_returns_action_result():
    """Rule.execute() invokes action_fn and returns its result."""
    rule = GovernanceRule(
        name="freeze",
        condition_fn=lambda ctx: True,
        action_fn=lambda ctx: {"transition_to": "frozen", "reason": "bad sharpe"},
    )
    result = rule.execute({"sharpe": -5.0})
    assert result == {"transition_to": "frozen", "reason": "bad sharpe"}


def test_rule_default_priority_zero():
    """GovernanceRule defaults priority to 0."""
    rule = GovernanceRule(
        name="low",
        condition_fn=lambda ctx: True,
        action_fn=lambda ctx: {},
    )
    assert rule.priority == 0


# ── GovernanceRuleEngine._most_severe ───────────────────────────────────────


def test_most_severe_empty_returns_none():
    """_most_severe of empty list returns None."""
    assert GovernanceRuleEngine._most_severe([]) is None


def test_most_severe_single_result():
    """_most_severe of a single result returns that result."""
    results = [{"transition_to": "frozen"}]
    chosen = GovernanceRuleEngine._most_severe(results)
    assert chosen is not None
    assert chosen["transition_to"] == "frozen"


def test_most_severe_picks_highest_severity():
    """retired(5) > frozen(4) > probation(3) > live(2) > candidate(1)."""
    results = [
        {"transition_to": "live"},
        {"transition_to": "frozen"},
        {"transition_to": "probation"},
        {"transition_to": "retired"},
    ]
    chosen = GovernanceRuleEngine._most_severe(results)
    assert chosen is not None
    assert chosen["transition_to"] == "retired"


def test_most_severe_unknown_target_gets_zero():
    """An unknown transition_to maps to severity 0."""
    results = [
        {"transition_to": "bogus"},
        {"transition_to": "candidate"},  # severity 1
    ]
    chosen = GovernanceRuleEngine._most_severe(results)
    assert chosen is not None
    assert chosen["transition_to"] == "candidate"


# ── GovernanceRuleEngine.add_rule ───────────────────────────────────────────


def test_add_rule_sorts_by_priority_descending():
    """Rules are sorted highest-priority first after each add_rule()."""
    svc = SimpleNamespace(get_brain_state=lambda bid: None, transition=lambda bid, s, r: {})
    engine = GovernanceRuleEngine(svc)
    engine.add_rule(GovernanceRule("low", lambda c: True, lambda c: {}, priority=10))
    engine.add_rule(GovernanceRule("high", lambda c: True, lambda c: {}, priority=100))
    engine.add_rule(GovernanceRule("mid", lambda c: True, lambda c: {}, priority=50))
    assert [r.name for r in engine._rules] == ["high", "mid", "low"]


# ── GovernanceRuleEngine.evaluate ───────────────────────────────────────────


def test_evaluate_empty_brain_summaries_returns_empty():
    """No brain summaries → no rules fired."""
    svc = SimpleNamespace(get_brain_state=lambda bid: None, transition=lambda bid, s, r: {})
    engine = GovernanceRuleEngine(svc)
    assert engine.evaluate({}) == []


def test_evaluate_no_matching_rules_returns_empty():
    """Brain summaries exist but no rule matches → still empty fired list."""
    svc = SimpleNamespace(get_brain_state=lambda bid: None, transition=lambda bid, s, r: {})
    engine = GovernanceRuleEngine(svc)
    engine.add_rule(
        GovernanceRule(
            "never_matches",
            condition_fn=lambda ctx: False,
            action_fn=lambda ctx: {"transition_to": "frozen"},
            priority=100,
        )
    )
    result = engine.evaluate({"B1": {"sharpe_ratio": -0.5}})
    assert result == []


def test_evaluate_single_rule_fires_and_transitions():
    """A matching rule triggers a state transition."""
    states = {"B1": {"status": "live", "freeze_count": 0}}
    transitions = []

    def _get_state(bid):
        return states.get(bid)

    def _transition(bid, new_status, reason=""):
        transitions.append((bid, new_status, reason))
        return {"action": "transitioned", "brain_id": bid, "from": "live", "to": new_status}

    svc = SimpleNamespace(get_brain_state=_get_state, transition=_transition)
    engine = GovernanceRuleEngine(svc)
    engine.add_rule(
        GovernanceRule(
            "freeze_bad",
            condition_fn=lambda ctx: ctx.get("health_signal") == "critical",
            action_fn=lambda ctx: {"transition_to": "frozen", "reason": "test"},
            priority=100,
        )
    )
    fired = engine.evaluate({"B1": {"health_signal": "critical", "sample_count": 15}})
    assert len(fired) == 1
    assert fired[0]["transition_to"] == "frozen"
    assert len(transitions) == 1
    assert transitions[0][:2] == ("B1", "frozen")


def test_evaluate_multiple_rules_picks_most_severe():
    """When multiple rules fire for the same brain, only the most severe
    transition is applied."""
    states = {"B1": {"status": "live", "freeze_count": 0}}

    def _transition(bid, new_status, reason=""):
        return {"action": "transitioned", "brain_id": bid, "from": "live", "to": new_status}

    svc = SimpleNamespace(get_brain_state=lambda bid: states.get(bid), transition=_transition)
    engine = GovernanceRuleEngine(svc)
    engine.add_rule(
        GovernanceRule(
            "probation_rule",
            condition_fn=lambda ctx: True,
            action_fn=lambda ctx: {"transition_to": "probation"},
            priority=50,
        )
    )
    engine.add_rule(
        GovernanceRule(
            "retire_rule",
            condition_fn=lambda ctx: True,
            action_fn=lambda ctx: {"transition_to": "retired"},
            priority=120,
        )
    )

    fired = engine.evaluate({"B1": {"health_signal": "degraded"}})
    assert len(fired) == 1
    assert fired[0]["transition_to"] == "retired"


def test_evaluate_rejected_transition_logs_warning():
    """When transition is rejected by the state machine, only the most severe
    matching result appears in fired (one entry per brain)."""
    def _transition(bid, new_status, reason=""):
        return {"action": "rejected", "reason": "invalid"}

    svc = SimpleNamespace(
        get_brain_state=lambda bid: {"status": "live", "freeze_count": 0},
        transition=_transition,
    )
    engine = GovernanceRuleEngine(svc)
    engine.add_rule(
        GovernanceRule(
            "test",
            condition_fn=lambda ctx: True,
            action_fn=lambda ctx: {"transition_to": "frozen"},
            priority=100,
        )
    )
    fired = engine.evaluate({"B1": {"health_signal": "ok"}})
    assert len(fired) == 1
    assert fired[0]["transition_to"] == "frozen"


def test_evaluate_with_audit_log():
    """When audit_log is provided, rule firings are logged."""
    audit = MagicMock()
    def _transition(bid, new_status, reason=""):
        return {"action": "transitioned", "from": "live", "to": new_status}

    svc = SimpleNamespace(
        get_brain_state=lambda bid: {"status": "live", "freeze_count": 0},
        transition=_transition,
    )
    engine = GovernanceRuleEngine(svc, audit_log=audit)
    engine.add_rule(
        GovernanceRule(
            "audit_test",
            condition_fn=lambda ctx: True,
            action_fn=lambda ctx: {"transition_to": "frozen"},
            priority=100,
        )
    )
    engine.evaluate({"B1": {"health_signal": "ok"}})
    assert audit.log_governance_signal.called


def test_evaluate_brain_not_registered_skipped():
    """When get_brain_state returns None, the brain's current_status is None."""
    def _transition(bid, new_status, reason=""):
        return {"action": "transitioned"}

    svc = SimpleNamespace(
        get_brain_state=lambda bid: None,
        transition=_transition,
    )
    engine = GovernanceRuleEngine(svc)
    engine.add_rule(
        GovernanceRule(
            "only_frozen_triggers",
            condition_fn=lambda ctx: ctx.get("current_status") == "frozen",
            action_fn=lambda ctx: {"transition_to": "retired"},
            priority=100,
        )
    )
    # Brain not registered → current_status is None → rule should not fire
    fired = engine.evaluate({"B99": {"health_signal": "ok"}})
    assert fired == []


# ── GovernanceRuleEngine.execute_transitions ────────────────────────────────


def test_execute_transitions_dry_run():
    """dry_run=True logs what would happen without writing."""
    svc = SimpleNamespace(
        get_brain_state=lambda bid: {"status": "live"} if bid == "B1" else None,
        transition=MagicMock(),
    )
    engine = GovernanceRuleEngine(svc)
    report = [
        SimpleNamespace(
            brain_id="B1",
            approved=True,
            target_status="frozen",
            action="freeze",
            reasons=["bad sharpe"],
        ),
    ]
    changes = engine.execute_transitions(report, dry_run=True)
    assert len(changes) == 1
    assert "B1" in changes[0]
    assert "live → frozen" in changes[0]
    assert not svc.transition.called


def test_execute_transitions_applies_writes():
    """Non-dry-run actually calls transition()."""
    transitions_called = []

    def _transition(bid, new_status, reason=""):
        transitions_called.append((bid, new_status, reason))
        return {"action": "transitioned"}

    svc = SimpleNamespace(
        get_brain_state=lambda bid: {"status": "live"} if bid == "B1" else None,
        transition=_transition,
    )
    engine = GovernanceRuleEngine(svc)
    report = [
        SimpleNamespace(
            brain_id="B1",
            approved=True,
            target_status="frozen",
            action="freeze",
            reasons=["test"],
        ),
    ]
    changes = engine.execute_transitions(report, dry_run=False)
    assert len(transitions_called) == 1
    assert transitions_called[0][:2] == ("B1", "frozen")


def test_execute_transitions_not_approved_skipped():
    """Decisions with approved=False are silently skipped."""
    svc = SimpleNamespace(
        get_brain_state=lambda bid: {"status": "live"},
        transition=MagicMock(),
    )
    engine = GovernanceRuleEngine(svc)
    report = [
        SimpleNamespace(
            brain_id="B1",
            approved=False,
            target_status="frozen",
            action="freeze",
            reasons=[],
        ),
    ]
    changes = engine.execute_transitions(report, dry_run=False)
    assert changes == ["no_changes"]
    assert not svc.transition.called


def test_execute_transitions_same_status_skipped():
    """If current status == target_status, no transition is attempted."""
    svc = SimpleNamespace(
        get_brain_state=lambda bid: {"status": "frozen"},
        transition=MagicMock(),
    )
    engine = GovernanceRuleEngine(svc)
    report = [
        SimpleNamespace(
            brain_id="B1",
            approved=True,
            target_status="frozen",
            action="freeze",
            reasons=["already"],
        ),
    ]
    changes = engine.execute_transitions(report, dry_run=False)
    assert changes == ["no_changes"]
    assert not svc.transition.called


def test_execute_transitions_brain_not_registered():
    """Unregistered brains produce a descriptive skip message."""
    svc = SimpleNamespace(
        get_brain_state=lambda bid: None,
        transition=MagicMock(),
    )
    engine = GovernanceRuleEngine(svc)
    report = [
        SimpleNamespace(
            brain_id="B_ghost",
            approved=True,
            target_status="frozen",
            action="freeze",
            reasons=[],
        ),
    ]
    changes = engine.execute_transitions(report, dry_run=False)
    assert any("B_ghost" in c and "not registered" in c for c in changes)


# ── with_default_rules ─────────────────────────────────────────────────────


def _make_svc(states=None):
    """Build a mock GovernanceService with given brain states."""
    state_map = states or {}

    def _get_state(bid):
        return state_map.get(bid)

    transitions = []

    def _transition(bid, status, reason=""):
        transitions.append((bid, status, reason))
        current = state_map.get(bid, {})
        old = current.get("status", "unknown") if current else "unknown"
        return {"action": "transitioned", "brain_id": bid, "from": old, "to": status}

    svc = SimpleNamespace(get_brain_state=_get_state, transition=_transition)
    return svc, transitions


def test_default_rules_auto_freeze_critical():
    """health_signal=critical with ≥10 samples → frozen."""
    svc, transitions = _make_svc({"B1": {"status": "live", "freeze_count": 0}})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate({"B1": {"health_signal": "critical", "sample_count": 15}})
    assert len(fired) >= 1
    # Most severe should be frozen (severity 4) not retired
    assert any(r["transition_to"] == "frozen" for r in fired)


def test_default_rules_auto_freeze_insufficient_samples():
    """health_signal=critical with <10 samples → no freeze."""
    svc, transitions = _make_svc({"B1": {"status": "live", "freeze_count": 0}})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate({"B1": {"health_signal": "critical", "sample_count": 5}})
    # auto_freeze_critical requires >=10 samples
    assert not any(r.get("rule_name") == "auto_freeze_critical" for r in fired)


def test_default_rules_negative_sharpe_freeze():
    """Sharpe < -1.0 with ≥50 samples and status=live → frozen."""
    svc, transitions = _make_svc({"B1": {"status": "live", "freeze_count": 0}})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate({"B1": {"sharpe_ratio": -5.0, "sample_count": 60}})
    assert any(r["rule_name"] == "auto_freeze_negative_sr" for r in fired)


def test_default_rules_negative_sharpe_borderline_skipped():
    """Sharpe -0.9 (above -1.0 threshold) → NOT frozen by sr rule."""
    svc, transitions = _make_svc({"B1": {"status": "live", "freeze_count": 0}})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate({"B1": {"sharpe_ratio": -0.9, "sample_count": 60}})
    assert not any(r.get("rule_name") == "auto_freeze_negative_sr" for r in fired)


def test_default_rules_negative_sharpe_insufficient_samples():
    """Sharpe < -1.0 but <50 samples → NOT frozen (defer to promotion pipeline)."""
    svc, transitions = _make_svc({"B1": {"status": "live", "freeze_count": 0}})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate({"B1": {"sharpe_ratio": -10.0, "sample_count": 30}})
    assert not any(r.get("rule_name") == "auto_freeze_negative_sr" for r in fired)


def test_default_rules_auto_promote_healthy():
    """health_signal=healthy + composite≥0.75 + ≥30 samples → live."""
    svc, transitions = _make_svc({"B1": {"status": "probation", "freeze_count": 0}})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate(
        {"B1": {"health_signal": "healthy", "composite_mean": 0.80, "sample_count": 40}}
    )
    assert any(r.get("rule_name") == "auto_promote_healthy" for r in fired)


def test_default_rules_auto_promote_low_composite_skipped():
    """composite < 0.75 → promotion rule should not fire."""
    svc, transitions = _make_svc({"B1": {"status": "probation", "freeze_count": 0}})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate(
        {"B1": {"health_signal": "healthy", "composite_mean": 0.50, "sample_count": 40}}
    )
    assert not any(r.get("rule_name") == "auto_promote_healthy" for r in fired)


def test_default_rules_auto_demote_degraded():
    """health_signal=degraded + status=live + ≥15 samples → probation."""
    svc, transitions = _make_svc({"B1": {"status": "live", "freeze_count": 0}})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate({"B1": {"health_signal": "degraded", "sample_count": 20}})
    assert any(r.get("rule_name") == "auto_demote_degraded" for r in fired)


def test_default_rules_probation_to_frozen():
    """status=probation + health=degraded + ≥20 samples + freeze_count<3 → frozen."""
    svc, transitions = _make_svc({"B1": {"status": "probation", "freeze_count": 1}})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate({"B1": {"health_signal": "degraded", "sample_count": 25}})
    assert any(r.get("rule_name") == "auto_demote_probation_to_frozen" for r in fired)


def test_default_rules_probation_to_frozen_excessive_freezes_skipped():
    """freeze_count >= 3 prevents further frozen → retired rule may fire instead."""
    svc, transitions = _make_svc({"B1": {"status": "probation", "freeze_count": 3}})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate({"B1": {"health_signal": "degraded", "sample_count": 25}})
    assert not any(r.get("rule_name") == "auto_demote_probation_to_frozen" for r in fired)


def test_default_rules_auto_retire_repeated_frozen():
    """freeze_count >= 3 → retired."""
    svc, transitions = _make_svc({"B1": {"status": "frozen", "freeze_count": 3}})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate({"B1": {"health_signal": "degraded"}})
    assert any(r.get("rule_name") == "auto_retire_repeated_frozen" for r in fired)


def test_default_rules_unfreeze_recovered():
    """status=frozen + health=stable → probation (unfreeze)."""
    svc, transitions = _make_svc({"B1": {"status": "frozen", "freeze_count": 2}})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate(
        {"B1": {"health_signal": "stable", "recommendation": "observe"}}
    )
    assert any(r.get("rule_name") == "unfreeze_recovered" for r in fired)


def test_default_rules_shadow_to_probation():
    """status=candidate + ≥50 shadow signals + min 5 long/5 short → probation."""
    svc, transitions = _make_svc({"B1": {"status": "candidate", "freeze_count": 0}})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate(
        {
            "B1": {
                "shadow_signal_count": 60,
                "shadow_long_count": 10,
                "shadow_short_count": 8,
                "shadow_avg_confidence": 0.65,
            }
        }
    )
    assert any(r.get("rule_name") == "auto_promote_shadow_to_probation" for r in fired)


def test_default_rules_shadow_insufficient_diversity():
    """Only 2 short signals < 5 → promotion rule should not fire."""
    svc, transitions = _make_svc({"B1": {"status": "candidate", "freeze_count": 0}})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate(
        {
            "B1": {
                "shadow_signal_count": 60,
                "shadow_long_count": 30,
                "shadow_short_count": 2,
                "shadow_avg_confidence": 0.65,
            }
        }
    )
    assert not any(r.get("rule_name") == "auto_promote_shadow_to_probation" for r in fired)


def test_default_rules_probation_to_live():
    """status=probation + ≥100 samples + stable/healthy + composite≥0.55 → live."""
    svc, transitions = _make_svc({"B1": {"status": "probation", "freeze_count": 0}})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate(
        {"B1": {"health_signal": "stable", "composite_mean": 0.60, "sample_count": 120}}
    )
    assert any(r.get("rule_name") == "auto_promote_probation_to_live" for r in fired)


def test_default_rules_all_rules_registered():
    """with_default_rules() registers exactly 9 built-in rules."""
    svc, _ = _make_svc({})
    engine = GovernanceRuleEngine.with_default_rules(svc)
    rule_names = [r.name for r in engine._rules]
    expected = {
        "auto_freeze_negative_sr",
        "auto_retire_repeated_frozen",
        "auto_freeze_critical",
        "auto_demote_degraded",
        "auto_promote_shadow_to_probation",
        "auto_demote_probation_to_frozen",
        "auto_promote_probation_to_live",
        "auto_promote_healthy",
        "unfreeze_recovered",
    }
    assert set(rule_names) == expected
    # Verify priority ordering
    priorities = [r.priority for r in engine._rules]
    assert priorities == sorted(priorities, reverse=True), "rules must be sorted by priority desc"


def test_evaluate_multiple_brains_independent():
    """Each brain is evaluated independently — B1 frozen, B2 promoted."""
    states = {
        "B1": {"status": "live", "freeze_count": 0},
        "B2": {"status": "probation", "freeze_count": 0},
    }
    transitions = []

    def _transition(bid, new_status, reason=""):
        transitions.append((bid, new_status, reason))
        return {"action": "transitioned"}

    svc = SimpleNamespace(
        get_brain_state=lambda bid: states.get(bid),
        transition=_transition,
    )
    engine = GovernanceRuleEngine.with_default_rules(svc)
    fired = engine.evaluate(
        {
            "B1": {"health_signal": "critical", "sample_count": 15},
            "B2": {"health_signal": "healthy", "composite_mean": 0.80, "sample_count": 120},
        }
    )
    # B1 should fire some rule; B2 should fire promotion rule
    assert len(fired) >= 2
