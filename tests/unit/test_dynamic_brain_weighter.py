"""Tests for DynamicBrainWeighter."""

import pytest

from core.brains.services.dynamic_brain_weighter import DynamicBrainWeighter
from core.feedback.brain_performance_tracker import BrainPerformanceTracker

# ── _compute_weight ──


@pytest.mark.parametrize(
    "health, composite, expected_min, expected_max",
    [
        ("insufficient_data", 0.0, 1.0, 1.0),
        ("critical", 0.0, 0.1, 0.1),
        ("degraded", 0.3, 0.1, 0.1),
        ("warning", 0.4, 0.5, 0.5),
        ("stable", 0.5, 1.0, 2.0),
        ("healthy", 0.6, 1.5, 2.5),
        ("healthy", 0.9, 2.5, 3.0),
    ],
)
def test_compute_weight_health_buckets(health, composite, expected_min, expected_max):
    tracker = BrainPerformanceTracker()
    weighter = DynamicBrainWeighter(tracker)
    summary = {
        "brain_id": "B1",
        "health_signal": health,
        "composite_mean": composite,
        "sample_count": 50,
    }
    w = weighter._compute_weight(summary)
    assert expected_min <= w <= expected_max


def test_compute_weight_max_clamped():
    tracker = BrainPerformanceTracker()
    weighter = DynamicBrainWeighter(tracker)
    w = weighter._compute_weight(
        {"health_signal": "healthy", "composite_mean": 1.5, "sample_count": 50}
    )
    assert w == 3.0


def test_compute_weight_min_clamped():
    tracker = BrainPerformanceTracker()
    weighter = DynamicBrainWeighter(tracker)
    w = weighter._compute_weight(
        {"health_signal": "healthy", "composite_mean": -0.5, "sample_count": 50}
    )
    assert w == 0.1  # formula gives negative → clamped to 0.1 floor


# ── get_weights ──


def test_get_weights_empty_tracker():
    tracker = BrainPerformanceTracker()
    weighter = DynamicBrainWeighter(tracker)
    assert weighter.get_weights() == {}


def test_get_weights_returns_all_tracked():
    tracker = BrainPerformanceTracker()
    tracker.record_outcome("Brain_A", {"composite_score": 0.8, "execution_outcome": "filled"})
    tracker.record_outcome("Brain_B", {"composite_score": 0.3, "execution_outcome": "rejected"})
    # Need enough samples to leave insufficient_data
    for _ in range(15):
        tracker.record_outcome("Brain_A", {"composite_score": 0.8, "execution_outcome": "filled"})
        tracker.record_outcome("Brain_B", {"composite_score": 0.3, "execution_outcome": "rejected"})

    weighter = DynamicBrainWeighter(tracker)
    weights = weighter.get_weights()
    assert "Brain_A" in weights
    assert "Brain_B" in weights
    assert weights["Brain_A"] > weights["Brain_B"]


# ── apply_weights ──


class _FakeProposal:
    def __init__(self, brain_id, vote_weight=1.0):
        self.brain_id = brain_id
        self.vote_weight = vote_weight
        self.prediction = {
            "direction_bias": "long",
            "up_probability": 0.6,
            "down_probability": 0.4,
            "confidence": 0.7,
        }
        self.health = {"fallback_used": False, "risk_score": 0.3}


def test_apply_weights_modifies_proposals():
    tracker = BrainPerformanceTracker()
    # Brain_A: healthy high performer
    for _ in range(20):
        tracker.record_outcome("Brain_A", {"composite_score": 0.85, "execution_outcome": "filled"})
    # Brain_B: degraded
    for _ in range(20):
        tracker.record_outcome(
            "Brain_B", {"composite_score": 0.25, "execution_outcome": "rejected"}
        )

    weighter = DynamicBrainWeighter(tracker)
    proposals = [_FakeProposal("Brain_A"), _FakeProposal("Brain_B"), _FakeProposal("Brain_C")]

    result = weighter.apply_weights(proposals)
    assert result is proposals  # returns same list

    assert proposals[0].vote_weight > 1.5  # Brain_A: healthy, high composite
    assert proposals[1].vote_weight == 0.1  # Brain_B: degraded
    assert proposals[2].vote_weight == 1.0  # Brain_C: untracked → default unchanged


def test_apply_weights_insufficient_data_defaults():
    tracker = BrainPerformanceTracker()
    tracker.record_outcome("NewBrain", {"composite_score": 0.6})  # only 1 sample

    weighter = DynamicBrainWeighter(tracker)
    proposals = [_FakeProposal("NewBrain")]
    weighter.apply_weights(proposals)
    assert proposals[0].vote_weight == 1.0  # insufficient_data → 1.0


def test_apply_weights_empty_proposals():
    tracker = BrainPerformanceTracker()
    weighter = DynamicBrainWeighter(tracker)
    result = weighter.apply_weights([])
    assert result == []
