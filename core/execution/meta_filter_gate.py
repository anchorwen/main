"""Meta-filter gate for OU signal quality prediction.

Builds a 47-dim feature array from the live feature pipeline and runs
the LightGBM meta-filter to predict P(breakeven | signal_fired).

Integration point: called from StrategyLine.evaluate() for OU strategies
(barrier_12bar, statarb_dynamic) as an additional quality gate.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

from core.features.schemas.microstructure_schema import MICROSTRUCTURE_9_FEATURES
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES


def _load_feature_names(feature_names_path: str | Path) -> list[str]:
    """Load the training feature names for parity checking."""
    fn_path = Path(feature_names_path)
    if not fn_path.exists():
        raise FileNotFoundError(f"feature_names.json not found at {fn_path}")
    return json.loads(fn_path.read_text(encoding="utf-8"))


def build_meta_filter_array(
    feature_vector: np.ndarray,
    micro_features: dict[str, float],
    ou_z_entry: float = 1.3,
    *,
    feature_names_path: str | Path,
) -> np.ndarray:
    """Build a 47-dim array in training order from live feature pipeline.

    Args:
        feature_vector: 40-dim V9 institutional features in
            V9_INSTITUTIONAL_40_FEATURES order.
        micro_features: Dict of 9 microstructure features (no prefix).
        ou_z_entry: OU z_entry threshold (constant for live, varied in
            training parallel universes).
        feature_names_path: Path to training feature_names.json for
            order verification.

    Returns:
        47-dim float32 array ready for MetaFilterAdapter.filter_array().

    Raises:
        FeatureParityError: If feature names or dimensions don't match.
    """
    expected_names = _load_feature_names(feature_names_path)
    expected_n = len(expected_names)

    # Build dict from arrays
    feat_dict: dict[str, float] = {}

    # V9 features (40): map from array position to feature name
    for i, name in enumerate(V9_INSTITUTIONAL_40_FEATURES):
        feat_dict[name] = float(feature_vector[i]) if i < len(feature_vector) else 0.0

    # Micro features (9): from micro_features dict
    for name in MICROSTRUCTURE_9_FEATURES:
        feat_dict[name] = float(micro_features.get(name, 0.0))

    # OU z_entry (constant in live, varied in training)
    feat_dict["ou_z_entry"] = float(ou_z_entry)

    # Build array in exact training order with hard assertions
    values = []
    missing = []
    for name in expected_names:
        val = feat_dict.get(name)
        if val is None or (isinstance(val, float) and val != val):
            missing.append(name)
            values.append(0.0)
        else:
            values.append(float(val))

    if missing:
        raise ValueError(
            f"Meta-filter: {len(missing)} features missing from live packet: {missing}"
        )

    arr = np.asarray(values, dtype=np.float32)
    if len(arr) != expected_n:
        raise ValueError(f"Meta-filter dimension mismatch: built {len(arr)}, expected {expected_n}")

    return arr


class MetaFilterGate:
    """Thin wrapper around MetaFilterAdapter for strategy-line integration.

    Handles feature array construction from live pipeline outputs and
    delegates to MetaFilterAdapter for inference.

    When a ConformalCalibrator is provided, the gate uses an adaptive
    threshold computed from the empirical P(win) distribution instead of
    a fixed threshold.  This is Track 3d — data-driven OU signal gating.
    """

    def __init__(
        self,
        model_dir: str | Path,
        threshold: float = 0.50,
        ou_z_entry: float = 1.3,
        calibrator=None,  # ConformalCalibrator | None
    ):
        self._model_dir = Path(model_dir)
        self._threshold = threshold
        self._ou_z_entry = ou_z_entry
        self._calibrator = calibrator
        self._adapter: Any = None
        self._loaded = False

    def load(self) -> None:
        """Load the underlying MetaFilterAdapter."""
        from core.brains.adapters.meta_filter_adapter import MetaFilterAdapter

        pkl_path = self._model_dir / "meta_filter_lightgbm.pkl"
        fn_path = self._model_dir / "feature_names.json"

        if not pkl_path.exists():
            logging.warning("MetaFilterGate: model not found at %s — gate disabled", pkl_path)
            return

        self._adapter = MetaFilterAdapter(
            model_path=pkl_path,
            feature_names_path=fn_path,
            threshold=self._threshold,
        )
        self._adapter.load()
        self._loaded = True

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def filter(
        self,
        feature_vector: np.ndarray,
        micro_features: dict[str, float],
        ou_z_entry: float | None = None,
    ) -> dict[str, Any]:
        """Run meta-filter on live feature pipeline outputs.

        Returns dict with passed, p_win, threshold, reason.

        When a ConformalCalibrator is present and warm, the threshold is
        dynamically computed from the empirical P(win) distribution (Q10,
        clamped [0.35, 0.70]).  Otherwise the fixed threshold is used.
        """
        if not self._loaded or self._adapter is None:
            return {
                "passed": True,
                "p_win": 0.0,
                "threshold": self._threshold,
                "reason": "meta_filter_not_loaded",
            }

        z_entry = ou_z_entry if ou_z_entry is not None else self._ou_z_entry
        fn_path = self._model_dir / "feature_names.json"

        arr = build_meta_filter_array(
            feature_vector=feature_vector,
            micro_features=micro_features,
            ou_z_entry=z_entry,
            feature_names_path=fn_path,
        )

        # ── Determine threshold (adaptive vs fixed) ──
        if self._calibrator is not None and self._calibrator.is_warm:
            effective_threshold = self._calibrator.compute_threshold()
            threshold_source = "conformal_q10"
        else:
            effective_threshold = self._threshold
            threshold_source = "fixed"

        result = self._adapter.filter_array(arr)

        # Re-evaluate with the adaptive threshold if the adapter used a
        # different one internally.
        p_win = float(result.get("p_win", 0.0))
        passed = p_win >= effective_threshold

        return {
            "passed": passed,
            "p_win": p_win,
            "threshold": effective_threshold,
            "threshold_source": threshold_source,
            "reason": result.get("reason", "ok") if passed else "below_adaptive_threshold",
        }


# ── Module-level convenience ──

_global_gate: MetaFilterGate | None = None


def get_meta_filter_gate(
    model_dir: str,
    threshold: float = 0.50,
    ou_z_entry: float = 1.3,
    calibrator=None,  # ConformalCalibrator | None
) -> MetaFilterGate:
    """Get or create the global meta-filter gate (singleton pattern)."""
    global _global_gate
    if _global_gate is None:
        _global_gate = MetaFilterGate(
            model_dir=model_dir,
            threshold=threshold,
            ou_z_entry=ou_z_entry,
            calibrator=calibrator,
        )
        _global_gate.load()
    return _global_gate
