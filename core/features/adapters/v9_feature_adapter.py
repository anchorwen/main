import numpy as np

from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES


class V9FeatureAdapter:
    def __init__(self, normalization_config: dict | None = None):
        self._normalization_config = normalization_config or {}

    def build_raw_vector(self, feature_source: dict) -> np.ndarray:
        values = []
        for name in V9_INSTITUTIONAL_40_FEATURES:
            values.append(float(feature_source.get(name, 0.0)))
        return np.asarray(values, dtype=np.float32)

    def normalize(self, raw_vector: np.ndarray) -> np.ndarray:
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


