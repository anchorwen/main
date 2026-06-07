"""Barrier strategy line — 60-min survival-barrier contract.

Brains: onnx_v9, deepresmlp, online_sgd, xgboost_v9, lightgbm_v1
Contract: survival_barrier_2.0sl_3.5tp_12bar
Magic: 90001

FIX-20260601-039: Feature assembly is now delegated to the central factory
(``core.features.feature_assembler.assemble_features_by_schema()``).
BarrierStrategy no longer assumes all brains use V9 40-dim — brains with
``swing_enhanced_35`` schema (Barrier_V9_12B_V1) receive correctly
assembled 35-dim vectors.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np

from core.execution.strategy_line import StrategyLine
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES

# V9 canonical order → name index (built once at import)
_V9_NAME_TO_IDX: dict[str, int] = {name: i for i, name in enumerate(V9_INSTITUTIONAL_40_FEATURES)}


def _reorder_for_brain(feature_vector: np.ndarray, brain_features: list[str] | None) -> np.ndarray:
    """Reorder feature vector to brain training order via name-based lookup.

    When lengths match and brain_features uses V9 canonical names, reorder
    from V9 order to brain training order.  When lengths differ, the feature
    factory has already assembled the correct vector — pass through as-is.

    FIX-20260603-076: When lengths match but brain_features use a DIFFERENT
    schema (e.g. swing_enhanced_35 vs v9_institutional), name lookup returns
    all zeros.  Detect this by checking whether any brain feature names were
    actually resolved — if not, pass through the factory-assembled vector.
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

    # FIX-20260603-076: if ALL values defaulted to 0.0, the brain feature
    # names don't match V9 canonical names — this happens for swing_enhanced_*
    # schemas where the factory already assembled the correct order.
    # Pass through the factory vector instead of returning zeros.
    if np.max(np.abs(reordered)) < 1e-10:
        return np.asarray(feature_vector, dtype=np.float32)

    return reordered


class BarrierStrategy(StrategyLine):
    """60-min barrier prediction strategy.

    Each brain declares its feature schema via the adapter.  The factory
    assembles the correct vector, then ``_reorder_for_brain`` aligns the
    feature order to what the brain saw during training.

    TF close buffer and OU/Hurst computation are inherited from StrategyLine
    base class (shared with SwingStrategy — no copy-paste).
    """

    def _run_inference(
        self,
        feature_vector: Any,
        micro_feature_vector: Any,
        mid_price: float | None,
        micro_sequences: dict[str, Any] | None = None,
        daily_feature_vector: Any = None,
        btc_augment: Any = None,  # FIX-20260607-XXX: pre-computed 37-dim BTC vector
    ) -> list[Any]:
        proposals: list[Any] = []

        # Track TF close prices for OU/Hurst computation (buffer in base class)
        if mid_price is not None and mid_price > 0:
            self._tf_close_buffer.append(mid_price)

        from core.features.feature_assembler import assemble_features_by_schema

        for b_info in self.brains:
            try:
                adapter = b_info.get("adapter")
                if adapter is None:
                    continue
                schema = getattr(adapter, "feature_schema", "") or "v9_institutional"

                fv = assemble_features_by_schema(
                    schema,
                    legacy_v9_vector=np.asarray(feature_vector, dtype=np.float64).ravel(),
                    daily_features=daily_feature_vector,
                    micro_features=micro_feature_vector,
                    tf_ou=self._compute_tf_ou_theta(),
                    tf_hurst=self._compute_tf_hurst(),
                )

                # Reorder to brain training order (name-based)
                final_fv = _reorder_for_brain(fv, b_info.get("features"))
                prop = adapter.inference(final_fv)

                bid = b_info.get("brain_id", "unknown")
                try:
                    if not getattr(prop, "brain_id", None):
                        prop.brain_id = bid
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).warning(
                        "Brain proposal build failed brain_id=%s", bid
                    )
                proposals.append(prop)
            except Exception as _exc:  # noqa: BLE001
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
