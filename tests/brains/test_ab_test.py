"""Tests for A/B testing framework."""

from __future__ import annotations

import numpy as np

from core.brains.services.ab_test import (
    ExperimentConfig,
    ExperimentTracker,
    TrafficSplitter,
    minimum_sample_size,
)


class TestTrafficSplitter:
    def test_deterministic_assignment(self):
        s = TrafficSplitter(control_weight=0.5, treatment_weight=0.5)
        a1 = s.assign("order_123")
        a2 = s.assign("order_123")
        assert a1 == a2

    def test_different_keys_may_differ(self):
        s = TrafficSplitter(control_weight=0.5, treatment_weight=0.5)
        results = {s.assign(f"order_{i}") for i in range(100)}
        assert len(results) >= 1

    def test_all_control_always_control(self):
        s = TrafficSplitter(control_weight=1.0, treatment_weight=0.0)
        for i in range(50):
            assert s.assign(f"key_{i}") == "control"

    def test_assign_many(self):
        s = TrafficSplitter()
        variants = s.assign_many(["a", "b", "c"])
        assert len(variants) == 3

    def test_salt_changes_assignment(self):
        s1 = TrafficSplitter(salt="a")
        s2 = TrafficSplitter(salt="b")
        # Some keys should differ
        keys = [f"k{i}" for i in range(20)]
        v1 = s1.assign_many(keys)
        v2 = s2.assign_many(keys)
        diffs = sum(1 for a, b in zip(v1, v2, strict=False) if a != b)
        assert diffs > 0

    def test_roughly_balanced_split(self):
        s = TrafficSplitter(control_weight=0.5, treatment_weight=0.5)
        variants = [s.assign(f"key_{i}") for i in range(1000)]
        c_count = variants.count("control")
        t_count = variants.count("treatment")
        # Within 10% of 500
        assert 400 <= c_count <= 600
        assert 400 <= t_count <= 600


class TestExperimentTracker:
    def test_insufficient_data(self):
        t = ExperimentTracker("exp_1")
        result = t.evaluate()
        assert result.recommendation == "insufficient_data"
        assert not result.significant

    def test_identical_means_no_significance(self):
        rng = np.random.default_rng(42)
        t = ExperimentTracker("exp_2")
        for _ in range(50):
            t.record("control", rng.normal(0.002, 0.005))
            t.record("treatment", rng.normal(0.002, 0.005))
        result = t.evaluate()
        # With same distribution, p > 0.01
        assert result.p_value > 0.01

    def test_clear_winner_treatment(self):
        rng = np.random.default_rng(42)
        t = ExperimentTracker("exp_3", metric_direction="higher")
        for _ in range(100):
            t.record("control", rng.normal(0.001, 0.005))
            t.record("treatment", rng.normal(0.010, 0.005))
        result = t.evaluate()
        assert result.control_mean < result.treatment_mean
        assert result.absolute_lift > 0
        # Very low p-value expected
        assert result.p_value < 0.01

    def test_metric_direction_lower(self):
        rng = np.random.default_rng(42)
        t = ExperimentTracker("exp_4", metric_direction="lower")
        for _ in range(100):
            t.record("control", rng.normal(0.010, 0.005))
            t.record("treatment", rng.normal(0.001, 0.005))
        result = t.evaluate()
        assert result.recommendation == "rollout_treatment"

    def test_keep_control_when_treatment_worse(self):
        rng = np.random.default_rng(42)
        t = ExperimentTracker("exp_5", metric_direction="higher")
        for _ in range(100):
            t.record("control", rng.normal(0.010, 0.005))
            t.record("treatment", rng.normal(0.001, 0.005))
        result = t.evaluate()
        assert result.recommendation == "keep_control"

    def test_to_dict(self):
        rng = np.random.default_rng(42)
        t = ExperimentTracker("exp_6")
        for _ in range(50):
            t.record("control", rng.normal(0.002, 0.005))
            t.record("treatment", rng.normal(0.003, 0.005))
        result = t.evaluate()
        d = result.to_dict()
        assert d["experiment_id"] == "exp_6"
        assert "p_value" in d
        assert "effect_size" in d

    def test_single_variant_small_n(self):
        t = ExperimentTracker("exp_7")
        t.record("control", 0.01)
        t.record("treatment", 0.02)
        result = t.evaluate()
        assert result.recommendation == "insufficient_data"


class TestMinimumSampleSize:
    def test_reasonable_output(self):
        n = minimum_sample_size(baseline_rate=0.02, minimum_detectable_effect=0.005)
        assert n >= 10
        assert isinstance(n, int)

    def test_small_mde_needs_more_samples(self):
        n1 = minimum_sample_size(baseline_rate=0.02, minimum_detectable_effect=0.01)
        n2 = minimum_sample_size(baseline_rate=0.02, minimum_detectable_effect=0.001)
        assert n2 > n1


class TestExperimentConfig:
    def test_weights_property(self):
        cfg = ExperimentConfig(
            experiment_id="test",
            control_weight=0.5,
            treatment_weight=0.5,
            treatment_names=["v1", "v2"],
        )
        w = cfg.weights
        assert "control" in w
        assert "v1" in w
        assert "v2" in w
        assert w["control"] == 0.5
