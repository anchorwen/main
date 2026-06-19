"""Unit tests for DecisionScorer — pure scoring logic.

All scoring methods are pure functions: dict in → dict out, zero I/O.
Part of Test 2: feedback dedicated test suite.
"""

from __future__ import annotations

import pytest

from core.feedback.decision_scorer import DecisionScorer


@pytest.fixture
def scorer():
    return DecisionScorer()


# ── _score_fill ───────────────────────────────────────────────────────────


class TestScoreFill:
    def test_clean_fill(self, scorer):
        r = scorer._score_fill({"fill_quality": {"grade": "clean_fill", "fill_ratio": 1.0}})
        assert r["score"] == 1.0
        assert r["grade"] == "clean_fill"

    def test_rejected(self, scorer):
        r = scorer._score_fill({"fill_quality": {"grade": "rejected"}})
        assert r["score"] == 0.0

    def test_partial_cancel(self, scorer):
        r = scorer._score_fill({"fill_quality": {"grade": "partial_cancel", "fill_ratio": 0.5}})
        assert r["score"] == 0.4

    def test_missing_fill_quality(self, scorer):
        r = scorer._score_fill({})
        assert r["score"] == 0.0  # unknown → 0.0
        assert r["grade"] == "unknown"


# ── _score_timing ─────────────────────────────────────────────────────────


class TestScoreTiming:
    def test_no_events(self, scorer):
        r = scorer._score_timing({"timeline": {"event_count": 0}}, {})
        assert r["score"] == 0.0

    def test_instant_fill(self, scorer):
        r = scorer._score_timing(
            {"timeline": {"event_count": 1, "event_types": ["filled"], "terminal_event_type": "filled"}}, {}
        )
        assert r["score"] == 1.0

    def test_fast_fill_two_partials(self, scorer):
        r = scorer._score_timing(
            {"timeline": {"event_count": 3, "event_types": ["partially_filled", "partially_filled", "filled"], "terminal_event_type": "filled"}}, {}
        )
        assert r["score"] == 0.8

    def test_moderate_fill(self, scorer):
        events = ["partially_filled"] * 4 + ["filled"]
        r = scorer._score_timing(
            {"timeline": {"event_count": 5, "event_types": events}}, {}
        )
        assert r["score"] == 0.5

    def test_slow_fill(self, scorer):
        events = ["partially_filled"] * 6 + ["filled"]
        r = scorer._score_timing(
            {"timeline": {"event_count": 7, "event_types": events}}, {}
        )
        assert r["score"] == 0.3


# ── _score_accuracy ───────────────────────────────────────────────────────


class TestScoreAccuracy:
    def test_long_direction_correct(self, scorer):
        r = scorer._score_accuracy(
            {"intended_side": "long"}, {"price_move_pct": 2.0}
        )
        assert r["score"] > 0.5

    def test_long_direction_wrong(self, scorer):
        r = scorer._score_accuracy(
            {"intended_side": "long"}, {"price_move_pct": -1.0}
        )
        assert r["score"] < 0.5

    def test_profitable(self, scorer):
        r = scorer._score_accuracy({}, {"realized_pnl": 500})
        assert r["score"] > 0.7

    def test_breakeven(self, scorer):
        r = scorer._score_accuracy({}, {"realized_pnl": 0})
        assert r["score"] == 0.5

    def test_loss(self, scorer):
        r = scorer._score_accuracy({}, {"realized_pnl": -200})
        assert r["score"] < 0.5

    def test_no_directional_signal(self, scorer):
        r = scorer._score_accuracy({"intended_side": "neutral"}, {})
        assert r["score"] == 0.5


# ── _score_risk ───────────────────────────────────────────────────────────


class TestScoreRisk:
    def test_clean_reconciliation(self, scorer):
        r = scorer._score_risk({"reconciliation": {"status": "matched"}})
        assert r["score"] == 1.0

    def test_no_reconciliation(self, scorer):
        r = scorer._score_risk({})
        assert r["score"] == 0.5

    def test_breached(self, scorer):
        r = scorer._score_risk({"reconciliation": {"status": "breached"}})
        assert r["score"] == 0.0

    def test_partial(self, scorer):
        r = scorer._score_risk({"reconciliation": {"status": "partial"}})
        assert r["score"] == 0.6

    def test_stale(self, scorer):
        r = scorer._score_risk({"reconciliation": {"status": "stale"}})
        assert r["score"] == 0.3


# ── score (composite) ─────────────────────────────────────────────────────


class TestScoreComposite:
    def test_returns_composite_and_dimensions(self, scorer):
        outcome = {
            "fill_quality": {"grade": "clean_fill", "fill_ratio": 1.0},
            "timeline": {"event_count": 1, "event_types": ["filled"], "terminal_event_type": "filled"},
            "intended_side": "long",
            "reconciliation": {"status": "matched"},
        }
        r = scorer.score(outcome, market_context={"price_move_pct": 1.5})
        assert "composite_score" in r
        assert "dimensions" in r
        assert 0.0 <= r["composite_score"] <= 1.0
        assert len(r["dimensions"]) == 4
