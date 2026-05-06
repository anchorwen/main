"""Tests for DynamicBrainWeighter."""

import pytest

from core.brains.services.dynamic_brain_weighter import DynamicBrainWeighter
from core.feedback.brain_performance_tracker import BrainPerformanceTracker
from core.feedback.brain_pnl_ledger import BrainPnLMetrics, BrainPnLStore

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


# ── Phase 2: P&L metrics-driven weighting ──


class TestPnLMetricsWeighting:
    """Weight computation from real BrainPnLMetrics (Sharpe-driven)."""

    @pytest.mark.parametrize(
        "health, sharpe, win_rate, expected_min, expected_max",
        [
            ("insufficient_data", 0.0, 0.5, 1.0, 1.0),
            ("critical", -2.0, 0.2, 0.1, 0.1),
            ("degraded", -0.7, 0.35, 0.25, 0.25),
            ("warning", -0.3, 0.42, 0.5, 0.5),
            ("stable", 0.0, 0.50, 0.5, 0.5),  # Sharpe 0 → base
            ("stable", 5.0, 0.50, 2.0, 2.5),  # Sharpe 5 → near max
            ("healthy", 0.0, 0.55, 1.0, 1.1),  # Sharpe 0 → base + WR boost
            ("healthy", 5.0, 0.55, 2.5, 3.0),  # Sharpe 5 → near max
            ("healthy", 10.0, 0.55, 2.5, 3.0),  # Sharpe 10 → clamped
        ],
    )
    def test_compute_weight_from_metrics_buckets(
        self, health, sharpe, win_rate, expected_min, expected_max
    ):
        m = BrainPnLMetrics(
            brain_id="B1",
            sample_count=20,
            sharpe_ratio=sharpe,
            win_rate=win_rate,
            health_signal=health,
        )
        tracker = BrainPerformanceTracker()
        weighter = DynamicBrainWeighter(tracker)
        w = weighter._compute_weight_from_metrics(m)
        assert (
            expected_min <= w <= expected_max
        ), f"weight={w} not in [{expected_min}, {expected_max}]"

    def test_sharpe_monotonic_increasing(self):
        """Higher Sharpe → higher weight (all else equal)."""
        tracker = BrainPerformanceTracker()
        weighter = DynamicBrainWeighter(tracker)
        w0 = weighter._compute_weight_from_metrics(
            BrainPnLMetrics(
                brain_id="B1",
                sample_count=20,
                sharpe_ratio=0.0,
                win_rate=0.55,
                health_signal="healthy",
            )
        )
        w2 = weighter._compute_weight_from_metrics(
            BrainPnLMetrics(
                brain_id="B1",
                sample_count=20,
                sharpe_ratio=2.0,
                win_rate=0.55,
                health_signal="healthy",
            )
        )
        w5 = weighter._compute_weight_from_metrics(
            BrainPnLMetrics(
                brain_id="B1",
                sample_count=20,
                sharpe_ratio=5.0,
                win_rate=0.55,
                health_signal="healthy",
            )
        )
        assert w0 < w2 < w5, f"w0={w0}, w2={w2}, w5={w5}"

    def test_win_rate_modifier(self):
        """Higher win rate increases weight within same health tier."""
        tracker = BrainPerformanceTracker()
        weighter = DynamicBrainWeighter(tracker)
        w_low = weighter._compute_weight_from_metrics(
            BrainPnLMetrics(
                brain_id="B1",
                sample_count=20,
                sharpe_ratio=2.0,
                win_rate=0.40,
                health_signal="stable",
            )
        )
        w_mid = weighter._compute_weight_from_metrics(
            BrainPnLMetrics(
                brain_id="B1",
                sample_count=20,
                sharpe_ratio=2.0,
                win_rate=0.50,
                health_signal="stable",
            )
        )
        w_high = weighter._compute_weight_from_metrics(
            BrainPnLMetrics(
                brain_id="B1",
                sample_count=20,
                sharpe_ratio=2.0,
                win_rate=0.60,
                health_signal="stable",
            )
        )
        assert w_low < w_mid < w_high

    def test_drawdown_penalty(self):
        """Large drawdown reduces weight."""
        tracker = BrainPerformanceTracker()
        weighter = DynamicBrainWeighter(tracker)
        w_normal = weighter._compute_weight_from_metrics(
            BrainPnLMetrics(
                brain_id="B1",
                sample_count=20,
                sharpe_ratio=2.0,
                win_rate=0.55,
                max_drawdown=1.0,
                health_signal="healthy",
            )
        )
        w_dd = weighter._compute_weight_from_metrics(
            BrainPnLMetrics(
                brain_id="B1",
                sample_count=20,
                sharpe_ratio=2.0,
                win_rate=0.55,
                max_drawdown=5.0,
                health_signal="healthy",
            )
        )
        assert w_dd < w_normal

    def test_negative_sharpe_stable_clamps_low(self):
        """Stable health with negative Sharpe → near lower bound of stable tier."""
        tracker = BrainPerformanceTracker()
        weighter = DynamicBrainWeighter(tracker)
        w = weighter._compute_weight_from_metrics(
            BrainPnLMetrics(
                brain_id="B1",
                sample_count=20,
                sharpe_ratio=-3.0,
                win_rate=0.45,
                health_signal="stable",
            )
        )
        # tanh(-0.6) ≈ -0.537, clamped to 0 → base 0.5, WR penalty → ~0.48
        assert 0.4 <= w <= 0.6


