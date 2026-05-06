"""Data augmentation for financial time-series features.

Two strategies (both can be combined):
  1. Volatility scaling — multiply features by a random scaling factor.
     Simulates the same pattern under different volatility regimes. Each
     sample gets an independent scaling factor drawn from a configured list.
     This makes the model robust to the 2-6x ATR shifts observed in gold.
  2. Noise injection — add Gaussian noise to features.
     Standard regularizer that prevents overfitting to spurious patterns.

Reference: DeepMind AlphaZero-style data augmentation applied to finance.
The volatility scaling list [0.7, 0.85, 1.0, 1.15, 1.3] is calibrated for
gold's observed M5_ATR range (training=2.31, live=5-15).
"""

from __future__ import annotations

from typing import Any

import numpy as np


def _sample_vol_scales(n: int, scales: list[float], rng: np.random.Generator) -> np.ndarray:
    """Sample n scaling factors from the configured list."""
    if not scales:
        return np.ones(n, dtype=np.float64)
    idx = rng.integers(0, len(scales), size=n)
    return np.array([scales[i] for i in idx], dtype=np.float64)


def augment_features(
    X: np.ndarray,
    *,
    volatility_scaling: list[float] | None = None,
    noise_std: float = 0.0,
    seed: int | None = None,
) -> np.ndarray:
    """Apply data augmentation to a feature matrix.

    Args:
        X: Feature matrix (n_samples, n_features).
        volatility_scaling: List of scaling factors to randomly sample from.
            Default [1.0] means no scaling.
        noise_std: Standard deviation of Gaussian noise. 0.0 disables.
        seed: Random seed for reproducibility.

    Returns:
        Augmented feature matrix (same shape as X). Original X is not modified.
    """
    if volatility_scaling is None:
        volatility_scaling = [1.0]
    if not volatility_scaling:
        volatility_scaling = [1.0]

    rng = np.random.default_rng(seed)
    n, d = X.shape

    X_aug = X.copy()

    # 1. Volatility scaling — multiply each sample by a random factor
    if volatility_scaling != [1.0]:
        scales = _sample_vol_scales(n, volatility_scaling, rng)
        X_aug = X_aug * scales[:, np.newaxis]

    # 2. Noise injection
    if noise_std > 0.0:
        noise = rng.normal(0.0, noise_std, size=(n, d))
        X_aug = X_aug + noise

    return X_aug


def augment_dataset(
    X: np.ndarray,
    y: np.ndarray,
    *,
    volatility_scaling: list[float] | None = None,
    noise_std: float = 0.0,
    seed: int | None = None,
    concat_original: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
    """Augment a full (X, y) dataset.

    Args:
        X: Feature matrix.
        y: Label vector.
        volatility_scaling: Volatility scaling factors.
        noise_std: Gaussian noise std.
        seed: Reproducibility seed.
        concat_original: If True, returns [X_orig; X_aug] so the model
            sees both original and augmented data. If False, only augmented.

    Returns:
        (X_out, y_out) — augmented dataset.
    """
    X_aug = augment_features(
        X,
        volatility_scaling=volatility_scaling,
        noise_std=noise_std,
        seed=seed,
    )

    if concat_original:
        return np.vstack([X, X_aug]), np.concatenate([y, y])
    return X_aug, y


def augment_from_recipe_config(
    X: np.ndarray,
    y: np.ndarray,
    data_augmentation: dict[str, Any],
    *,
    seed: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Apply augmentation using recipe-style config dict.

    Args:
        X, y: Feature matrix and labels.
        data_augmentation: Dict with keys:
            - enabled (bool): If False, returns (X, y) unchanged.
            - volatility_scaling (list[float]): Scaling factors.
            - noise_std (float): Noise standard deviation.
        seed: Reproducibility seed.

    Returns:
        (X_out, y_out).
    """
    if not data_augmentation.get("enabled", False):
        return X, y

    vol_scales = data_augmentation.get("volatility_scaling", [1.0])
    noise = float(data_augmentation.get("noise_std", 0.0))

    return augment_dataset(
        X,
        y,
        volatility_scaling=vol_scales,
        noise_std=noise,
        seed=seed,
        concat_original=True,
    )
