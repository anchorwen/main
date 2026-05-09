"""Microstructure 9-feature adapter: raw → StandardScaler-normalised model input.

Mirrors V9FeatureAdapter but for the v4.3_microstructure_9 schema.
Features are extracted in the fixed order defined by MICROSTRUCTURE_9_FEATURES
and scaled with a sklearn StandardScaler loaded from the training artefacts.
"""

from __future__ import annotations

import numpy as np

from core.features.schemas.microstructure_schema import MICROSTRUCTURE_9_FEATURES


class MicrostructureFeatureAdapter:
    """Extracts 9 microstructure features from a feature_source dict and
    applies StandardScaler normalization when a scaler is configured."""

    def __init__(self, scaler_path: str | None = None):
        self._scaler = None
        self._scaler_path = scaler_path
        if scaler_path:
            import joblib

            self._scaler = joblib.load(scaler_path)

    @property
    def is_warmed_up(self) -> bool:
        return True  # static scaler is always ready

    @property
    def feature_count(self) -> int:
        return len(MICROSTRUCTURE_9_FEATURES)

    def build_raw_vector(self, feature_source: dict) -> np.ndarray:
        """Extract the 9 microstructure features in canonical order."""
        values = []
        for name in MICROSTRUCTURE_9_FEATURES:
            values.append(float(feature_source.get(name, 0.0)))
        return np.asarray(values, dtype=np.float32)

    def normalize(self, raw_vector: np.ndarray) -> np.ndarray:
        """Apply StandardScaler if configured, otherwise return raw."""
        if self._scaler is not None:
            return np.asarray(
                self._scaler.transform(raw_vector.reshape(1, -1)).ravel(),
                dtype=np.float32,
            )
        return np.asarray(raw_vector, dtype=np.float32)

    def build_model_input(self, feature_source: dict) -> np.ndarray:
        raw = self.build_raw_vector(feature_source)
        return self.normalize(raw).reshape(1, -1)