class TestPnLStoreIntegration:
    """DynamicBrainWeighter with BrainPnLStore wired in."""

    def test_get_weights_prefers_pnl_metrics(self):
        """When P&L data is available, it should drive the weight."""
        pnl = BrainPnLStore()
        # Record winning signals for B1 → healthy, high Sharpe
        for i in range(20):
            sid = pnl.record_signal("B1", "XAUUSDc", "long", 100.0)
            pnl.settle_one(sid, 101.0 + i * 0.05)

        tracker = BrainPerformanceTracker()
        # Tracker has conflicting data (degraded)
        for _ in range(20):
            tracker.record_outcome("B1", {"composite_score": 0.2, "execution_outcome": "rejected"})

        weighter = DynamicBrainWeighter(tracker, pnl_store=pnl)
        weights = weighter.get_weights()
        assert "B1" in weights
        # P&L metrics (healthy) should override tracker (degraded)
        assert weights["B1"] > 1.0  # healthy from P&L, not 0.1 from tracker

    def test_get_weights_falls_back_to_tracker(self):
        """When P&L has insufficient data, fall back to tracker."""
        pnl = BrainPnLStore()
        # Only 2 samples in P&L → insufficient
        for _ in range(2):
            sid = pnl.record_signal("B1", "XAUUSDc", "long", 100.0)
            pnl.settle_one(sid, 101.0)

        tracker = BrainPerformanceTracker()
        for _ in range(20):
            tracker.record_outcome("B1", {"composite_score": 0.8, "execution_outcome": "filled"})

        weighter = DynamicBrainWeighter(tracker, pnl_store=pnl)
        weights = weighter.get_weights()
        assert "B1" in weights
        # Should use tracker: healthy + high composite → weight > 1.5
        assert weights["B1"] > 1.5

    def test_get_weights_merges_both_sources(self):
        """Brain in P&L but not tracker, and vice versa."""
        pnl = BrainPnLStore()
        for i in range(20):
            sid = pnl.record_signal("Brain_PnL", "XAUUSDc", "long", 100.0)
            pnl.settle_one(sid, 101.0 + i * 0.05)

        tracker = BrainPerformanceTracker()
        for _ in range(20):
            tracker.record_outcome(
                "Brain_Tracker", {"composite_score": 0.8, "execution_outcome": "filled"}
            )

        weighter = DynamicBrainWeighter(tracker, pnl_store=pnl)
        weights = weighter.get_weights()
        assert "Brain_PnL" in weights
        assert "Brain_Tracker" in weights

    def test_apply_weights_with_pnl_store(self):
        """apply_weights uses P&L metrics when available."""
        pnl = BrainPnLStore()
        for i in range(20):
            sid = pnl.record_signal("B1", "XAUUSDc", "long", 100.0)
            pnl.settle_one(sid, 101.0 + i * 0.05)

        tracker = BrainPerformanceTracker()
        weighter = DynamicBrainWeighter(tracker, pnl_store=pnl)
        proposals = [_FakeProposal("B1"), _FakeProposal("B2")]
        weighter.apply_weights(proposals)
        # B1 has P&L data → should get non-default weight
        assert proposals[0].vote_weight != 1.0
        # B2 has no data anywhere → remains default
        assert proposals[1].vote_weight == 1.0

    def test_no_pnl_store_unchanged_behavior(self):
        """Without pnl_store, behavior is identical to Phase 1."""
        tracker = BrainPerformanceTracker()
        for _ in range(20):
            tracker.record_outcome("B1", {"composite_score": 0.8, "execution_outcome": "filled"})

        # No pnl_store passed
        weighter = DynamicBrainWeighter(tracker)
        weights = weighter.get_weights()
        assert "B1" in weights
        assert weights["B1"] > 1.0
