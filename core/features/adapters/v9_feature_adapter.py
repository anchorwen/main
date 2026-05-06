"""V9 Institutional 40-feature adapter: raw → normalized model input.

Supports two normalization strategies:
  - fixed: Static train-set mean/std (via normalization_config dict).
  - rolling_ewma: Online adaptive EWMA estimates (via RollingNormalizer).
"""

from __future__ import annotations

import numpy as np

from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES


class V9FeatureAdapter:
    def __init__(
        self,
        normalization_config: dict | None = None,
        rolling_normalizer=None,
    ):
        """Initialize feature adapter.

        Args:
            normalization_config: Dict with "mean" and "std" lists of length 40.
                Used for fixed normalization (training-set statistics).
            rolling_normalizer: RollingNormalizer instance for online adaptive
                normalization. When provided, takes precedence over static config.
                The adapter calls normalizer.normalize() which also updates the
                running estimates.
        """
        self._normalization_config = normalization_config or {}
        self._rolling = rolling_normalizer

    @property
    def normalization_strategy(self) -> str:
        if self._rolling is not None:
            return "rolling_ewma"
        if self._normalization_config:
            return "fixed"
        return "none"

    @property
    def is_warmed_up(self) -> bool:
        if self._rolling is not None:
            return self._rolling.is_warmed_up
        return True  # fixed normalizer is always ready

    def build_raw_vector(self, feature_source: dict) -> np.ndarray:
        values = []
        for name in V9_INSTITUTIONAL_40_FEATURES:
            values.append(float(feature_source.get(name, 0.0)))
        return np.asarray(values, dtype=np.float32)

    def normalize(self, raw_vector: np.ndarray) -> np.ndarray:
        # ── Config-driven skip: when normalize=false the model was trained
        #     on raw features so we must not z-score at inference. ──
        if not self._normalization_config.get("normalize", True):
            return raw_vector

        # ── Rolling EWMA (online adaptive) ──
        if self._rolling is not None:
            result = self._rolling.normalize(np.asarray(raw_vector, dtype=np.float64))
            return np.asarray(result, dtype=np.float32)

        # ── Fixed (train-set mean/std) ──
        mean = self._normalization_config.get("mean", [])
        std = self._normalization_config.get("std", [])

        if not mean or not std:
            return raw_vector

        mean_arr = np.asarray(mean, dtype=np.float32)
        std_arr = np.asarray(std, dtype=np.float32)

        if mean_arr.shape[0] != raw_vector.shape[0]:
            raise ValueError("normalization mean dimension mismatch")
        if std_arr.shape[0] != raw_vector.shape[0]:
            raise ValueError("normalization std dimension mismatch")

        return (raw_vector - mean_arr) / (std_arr + 1e-8)

    def build_model_input(self, feature_source: dict) -> np.ndarray:
        raw_vector = self.build_raw_vector(feature_source)
        normalized = self.normalize(raw_vector)
        return normalized.reshape(1, -1)
