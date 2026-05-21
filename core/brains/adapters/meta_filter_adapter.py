"""Meta-filter adapter for LightGBM precision filter.

Loads a LightGBM binary classifier trained on meta-labeling data and
provides a simple filter(signal_features) -> (passed, p_win) API.

Hard feature-parity assertions ensure the live feature packet matches
the training feature_names_in_ exactly — preventing silent accuracy
degradation from feature mismatches (Feature Parity Trap fix).

Usage::

    adapter = MetaFilterAdapter(
        model_path="data/models/meta_filter_v3/meta_filter_lgb.pkl",
        feature_names_path="data/models/meta_filter_v3/feature_names.json",
        threshold=0.50,
    )
    adapter.load()

    result = adapter.filter(feature_dict)
    if result["passed"]:
        execute(signal)
"""

from __future__ import annotations

import json
import pickle
import warnings
from pathlib import Path
from typing import Any

import numpy as np


class FeatureParityError(Exception):
    """Raised when live feature packet does not match training schema."""


class MetaFilterAdapter:
    """LightGBM meta-filter for OU signal quality prediction.

    Predicts P(breakeven hit | signal_fired, features) and applies a
    threshold to produce a binary pass/block decision.
    """

    def __init__(
        self,
        model_path: str | Path,
        feature_names_path: str | Path,
        threshold: float = 0.50,
    ):
        self._model_path = Path(model_path)
        self._feature_names_path = Path(feature_names_path)
        self._threshold = threshold
        self._model: Any = None
        self._feature_names: list[str] = []
        self._n_features: int = 0
        self._loaded = False

    # ── Loading ────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load model and feature names, then verify consistency."""
        # Load feature names
        if not self._feature_names_path.exists():
            raise FileNotFoundError(f"Feature names file not found: {self._feature_names_path}")
        self._feature_names = json.loads(self._feature_names_path.read_text(encoding="utf-8"))
        self._n_features = len(self._feature_names)

        # Load model
        if not self._model_path.exists():
            raise FileNotFoundError(f"Model file not found: {self._model_path}")
        with open(self._model_path, "rb") as f:
            self._model = pickle.load(f)

        # ── Hard feature-parity assertions ─────────────────────────
        # Verify the model was trained on the same number of features.
        # Note: sklearn pickle loses column names (stores Column_0 etc.),
        # so we only check dimension count here.  The actual feature-name
        # validation happens in _build_vector() — if the live computer
        # changes a feature name, it will be caught as a missing feature.
        if hasattr(self._model, "n_features_in_"):
            model_n = self._model.n_features_in_
            if model_n != self._n_features:
                raise FeatureParityError(
                    f"Feature count mismatch: model expects {model_n} features, "
                    f"but feature_names.json has {self._n_features}"
                )

        self._loaded = True

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def feature_names(self) -> list[str]:
        return list(self._feature_names)

    # ── Filtering ──────────────────────────────────────────────────────

    def filter(self, feature_dict: dict[str, float]) -> dict[str, Any]:
        """Run meta-filter on a live feature packet.

        Args:
            feature_dict: Feature name → value mapping from live feature
                computer (e.g. V9MicroComputer.compute_all()).

        Returns:
            dict with keys: passed (bool), p_win (float), threshold (float),
            reason (str).
        """
        if not self._loaded:
            raise RuntimeError("MetaFilterAdapter not loaded — call load() first")

        # Build feature vector in exact training order
        vector = self._build_vector(feature_dict)

        # Run inference
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="X does not have valid feature names")
            p_win = float(self._model.predict_proba(vector.reshape(1, -1))[0, 1])

        passed = p_win >= self._threshold
        reason = (
            f"meta_filter_pass_{p_win:.3f}_gte_{self._threshold:.2f}"
            if passed
            else f"meta_filter_block_{p_win:.3f}_lt_{self._threshold:.2f}"
        )

        return {
            "passed": passed,
            "p_win": round(p_win, 4),
            "threshold": self._threshold,
            "reason": reason,
        }

    def predict_proba(self, feature_dict: dict[str, float]) -> float:
        """Return raw P(win) without threshold comparison."""
        if not self._loaded:
            raise RuntimeError("MetaFilterAdapter not loaded — call load() first")
        vector = self._build_vector(feature_dict)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="X does not have valid feature names")
            return float(self._model.predict_proba(vector.reshape(1, -1))[0, 1])

    # ── Internal ────────────────────────────────────────────────────────

    def _build_vector(self, feature_dict: dict[str, float]) -> np.ndarray:
        """Build feature vector in exact training order with hard assertions."""
        values = []
        missing = []
        for name in self._feature_names:
            val = feature_dict.get(name)
            if val is None or (isinstance(val, float) and val != val):  # NaN check
                missing.append(name)
                values.append(0.0)
            else:
                values.append(float(val))

        if missing:
            raise FeatureParityError(
                f"Live feature packet missing {len(missing)} features: {missing[:5]}..."
            )

        # ── Hard assertion: feature count must match ──
        actual_n = len(values)
        if actual_n != self._n_features:
            raise FeatureParityError(
                f"Feature dimension mismatch: built vector has {actual_n} dims, "
                f"model expects {self._n_features}"
            )

        return np.asarray(values, dtype=np.float32)

    # ── Filter with raw array (for batch/testing) ──────────────────────

    def filter_array(self, X: np.ndarray) -> dict[str, Any]:
        """Run filter on a pre-built (n_features,) feature array.

        Used for backtesting / batch evaluation where features are
        already assembled in the correct order.
        """
        if not self._loaded:
            raise RuntimeError("MetaFilterAdapter not loaded — call load() first")

        if X.ndim == 1:
            X = X.reshape(1, -1)

        if X.shape[1] != self._n_features:
            raise FeatureParityError(
                f"Array dimension mismatch: got {X.shape[1]} features, "
                f"model expects {self._n_features}"
            )

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="X does not have valid feature names")
            p_win = float(self._model.predict_proba(X)[0, 1])
        passed = p_win >= self._threshold
        reason = f"meta_filter_pass_{p_win:.3f}" if passed else f"meta_filter_block_{p_win:.3f}"

        return {
            "passed": passed,
            "p_win": round(p_win, 4),
            "threshold": self._threshold,
            "reason": reason,
        }
