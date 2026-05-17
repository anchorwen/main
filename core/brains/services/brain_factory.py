import json
import logging

from core.brains.adapters import ADAPTER_REGISTRY, BRAIN_TYPE_MAP
from core.deployment.brain_alert import emit_brain_alert
from core.deployment.brain_config_validator import BrainConfigError, get_validator
from core.features.adapters.microstructure_feature_adapter import (
    MicrostructureFeatureAdapter,
)
from core.features.adapters.v9_feature_adapter import V9FeatureAdapter

logger = logging.getLogger(__name__)

MICROSTRUCTURE_BRAIN_TYPES = {
    "transformer_v4.3",
    "transformer_v5",
    "transformer_v5_m15",
    "transformer_v5_h1",
    "transformer_v5_h4",
    "xgboost_v4.5",
    "xgboost_v4.5_m15",
    "xgboost_v4.5_h1",
    "xgboost_v4.5_h4",
}
V9_BRAIN_TYPES = {"onnx_v9", "deepresmlp", "lightgbm_v1", "xgboost_v9"}


class BrainFactory:
    def build(self, brain_entry: dict):
        brain_type = brain_entry["brain_type"]
        registry_key = BRAIN_TYPE_MAP.get(brain_type)
        if registry_key is None:
            raise ValueError(
                f"Unsupported brain_type: {brain_type!r}. " f"Known types: {list(BRAIN_TYPE_MAP)}"
            )
        adapter_cls = ADAPTER_REGISTRY[registry_key]

        if brain_type in V9_BRAIN_TYPES:
            normalization_path = brain_entry.get("normalization_config_path", "")
            if normalization_path:
                with open(normalization_path, encoding="utf-8") as f:
                    normalization_config = json.load(f)
                feature_adapter = V9FeatureAdapter(
                    normalization_config=normalization_config,
                )
                adapter = adapter_cls(
                    brain_entry=brain_entry,
                    feature_adapter=feature_adapter,
                )
            else:
                adapter = adapter_cls(brain_entry=brain_entry)
        elif brain_type == "online_sgd":
            norm_path = brain_entry.get("normalization_config_path", "")
            feat_adapter = None
            if norm_path:
                with open(norm_path, encoding="utf-8") as f:
                    norm_config = json.load(f)
                feat_adapter = V9FeatureAdapter(normalization_config=norm_config)
            adapter = adapter_cls(brain_entry=brain_entry, feature_adapter=feat_adapter)
        elif brain_type in MICROSTRUCTURE_BRAIN_TYPES:
            scaler_path = brain_entry.get("normalization_artifact_path", "")
            feat_adapter = (
                MicrostructureFeatureAdapter(scaler_path=scaler_path) if scaler_path else None
            )
            adapter = adapter_cls(brain_entry=brain_entry, feature_adapter=feat_adapter)
        else:
            adapter = adapter_cls(brain_entry=brain_entry)

        # ── Load-time validation (catches config drift before inference) ──
        validator = get_validator()
        vresult = validator.validate(brain_entry)
        brain_id = brain_entry.get("brain_id", "?")
        if not vresult.ok:
            for err in vresult.errors:
                logger.error("BrainFactory config error: %s", err)
                emit_brain_alert(brain_id, "config_validation_error", {"errors": vresult.errors})
            raise BrainConfigError(
                f"Brain config validation failed for {brain_id}: " + "; ".join(vresult.errors)
            )
        for warn in vresult.warnings:
            logger.warning("BrainFactory config warning: %s", warn)

        adapter.load()

        # ── Post-load dimension validation ──
        num_features = getattr(adapter, "_num_features", None)
        dim_result = validator.validate_model_dimension(brain_entry, num_features)
        if not dim_result.ok:
            for err in dim_result.errors:
                logger.error("BrainFactory dimension mismatch: %s", err)
                emit_brain_alert(
                    brain_id,
                    "feature_dimension_mismatch",
                    {"errors": dim_result.errors, "num_features": num_features},
                )
            raise BrainConfigError(
                f"Model dimension mismatch for {brain_id}: " + "; ".join(dim_result.errors)
            )

        logger.info(
            "BrainFactory built and loaded adapter brain_id=%s type=%s backend=%s",
            brain_id,
            brain_type,
            getattr(adapter, "_backend", "unknown"),
        )
        return adapter
