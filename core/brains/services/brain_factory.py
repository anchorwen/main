import json
import logging

from core.brains.adapters import ADAPTER_REGISTRY, BRAIN_TYPE_MAP
from core.features.adapters.microstructure_feature_adapter import (
    MicrostructureFeatureAdapter,
)
from core.features.adapters.v9_feature_adapter import V9FeatureAdapter

logger = logging.getLogger(__name__)

MICROSTRUCTURE_BRAIN_TYPES = {"transformer_v4.3", "transformer_v5", "xgboost_v4.5"}
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

        adapter.load()
        logger.info(
            "BrainFactory built and loaded adapter brain_id=%s type=%s backend=%s",
            brain_entry.get("brain_id", "?"),
            brain_type,
            getattr(adapter, "_backend", "unknown"),
        )
        return adapter
