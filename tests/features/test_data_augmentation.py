"""Tests for core.features.data_augmentation — feature augmentation.

FIX-20260619-046: Tier 2 zero-coverage breakout #1.
Pure numpy functions, fully deterministic with seed.
"""

from __future__ import annotations

import numpy as np

from core.features.data_augmentation import (
    _sample_vol_scales,
    augment_dataset,
    augment_features,
    augment_from_recipe_config,
)


class TestSampleVolScales:
    def test_returns_ones_when_scales_empty(self) -> None:
        rng = np.random.default_rng(42)
        result = _sample_vol_scales(5, [], rng)
        assert np.all(result == 1.0)
        assert result.shape == (5,)

    def test_samples_from_given_list(self) -> None:
        rng = np.random.default_rng(42)
        scales = [0.7, 0.85, 1.0, 1.15, 1.3]
        result = _sample_vol_scales(100, scales, rng)
        assert result.shape == (100,)
        assert np.all(result >= 0.7)
        assert np.all(result <= 1.3)


class TestAugmentFeatures:
    def test_no_augmentation_returns_copy(self) -> None:
        X = np.array([[1.0, 2.0], [3.0, 4.0]])
        result = augment_features(X, noise_std=0.0)
        assert result.shape == X.shape
        np.testing.assert_array_equal(result, X)

    def test_default_volatility_scaling_is_one(self) -> None:
        X = np.array([[1.0, 2.0], [3.0, 4.0]])
        result = augment_features(X, volatility_scaling=None, noise_std=0.0, seed=42)
        np.testing.assert_array_equal(result, X)  # scale=1.0, no noise

    def test_empty_scales_defaults_to_one(self) -> None:
        X = np.array([[1.0, 2.0]])
        result = augment_features(X, volatility_scaling=[], noise_std=0.0, seed=42)
        np.testing.assert_array_equal(result, X)

    def test_volatility_scaling_changes_values(self) -> None:
        X = np.array([[10.0, 20.0], [30.0, 40.0]])
        scales = [0.5, 2.0]
        result = augment_features(X, volatility_scaling=scales, noise_std=0.0, seed=42)
        assert not np.allclose(result, X)

    def test_noise_injection_adds_variation(self) -> None:
        X = np.ones((10, 5))
        result = augment_features(X, noise_std=0.5, seed=42)
        assert not np.allclose(result, X)
        assert result.shape == X.shape

    def test_reproducible_with_seed(self) -> None:
        X = np.random.default_rng(0).random((20, 5))
        r1 = augment_features(X, volatility_scaling=[0.7, 1.3], noise_std=0.1, seed=42)
        r2 = augment_features(X, volatility_scaling=[0.7, 1.3], noise_std=0.1, seed=42)
        np.testing.assert_array_equal(r1, r2)

    def test_original_not_modified(self) -> None:
        X = np.array([[1.0, 2.0]])
        _ = augment_features(X, volatility_scaling=[0.5, 2.0], noise_std=0.1)
        np.testing.assert_array_equal(X, np.array([[1.0, 2.0]]))


class TestAugmentDataset:
    def test_concat_doubles_samples(self) -> None:
        X = np.array([[1.0], [2.0]])
        y = np.array([0, 1])
        Xo, yo = augment_dataset(X, y, noise_std=0.0, seed=42, concat_original=True)
        assert Xo.shape[0] == 4
        assert len(yo) == 4
        np.testing.assert_array_equal(Xo[:2], X)
        np.testing.assert_array_equal(yo[:2], y)

    def test_no_concat_returns_only_augmented(self) -> None:
        X = np.array([[1.0], [2.0]])
        y = np.array([0, 1])
        Xo, yo = augment_dataset(X, y, noise_std=0.0, seed=42, concat_original=False)
        assert Xo.shape[0] == 2
        assert len(yo) == 2


class TestAugmentFromRecipeConfig:
    def test_disabled_returns_unchanged(self) -> None:
        X = np.array([[1.0]])
        y = np.array([0])
        Xo, yo = augment_from_recipe_config(X, y, {"enabled": False})
        np.testing.assert_array_equal(Xo, X)
        np.testing.assert_array_equal(yo, y)

    def test_enabled_applies_augmentation(self) -> None:
        X = np.array([[1.0], [2.0]])
        y = np.array([0, 1])
        Xo, yo = augment_from_recipe_config(
            X,
            y,
            {"enabled": True, "volatility_scaling": [0.5], "noise_std": 0.0},
            seed=42,
        )
        assert Xo.shape[0] == 4  # concat_original=True doubles
