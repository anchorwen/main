"""Meta-labeling feature vector builder.

Extracted from live_cycle.py per the Strangler Fig pattern.
Builds the 40-dim raw V9 institutional feature vector used by
Meta_Stage1_MetaLabel_Binary_V1 for binary win/loss classification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from core.brains.adapters.params_brain_adapter import ParamsBrainAdapter
from core.features.schemas.registry import get_schema_dimension
from core.runtime.fault_handler import fail_open_guard, log_and_continue


def build_meta_feature_vector(
    *,
    brains: list[dict[str, Any]],
    feature_store: Any,
    mid_price: float | None,
    symbol: str,
) -> tuple[Any, dict[str, float] | None]:
    """Build 40-dim raw feature vector for meta-labeling binary classifier.

    The meta labeler (Meta_Stage1_MetaLabel_Binary_V1) was trained on
    40 raw V9 institutional features WITHOUT z-score normalization.
    This function builds the same 40-dim raw vector at inference time.

    Returns (feature_vector, ou_params) where:
      - feature_vector is a 1×40 np.ndarray (float32)
      - ou_params is {z_score, half_life, theta} for diagnostic logging
      - returns (None, None) if V9 features cannot be read from feature store
    """
    # ── Step 1: Compute OU params from statarb brain adapter ──
    ou_params: dict[str, float] | None = None
    _price = mid_price if mid_price is not None and mid_price > 0 else 0.0
    for b_info in brains:
        adapter = b_info.get("adapter")
        if isinstance(adapter, ParamsBrainAdapter):
            with log_and_continue(component="BrainInference:OU_params"):
                raw = adapter.infer(np.array([_price], dtype=np.float32))
                ou_params = {
                    "z_score": float(raw.get("z_score", 0.0)),
                    "half_life": float(raw.get("half_life", float("inf"))),
                    "theta": float(raw.get("theta", 0.0)),
                }
                break

    # ═══════════════════════════════════════════════════════════════
    # Step 2: Read raw V9 features from feature store (40-dim)
    # ═══════════════════════════════════════════════════════════════
    raw_features: dict[str, float] | None = None
    try:
        record = feature_store.latest(symbol, "M5", schema_name="v9_institutional_40")
        if record is not None:
            raw_features = dict(record.values) if record.values else {}
    except Exception:  # BLE001:FOG
        with fail_open_guard("meta_feature_builder:build_meta_feature_vector"):
            pass
    # ── Step 3: Build 40-dim raw vector in TRAINING feature order ──
    # FIX-20260525-026: The V9_INSTITUTIONAL_40_FEATURES schema order
    # (M5→H1, OU_Theta/Hurst blocked at end) does NOT match the training
    # order (H1→M5, OU_Theta/Hurst inline per-TF).  LightGBM uses
    # position-based indexing — every single feature position was
    # scrambled, making the model receive random noise.
    # Fix: read the authoritative feature_names from the MetaLabel brain
    # config or model metadata, then assemble in that exact order.
    _feature_names: list[str] | None = None

    # Source 1: brain config features field (authoritative — training order)
    for b_info in brains:
        _bid = str(b_info.get("brain_id", ""))
        if (
            "metalabel" in _bid.lower()
            or "barrier_12bar_meta" in str(b_info.get("contract_group", "")).lower()
        ):
            _features = b_info.get("features")
            if _features and isinstance(_features, list):
                _schema_id = str(b_info.get("feature_schema_id", "v9_institutional_40"))
                _expected_dim = get_schema_dimension(_schema_id)
                if len(_features) == _expected_dim:
                    _feature_names = [str(f) for f in _features]
            break

    # Source 2: model metadata file (fallback)
    if _feature_names is None:
        _meta_path = None
        for b_info in brains:
            _bid = str(b_info.get("brain_id", ""))
            if "metalabel" in _bid.lower():
                _meta_path = b_info.get("normalization_config_path")
                break
        if _meta_path:
            try:
                _meta = json.loads(Path(_meta_path).read_text(encoding="utf-8"))
                _names = _meta.get("feature_names")
                if _names and isinstance(_names, list):
                    _meta_schema_id = (
                        str(b_info.get("feature_schema_id", "v9_institutional_40"))
                        if b_info
                        else "v9_institutional_40"
                    )
                    _expected_dim = get_schema_dimension(_meta_schema_id)
                    if len(_names) == _expected_dim:
                        _feature_names = [str(f) for f in _names]
            except Exception:  # BLE001:FOG
                with fail_open_guard("meta_feature_builder:build_meta_feature_vector"):
                    pass
    if raw_features is None:
        return None, None

    # ── Step 3: Assemble 40-dim vector in TRAINING feature order ──
    if _feature_names is not None:
        values = [float(raw_features.get(name, 0.0)) for name in _feature_names]
    else:
        # Legacy fallback with a loud diagnostic — this path should
        # never be reached in production, but preserves back-compat
        # for environments where the brain config is unavailable.
        import logging

        _logger = logging.getLogger(__name__)
        _logger.error(
            "MetaLabel brain feature_names unavailable — "
            "falling back to V9 schema order (TRAIN-SERVE SKEW LIKELY)"
        )
        from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES

        values = [float(raw_features.get(name, 0.0)) for name in V9_INSTITUTIONAL_40_FEATURES]

    feature_vec = np.array(values, dtype=np.float32).reshape(1, -1)
    return feature_vec, ou_params
