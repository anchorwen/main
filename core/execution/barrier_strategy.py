"""Barrier strategy line — 60-min survival-barrier contract.

Brains: onnx_v9, deepresmlp, online_sgd, xgboost_v9, lightgbm_v1
Contract: survival_barrier_2.0sl_3.5tp_12bar
Magic: 90001

Uses V9 40-dim institutional features for all brains.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

from core.execution.strategy_line import StrategyLine
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES

# V9 canonical order → name index (built once at import)
_V9_NAME_TO_IDX: dict[str, int] = {name: i for i, name in enumerate(V9_INSTITUTIONAL_40_FEATURES)}


def _reorder_for_brain(feature_vector: np.ndarray, brain_features: list[str] | None) -> np.ndarray:
    """Reorder feature vector from V9 canonical order to brain training order.

    LightGBM/XGBoost use positional indexing — if the feature order at inference
    doesn't match the training order, every tree split reads the wrong feature
    (train-serve skew → frozen confidence).  This function builds a name→value
    map from the V9-ordered vector and extracts values in the brain's training
    order, matching what the model saw during fit().
    """
    if not brain_features or len(brain_features) != len(feature_vector):
        return np.asarray(feature_vector, dtype=np.float32)

    name_to_val: dict[str, float] = {}
    for i, val in enumerate(np.asarray(feature_vector).flat):
        if i < len(V9_INSTITUTIONAL_40_FEATURES):
            name_to_val[V9_INSTITUTIONAL_40_FEATURES[i]] = float(val)

    reordered = np.array(
        [name_to_val.get(name, 0.0) for name in brain_features],
        dtype=np.float32,
    )
    return reordered


class BarrierStrategy(StrategyLine):
    """60-min barrier prediction strategy.

    All brains share the same V9 40-dim feature vector as input.
    Each brain may have been trained with a different feature order —
    the adapter receives a reordered vector matching that brain's training
    feature_names.
    """

    def _run_inference(
        self,
        feature_vector: Any,
        micro_feature_vector: Any,
        mid_price: float | None,
        micro_sequences: dict[str, Any] | None = None,
        daily_feature_vector: Any = None,
    ) -> list[Any]:
        proposals: list[Any] = []
        for b_info in self.brains:
            try:
                fv = _reorder_for_brain(
                    np.asarray(feature_vector, dtype=np.float32),
                    b_info.get("features"),
                )
                prop = b_info["adapter"].inference(fv)
                # Stamp brain_id
                bid = b_info.get("brain_id", "unknown")
                try:
                    if not getattr(prop, "brain_id", None):
                        prop.brain_id = bid
                except Exception:
                    pass
                proposals.append(prop)
            except Exception as _exc:
                print(
                    json.dumps(
                        {
                            "event": "brain_inference_error",
                            "brain_id": b_info.get("brain_id", "unknown"),
                            "brain_type": b_info.get("brain_type", "unknown"),
                            "strategy": "barrier_12bar",
                            "error": str(_exc),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        return proposals
