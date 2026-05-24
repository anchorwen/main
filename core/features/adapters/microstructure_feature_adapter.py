"""Microstructure feature adapter — dict or sequence → normalised model input.

Handles three input formats:
  1. Single-bar dict (backward-compat) → (1, 9)
  2. Sequence (n_bars, 9) → (1, n_bars, 9) for Transformer
  3. Flattened (n_bars*9,) → (1, 288) for XGBoost

All outputs are StandardScaler-normalised when a scaler is configured.
"""

from __future__ import annotations

import logging

import numpy as np

from core.features.schemas.microstructure_schema import MICROSTRUCTURE_9_FEATURES

_logger = logging.getLogger(__name__)
DEFAULT_SEQ_LEN = 32


class MicrostructureFeatureAdapter:
    """Extracts and normalises microstructure features for model input.

    When a scaler_path is provided, loads a sklearn StandardScaler
    that was fit on per-bar features during training.
    """

    def __init__(self, scaler_path: str | None = None):
        self._scaler = None
        self._scaler_path = scaler_path
        self._scaler_warned: bool = False
        if scaler_path:
            import joblib

            self._scaler = joblib.load(scaler_path)

    @property
    def is_warmed_up(self) -> bool:
        return True

    @property
    def feature_count(self) -> int:
        return len(MICROSTRUCTURE_9_FEATURES)

    # ── Single-bar (backward-compat) ──────────────────────────────────

    def build_raw_vector(self, feature_source: dict) -> np.ndarray:
        """Extract 9 microstructure features in canonical order from a dict."""
        values = [float(feature_source.get(name, 0.0)) for name in MICROSTRUCTURE_9_FEATURES]
        return np.asarray(values, dtype=np.float32)

    def build_model_input(self, feature_source: dict) -> np.ndarray:
        """Single bar: dict → (1, 9) normalised array."""
        raw = self.build_raw_vector(feature_source)
        return self.normalize(raw).reshape(1, -1)

    # ── Sequence input ───────────────────────────────────────────────

    def build_sequence_input(self, seq: np.ndarray) -> np.ndarray:
        """Sequence (n_bars, 9) → (1, n_bars, 9) normalised.

        Applies per-bar normalization (broadcast over bars).
        """
        if seq.ndim != 2 or seq.shape[1] != 9:
            raise ValueError(f"Expected (n, 9) array, got {seq.shape}")
        normalized = self._normalize_2d(seq)
        return normalized.reshape(1, seq.shape[0], 9).astype(np.float32)

    def build_flat_input(self, seq: np.ndarray) -> np.ndarray:
        """Sequence (n_bars, 9) → (1, n_bars*9) normalised flat.

        Row-major (C-order): bar0_f0...bar0_f8, bar1_f0...bar1_f8, ...
        Matches training data built with sliding_window_view.reshape(-1).
        """
        if seq.ndim != 2 or seq.shape[1] != 9:
            raise ValueError(f"Expected (n, 9) array, got {seq.shape}")
        normalized = self._normalize_2d(seq)
        return normalized.ravel().reshape(1, -1).astype(np.float32)

    # ── Normalization ────────────────────────────────────────────────

    def normalize(self, raw_vector: np.ndarray) -> np.ndarray:
        """Apply StandardScaler to a 1-D vector, or return raw."""
        if self._scaler is not None:
            return np.asarray(
                self._scaler.transform(raw_vector.reshape(1, -1)).ravel(),
                dtype=np.float32,
            )
        if not self._scaler_warned:
            _logger.warning(
                "MicrostructureFeatureAdapter: no scaler loaded — returning raw features"
            )
            self._scaler_warned = True
        return np.asarray(raw_vector, dtype=np.float32)

    def _normalize_2d(self, arr: np.ndarray) -> np.ndarray:
        """Apply StandardScaler per-row, or return raw if no scaler."""
        if self._scaler is not None:
            return np.asarray(self._scaler.transform(arr), dtype=np.float32)
        if not self._scaler_warned:
            _logger.warning(
                "MicrostructureFeatureAdapter: no scaler loaded — returning raw features"
            )
            self._scaler_warned = True
        return np.asarray(arr, dtype=np.float32)
