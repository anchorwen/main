"""Tests for core.features.feature_router — schema-contracted dispatch.

FIX-20260619-050: Tier 2 zero-coverage breakout #5.
"""
from __future__ import annotations
import numpy as np
import pytest
from core.features.feature_router import FeatureMissingError, FeatureRouter

class TestFeatureRouter:
    def test_dispatch_returns_correct_shape(self) -> None:
        router = FeatureRouter()
        lake = {"feat_a": 1.0, "feat_b": 2.0, "feat_c": 3.0}
        tensor = router._build_tensor(lake, ["feat_a", "feat_b", "feat_c"])
        assert tensor.shape == (3,)
        np.testing.assert_array_equal(tensor, np.array([1.0, 2.0, 3.0]))

    def test_missing_key_raises_feature_missing(self) -> None:
        router = FeatureRouter()
        with pytest.raises(FeatureMissingError):
            router._build_tensor({"feat_a": 1.0}, ["feat_a", "missing"])

    def test_dispatch_with_none_raises(self) -> None:
        router = FeatureRouter()
        lake: dict = {"feat_a": None, "feat_b": 2.0}
        with pytest.raises((FeatureMissingError, TypeError)):
            router._build_tensor(lake, ["feat_a", "feat_b"])
